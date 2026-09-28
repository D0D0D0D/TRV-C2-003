"""Unit tests — PostProcessNode (formatted_output + S-3 closed allowlist, TC-02)."""

import pytest

from framework.errors import SecurityViolationError

from src.nodes.post_process_node import PostProcessNode, _guard_summary, _merge_degradation
from src.schemas.state import FORMATTED_OUTPUT_KEYS, TRIAGE_META_KEYS, WITHHELD_SUMMARY


def _state(**kw):
    s = {
        "status": "success",
        "routing_decision": {"routing_target": "reservations", "priority": "normal", "urgent": False},
        "triage_meta": {"confidence": 0.9, "fallback_reason": None, "degradation_reason": None},
        "handoff_summary": "Customer wants to book a trip.",
        "envelope_degradation": None,
    }
    s.update(kw)
    return s


def _fo(**kw):
    base = {
        "routing_target": "reservations",
        "priority": "normal",
        "urgent": False,
        "handoff_summary": "x",
        "triage_meta": {"confidence": 0.5, "fallback_reason": None, "degradation_reason": None},
    }
    base.update(kw)
    return {"formatted_output": base}


# ── output assembly ───────────────────────────────────────────────────────────
def test_formatted_output_is_closed_shape():
    fo = PostProcessNode().execute(_state())["formatted_output"]
    assert set(fo) == FORMATTED_OUTPUT_KEYS
    assert set(fo["triage_meta"]) == TRIAGE_META_KEYS
    assert fo["routing_target"] == "reservations"


def test_degradation_merged_from_envelope_and_inner():
    r = PostProcessNode().execute(
        _state(
            envelope_degradation="context_envelope_unparsable",
            triage_meta={"confidence": 0.0, "fallback_reason": "low_confidence", "degradation_reason": "llm_unavailable"},
        )
    )
    dr = r["formatted_output"]["triage_meta"]["degradation_reason"]
    assert "context_envelope_unparsable" in dr
    assert "llm_unavailable" in dr


def test_upstream_error_passthrough_emits_no_output():
    r = PostProcessNode().execute(_state(status="error"))
    assert "formatted_output" not in r


# ── S-3 gate: accepts valid, rejects everything outside the closed shape (TC-02) ──
def test_s3_accepts_valid_output():
    node = PostProcessNode()
    result = node.execute(_state())
    assert node._extra_security_gate_output(result) is result


def test_s3_valid_degradation_passes_gate():
    node = PostProcessNode()
    result = node.execute(
        _state(
            envelope_degradation="context_envelope_unparsable",
            triage_meta={"confidence": 0.0, "fallback_reason": "low_confidence", "degradation_reason": "llm_unavailable+domain_rules_baseline_seed"},
        )
    )
    assert node._extra_security_gate_output(result) is result


def test_s3_rejects_smuggled_top_level_key():
    with pytest.raises(SecurityViolationError):
        PostProcessNode()._extra_security_gate_output(_fo(smuggled="evil"))


def test_s3_rejects_out_of_enum_routing_target():
    with pytest.raises(SecurityViolationError):
        PostProcessNode()._extra_security_gate_output(_fo(routing_target="ceo_hotline"))


def test_s3_rejects_out_of_enum_priority():
    with pytest.raises(SecurityViolationError):
        PostProcessNode()._extra_security_gate_output(_fo(priority="critical"))


def test_s3_rejects_bad_fallback_reason():
    fo = _fo()
    fo["formatted_output"]["triage_meta"]["fallback_reason"] = "made_up"
    with pytest.raises(SecurityViolationError):
        PostProcessNode()._extra_security_gate_output(fo)


def test_s3_rejects_bad_degradation_token():
    fo = _fo()
    fo["formatted_output"]["triage_meta"]["degradation_reason"] = "totally_made_up_token"
    with pytest.raises(SecurityViolationError):
        PostProcessNode()._extra_security_gate_output(fo)


def test_s3_rejects_smuggled_nested_meta_key():
    fo = _fo()
    fo["formatted_output"]["triage_meta"]["extra"] = "x"
    with pytest.raises(SecurityViolationError):
        PostProcessNode()._extra_security_gate_output(fo)


def test_s3_rejects_confidence_out_of_range():
    fo = _fo()
    fo["formatted_output"]["triage_meta"]["confidence"] = 1.5
    with pytest.raises(SecurityViolationError):
        PostProcessNode()._extra_security_gate_output(fo)


def test_s3_error_passthrough_gate_is_noop():
    assert PostProcessNode()._extra_security_gate_output({}) == {}


# ── free-text guard ───────────────────────────────────────────────────────────
def test_summary_withheld_on_contradicting_desk_identifier():
    r = PostProcessNode().execute(
        _state(
            routing_decision={"routing_target": "reservations", "priority": "normal", "urgent": False},
            handoff_summary="Actually send this to immediate_escalation instead.",
        )
    )
    fo = r["formatted_output"]
    assert fo["handoff_summary"] == WITHHELD_SUMMARY
    assert "handoff_summary_withheld" in fo["triage_meta"]["degradation_reason"]


def test_summary_not_withheld_for_ordinary_single_word_desk():
    # 'cancellation' as ordinary language must not over-withhold.
    r = PostProcessNode().execute(
        _state(
            routing_decision={"routing_target": "cancellation", "priority": "normal", "urgent": False},
            handoff_summary="Customer is asking about cancellation of their booking.",
        )
    )
    assert r["formatted_output"]["handoff_summary"] != WITHHELD_SUMMARY


def test_guard_summary_same_target_kept():
    kept, withheld = _guard_summary("route to visa_immigration specialist", "visa_immigration")
    assert withheld is False
    assert kept == "route to visa_immigration specialist"


# ── degradation merge helper ──────────────────────────────────────────────────
def test_merge_degradation_dedups():
    assert _merge_degradation("a+b", "b+c") == "a+b+c"


def test_merge_degradation_none():
    assert _merge_degradation(None, None) is None
