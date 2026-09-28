"""Unit tests — PreProcessNode (envelope parse + Executable Contract, docs §3)."""

import json

from framework.schemas.trust_level import TrustLevel

from src.nodes.pre_process_node import PreProcessNode


def _node(max_chars: int = 8000) -> PreProcessNode:
    return PreProcessNode(max_input_chars=max_chars)


def _envelope(text: str, context=None, reference_datetime=None) -> str:
    return json.dumps(
        {"text": text, "context": context or {}, "reference_datetime": reference_datetime}
    )


def test_plain_envelope_success():
    result = _node().execute(
        {"user_input": _envelope("予約を変更したい", reference_datetime="2026-08-04T00:00:00+00:00")}
    )
    assert result["status"] == "success"
    assert result["validated_input"] == "予約を変更したい"
    assert result["booking_context"] == {}
    assert result["history_context"] == []
    assert result["reference_datetime"] == "2026-08-04T00:00:00+00:00"
    assert result["envelope_degradation"] is None


def test_empty_text_is_error():
    result = _node().execute({"user_input": _envelope("   ")})
    assert result["status"] == "error"
    assert result["error_log"]


def test_unparsable_envelope_degrades_not_errors():
    # raw invoke bypassing the adapter → treated as text, degraded, ref null.
    result = _node().execute({"user_input": "just raw text, not json"})
    assert result["status"] == "success"
    assert result["validated_input"] == "just raw text, not json"
    assert result["envelope_degradation"] == "context_envelope_unparsable"
    assert result["reference_datetime"] is None


def test_booking_allowlist_extraction_drops_unknown_keys():
    env = _envelope(
        "x",
        context={
            "booking": {
                "booking_id": "BK1",
                "departure_date": "2026-09-01",
                "status": "confirmed",
                "evil_key": "drop me",
            }
        },
    )
    result = _node().execute({"user_input": env})
    assert result["booking_context"] == {
        "booking_id": "BK1",
        "departure_date": "2026-09-01",
        "status": "confirmed",
    }


def test_booking_invalid_date_rejected_and_safe():
    env = _envelope("x", context={"booking": {"departure_date": "not-a-date"}})
    result = _node().execute({"user_input": env})
    assert "departure_date" not in result["booking_context"]
    assert any(r["path"] == "context.booking.departure_date" for r in result["rejected_context"])


def test_booking_invalid_status_rejected():
    env = _envelope("x", context={"booking": {"status": "voided"}})
    result = _node().execute({"user_input": env})
    assert "status" not in result["booking_context"]
    assert any(r["reason"] == "invalid_enum" for r in result["rejected_context"])


def test_no_booking_is_empty_dict():
    result = _node().execute({"user_input": _envelope("x", context={})})
    assert result["booking_context"] == {}


def test_history_non_list_rejected():
    env = _envelope("x", context={"history": "oops"})
    result = _node().execute({"user_input": env})
    assert result["history_context"] == []
    assert any(r["path"] == "context.history" for r in result["rejected_context"])


def test_history_keeps_valid_drops_invalid_elements():
    env = _envelope(
        "x",
        context={
            "history": [
                {"role": "customer", "text": "hi", "extra": "drop"},
                {"role": "bot", "text": "bad role"},
                "not-a-dict",
                {"role": "staff", "text": 42},
            ]
        },
    )
    result = _node().execute({"user_input": env})
    assert result["history_context"] == [{"role": "customer", "text": "hi"}]
    assert len(result["rejected_context"]) == 3


def test_context_type_mismatch_rejected():
    env = _envelope("x", context="not a dict")
    result = _node().execute({"user_input": env})
    assert any(r["path"] == "context" for r in result["rejected_context"])


def test_validated_input_truncated_to_ceiling():
    result = _node(max_chars=5).execute({"user_input": _envelope("abcdefghij")})
    assert result["validated_input"] == "abcde"


def test_raw_dos_ceiling_rejects_via_call():
    # S-2 gate runs in __call__; raw envelope over 4x ceiling → error before execute.
    node = _node(max_chars=5)  # raw ceiling = 20
    huge = _envelope("x" * 200)
    state = {
        "user_input": huge,
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "correlation_id": "t",
    }
    result = node(state)
    assert result["status"] == "error"


def test_tc08_trust_gate_blocks_anonymous():
    node = _node()
    state = {
        "user_input": _envelope("x"),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "t",
    }
    result = node(state)  # via __call__ — S-1 gate
    assert result["status"] == "error"
    assert "trust" in " ".join(result.get("error_log", [])).lower()
