"""AgentCore Platform v1.0 — TRV-C2-003 domain-rule KB loader.

Service layer: loads and validates the deterministic domain-rule KB (routing table,
cancellation-fee bands, emergency keywords, visa matrix, priority inputs). No business
routing logic lives here — nodes call this and apply the rules. No credentials.

Failure regime (docs/02_design.md §3 "KB 読込の失敗規則"):
  * operator_kb_path null / unreadable  -> degrade to bundled baseline seed (normal)
  * operator KB present but schema-invalid -> ConfigError (never silently ignored)
  * baseline seed missing / schema-invalid -> ConfigError (last fallback broken -> unstartable)
"""

from __future__ import annotations

from pathlib import Path


import yaml

from framework.errors import ConfigError

from src.schemas.state import CANCEL_BANDS, INTENTS, PRIORITIES, ROUTING_TARGETS

_DEFAULT_BASELINE_PATH = "config/domain_rules.baseline.yaml"


def load_domain_rules(config: dict) -> tuple[dict, bool]:
    """Return ``(rules, degraded)``.

    ``degraded`` is True when the bundled baseline seed is used instead of an
    operator-supplied KB (surfaced downstream as
    ``degradation_reason=domain_rules_baseline_seed``).
    """
    dr = config.get("domain_rules") or {}
    operator_path = dr.get("operator_kb_path")
    baseline_path = dr.get("baseline_seed_path", _DEFAULT_BASELINE_PATH)

    if operator_path:
        operator_rules = _parse_operator_kb(operator_path)
        if operator_rules is not None:
            # Operator KB present + readable: a schema error must fail closed (never
            # fall through to the baseline — that would silently ignore a misconfigured
            # KB). A present-but-broken KB (unparseable / non-mapping) already raised
            # ConfigError inside _parse_operator_kb.
            _validate_schema(operator_rules, source=f"operator KB ({operator_path})")
            return operator_rules, False
        # operator_kb_path declared but the file is absent / unreadable == "未読込"
        # -> degrade to baseline (normal operating mode, docs §3).

    return _load_baseline(baseline_path), True


def _load_baseline(baseline_path: str) -> dict:
    rules = _read_yaml(baseline_path)
    if rules is None:
        raise ConfigError(
            f"baseline domain-rule seed not found/unreadable: {baseline_path}"
        )
    # Last-resort fallback: if the seed itself is broken, refuse to start.
    _validate_schema(rules, source=f"baseline seed ({baseline_path})")
    return rules


def _read_yaml(path: str) -> dict | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _parse_operator_kb(path: str) -> dict | None:
    """Parse the operator-supplied KB with a fail-closed bias.

    Return the parsed mapping, or ``None`` when the file cannot be read at all
    (absent / OSError == "未読込" → the caller degrades to the baseline, docs §3).
    Raise ``ConfigError`` when the file IS readable but its content is broken
    (unparseable YAML, or a valid-YAML non-mapping document such as a bare list):
    a present-but-malformed operator KB must fail closed, never silently degrade to
    the baseline (which would hide the operator's misconfiguration).
    """
    p = Path(path)
    if not p.is_file():
        return None
    try:
        with p.open("r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None  # unreadable → treat as "未読込", degrade (docs §3)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"operator KB unparseable: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"operator KB is not a mapping (got {type(data).__name__}): {path}")
    return data


def _validate_schema(rules: dict, *, source: str) -> None:
    """Raise ConfigError if the KB does not satisfy the closed shape (docs §3/§4)."""

    def fail(msg: str) -> None:
        raise ConfigError(f"invalid domain-rule KB [{source}]: {msg}")

    # routing_table: must map every intent to a valid routing_target.
    table = rules.get("routing_table")
    if not isinstance(table, dict):
        fail("routing_table missing or not a mapping")
    missing = INTENTS - set(table)
    if missing:
        fail(f"routing_table missing intents: {sorted(missing)}")
    bad_targets = {v for v in table.values() if v not in ROUTING_TARGETS}
    if bad_targets:
        fail(f"routing_table has non-enum targets: {sorted(bad_targets)}")

    # cancellation_fee_bands: list of {band, min_days_inclusive?, max_days_exclusive?}.
    bands = rules.get("cancellation_fee_bands")
    if not isinstance(bands, list) or not bands:
        fail("cancellation_fee_bands missing or empty")
    for entry in bands:
        if not isinstance(entry, dict) or entry.get("band") not in CANCEL_BANDS:
            fail(f"cancellation_fee_bands has invalid band entry: {entry!r}")
        for key in ("min_days_inclusive", "max_days_exclusive"):
            val = entry.get(key)
            if val is not None and not isinstance(val, int):
                fail(f"cancellation_fee_bands.{key} must be int or null: {entry!r}")

    # emergency_keywords: NON-EMPTY list of strings. The R1 keyword layer is the
    # deterministic emergency backstop that doubles the LLM signal (docs §5); an empty
    # list would silently disable emergency detection when the LLM is unavailable, so
    # an empty list fails closed rather than shipping a triage with no safety net.
    kw = rules.get("emergency_keywords")
    if not isinstance(kw, list) or not kw or not all(isinstance(k, str) for k in kw):
        fail("emergency_keywords must be a non-empty list of strings")

    # visa: mapping with simple_purposes / simple_country_pairs as lists of STRINGS.
    # domain_rule builds a set() from each list, so a non-string (unhashable) element
    # would crash at request time — validate element types here so a malformed KB
    # fails closed at startup instead. Empty lists are valid (docs §4-2).
    visa = rules.get("visa")
    if not isinstance(visa, dict):
        fail("visa section missing or not a mapping")
    for key in ("simple_purposes", "simple_country_pairs"):
        val = visa.get(key, [])
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            fail(f"visa.{key} must be a list of strings")

    # priority: mapping with the derivation inputs used by routing_decision.
    priority = rules.get("priority")
    if not isinstance(priority, dict):
        fail("priority section missing or not a mapping")
    if priority.get("default") not in PRIORITIES:
        fail("priority.default must be a valid priority enum value")
