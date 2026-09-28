"""AgentCore Platform v1.0 — TRV-C2-003 inner context_seed node.

Deserialises the JSON envelope produced by TravelTriageGraphNode.extract_input into
the inner State. No re-validation (the outer pre_process already validated — docs
§2-1 step 5). Deterministic / pure.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class ContextSeedNode(FunctionNode):
    """Load the validated fields from the extract_input envelope into inner State."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        raw = state.get("user_input", "")
        data: dict[str, Any] = {}
        if isinstance(raw, str) and raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    data = loaded
            except (ValueError, TypeError):
                data = {}

        emit_trace_event(
            "triage_context_seeded",
            {
                "has_booking": bool(data.get("booking_context")),
                "history_len": len(data.get("history_context") or []),
                "has_reference_datetime": data.get("reference_datetime") is not None,
            },
            state,
        )

        return {
            "validated_input": data.get("validated_input", ""),
            "booking_context": data.get("booking_context") or {},
            "history_context": data.get("history_context") or [],
            "reference_datetime": data.get("reference_datetime"),
            "status": AgentStatus.SUCCESS.value,
        }
