"""Unit tests — domain-rule KB loader (fail-closed schema validation, InsuranceLargeLossReserveChangeEvidenceReviewAgent)."""

import copy

import pytest
import yaml

from framework.errors import ConfigError

from src.services.service import load_domain_rules

_VALID = {
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
        {"band": "none", "min_days_inclusive": 21, "max_days_exclusive": None},
        {"band": "high", "min_days_inclusive": 0, "max_days_exclusive": 2},
    ],
    "emergency_keywords": ["事故", "emergency"],
    "visa": {"simple_purposes": ["transit"], "simple_country_pairs": []},
    "priority": {"emergency": "urgent", "severe_sentiment": "high", "imminent_cancellation": "high", "default": "normal"},
}


def _write(tmp_path, data, name="kb.yaml"):
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data, allow_unicode=True))
    return str(p)


def _cfg(**dr):
    return {"domain_rules": dr}


def test_baseline_loads_and_flags_degraded(tmp_path):
    base = _write(tmp_path, _VALID)
    rules, degraded = load_domain_rules(_cfg(operator_kb_path=None, baseline_seed_path=base))
    assert degraded is True
    assert rules["routing_table"]["visa"] == "visa_immigration"


def test_operator_kb_valid_not_degraded(tmp_path):
    op = _write(tmp_path, _VALID, "op.yaml")
    rules, degraded = load_domain_rules(_cfg(operator_kb_path=op, baseline_seed_path="unused"))
    assert degraded is False
    assert rules


def test_operator_path_unreadable_degrades_to_baseline(tmp_path):
    base = _write(tmp_path, _VALID)
    rules, degraded = load_domain_rules(
        _cfg(operator_kb_path=str(tmp_path / "missing.yaml"), baseline_seed_path=base)
    )
    assert degraded is True


# ── fail-closed with provisioned control (InsuranceLargeLossReserveChangeEvidenceReviewAgent) ────────────────────────
def test_operator_kb_invalid_schema_fails_closed(tmp_path):
    bad = copy.deepcopy(_VALID)
    del bad["routing_table"]
    op = _write(tmp_path, bad, "bad.yaml")
    with pytest.raises(ConfigError):
        load_domain_rules(_cfg(operator_kb_path=op, baseline_seed_path=_write(tmp_path, _VALID, "b.yaml")))


def test_operator_kb_valid_control_succeeds(tmp_path):
    # control for the test above: same shape, valid KB → success (proves the failure
    # was the schema, not some unrelated error).
    op = _write(tmp_path, _VALID, "ok.yaml")
    _, degraded = load_domain_rules(_cfg(operator_kb_path=op))
    assert degraded is False


def test_routing_table_missing_intent_fails(tmp_path):
    bad = copy.deepcopy(_VALID)
    del bad["routing_table"]["visa"]
    with pytest.raises(ConfigError):
        load_domain_rules(_cfg(baseline_seed_path=_write(tmp_path, bad)))


def test_routing_table_bad_target_fails(tmp_path):
    bad = copy.deepcopy(_VALID)
    bad["routing_table"]["visa"] = "nonexistent_desk"
    with pytest.raises(ConfigError):
        load_domain_rules(_cfg(baseline_seed_path=_write(tmp_path, bad)))


def test_empty_bands_fails(tmp_path):
    bad = copy.deepcopy(_VALID)
    bad["cancellation_fee_bands"] = []
    with pytest.raises(ConfigError):
        load_domain_rules(_cfg(baseline_seed_path=_write(tmp_path, bad)))


def test_bad_band_member_fails(tmp_path):
    bad = copy.deepcopy(_VALID)
    bad["cancellation_fee_bands"] = [{"band": "nonsense"}]
    with pytest.raises(ConfigError):
        load_domain_rules(_cfg(baseline_seed_path=_write(tmp_path, bad)))


def test_baseline_missing_fails_closed(tmp_path):
    with pytest.raises(ConfigError):
        load_domain_rules(_cfg(operator_kb_path=None, baseline_seed_path=str(tmp_path / "nope.yaml")))


# ── present-but-broken operator KB must fail closed (not silently degrade) ───
def test_operator_kb_non_mapping_fails_closed(tmp_path):
    # A readable but non-mapping operator KB (bare list) is malformed, not "unreadable"
    # — it must fail closed rather than degrade to the baseline (would hide misconfig).
    op = tmp_path / "list.yaml"
    op.write_text("- a\n- b\n")
    with pytest.raises(ConfigError):
        load_domain_rules(
            _cfg(operator_kb_path=str(op), baseline_seed_path=_write(tmp_path, _VALID))
        )


def test_operator_kb_unparseable_fails_closed(tmp_path):
    op = tmp_path / "broken.yaml"
    op.write_text("key: [unterminated\n")
    with pytest.raises(ConfigError):
        load_domain_rules(
            _cfg(operator_kb_path=str(op), baseline_seed_path=_write(tmp_path, _VALID))
        )


def test_empty_emergency_keywords_fails_closed(tmp_path):
    # An empty keyword list would disable the R1 deterministic emergency backstop.
    bad = copy.deepcopy(_VALID)
    bad["emergency_keywords"] = []
    with pytest.raises(ConfigError):
        load_domain_rules(_cfg(baseline_seed_path=_write(tmp_path, bad)))


def test_visa_non_string_element_fails_closed(tmp_path):
    # Non-string (unhashable) visa element would crash set() at request time — reject
    # it at startup instead.
    bad = copy.deepcopy(_VALID)
    bad["visa"]["simple_purposes"] = [{}]
    with pytest.raises(ConfigError):
        load_domain_rules(_cfg(baseline_seed_path=_write(tmp_path, bad)))


# ── the shipped seed must actually load ──────────────────────────────────────
def test_bundled_baseline_seed_is_valid():
    rules, degraded = load_domain_rules(
        _cfg(operator_kb_path=None, baseline_seed_path="config/domain_rules.baseline.yaml")
    )
    assert degraded is True
    assert "routing_table" in rules
    assert rules["priority"]["default"] == "normal"
