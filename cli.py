"""AGENTIC STAR Marketplace entry adapter for TRV-C2-003."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


from src.graph.graph import TravelInquiryTriageAgent

_NAMESPACE = "trv"
_AGENT_NAME = "TRV-C2-003"
_PROVIDER_AGENT_NAME = "trv-c2-003"
_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_ALLOWED_SECRET_KEYS = frozenset(_AZURE_OPENAI_SECRET_KEYS)


class _AllowlistedEnvironmentProvider(SecretProvider):
    """Expose only this agent's Azure connection values from the environment."""

    def __init__(self) -> None:
        self._namespace = _NAMESPACE
        self._agent_name = _PROVIDER_AGENT_NAME

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_SECRET_KEYS:
            return default
        return os.environ.get(key, default)


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Require a non-empty value without repairing deployment configuration."""

    value = provider.require(key)
    if not value or value != value.strip() or "\n" in value or "\r" in value:
        raise ValueError(
            f"Marketplace environment variable {key!r} must be non-empty, "
            "have no surrounding whitespace, and contain no newlines"
        )
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


class _AzureOpenAIClientCompat(BaseLLM):
    """Minimal Azure chat-completions client for AgentCore before 1.0.2."""

    def __init__(self, config: dict[str, str], *, tools: tuple[Any, ...] = ()) -> None:
        self._config = dict(config)
        self._tools = tools

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        endpoint = self._config["azure_endpoint"].rstrip("/")
        deployment = quote(self._config["azure_deployment"], safe="")
        body: dict[str, Any] = {"messages": messages}
        if self._tools:
            body["tools"] = list(self._tools)
        request = Request(
            f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-10-21",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "api-key": self._config["api_key"],
                "content-type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - deployment-owned endpoint
            payload = json.loads(response.read().decode("utf-8"))

        choices = payload.get("choices", []) if isinstance(payload, dict) else []
        message = choices[0].get("message", {}) if choices else {}
        if not isinstance(message, dict):
            message = {}
        model = str(payload.get("model") or self._config["azure_deployment"])
        raw_usage = payload.get("usage") if isinstance(payload, dict) else None
        raw_usage = raw_usage if isinstance(raw_usage, dict) else {}
        content = message.get("content", "")
        return {
            "content": content if isinstance(content, str) else str(content or ""),
            "tool_calls": list(message.get("tool_calls") or []),
            "model": model,
            "usage": {
                "prompt_tokens": int(
                    raw_usage.get("prompt_tokens", raw_usage.get("input_tokens", 0))
                ),
                "completion_tokens": int(
                    raw_usage.get("completion_tokens", raw_usage.get("output_tokens", 0))
                ),
                "model": model,
            },
        }

    def stream(self, messages: list[Any]) -> Iterator[str]:
        content = self.complete(messages)["content"]
        if content:
            yield content

    def bind_tools(self, tools: list[Any]) -> "_AzureOpenAIClientCompat":
        return type(self)(self._config, tools=tuple(tools))


class _AzureMarketplaceLLM(BaseLLM):
    """Resolve Azure secrets and construct a fresh client for every operation."""

    def __init__(self, *, tools: tuple[Any, ...] = ()) -> None:
        self._tools = tools

    def _client(self) -> BaseLLM:
        provider = current_secrets()
        values = {key: _require_clean(provider, key) for key in _AZURE_OPENAI_SECRET_KEYS}

        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
            if exc.name != "shared.services.llm.azure_openai_client":
                raise
            AzureOpenAIClient = _AzureOpenAIClientCompat  # type: ignore[misc,assignment]

        client = cast(
            BaseLLM,
            AzureOpenAIClient(
                {
                    "api_key": values["AZURE_OPENAI_API_KEY"],
                    "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
                    "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
                }
            ),
        )
        return client.bind_tools(list(self._tools)) if self._tools else client

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "_AzureMarketplaceLLM":
        return type(self)(tools=tuple(tools))


def _merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge config while giving explicitly supplied values priority."""

    merged = dict(base)
    for key, value in override.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def _marketplace_envelope(user_input: str) -> str:
    """Build the allowlisted context envelope expected by the pre-process node."""

    if not isinstance(user_input, str):
        raise TypeError("Marketplace input must be one plain string")

    try:
        parsed = json.loads(user_input)
    except (json.JSONDecodeError, TypeError):
        parsed = None

    if isinstance(parsed, dict) and "text" in parsed:
        text = parsed["text"]
        if not isinstance(text, str):
            raise TypeError("Marketplace envelope 'text' must be a string")
        raw_context = parsed.get("context", {})
        context = raw_context if isinstance(raw_context, dict) else {}
    else:
        text = user_input
        context = {}

    return json.dumps(
        {
            "text": text,
            "context": context,
            "reference_datetime": datetime.now(timezone.utc).isoformat(),
        },
        default=str,
    )


class MarketplaceGraph(TravelInquiryTriageAgent):
    """Marketplace boundary with early config/LLM wiring and fail-closed secrets."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        agent_dir = Path(__file__).resolve().parent
        file_config = load_agent_config(agent_dir)
        merged = _merge_config(file_config, dict(config or {}))
        merged.setdefault("llm", _AzureMarketplaceLLM())
        merged["memory_enabled"] = False
        merged["hitl"] = {"enabled": False}
        super().__init__(merged)

    def provision_secrets(self, provider: SecretProvider) -> None:
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _AZURE_OPENAI_SECRET_KEYS:
            _require_clean(environment_provider, key)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del input_context
        envelope = _marketplace_envelope(user_input)
        marketplace_ctx = replace(ctx, hitl_allowed=False) if ctx is not None else None
        return cast(
            dict[str, Any],
            super().invoke(
                envelope,
                session_id=session_id,
                ctx=marketplace_ctx,
                input_context={},
            ),
        )


def main() -> None:
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=load_agent_config(Path(__file__).resolve().parent),
    )


if __name__ == "__main__":
    main()
