"""Unit tests — HandoffSummaryNode (narration + triage_meta / degradation assembly)."""

from src.nodes.handoff_summary_node import HandoffSummaryNode, build_handoff_prompt


class FakeLLM:
    def __init__(self, content: str):
        self._content = content

    def complete(self, messages):
        return {"content": self._content}


def _state(**kw):
    s = {
        "validated_input": "x",
        "rule_findings": {
            "r1_emergency": False,
            "r6_low_confidence": False,
            "r2_cancel_band": None,
            "r4_sentiment_band": "normal",
        },
        "routing_decision": {"routing_target": "reservations", "priority": "normal", "urgent": False},
        "confidence": 0.9,
        "fallback_reason": None,
        "intent_degraded": False,
    }
    s.update(kw)
    return s


def test_prompt_marks_decision_final_and_includes_target():
    msgs = build_handoff_prompt(
        "cancel", {"r1_emergency": True}, {"routing_target": "immediate_escalation", "priority": "urgent", "urgent": True}
    )
    blob = " ".join(m["content"] for m in msgs)
    assert "FINAL" in blob
    assert "immediate_escalation" in blob


def test_llm_summary_used_no_degradation():
    node = HandoffSummaryNode(llm=FakeLLM("Customer wants to cancel; hand to the desk."))
    r = node.execute(_state())
    assert "cancel" in r["handoff_summary"].lower()
    assert r["triage_meta"]["degradation_reason"] is None
    assert r["triage_meta"]["confidence"] == 0.9


def test_no_llm_uses_deterministic_fallback():
    r = HandoffSummaryNode(llm=None).execute(_state())
    assert r["handoff_summary"]
    assert r["triage_meta"]["degradation_reason"] == "llm_unavailable"


def test_empty_llm_response_falls_back():
    r = HandoffSummaryNode(llm=FakeLLM("   ")).execute(_state())
    assert r["triage_meta"]["degradation_reason"] == "llm_unavailable"


def test_baseline_degraded_token():
    r = HandoffSummaryNode(llm=FakeLLM("ok"), baseline_degraded=True).execute(_state())
    assert "domain_rules_baseline_seed" in r["triage_meta"]["degradation_reason"]


def test_intent_degraded_adds_llm_unavailable():
    r = HandoffSummaryNode(llm=FakeLLM("ok")).execute(_state(intent_degraded=True))
    assert "llm_unavailable" in r["triage_meta"]["degradation_reason"]


def test_both_degradations_joined():
    r = HandoffSummaryNode(llm=None, baseline_degraded=True).execute(_state(intent_degraded=True))
    dr = r["triage_meta"]["degradation_reason"]
    assert "domain_rules_baseline_seed" in dr
    assert "llm_unavailable" in dr


def test_fallback_reason_passed_into_meta():
    r = HandoffSummaryNode(llm=FakeLLM("ok")).execute(_state(fallback_reason="low_confidence"))
    assert r["triage_meta"]["fallback_reason"] == "low_confidence"


def test_fallback_summary_mentions_target_and_notes():
    r = HandoffSummaryNode(llm=None).execute(
        _state(
            routing_decision={"routing_target": "immediate_escalation", "priority": "urgent", "urgent": True},
            rule_findings={"r1_emergency": True, "r6_low_confidence": False, "r2_cancel_band": None, "r4_sentiment_band": "normal"},
        )
    )
    assert "emergency" in r["handoff_summary"].lower()
