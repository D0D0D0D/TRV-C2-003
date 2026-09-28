"""Regression tests for the AGENTIC STAR Marketplace entry adapter."""

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import NullProvider
from framework.secrets.context import bound_secrets

_ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("cli", _ROOT / "cli.py")
cli = importlib.util.module_from_spec(spec)
sys.modules["cli"] = cli
assert spec.loader is not None
spec.loader.exec_module(cli)

_AZURE_ENV = {
    "AZURE_OPENAI_API_KEY": "dummy-key",
    "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
    "AZURE_OPENAI_DEPLOYMENT": "dummy-deployment",
}
_OBSERVATION = {
    "intent": "cancellation",
    "urgency_signal": False,
    "sentiment_score": 0.2,
    "confidence": 0.95,
    "emergency_signal": False,
    "arrival_country": None,
    "nationality": None,
    "purpose": None,
}


class _StubAzureClient:
    def complete(self, messages):
        system_prompt = messages[0]["content"]
        content = (
            json.dumps(_OBSERVATION)
            if "OUTPUT OBSERVATIONS ONLY" in system_prompt
            else "Customer requests cancellation assistance; handoff details are ready."
        )
        return {
            "content": content,
            "tool_calls": [],
            "model": "dummy-deployment",
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "model": "dummy-deployment",
            },
        }


def _set_azure_environment(monkeypatch):
    for key, value in _AZURE_ENV.items():
        monkeypatch.setenv(key, value)


def _marketplace_context():
    return InvocationContext(
        caller_id="marketplace-reviewer",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
    )


def test_runner_order_end_to_end_plain_text(monkeypatch):
    _set_azure_environment(monkeypatch)
    monkeypatch.setattr(cli._AzureMarketplaceLLM, "_client", lambda self: _StubAzureClient())

    agent = cli.MarketplaceGraph(config={})
    agent.compile()
    agent.provision_secrets(NullProvider())
    with bound_secrets(agent._secrets_provider):
        result = agent.invoke(
            "Please cancel my flight reservation.",
            ctx=_marketplace_context(),
            input_context={
                "conversation_history": [{"role": "user", "content": "untrusted"}]
            },
        )

    assert result["status"] == "success"
    assert result["output"]["routing_target"] == "cancellation"
    assert result["output"]["handoff_summary"]
    degradation = result["output"]["triage_meta"]["degradation_reason"] or ""
    assert "context_envelope_unparsable" not in degradation.split("+")


def test_plain_text_envelope_has_aware_timestamp_and_empty_input_context(monkeypatch):
    captured = {}

    def capture_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        captured.update(
            user_input=user_input,
            session_id=session_id,
            ctx=ctx,
            input_context=input_context,
        )
        return {"status": "success"}

    monkeypatch.setattr(AgentBaseGraph, "invoke", capture_invoke)
    original_ctx = _marketplace_context()
    result = cli.MarketplaceGraph(config={}).invoke(
        "I need help changing my trip",
        session_id="session-1",
        ctx=original_ctx,
        input_context={"conversation_history": ["must never reach State"]},
    )

    envelope = json.loads(captured["user_input"])
    timestamp = datetime.fromisoformat(envelope["reference_datetime"])
    assert result == {"status": "success"}
    assert envelope["text"] == "I need help changing my trip"
    assert envelope["context"] == {}
    assert timestamp.tzinfo is not None
    assert captured["input_context"] == {}
    assert captured["ctx"].hitl_allowed is False
    assert original_ctx.hitl_allowed is True


def test_pasted_reference_datetime_is_overwritten_and_object_is_allowlisted(monkeypatch):
    captured = {}

    def capture_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        captured["envelope"] = json.loads(user_input)
        return {"status": "success"}

    monkeypatch.setattr(AgentBaseGraph, "invoke", capture_invoke)
    supplied = "1999-01-01T00:00:00+00:00"
    cli.MarketplaceGraph(config={}).invoke(
        json.dumps(
            {
                "text": "x",
                "context": {"history": [{"role": "customer", "text": "earlier"}]},
                "reference_datetime": supplied,
                "evil": "y",
            }
        ),
        ctx=_marketplace_context(),
    )

    envelope = captured["envelope"]
    assert set(envelope) == {"text", "context", "reference_datetime"}
    assert envelope["text"] == "x"
    assert envelope["context"] == {
        "history": [{"role": "customer", "text": "earlier"}]
    }
    assert envelope["reference_datetime"] != supplied
    assert datetime.fromisoformat(envelope["reference_datetime"]).tzinfo is not None


def test_non_mapping_context_is_replaced_not_forwarded(monkeypatch):
    captured = {}

    def capture_invoke(self, user_input, **kwargs):
        captured.update(json.loads(user_input))
        return {"status": "success"}

    monkeypatch.setattr(AgentBaseGraph, "invoke", capture_invoke)
    cli.MarketplaceGraph(config={}).invoke(
        '{"text":"hello","context":["not", "trusted"]}',
        ctx=_marketplace_context(),
    )
    assert captured["context"] == {}


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("AZURE_OPENAI_API_KEY", None),
        ("AZURE_OPENAI_ENDPOINT", ""),
        ("AZURE_OPENAI_DEPLOYMENT", "  "),
        ("AZURE_OPENAI_API_KEY", "a\nb"),
    ],
)
def test_azure_secrets_fail_closed(monkeypatch, key, bad_value):
    _set_azure_environment(monkeypatch)
    if bad_value is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, bad_value)

    with pytest.raises((KeyError, ValueError)):
        cli.MarketplaceGraph(config={}).provision_secrets(NullProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    _set_azure_environment(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "gate-test-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gate-test-deployment")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        cli.MarketplaceGraph(config={}).provision_secrets(NullProvider())


def test_config_self_loads_before_agent_construction():
    agent = cli.MarketplaceGraph(config={})
    assert agent.config["thresholds"]["max_input_chars"] == 8000
    assert agent.config["domain_rules"]["baseline_seed_path"] == (
        "config/domain_rules.baseline.yaml"
    )
    assert agent.config["memory_enabled"] is False
    assert agent.config["hitl"] == {"enabled": False}


def test_llm_is_captured_by_both_inner_nodes_before_provisioning():
    agent = cli.MarketplaceGraph(config={})
    llm = agent.config["llm"]
    agent.compile()

    inner_nodes = agent._nodes["main"]._subgraph._nodes
    assert isinstance(llm, cli._AzureMarketplaceLLM)
    assert inner_nodes["intent_context"]._llm is llm
    assert inner_nodes["handoff_summary"]._llm is llm


def test_explicit_llm_is_not_clobbered():
    injected = object()
    agent = cli.MarketplaceGraph(config={"llm": injected})
    assert agent.config["llm"] is injected


def test_bind_tools_returns_new_instance_without_mutating_original():
    original = cli._AzureMarketplaceLLM()
    bound = original.bind_tools([{"type": "function", "function": {"name": "lookup"}}])

    assert bound is not original
    assert original._tools == ()
    assert bound._tools == ({"type": "function", "function": {"name": "lookup"}},)
