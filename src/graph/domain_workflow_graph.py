"""AgentCore Platform v1.0 — TRV-C2-003 inner domain-workflow graph.

Linear 5-node travel-triage workflow, instantiated + compiled by
TravelTriageGraphNode.get_subgraph() (docs §2). Inherits BaseGraph directly (custom
node names, no initialize/finalize backbone). route()/get_output() are annotated with
InnerTriageState so the projection keeps the custom fields (Business Travel Hotel & Plan Recommendation Agent).

    START → context_seed → intent_context → domain_rule → routing_decision
            → handoff_summary → END
"""

from __future__ import annotations

from langgraph.graph import END, START

from framework.errors import ConfigError
from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus

from src.nodes.context_seed_node import ContextSeedNode
from src.nodes.domain_rule_node import DomainRuleNode
from src.nodes.handoff_summary_node import HandoffSummaryNode
from src.nodes.intent_context_node import IntentContextNode
from src.nodes.routing_decision_node import RoutingDecisionNode
from src.schemas.state import InnerTriageState


class TravelTriageInnerGraph(BaseGraph):
    """Inner deterministic-first triage workflow (LLM observes, rules decide)."""

    @property
    def name(self) -> str:
        return "travel_triage_workflow"

    @property
    def state_schema(self) -> type:
        return InnerTriageState

    def _validate_config(self) -> None:
        # The resolved + validated KB is injected by the outer main node at
        # construction (which is where a malformed KB fails closed). If it is absent,
        # the inner graph was constructed out of band — fail closed.
        if "domain_rules_resolved" not in self.config:
            raise ConfigError(
                "inner graph requires resolved domain_rules (injected by main node)"
            )

    def register_nodes(self) -> None:
        # No super() — BaseGraph.register_nodes() is abstract; no initialize/finalize.
        llm = self.config.get("llm")
        rules = self.config.get("domain_rules_resolved") or {}
        degraded = bool(self.config.get("domain_rules_degraded"))
        thresholds = self.config.get("thresholds") or {}
        operator_timezone = self.config.get("operator_timezone", "Asia/Tokyo")

        self._nodes["context_seed"] = ContextSeedNode()
        self._nodes["intent_context"] = IntentContextNode(llm=llm)
        self._nodes["domain_rule"] = DomainRuleNode(
            rules=rules, thresholds=thresholds, operator_timezone=operator_timezone
        )
        self._nodes["routing_decision"] = RoutingDecisionNode(rules=rules)
        self._nodes["handoff_summary"] = HandoffSummaryNode(
            llm=llm, baseline_degraded=degraded
        )

    def add_edges(self) -> None:
        self._sg.add_edge(START, "context_seed")
        self._sg.add_edge("context_seed", "intent_context")
        self._sg.add_edge("intent_context", "domain_rule")
        self._sg.add_edge("domain_rule", "routing_decision")
        self._sg.add_edge("routing_decision", "handoff_summary")
        self._sg.add_edge("handoff_summary", END)

    def route(self, state: InnerTriageState) -> str:
        # Linear topology — route() is required by the ABC but never reached (no
        # conditional edges). Defensive: terminate on a hard inner error.
        return END if state.get("status") == AgentStatus.ERROR.value else "handoff_summary"

    def get_output(self, state: InnerTriageState) -> dict:
        # Shaped for TravelTriageGraphNode.merge_output(): only the fields the outer
        # graph maps into its own state (no raw pass-through).
        return {
            "routing_decision": state.get("routing_decision") or {},
            "handoff_summary": state.get("handoff_summary", ""),
            "triage_meta": state.get("triage_meta") or {},
            "status": state.get("status", AgentStatus.SUCCESS.value),
        }
