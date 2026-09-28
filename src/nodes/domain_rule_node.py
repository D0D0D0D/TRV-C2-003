"""AgentCore Platform v1.0 — TRV-C2-003 inner domain_rule node (deterministic R1–R6).

Applies the deterministic rules over the LLM observation vector. Fully reproducible
and unit-testable — no LLM, no network, no now() (SupplyDataQualityPrecedentAgent: dates are parsed to
aware datetimes, never string-compared; the reference datetime is fixed by the adapter).
Emits rule_findings, escalation_required and fallback_reason (docs §2-2 / §4).
"""

from __future__ import annotations

import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any, ClassVar

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class DomainRuleNode(FunctionNode):
    """Deterministic travel-triage rules R1–R6."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        rules: dict | None = None,
        thresholds: dict | None = None,
        operator_timezone: str = "Asia/Tokyo",
    ) -> None:
        super().__init__()
        self._rules = rules or {}
        self._thresholds = thresholds or {}
        self._tz = operator_timezone or "Asia/Tokyo"
        # Pre-normalise the emergency keyword layer once (deterministic keyword R1).
        self._emergency_kw = [
            _normalize(k) for k in (self._rules.get("emergency_keywords") or [])
        ]

    def execute(self, state: dict) -> dict:
        intent = state.get("intent", "other")
        booking = state.get("booking_context") or {}
        reference_dt = state.get("reference_datetime")

        low_conf = float(self._thresholds.get("low_confidence", 0.55))
        med_sent = float(self._thresholds.get("medium_sentiment_severity", 0.50))
        high_sent = float(self._thresholds.get("high_sentiment_severity", 0.75))

        # R1 — emergency (LLM signal OR deterministic keyword layer; either wins).
        r1_emergency = bool(state.get("emergency_signal")) or self._keyword_emergency(
            state.get("validated_input", "")
        )

        # R2 — cancellation-fee band (booking or reference missing → skip / null).
        r2_band = self._cancel_band(booking.get("departure_date"), reference_dt)

        # R3 — visa complexity (only meaningful for visa intent; else null).
        r3_complexity = self._visa_complexity(intent, state)

        # R4 — sentiment / complaint severity band.
        r4_band = _sentiment_band(float(state.get("sentiment_score", 0.0)), med_sent, high_sent)

        # R5 — booking consistency (booking missing → null / skip).
        r5_consistent = _booking_consistency(booking, intent)

        # R6 — low-confidence fallback.
        r6_low = float(state.get("confidence", 0.0)) < low_conf

        rule_findings = {
            "r1_emergency": r1_emergency,
            "r2_cancel_band": r2_band,
            "r3_visa_complexity": r3_complexity,
            "r4_sentiment_band": r4_band,
            "r5_booking_consistent": r5_consistent,
            "r6_low_confidence": r6_low,
        }

        # Conflict order (fixed): R1 > R6 > R2/R3/R4/R5 (docs §4).
        if r1_emergency:
            escalation_required, fallback_reason = True, "emergency_safeside"
        elif r6_low:
            escalation_required, fallback_reason = True, "low_confidence"
        elif intent == "other":
            escalation_required, fallback_reason = False, "unknown_intent"
        elif r5_consistent is False:
            escalation_required, fallback_reason = False, "booking_mismatch"
        else:
            escalation_required, fallback_reason = False, None

        emit_trace_event(
            "triage_rules_applied",
            {
                "r1_emergency": r1_emergency,
                "r2_cancel_band": r2_band,
                "r6_low_confidence": r6_low,
                "escalation_required": escalation_required,
                "fallback_reason": fallback_reason,
            },
            state,
        )

        return {
            "rule_findings": rule_findings,
            "escalation_required": escalation_required,
            "fallback_reason": fallback_reason,
            "status": AgentStatus.SUCCESS.value,
        }

    # ── R1 keyword layer ────────────────────────────────────────────────────
    def _keyword_emergency(self, text: str) -> bool:
        if not isinstance(text, str) or not text:
            return False
        norm = _normalize(text)
        return any(kw and kw in norm for kw in self._emergency_kw)

    # ── R2 cancellation-fee band ────────────────────────────────────────────
    def _cancel_band(self, departure_date: Any, reference_dt: Any) -> str | None:
        days = self._days_until_departure(departure_date, reference_dt)
        if days is None:
            return None
        for entry in self._rules.get("cancellation_fee_bands") or []:
            lo = entry.get("min_days_inclusive")
            hi = entry.get("max_days_exclusive")
            if (lo is None or days >= lo) and (hi is None or days < hi):
                return entry.get("band")
        return None

    def _days_until_departure(self, departure_date: Any, reference_dt: Any) -> int | None:
        if not isinstance(departure_date, str) or not isinstance(reference_dt, str):
            return None
        try:
            dep = date.fromisoformat(departure_date)
            ref = datetime.fromisoformat(reference_dt.replace("Z", "+00:00"))
        except ValueError:
            return None
        if ref.tzinfo is None:  # reference must be TZ-aware (adapter guarantees it)
            return None
        local_date = _to_local_date(ref, self._tz)
        return (dep - local_date).days

    # ── R3 visa complexity ──────────────────────────────────────────────────
    def _visa_complexity(self, intent: str, state: dict) -> str | None:
        if intent != "visa":
            return None
        arrival = state.get("arrival_country")
        nationality = state.get("nationality")
        purpose = state.get("purpose")
        if arrival is None or nationality is None or purpose is None:
            return "complex"  # missing info → safe side (always visa_immigration)
        visa_cfg = self._rules.get("visa") or {}
        simple_purposes = set(visa_cfg.get("simple_purposes") or [])
        simple_pairs = set(visa_cfg.get("simple_country_pairs") or [])
        pair = f"{nationality}>{arrival}"
        if purpose in simple_purposes and (not simple_pairs or pair in simple_pairs):
            return "simple"
        return "complex"


# ── pure helpers ─────────────────────────────────────────────────────────────
def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _sentiment_band(score: float, med: float, high: float) -> str:
    if score >= high:
        return "severe"
    if score >= med:
        return "elevated"
    return "normal"


def _booking_consistency(booking: dict, intent: str) -> bool | None:
    if not booking:
        return None
    status = booking.get("status")
    # Already-cancelled booking with a new cancellation request is inconsistent.
    if status == "cancelled" and intent == "cancellation":
        return False
    return True


def _to_local_date(ref: datetime, tz_name: str) -> date:
    if ZoneInfo is not None:
        try:
            return ref.astimezone(ZoneInfo(tz_name)).date()
        except Exception:
            pass
    # tzdata unavailable: Asia/Tokyo is a fixed UTC+9 offset (no DST since 1948);
    # any other zone falls back to UTC rather than guessing an offset.
    if tz_name == "Asia/Tokyo":
        return (ref.astimezone(timezone.utc) + timedelta(hours=9)).date()
    return ref.astimezone(timezone.utc).date()
