"""AgentCore Platform v1.0 — TRV-C2-003 outer pre_process node.

Parses the adapter-built JSON envelope, applies the Executable Contract for the
optional inputs (docs/02_design.md §3), and pins the outer-state fields the inner
graph consumes. Deterministic / pure — no LLM, no now() (reference_datetime is fixed
once by the adapter, docs §4-1).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

try:  # FW-standard PII detector (B-5: standard detector only, no bespoke JP dict).
    from shared.security.pii_detector import detect_pii
except Exception:  # pragma: no cover - detector always present in 1.0.0 wheel
    def detect_pii(text: str) -> list:  # type: ignore
        return []

_HISTORY_ROLES = frozenset({"customer", "staff"})
_BOOKING_STATUS = frozenset({"confirmed", "pending", "cancelled"})
_DEFAULT_MAX_INPUT_CHARS = 8000


class PreProcessNode(FunctionNode):
    """Validate + normalise the inbound envelope into outer State."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, max_input_chars: int = _DEFAULT_MAX_INPUT_CHARS) -> None:
        super().__init__()
        self._max_input_chars = int(max_input_chars)

    # ── S-2 domain extension ────────────────────────────────────────────────
    def _extra_security_gate_input(self, state: dict) -> dict:
        # S-2 runs BEFORE execute(), so it must operate on the raw envelope only —
        # never on validated_input / booking_context (produced later in execute()).
        # Enforce a generous raw-size DoS ceiling; the inquiry text itself is capped
        # to max_input_chars inside execute(). The @final S-2 gate already PII-masks
        # user_input; no bespoke JP name sanitizer is added (B-5 / §2-4).
        raw = state.get("user_input", "")
        ceiling = self._max_input_chars * 4
        if isinstance(raw, str) and len(raw) > ceiling:
            # Reject branch also gets a domain audit event (docs §6-5: not only the
            # success path). node_error is emitted by the framework on the resulting
            # error status; this records the specific reason.
            emit_trace_event(
                "triage_raw_input_rejected", {"ceiling": ceiling, "raw_len": len(raw)}, state
            )
            return {
                **state,
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: raw input exceeds {ceiling} chars"],
            }
        return state

    # ── main logic ──────────────────────────────────────────────────────────
    def execute(self, state: dict) -> dict:
        raw = state.get("user_input", "")
        envelope, unparsable = _parse_envelope(raw)

        text = envelope.get("text", "")
        if not isinstance(text, str) or not text.strip():
            emit_trace_event("triage_empty_input", {}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: inquiry text is empty or missing"],
            }

        rejected: list[dict] = []
        context = envelope.get("context")
        if context is not None and not isinstance(context, dict):
            rejected.append({"path": "context", "reason": "type_mismatch"})
            context = {}
        elif not isinstance(context, dict):
            context = {}

        booking_context = _extract_booking(context, rejected)
        history_context = _extract_history(context, rejected)
        reference_datetime = _valid_reference_datetime(envelope.get("reference_datetime"))

        # Length ceiling on the inquiry text (produced here, so not an S-2 concern).
        validated_input = text.strip()
        if len(validated_input) > self._max_input_chars:
            validated_input = validated_input[: self._max_input_chars]
            emit_trace_event(
                "triage_input_truncated", {"limit": self._max_input_chars}, state
            )

        # Audit-only domain PII scan (customer identifiers are retained by design, A-5).
        pii_fields = _scan_domain_pii(booking_context, history_context)
        if pii_fields:
            emit_trace_event("triage_domain_pii_scanned", {"fields": pii_fields}, state)

        emit_trace_event(
            "triage_envelope_parsed",
            {
                "unparsable": unparsable,
                "rejected_count": len(rejected),
                "has_booking": bool(booking_context),
                "history_len": len(history_context),
                "has_reference_datetime": reference_datetime is not None,
            },
            state,
        )

        return {
            "validated_input": validated_input,
            "booking_context": booking_context,
            "history_context": history_context,
            "reference_datetime": reference_datetime,
            "rejected_context": rejected,
            "envelope_degradation": (
                "context_envelope_unparsable" if unparsable else None
            ),
            "status": AgentStatus.SUCCESS.value,
        }


# ── module-level pure helpers ───────────────────────────────────────────────
# Node instances are shared across every invocation, so per-call data must never
# be stored on self; these stay module-level pure functions.
def _parse_envelope(raw: Any) -> tuple[dict, bool]:
    """Return (envelope, unparsable). A raw invoke that bypasses the adapter is
    treated as plain text with empty context and null reference_datetime."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {"text": raw, "context": {}, "reference_datetime": None}, True
        if isinstance(parsed, dict) and "text" in parsed:
            return parsed, False
        # Valid JSON but not our envelope shape → treat the whole thing as text.
        return {"text": raw, "context": {}, "reference_datetime": None}, True
    return {"text": "", "context": {}, "reference_datetime": None}, True


def _extract_booking(context: dict, rejected: list[dict]) -> dict:
    booking = context.get("booking")
    if booking is None:
        return {}
    if not isinstance(booking, dict):
        rejected.append({"path": "context.booking", "reason": "type_mismatch"})
        return {}

    out: dict = {}
    bid = booking.get("booking_id")
    if isinstance(bid, str) and bid.strip():
        out["booking_id"] = bid.strip()
    elif bid is not None:
        rejected.append({"path": "context.booking.booking_id", "reason": "type_mismatch"})

    dep = booking.get("departure_date")
    if dep is not None:
        normalized = _parse_iso_date(dep)
        if normalized is None:
            rejected.append(
                {"path": "context.booking.departure_date", "reason": "invalid_date"}
            )
        else:
            out["departure_date"] = normalized

    st = booking.get("status")
    if st in _BOOKING_STATUS:
        out["status"] = st
    elif st is not None:
        rejected.append({"path": "context.booking.status", "reason": "invalid_enum"})

    return out


def _extract_history(context: dict, rejected: list[dict]) -> list[dict]:
    history = context.get("history")
    if history is None:
        return []
    if not isinstance(history, list):
        rejected.append({"path": "context.history", "reason": "type_mismatch"})
        return []

    out: list[dict] = []
    for i, item in enumerate(history):
        if not isinstance(item, dict):
            rejected.append({"path": f"context.history[{i}]", "reason": "type_mismatch"})
            continue
        role = item.get("role")
        text = item.get("text")
        if role not in _HISTORY_ROLES or not isinstance(text, str):
            rejected.append(
                {"path": f"context.history[{i}]", "reason": "invalid_element"}
            )
            continue
        out.append({"role": role, "text": text})
    return out


def _parse_iso_date(value: Any) -> str | None:
    """Validate a calendar date (no time). Return normalised ISO date str or None."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def _valid_reference_datetime(value: Any) -> str | None:
    """Keep the adapter-supplied TZ-aware ISO-8601 string if parseable, else None.
    Parsing (not string compare) is used everywhere downstream (SupplyDataQualityPrecedentAgent)."""
    if not isinstance(value, str):
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _scan_domain_pii(booking: dict, history: list) -> list[str]:
    fields: list[str] = []
    for key, val in (booking or {}).items():
        if isinstance(val, str) and detect_pii(val):
            fields.append(f"booking_context.{key}")
    for i, item in enumerate(history or []):
        text = item.get("text") if isinstance(item, dict) else None
        if isinstance(text, str) and detect_pii(text):
            fields.append(f"history_context[{i}].text")
    return fields
