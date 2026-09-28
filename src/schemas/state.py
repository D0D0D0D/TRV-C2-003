"""AgentCore Platform v1.0 — TRV-C2-003 State schema & output contracts.

ADR-005: State must be a flat TypedDict (msgpack-serializable primitives only).
No credentials / secrets / Pydantic / dataclass / InvocationContext in State.

This module is the single authoritative shape for the agent (docs/02_design.md §2-4):
the deterministic decision is carried in ``routing_decision`` and post_process is the
only place that flattens it into ``formatted_output``.
"""

from framework.schemas.agent_state import AgentState

# ── Closed enums (docs/02_design.md §2-4 / §2-5) ─────────────────────────────
# Values are stored in State as plain strings; frozensets are the allowlists used
# by the deterministic rules and the S-3 output gate.

ROUTING_TARGETS = frozenset(
    {
        "reservations",
        "cancellation",
        "rebooking",
        "complaints",
        "visa_immigration",
        "emergency_desk",
        "immediate_escalation",
        "manual_triage",
    }
)

PRIORITIES = frozenset({"urgent", "high", "normal", "low"})

INTENTS = frozenset(
    {
        "reservation",
        "cancellation",
        "rebooking",
        "complaint",
        "visa",
        "emergency",
        "other",
    }
)

# fallback_reason is enum|null — None is a valid value (no fallback applied).
FALLBACK_REASONS = frozenset(
    {"emergency_safeside", "low_confidence", "unknown_intent", "booking_mismatch"}
)

# degradation_reason is a "+"-joined set of the tokens below (docs §2-4). The S-3
# gate splits on "+" and rejects any token outside this closed set.
DEGRADATION_TOKENS = frozenset(
    {
        "domain_rules_baseline_seed",
        "llm_unavailable",
        "context_envelope_unparsable",
        "handoff_summary_withheld",
    }
)

# Nested enum members carried inside rule_findings (docs §2-5). Band boundaries are
# DATA in config/domain_rules.baseline.yaml; only the member set is fixed here.
CANCEL_BANDS = frozenset({"none", "low", "medium", "high", "full"})
VISA_COMPLEXITIES = frozenset({"simple", "complex"})
SENTIMENT_BANDS = frozenset({"normal", "elevated", "severe"})
PURPOSES = frozenset({"tourism", "business", "transit", "other"})

# S-3 closed allowlist for formatted_output (docs §6-3).
FORMATTED_OUTPUT_KEYS = frozenset(
    {"routing_target", "priority", "urgent", "handoff_summary", "triage_meta"}
)
TRIAGE_META_KEYS = frozenset({"confidence", "fallback_reason", "degradation_reason"})

# Fixed safe replacement when the handoff_summary free-text guard trips (docs §6-3).
WITHHELD_SUMMARY = "[triage summary withheld — see routing_target]"


class State(AgentState):
    """Outer graph state (AgentBaseGraph pipeline).

    Inherits all shared fields (user_input, status, session_id, node_history,
    error_log, correlation_id, trace_id, thread_id, caller_trust_level, caller_id,
    hitl_*, retry_count …) from AgentState. Only agent-specific derived fields are
    declared below (all optional / derived — total is not enforced at runtime).
    """

    validated_input: str
    booking_context: dict
    history_context: list
    reference_datetime: str | None
    rejected_context: list
    # Degradation raised at the outer envelope layer (pre_process); post_process
    # folds it together with the inner triage_meta.degradation_reason. Kept outside
    # triage_meta because merge_output overwrites triage_meta with the inner value.
    envelope_degradation: str | None
    routing_decision: dict
    handoff_summary: str
    triage_meta: dict
    formatted_output: dict


class InnerTriageState(AgentState):
    """Inner domain-workflow state (BaseGraph). Used for route()/get_output() type
    annotations so the projection keeps the custom fields (AgentState annotation
    would drop them — Business Travel Hotel & Plan Recommendation Agent)."""

    validated_input: str
    booking_context: dict
    history_context: list
    reference_datetime: str | None
    intent: str
    urgency_signal: bool
    sentiment_score: float
    confidence: float
    emergency_signal: bool
    arrival_country: str | None
    nationality: str | None
    purpose: str | None
    rule_findings: dict
    escalation_required: bool
    fallback_reason: str | None
    routing_decision: dict
    handoff_summary: str
    triage_meta: dict
