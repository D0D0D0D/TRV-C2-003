"""AgentCore Platform v1.0 — TRV-C2-003 inner intent_context node (LLM observation).

The LLM produces *observations only* (intent / urgency / sentiment / confidence /
emergency signal / visa fields). It never selects routing_target — the deterministic
rules do (docs §5). A missing / malformed / schema-violating response collapses the
whole observation vector to a safe deterministic fallback (intent=other,
confidence=0.0 → R6 manual_triage; visa fields null → R3 visa_immigration).
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import INTENTS, PURPOSES

# ── LLM output schema (docs §5). Kept as data so build_intent_prompt embeds a
# concrete element example + the allowlists (empty-schema prompts collapse the LLM
# to empty output — MunicipalContractVariationEvidenceBriefAgent / JAMemberInquiryIntakeAgent). ────────────────────────────────
_ISO_ALPHA2_HINT = "ISO-3166 alpha-2 uppercase (e.g. JP, US, FR) or null"

_SCHEMA_EXAMPLE = {
    "intent": "cancellation",
    "urgency_signal": True,
    "sentiment_score": 0.82,
    "confidence": 0.9,
    "emergency_signal": False,
    "arrival_country": "US",
    "nationality": "JP",
    "purpose": "tourism",
}


def build_intent_prompt(validated_input: str, history: list[dict]) -> list[dict]:
    """Return the chat messages for the observation call. Returned verbatim so a
    prompt-contract test can assert the schema keys, allowlists and imperative
    phrasing are present (JAMemberInquiryIntakeAgent)."""
    history_lines = "\n".join(
        f"- {h.get('role', '?')}: {h.get('text', '')}"
        for h in (history or [])
        if isinstance(h, dict)
    )
    allowlists = (
        f"intent MUST be exactly one of: {sorted(INTENTS)}.\n"
        f"purpose MUST be exactly one of: {sorted(PURPOSES)} or null.\n"
        f"arrival_country / nationality MUST be {_ISO_ALPHA2_HINT}.\n"
        "urgency_signal / emergency_signal MUST be booleans.\n"
        "sentiment_score / confidence MUST be numbers between 0.0 and 1.0."
    )
    system = (
        "You are a travel front-desk triage observer. Read the customer inquiry and "
        "OUTPUT OBSERVATIONS ONLY as a single JSON object. Do NOT decide which desk "
        "handles it and do NOT answer the inquiry.\n\n"
        "Return EXACTLY this JSON shape (all keys required, no extra keys):\n"
        f"{json.dumps(_SCHEMA_EXAMPLE, ensure_ascii=False)}\n\n"
        f"{allowlists}\n\n"
        "Rules (imperative):\n"
        "- Set emergency_signal to true when the text describes an accident, illness, "
        "hospitalisation, theft, lost passport, or a natural disaster.\n"
        "- Set sentiment_score high (>=0.75) for angry / severe complaints.\n"
        "- When a visa/immigration question mentions an arrival country, nationality, "
        "or stay purpose, extract them; when a field is not stated, set it to null.\n"
        "- Output only the JSON object, no prose, no code fence."
    )
    user = f"Inquiry:\n{validated_input}"
    if history_lines:
        user += f"\n\nRecent conversation:\n{history_lines}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


class IntentContextNode(FunctionNode):
    """Extract the structured observation vector from the inquiry (LLM)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm: Any = None) -> None:
        super().__init__()
        self._llm = llm

    def execute(self, state: dict) -> dict:
        validated_input = state.get("validated_input", "")
        history = state.get("history_context", [])

        obs, degraded = self._observe(validated_input, history)

        if degraded:
            emit_trace_event("triage_intent_llm_unavailable", {}, state)
        else:
            emit_trace_event(
                "triage_intent_observed",
                {"intent": obs["intent"], "emergency": obs["emergency_signal"]},
                state,
            )

        return {
            **obs,
            "intent_degraded": degraded,
            "status": AgentStatus.SUCCESS.value,
        }

    def _observe(self, validated_input: str, history: list[dict]) -> tuple[dict, bool]:
        if self._llm is None:
            return _fallback_observation(), True
        try:
            messages = build_intent_prompt(validated_input, history)
            resp = self._llm.complete(messages)
            content = resp.get("content", "") if isinstance(resp, dict) else str(resp)
            parsed = json.loads(_strip_code_fence(content))
            return _coerce_observation(parsed), False
        except Exception:
            # parse failure / schema violation / LLM error → safe deterministic vector
            return _fallback_observation(), True


# ── pure helpers ─────────────────────────────────────────────────────────────
def _fallback_observation() -> dict:
    return {
        "intent": "other",
        "urgency_signal": False,
        "sentiment_score": 0.0,
        "confidence": 0.0,
        "emergency_signal": False,
        "arrival_country": None,
        "nationality": None,
        "purpose": None,
    }


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _coerce_observation(parsed: Any) -> dict:
    """Strict validation — any violation raises ValueError so the caller falls back
    to the whole safe vector (field-level rescue is deliberately not done, docs §5)."""
    if not isinstance(parsed, dict):
        raise ValueError("observation is not a JSON object")

    expected = set(_SCHEMA_EXAMPLE)
    if set(parsed) != expected:
        raise ValueError("observation keys do not match schema exactly")

    intent = parsed["intent"]
    if intent not in INTENTS:
        raise ValueError("intent not in allowlist")

    for key in ("urgency_signal", "emergency_signal"):
        if not isinstance(parsed[key], bool):
            raise ValueError(f"{key} must be bool")

    for key in ("sentiment_score", "confidence"):
        val = parsed[key]
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ValueError(f"{key} must be a number")
        if not 0.0 <= float(val) <= 1.0:
            raise ValueError(f"{key} out of range")

    arrival = _coerce_country(parsed["arrival_country"])
    nationality = _coerce_country(parsed["nationality"])

    purpose = parsed["purpose"]
    if purpose is not None and purpose not in PURPOSES:
        raise ValueError("purpose not in allowlist")

    return {
        "intent": intent,
        "urgency_signal": bool(parsed["urgency_signal"]),
        "sentiment_score": float(parsed["sentiment_score"]),
        "confidence": float(parsed["confidence"]),
        "emergency_signal": bool(parsed["emergency_signal"]),
        "arrival_country": arrival,
        "nationality": nationality,
        "purpose": purpose,
    }


def _coerce_country(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("country code must be str or null")
    v = value.strip().upper()
    if len(v) != 2 or not v.isalpha():
        raise ValueError("country code must be ISO-3166 alpha-2")
    return v
