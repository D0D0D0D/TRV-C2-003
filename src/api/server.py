"""AgentCore Platform v1.0 — TRV-C2-003 standalone HTTP adapter.

Adapter only — no business logic. Builds the structured envelope the agent expects
(docs §2-1) and generates ``reference_datetime`` once, server-side, so the R2
cancellation-fee day-count is deterministic (client-supplied values are ignored,
docs §4-1). The domain-rule KB is resolved + validated at construction (startup),
so a malformed operator KB fails closed before the server accepts traffic.
"""

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from framework.errors import ConfigError
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory

from src.graph.graph import TravelInquiryTriageAgent

_CONFIG_PATH = "config/config.yaml"


def _build_config() -> dict:
    """Load runtime config. The LLM is injected here in production
    (``config["llm"] = <BaseLLM>``); left unset for keyless STG / local runs, in which
    case the agent runs deterministically and reports ``llm_unavailable`` degradation.
    Inject a real client through config["llm"] before deploying.

    An absent config file is fine (the agent runs on code defaults + the bundled
    baseline KB). A PRESENT-but-corrupt config.yaml fails closed at import: silently
    swallowing a malformed runtime config would ignore the operator's tuning (and
    could leave stale defaults in force), so a YAML error stops startup instead."""
    try:
        with Path(_CONFIG_PATH).open("r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {}  # no config file → code defaults + baseline KB
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # present but corrupt → fail closed at startup
        raise ConfigError(f"malformed {_CONFIG_PATH}: {exc}") from exc
    return data or {}


app = FastAPI(title="TRV-C2-003 Travel Inquiry Triage & Routing Agent")

# Compile once at startup.
agent = TravelInquiryTriageAgent(config=_build_config())
agent.compile()
agent.provision_secrets(secrets_factory(namespace="trv", agent_name="trv-c2-003"))


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Optional structured context (docs §3): {"history": [...], "booking": {...}}.
    context: dict = Field(default_factory=dict)
    # reference_datetime is intentionally NOT accepted from the client (docs §4-1).


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller authentication: when
    # INVOKE_AUTH_TOKEN is set, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and run at VERIFIED_EXTERNAL.
    # Middleware-established trust is never demoted. This is the entry-point auth
    # boundary — a deployment-level caller credential read before any
    # InvocationContext exists, so ctx.secrets does not apply: this token
    # authenticates the caller before any invocation context is built.
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    # Build the JSON envelope; reference_datetime is server-generated (§4-1).
    envelope = json.dumps(
        {
            "text": req.input,
            "context": req.context or {},
            "reference_datetime": datetime.now(timezone.utc).isoformat(),
        },
        default=str,
    )

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return agent.invoke(envelope, ctx=ctx)


@app.get("/health")
def health():
    return {"status": "ok", "agent": "trv-c2-003"}
