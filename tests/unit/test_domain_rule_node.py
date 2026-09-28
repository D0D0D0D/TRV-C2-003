"""Unit tests — DomainRuleNode (deterministic R1–R6, R2 day-count, TZ/ISO handling)."""

from src.nodes.domain_rule_node import DomainRuleNode

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
    "cancellation_fee_bands": [
        {"band": "full", "min_days_inclusive": None, "max_days_exclusive": 0},
        {"band": "high", "min_days_inclusive": 0, "max_days_exclusive": 2},
        {"band": "medium", "min_days_inclusive": 2, "max_days_exclusive": 8},
        {"band": "low", "min_days_inclusive": 8, "max_days_exclusive": 21},
        {"band": "none", "min_days_inclusive": 21, "max_days_exclusive": None},
    ],
    "emergency_keywords": ["事故", "盗難", "emergency"],
    "visa": {"simple_purposes": ["transit"], "simple_country_pairs": []},
    "priority": {
        "emergency": "urgent",
        "severe_sentiment": "high",
        "imminent_cancellation": "high",
        "default": "normal",
    },
}
_THRESHOLDS = {
    "low_confidence": 0.55,
    "medium_sentiment_severity": 0.50,
    "high_sentiment_severity": 0.75,
}


def _node():
    return DomainRuleNode(rules=_RULES, thresholds=_THRESHOLDS, operator_timezone="Asia/Tokyo")


def _state(**kw):
    s = {
        "intent": "other",
        "validated_input": "",
        "booking_context": {},
        "reference_datetime": None,
        "emergency_signal": False,
        "sentiment_score": 0.0,
        "confidence": 0.9,
        "arrival_country": None,
        "nationality": None,
        "purpose": None,
    }
    s.update(kw)
    return s


def _findings(result):
    return result["rule_findings"]


# ── R1 emergency ──────────────────────────────────────────────────────────────
def test_r1_llm_signal():
    r = _node().execute(_state(emergency_signal=True))
    assert _findings(r)["r1_emergency"] is True
    assert r["escalation_required"] is True
    assert r["fallback_reason"] == "emergency_safeside"


def test_r1_keyword_layer_when_llm_missed_it():
    r = _node().execute(_state(emergency_signal=False, validated_input="パスポートが盗難に遭いました"))
    assert _findings(r)["r1_emergency"] is True


def test_r1_keyword_casefold_and_nfkc():
    r = _node().execute(_state(emergency_signal=False, validated_input="This is an EMERGENCY!!"))
    assert _findings(r)["r1_emergency"] is True


def test_r1_no_emergency():
    r = _node().execute(_state(emergency_signal=False, validated_input="just a normal question"))
    assert _findings(r)["r1_emergency"] is False


# ── R2 cancellation-fee band boundaries (reference 2026-08-04 JST) ────────────
# reference 2026-08-04T00:00:00+00:00 → JST 09:00 → local date 2026-08-04.
_REF = "2026-08-04T00:00:00+00:00"


def _band(departure):
    r = _node().execute(_state(booking_context={"departure_date": departure}, reference_datetime=_REF))
    return _findings(r)["r2_cancel_band"]


def test_r2_band_none_at_21_low_at_20():
    assert _band("2026-08-25") == "none"   # days = 21
    assert _band("2026-08-24") == "low"    # days = 20


def test_r2_band_low_medium_boundary():
    assert _band("2026-08-12") == "low"     # days = 8
    assert _band("2026-08-11") == "medium"  # days = 7


def test_r2_band_high_and_full_boundary():
    assert _band("2026-08-05") == "high"    # days = 1
    assert _band("2026-08-04") == "high"    # days = 0
    assert _band("2026-08-03") == "full"    # days = -1 (departed)


def test_r2_skips_when_reference_null():
    r = _node().execute(_state(booking_context={"departure_date": "2026-08-05"}, reference_datetime=None))
    assert _findings(r)["r2_cancel_band"] is None


