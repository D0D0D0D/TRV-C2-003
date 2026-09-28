"""AgentCore Platform v1.0 — TRV-C2-003 outer graph (Cat 2).

Fixed 5-node backbone (initialize → pre_process → main → post_process → finalize).
The multi-step travel-triage workflow is encapsulated in the `main` slot
(TravelTriageGraphNode → inner TravelTriageInnerGraph): the LLM produces observations,
deterministic rules decide the routing (docs/02_design.md §2). add_edges()/route() are
NOT overridden — backbone wiring belongs to the framework.
"""

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.main_node import TravelTriageGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


class TravelInquiryTriageAgent(AgentBaseGraph):
    """Travel inquiry triage & routing agent (Cat 2 outer graph)."""

    @property
    def name(self) -> str:
        return "trv-c2-003"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects initialize + finalize
        thresholds = self.config.get("thresholds") or {}
        self._nodes["pre_process"] = PreProcessNode(
            max_input_chars=int(thresholds.get("max_input_chars", 8000))
        )
        self._nodes["main"] = TravelTriageGraphNode(inner_config=self.config)
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — the framework wires the fixed backbone.


# Backward-compatible alias — AgentRegistry manifest entry point + scaffold import.
Graph = TravelInquiryTriageAgent
