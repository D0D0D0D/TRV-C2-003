"""AgentCore Platform v1.0 — TRV-C2-003 outer post_process node (S-3 output gate).

Assembles the final ``formatted_output`` envelope, folds all degradation tokens, and
enforces the S-3 output contract. Non-conclusiveness is guaranteed *structurally* by a
closed allowlist + closed enums (docs §6-3) — the free-text guard on handoff_summary
is a bounded product-safety mitigation, not the main assurance (Return Fraud Investigation Memo Drafting Agent lesson:
denylists don't close natural language).
"""

from __future__ import annotations

import unicodedata
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import (
    DEGRADATION_TOKENS,
    FALLBACK_REASONS,
    FORMATTED_OUTPUT_KEYS,
    PRIORITIES,
    ROUTING_TARGETS,
    TRIAGE_META_KEYS,
    WITHHELD_SUMMARY,
)

try:
    from shared.security.pii_detector import detect_pii
except Exception:  # pragma: no cover
    def detect_pii(text: str) -> list:  # type: ignore
        return []

try:
    from shared.security.credential_detector import detect_credentials
except Exception:  # pragma: no cover
    try:
        from shared.security.credential_detector import (  # type: ignore
            scan_credentials as detect_credentials,
        )
    except Exception:
        def detect_credentials(text: str) -> list:  # type: ignore
            return []


class PostProcessNode(FunctionNode):
    """Build formatted_output and run the final S-3 gate."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        # Upstream error passthrough — keep the error status, emit no output.
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("triage_post_error_passthrough", {}, state)
            return {}

        routing_decision = state.get("routing_decision") or {}
        # A valid routing decision is the mandatory input for the output envelope.
        # Without one (e.g. this node invoked in isolation, or an upstream node that
        # produced no decision) we must NOT fabricate an envelope with a None
        # routing_target — the S-3 closed-enum gate would (correctly) fail-closed on
        # it and raise. Treat a missing decision as an error passthrough: emit no
        # output. In the assembled graph this branch is unreachable because
        # routing_decision runs before post_process on every success path.
        if routing_decision.get("routing_target") not in ROUTING_TARGETS:
            emit_trace_event("triage_post_missing_routing", {}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PostProcessNode: no routing_decision to format"],
            }

        triage_meta = dict(state.get("triage_meta") or {})
        handoff_summary = state.get("handoff_summary", "")
        envelope_degradation = state.get("envelope_degradation")

        # Fold the outer-envelope degradation into the inner degradation_reason.
        degradation_reason = _merge_degradation(
            envelope_degradation, triage_meta.get("degradation_reason")
        )

        # Audit-only PII scan on the narration (customer identifiers retained — A-5).
        if isinstance(handoff_summary, str) and detect_pii(handoff_summary):
            emit_trace_event("triage_summary_pii_scanned", {}, state)

        guarded_summary, withheld = _guard_summary(
            handoff_summary, routing_decision.get("routing_target")
        )
        if withheld:
            degradation_reason = _merge_degradation(
                degradation_reason, "handoff_summary_withheld"
            )
            emit_trace_event("triage_summary_withheld", {}, state)

        formatted_output = {
            "routing_target": routing_decision.get("routing_target"),
            "priority": routing_decision.get("priority"),
            "urgent": bool(routing_decision.get("urgent", False)),
            "handoff_summary": guarded_summary,
            "triage_meta": {
                "confidence": float(triage_meta.get("confidence", 0.0)),
                "fallback_reason": triage_meta.get("fallback_reason"),
                "degradation_reason": degradation_reason,
            },
        }

        emit_trace_event(
            "triage_output_formatted",
            {"routing_target": formatted_output["routing_target"]},
            state,
        )

        return {
            "formatted_output": formatted_output,
            "status": AgentStatus.SUCCESS.value,
        }

    # ── S-3 output gate ─────────────────────────────────────────────────────
    def _extra_security_gate_output(self, result: dict) -> dict:
        fo = result.get("formatted_output")
        if fo is None:
            # error passthrough: no output envelope to gate.
            return result
        _assert_closed_output(fo)
        return result


# ── pure helpers ─────────────────────────────────────────────────────────────
def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _merge_degradation(*sources: Any) -> str | None:
    tokens: list[str] = []
    for s in sources:
        if not s:
            continue
        for t in str(s).split("+"):
            t = t.strip()
            if t and t not in tokens:
                tokens.append(t)
    return "+".join(tokens) if tokens else None


def _guard_summary(summary: Any, routing_target: Any) -> tuple[Any, bool]:
    if not isinstance(summary, str) or not summary:
        return summary, False
    # Secrets must never surface in free text — withhold if any credential appears.
    if detect_credentials(summary):
        return WITHHELD_SUMMARY, True
    # Routing contradiction: only underscore enum identifiers of a *different* desk
    # are treated as unambiguous contradictions (single-word desk names collide with
    # ordinary language and would over-withhold). Bounded mitigation, not a guarantee.
    norm = _normalize(summary)
    for target in ROUTING_TARGETS:
        if target == routing_target or "_" not in target:
            continue
        if _normalize(target.replace("_", " ")) in norm or _normalize(target) in norm:
            return WITHHELD_SUMMARY, True
    return summary, False


def _assert_closed_output(fo: Any) -> None:
    """Fail-closed S-3: reject anything outside the declared closed shape."""
    if not isinstance(fo, dict):
        raise SecurityViolationError("formatted_output is not a mapping")
    if set(fo) != FORMATTED_OUTPUT_KEYS:
        raise SecurityViolationError(
            f"formatted_output keys must be exactly {sorted(FORMATTED_OUTPUT_KEYS)}"
        )
    if fo["routing_target"] not in ROUTING_TARGETS:
        raise SecurityViolationError("routing_target not in closed enum")
    if fo["priority"] not in PRIORITIES:
        raise SecurityViolationError("priority not in closed enum")
    if not isinstance(fo["urgent"], bool):
        raise SecurityViolationError("urgent must be bool")
    if not isinstance(fo["handoff_summary"], str):
        raise SecurityViolationError("handoff_summary must be str")

    meta = fo["triage_meta"]
    if not isinstance(meta, dict) or set(meta) != TRIAGE_META_KEYS:
        raise SecurityViolationError(
            f"triage_meta keys must be exactly {sorted(TRIAGE_META_KEYS)}"
        )
    conf = meta["confidence"]
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0.0 <= float(conf) <= 1.0:
        raise SecurityViolationError("triage_meta.confidence must be a number in [0,1]")
    if meta["fallback_reason"] is not None and meta["fallback_reason"] not in FALLBACK_REASONS:
        raise SecurityViolationError("triage_meta.fallback_reason not in closed enum")
    _assert_degradation(meta["degradation_reason"])


def _assert_degradation(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise SecurityViolationError("degradation_reason must be str or null")
    for token in value.split("+"):
        if token.strip() not in DEGRADATION_TOKENS:
            raise SecurityViolationError(f"degradation_reason token not allowed: {token!r}")
