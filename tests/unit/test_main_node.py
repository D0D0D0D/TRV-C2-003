# TRV-C2-003 — Unit Tests: main slot GraphNode (TravelTriageGraphNode)
#
# The `main` slot is a Cat 2 GraphNode (not a plain FunctionNode): it wraps the inner
# travel-triage workflow. These tests cover the node's own contract — the schema
# boundary (merge_output maps only declared fields, no raw pass-through), the
# extract_input serialisation, and the two deterministic passthrough branches
# (upstream error / no validated input). The inner-node business logic is covered by
# the dedicated inner-node unit tests; the LLM-driven happy path is exercised by the
# live-LLM smoke check (docs/03_test_spec.md).

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.main_node import TravelTriageGraphNode


@pytest.fixture
def node():
    # Construction resolves + validates the bundled baseline KB and compiles the inner
    # graph (cwd = repo root during pytest, no LLM injected → deterministic path). This
    # mirrors server startup: a malformed KB would fail closed here.
    return TravelTriageGraphNode(inner_config={})


class TestTravelTriageGraphNodeContract:
    """Unit tests for the main GraphNode (composition boundary, docs §2-1 / §2-3)."""

    def test_class_contract(self, node):
        """S-1 trust + error/HITL strategy are declared explicitly (docs §6-1)."""
        assert node.required_trust_level == TrustLevel.VERIFIED_EXTERNAL
        assert node.error_strategy == "propagate"
        assert node.propagate_hitl is False

    def test_execute_signature(self):
        """node contract: override execute(self, state), never _invoke_impl()."""
        import inspect

        params = list(inspect.signature(TravelTriageGraphNode.execute).parameters)
        assert params[:2] == ["self", "state"]
        assert "_invoke_impl" not in TravelTriageGraphNode.__dict__

    def test_extract_input_serialises_validated_fields(self, node):
        """extract_input JSON-serialises the validated fields context_seed consumes."""
        state = {
            "validated_input": "please cancel my booking",
            "booking_context": {"booking_id": "B-1", "status": "confirmed"},
            "history_context": [{"role": "customer", "text": "hi"}],
            "reference_datetime": "2026-08-04T00:00:00+00:00",
        }
        payload = json.loads(node.extract_input(state))
        assert payload["validated_input"] == "please cancel my booking"
        assert payload["booking_context"] == {"booking_id": "B-1", "status": "confirmed"}
        assert payload["history_context"] == [{"role": "customer", "text": "hi"}]
        assert payload["reference_datetime"] == "2026-08-04T00:00:00+00:00"

    def test_extract_input_defaults_when_missing(self, node):
        """Missing optional fields serialise to their safe defaults (docs §3)."""
        payload = json.loads(node.extract_input({"validated_input": "x"}))
        assert payload["booking_context"] == {}
        assert payload["history_context"] == []
        assert payload["reference_datetime"] is None

    def test_merge_output_maps_only_declared_fields(self, node):
        """Schema boundary: only the four declared fields cross into parent State."""
        sub_result = {
            "routing_decision": {
                "routing_target": "reservations",
                "priority": "normal",
                "urgent": False,
            },
            "handoff_summary": "note",
            "triage_meta": {"confidence": 0.9, "fallback_reason": None, "degradation_reason": None},
            "status": AgentStatus.SUCCESS.value,
            "raw_inner_leak": "must not pass through",
        }
        merged = node.merge_output({}, sub_result)
        assert set(merged) == {"routing_decision", "handoff_summary", "triage_meta", "status"}
        assert "raw_inner_leak" not in merged
        assert merged["routing_decision"]["routing_target"] == "reservations"
        assert merged["status"] == AgentStatus.SUCCESS.value

    def test_merge_output_inner_error_passthrough(self, node):
        """An inner hard error is mapped to an error status + error_log (docs §2-3)."""
        merged = node.merge_output({}, {"status": AgentStatus.ERROR.value, "error_log": ["boom"]})
        assert merged["status"] == AgentStatus.ERROR.value
        assert merged["error_log"] == ["boom"]

    def test_execute_upstream_error_passthrough(self, node):
        """pre_process error short-circuits main: inner graph is NOT invoked (docs §2-3)."""
        result = node.execute({"status": AgentStatus.ERROR.value, "error_log": ["upstream"]})
        assert result == {}

    def test_execute_no_input_passthrough(self, node):
        """main requires a validated inquiry; without one it passes through (no seed)."""
        result = node.execute({"status": AgentStatus.SUCCESS.value, "validated_input": ""})
        assert result == {}
