"""AgentCore Platform v1.0 — TRV-C2-003 outer main node (GraphNode).

Wraps the inner travel-triage workflow. Resolves + validates the domain-rule KB at
construction (= outer compile / server startup) so a malformed operator KB fails
closed on the startup path, not only in tests. Passes upstream errors through
untouched (pre→main is an unconditional edge, docs §2-1 / §2-3).
"""

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import load_domain_rules


class TravelTriageGraphNode(GraphNode):
    """`main` slot — invokes the inner domain-workflow graph."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def __init__(self, inner_config: dict | None = None) -> None:
        super().__init__()
        cfg = dict(inner_config or {})
        # ConfigError raised here (outer compile / startup) if the KB is malformed.
        rules, degraded = load_domain_rules(cfg)
        cfg["domain_rules_resolved"] = rules
        cfg["domain_rules_degraded"] = degraded
        self._inner_config = cfg
        # Build + compile the inner graph once (cache); get_subgraph returns it.
        from src.graph.domain_workflow_graph import TravelTriageInnerGraph

        subgraph = TravelTriageInnerGraph(config=cfg)
        subgraph.compile()
        self._subgraph = subgraph

    def execute(self, state: dict) -> dict:
        # Upstream error passthrough — do not invoke the inner graph on a failed
        # pre_process (super().execute preserves context propagation for the happy path).
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event(
                "triage_upstream_error_passthrough",
                {"error_log_len": len(state.get("error_log") or [])},
                state,
            )
            return {}
        # Guard: main requires a validated inquiry from pre_process. An isolated call
        # without one (pre_process bypassed) passes through rather than seeding the
        # inner graph with empty input.
        if not state.get("validated_input"):
            emit_trace_event("triage_main_no_input_passthrough", {}, state)
            return {}
        result = super().execute(state)
        emit_trace_event(
            "triage_subgraph_merged",
            {
                "routing_target": (result.get("routing_decision") or {}).get("routing_target"),
                "status": result.get("status"),
            },
            state,
        )
        return result

    def get_subgraph(self):
        return self._subgraph

    def extract_input(self, state: dict) -> str:
        # Structured context is not auto-propagated to the subgraph (invoke takes a
        # single string) — serialise the validated fields; context_seed deserialises.
        return json.dumps(
            {
                "validated_input": state.get("validated_input", ""),
                "booking_context": state.get("booking_context") or {},
                "history_context": state.get("history_context") or [],
                "reference_datetime": state.get("reference_datetime"),
            },
            default=str,
        )

    def merge_output(self, state: dict, sub_result: dict) -> dict:
        # Schema boundary: map only the declared fields (no raw pass-through).
        if sub_result.get("status") == AgentStatus.ERROR.value:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": sub_result.get("error_log") or ["inner triage error"],
            }
        return {
            "routing_decision": sub_result.get("routing_decision") or {},
            "handoff_summary": sub_result.get("handoff_summary", ""),
            "triage_meta": sub_result.get("triage_meta") or {},
            "status": sub_result.get("status", AgentStatus.SUCCESS.value),
        }
