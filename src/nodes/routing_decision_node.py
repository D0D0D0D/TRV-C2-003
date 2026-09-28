"""AgentCore Platform v1.0 — TRV-C2-003 inner routing_decision node (deterministic).

Table-driven routing + priority. Never fabricates a destination: unknown intent and
low confidence fall to manual_triage; emergency is pinned to immediate_escalation at
urgent priority. Conflict order is fixed R1 > R6 > R2/R3/R4/R5 (docs §4). No LLM.
"""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import PRIORITIES, ROUTING_TARGETS


class RoutingDecisionNode(FunctionNode):
    """Pick routing_target + priority from the deterministic rule findings."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, rules: dict | None = None) -> None:
        super().__init__()
        self._rules = rules or {}

    def execute(self, state: dict) -> dict:
        findings = state.get("rule_findings") or {}
        intent = state.get("intent", "other")
        routing_table = self._rules.get("routing_table") or {}
        priority_cfg = self._rules.get("priority") or {}

        r1 = bool(findings.get("r1_emergency"))
        r6 = bool(findings.get("r6_low_confidence"))

        if r1:
            decision = {
                "routing_target": "immediate_escalation",
                "priority": priority_cfg.get("emergency", "urgent"),
                "urgent": True,
            }
        elif r6:
            decision = {
                "routing_target": "manual_triage",
                "priority": "normal",
                "urgent": False,
            }
        else:
            target = routing_table.get(intent, "manual_triage")
            if intent == "other":
                target = "manual_triage"
            decision = {
                "routing_target": target,
                "priority": _derive_priority(findings, intent, priority_cfg),
                "urgent": False,
            }

        # Defensive closed-enum guard — the deterministic table should never produce
        # an out-of-enum value; if it somehow does, fall to the safe manual queue
        # rather than emit a value the S-3 gate would reject.
        if decision["routing_target"] not in ROUTING_TARGETS:
            decision = {"routing_target": "manual_triage", "priority": "normal", "urgent": False}
        if decision["priority"] not in PRIORITIES:
            decision["priority"] = "normal"

        emit_trace_event(
            "triage_routed",
            {
                "routing_target": decision["routing_target"],
                "priority": decision["priority"],
                "urgent": decision["urgent"],
            },
            state,
        )

        return {"routing_decision": decision, "status": AgentStatus.SUCCESS.value}


def _derive_priority(findings: dict, intent: str, priority_cfg: dict) -> str:
    priority = priority_cfg.get("default", "normal")
    if findings.get("r4_sentiment_band") == "severe":
        priority = priority_cfg.get("severe_sentiment", "high")
    band = findings.get("r2_cancel_band")
    if intent == "cancellation" and band in {"high", "full"}:
        priority = priority_cfg.get("imminent_cancellation", "high")
    elif band == "high":
        # Imminent departure (0–1 days) is time-critical regardless of intent.
        priority = priority_cfg.get("imminent_cancellation", "high")
    return priority