def test_r2_skips_when_no_booking():
    r = _node().execute(_state(reference_datetime=_REF))
    assert _findings(r)["r2_cancel_band"] is None


def test_r2_same_instant_different_offset_same_band():
    # SupplyDataQualityPrecedentAgent: parse, do not string-compare. 15:00Z == 00:00+09:00 (next JST day).
    a = _node().execute(
        _state(booking_context={"departure_date": "2026-08-12"}, reference_datetime="2026-08-04T15:00:00+00:00")
    )
    b = _node().execute(
        _state(booking_context={"departure_date": "2026-08-12"}, reference_datetime="2026-08-05T00:00:00+09:00")
    )
    assert _findings(a)["r2_cancel_band"] == _findings(b)["r2_cancel_band"]


def test_r2_timezone_shifts_local_date():
    # 15:00Z → JST 2026-08-05, so departure 2026-08-12 is 7 days (medium), not 8 (low).
    r = _node().execute(
        _state(booking_context={"departure_date": "2026-08-12"}, reference_datetime="2026-08-04T15:00:00+00:00")
    )
    assert _findings(r)["r2_cancel_band"] == "medium"


# ── R3 visa complexity ────────────────────────────────────────────────────────
def test_r3_missing_field_is_complex():
    r = _node().execute(_state(intent="visa", arrival_country="US", nationality=None, purpose="tourism"))
    assert _findings(r)["r3_visa_complexity"] == "complex"


def test_r3_simple_purpose():
    r = _node().execute(_state(intent="visa", arrival_country="US", nationality="JP", purpose="transit"))
    assert _findings(r)["r3_visa_complexity"] == "simple"


def test_r3_non_simple_purpose_is_complex():
    r = _node().execute(_state(intent="visa", arrival_country="US", nationality="JP", purpose="tourism"))
    assert _findings(r)["r3_visa_complexity"] == "complex"


def test_r3_none_when_not_visa():
    r = _node().execute(_state(intent="reservation"))
    assert _findings(r)["r3_visa_complexity"] is None


# ── R4 sentiment bands ────────────────────────────────────────────────────────
def test_r4_severe_elevated_normal():
    assert _findings(_node().execute(_state(sentiment_score=0.8)))["r4_sentiment_band"] == "severe"
    assert _findings(_node().execute(_state(sentiment_score=0.6)))["r4_sentiment_band"] == "elevated"
    assert _findings(_node().execute(_state(sentiment_score=0.3)))["r4_sentiment_band"] == "normal"


# ── R5 booking consistency ────────────────────────────────────────────────────
def test_r5_cancelled_plus_cancellation_is_inconsistent():
    r = _node().execute(_state(intent="cancellation", booking_context={"status": "cancelled"}))
    assert _findings(r)["r5_booking_consistent"] is False
    assert r["fallback_reason"] == "booking_mismatch"


def test_r5_consistent_when_confirmed():
    r = _node().execute(_state(intent="cancellation", booking_context={"status": "confirmed"}))
    assert _findings(r)["r5_booking_consistent"] is True


def test_r5_none_without_booking():
    r = _node().execute(_state(intent="cancellation"))
    assert _findings(r)["r5_booking_consistent"] is None


# ── R6 low confidence ─────────────────────────────────────────────────────────
def test_r6_low_confidence_escalates():
    r = _node().execute(_state(confidence=0.4))
    assert _findings(r)["r6_low_confidence"] is True
    assert r["escalation_required"] is True
    assert r["fallback_reason"] == "low_confidence"


def test_r6_high_confidence_ok():
    r = _node().execute(_state(confidence=0.9))
    assert _findings(r)["r6_low_confidence"] is False


# ── conflict order R1 > R6 ────────────────────────────────────────────────────
def test_conflict_emergency_beats_low_confidence():
    r = _node().execute(_state(emergency_signal=True, confidence=0.1))
    assert r["fallback_reason"] == "emergency_safeside"


def test_unknown_intent_reason():
    r = _node().execute(_state(intent="other", confidence=0.9))
    assert r["fallback_reason"] == "unknown_intent"
