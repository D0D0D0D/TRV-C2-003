"""Unit tests — RoutingDecisionNode (deterministic table + priority, conflict order)."""

from src.nodes.routing_decision_node import RoutingDecisionNode
from src.schemas.state import PRIORITIES, ROUTING_TARGETS

_RULES = {
    "routing_table": {
        "reservation": "reservations",
        "cancellation": "cancellation",
        "rebooking": "rebooking",
        "complaint": "complaints",
        "visa": "visa_immigration",
        "emergency": "emergency_desk",
        "other": "manual_triage",
    },
    "priority": {
        "emergency": "urgent",
        "severe_sentiment": "high",
        "imminent_cancellation": "high",
        "default": "normal",
    },
}


def _f(**kw):
    base = {
        "r1_emergency": False,
        "r2_cancel_band": None,
        "r3_visa_complexity": None,
        "r4_sentiment_band": "normal",
        "r5_booking_consistent": None,
        "r6_low_confidence": False,
    }
    base.update(kw)
    return base


def _decide(findings, intent="other"):
    node = RoutingDecisionNode(rules=_RULES)
    return node.execute({"rule_findings": findings, "intent": intent})["routing_decision"]


def test_emergency_pins_immediate_escalation():
    d = _decide(_f(r1_emergency=True), "reservation")
    assert d == {"routing_target": "immediate_escalation", "priority": "urgent", "urgent": True}


def test_low_confidence_manual_triage():
    d = _decide(_f(r6_low_confidence=True), "reservation")
    assert d["routing_target"] == "manual_triage"
    assert d["urgent"] is False


def test_table_routing_reservation():
    assert _decide(_f(), "reservation")["routing_target"] == "reservations"


def test_unknown_intent_to_manual_triage():
    assert _decide(_f(), "other")["routing_target"] == "manual_triage"


def test_visa_intent_to_specialist():
    assert _decide(_f(), "visa")["routing_target"] == "visa_immigration"


def test_severe_sentiment_raises_priority():
    d = _decide(_f(r4_sentiment_band="severe"), "complaint")
    assert d["routing_target"] == "complaints"
    assert d["priority"] == "high"


def test_cancellation_high_band_raises_priority():
    d = _decide(_f(r2_cancel_band="high"), "cancellation")
    assert d["priority"] == "high"


def test_imminent_departure_raises_priority_any_intent():
    d = _decide(_f(r2_cancel_band="high"), "reservation")
    assert d["priority"] == "high"


def test_normal_priority_default():
    assert _decide(_f(), "reservation")["priority"] == "normal"


def test_conflict_emergency_beats_low_confidence():
    d = _decide(_f(r1_emergency=True, r6_low_confidence=True), "reservation")
    assert d["routing_target"] == "immediate_escalation"


def test_all_outputs_within_closed_enums():
    for intent in ("reservation", "cancellation", "visa", "other", "emergency"):
        d = _decide(_f(r2_cancel_band="full", r4_sentiment_band="severe"), intent)
        assert d["routing_target"] in ROUTING_TARGETS
        assert d["priority"] in PRIORITIES
        assert isinstance(d["urgent"], bool)
