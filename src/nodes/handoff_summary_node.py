"""AgentCore Platform v1.0 — TRV-C2-003 inner handoff_summary node (LLM narration).

Narrates the situation for the receiving desk *without changing the deterministic
routing_decision* (docs §2-2). Also assembles triage_meta (confidence /
fallback_reason / degradation_reason). When the LLM is unavailable it produces a
deterministic template summary and flags llm_unavailable — it never goes silent.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_ROUTING_LABELS = {
    "reservations": "Reservations desk",
    "cancellation": "Cancellation desk",
    "rebooking": "Rebooking desk",
    "complaints": "Complaints / customer-relations desk",
    "visa_immigration": "Visa & immigration specialist",
    "emergency_desk": "Emergency desk",
    "immediate_escalation": "Immediate escalation (supervisor)",
    "manual_triage": "Manual triage (human review)",
}


def build_handoff_prompt(
    validated_input: str, rule_findings: dict, routing_decision: dict
) -> list[dict]:
    """Chat messages for the narration call. The routing decision is provided as a
    FIXED fact the model must not override (it narrates, it does not decide)."""
    system = (
        "You are a travel front-desk assistant. The triage decision has ALREADY been "
        "made deterministically and is FINAL — do NOT change it, contradict it, or "
        "suggest a different desk. Write a short handoff note (2–4 sentences) for the "
        "receiving staff so they can pick up the case.\n"
        "- Summarise what the customer is asking for and any urgency/emotion.\n"
        "- Write in the customer's own language; if unsure, use both Japanese and English.\n"
        "- Do NOT give the customer an answer or a legal/visa determination.\n"
        "- Output the note only, no JSON, no preamble."
    )
    facts = {
        "routing_target": routing_decision.get("routing_target"),
        "priority": routing_decision.get("priority"),
        "urgent": routing_decision.get("urgent"),
        "rule_findings": rule_findings,
    }
    user = (
        f"Customer inquiry:\n{validated_input}\n\n"
        f"FINAL triage decision (do not change):\n"
        f"{json.dumps(facts, ensure_ascii=False)}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


class HandoffSummaryNode(FunctionNode):
    """Generate the handoff narration and assemble triage_meta."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm: Any = None, baseline_degraded: bool = False) -> None:
        super().__init__()
        self._llm = llm
        self._baseline_degraded = bool(baseline_degraded)

    def execute(self, state: dict) -> dict:
        validated_input = state.get("validated_input", "")
        findings = state.get("rule_findings") or {}
        routing_decision = state.get("routing_decision") or {}
        confidence = float(state.get("confidence", 0.0))
        fallback_reason = state.get("fallback_reason")
        intent_degraded = bool(state.get("intent_degraded"))

        summary, summary_degraded = self._summarize(validated_input, findings, routing_decision)

        tokens: list[str] = []
        if self._baseline_degraded:
            tokens.append("domain_rules_baseline_seed")
        if intent_degraded or summary_degraded:
            tokens.append("llm_unavailable")
        degradation_reason = "+".join(tokens) if tokens else None

        triage_meta = {
            "confidence": confidence,
            "fallback_reason": fallback_reason,
            "degradation_reason": degradation_reason,
        }

        emit_trace_event(
            "triage_summary_fallback" if summary_degraded else "triage_summary_generated",
            {"degradation_reason": degradation_reason},
            state,
        )

        return {
            "handoff_summary": summary,
            "triage_meta": triage_meta,
            "status": AgentStatus.SUCCESS.value,
        }

    def _summarize(
        self, validated_input: str, findings: dict, routing_decision: dict
    ) -> tuple[str, bool]:
        if self._llm is None:
            return _fallback_summary(findings, routing_decision), True
        try:
            messages = build_handoff_prompt(validated_input, findings, routing_decision)
            resp = self._llm.complete(messages)
            content = resp.get("content", "") if isinstance(resp, dict) else str(resp)
            text = content.strip()
            if not text:
                return _fallback_summary(findings, routing_decision), True
            return text, False
        except Exception:
            return _fallback_summary(findings, routing_decision), True


def _fallback_summary(findings: dict, routing_decision: dict) -> str:
    target = routing_decision.get("routing_target", "manual_triage")
    label = _ROUTING_LABELS.get(target, target)
    priority = routing_decision.get("priority", "normal")
    notes = []
    if findings.get("r1_emergency"):
        notes.append("emergency signal detected")
    if findings.get("r6_low_confidence"):
        notes.append("low-confidence classification")
    if findings.get("r2_cancel_band") in {"high", "full"}:
        notes.append("imminent/expired departure")
    if findings.get("r4_sentiment_band") == "severe":
        notes.append("severe complaint sentiment")
    note_str = f" Notes: {', '.join(notes)}." if notes else ""
    return (
        f"[auto] Route to {label} at {priority} priority.{note_str} "
        f"[自動要約] {label} に振り分け（優先度 {priority}）。"
    )
