"""Unit tests — IntentContextNode (LLM observation, prompt contract, strict fallback).

The prompt-contract test fixes what build_intent_prompt must contain so the real LLM
can produce structured output (a mock returning a finished JSON hides this — JAMemberInquiryIntakeAgent).
"""

import json

import pytest

from src.nodes.intent_context_node import (
    IntentContextNode,
    _coerce_observation,
    build_intent_prompt,
)
from src.schemas.state import INTENTS, PURPOSES

_VALID_OBS = {
    "intent": "cancellation",
    "urgency_signal": True,
    "sentiment_score": 0.8,
    "confidence": 0.9,
    "emergency_signal": False,
    "arrival_country": "US",
    "nationality": "JP",
    "purpose": "tourism",
}


class FakeLLM:
    """Returns exactly what a real LLM returns — a JSON string in {"content": ...}.
    Deliberately does NOT add framework-completed keys (ExamEvidenceSufficiencyReviewAgent)."""

    def __init__(self, content: str):
        self._content = content

    def complete(self, messages):
        return {"content": self._content}


class RaisingLLM:
    def complete(self, messages):
        raise RuntimeError("llm unavailable")


def _state(**kw):
    s = {"validated_input": "", "history_context": []}
    s.update(kw)
    return s


# ── prompt contract (mutation-proof: dropping schema/allowlists breaks these) ──
def test_prompt_contains_all_schema_keys():
    blob = " ".join(m["content"] for m in build_intent_prompt("cancel please", []))
    for key in _VALID_OBS:
        assert key in blob, f"schema key {key} missing from prompt"


def test_prompt_contains_allowlists():
    blob = " ".join(m["content"] for m in build_intent_prompt("x", []))
    for intent in INTENTS:
        assert intent in blob
    for purpose in PURPOSES:
        assert purpose in blob


def test_prompt_is_imperative_not_permissive():
    blob = " ".join(m["content"] for m in build_intent_prompt("x", []))
    assert "Set emergency_signal to true" in blob
    assert "OBSERVATIONS ONLY" in blob


def test_prompt_includes_history_when_present():
    blob = " ".join(
        m["content"] for m in build_intent_prompt("x", [{"role": "customer", "text": "earlier msg"}])
    )
    assert "earlier msg" in blob


# ── observation happy path ────────────────────────────────────────────────────
def test_llm_observation_success():
    node = IntentContextNode(llm=FakeLLM(json.dumps(_VALID_OBS)))
    result = node.execute(_state(validated_input="cancel my trip"))
    assert result["intent"] == "cancellation"
    assert result["confidence"] == 0.9
    assert result["arrival_country"] == "US"
    assert result["intent_degraded"] is False
    assert result["status"] == "success"


def test_code_fence_is_stripped():
    fenced = "```json\n" + json.dumps(_VALID_OBS) + "\n```"
    node = IntentContextNode(llm=FakeLLM(fenced))
    result = node.execute(_state(validated_input="x"))
    assert result["intent"] == "cancellation"
    assert result["intent_degraded"] is False


# ── fallback / degradation ────────────────────────────────────────────────────
def test_no_llm_falls_back_safe():
    result = IntentContextNode(llm=None).execute(_state(validated_input="hi"))
    assert result["intent"] == "other"
    assert result["confidence"] == 0.0
    assert result["arrival_country"] is None
    assert result["intent_degraded"] is True


def test_llm_error_falls_back():
    result = IntentContextNode(llm=RaisingLLM()).execute(_state(validated_input="hi"))
    assert result["intent"] == "other"
    assert result["intent_degraded"] is True


def test_schema_violation_collapses_whole_vector():
    bad = dict(_VALID_OBS, intent="not-an-intent")
    result = IntentContextNode(llm=FakeLLM(json.dumps(bad))).execute(_state(validated_input="x"))
    # no field-level rescue — the entire vector is safe fallback
    assert result["intent"] == "other"
    assert result["confidence"] == 0.0
    assert result["intent_degraded"] is True


# ── strict coercion ───────────────────────────────────────────────────────────
def test_coerce_rejects_out_of_range_score():
    with pytest.raises(ValueError):
        _coerce_observation(dict(_VALID_OBS, sentiment_score=1.5))


def test_coerce_rejects_extra_keys():
    with pytest.raises(ValueError):
        _coerce_observation(dict(_VALID_OBS, unexpected=1))


def test_coerce_rejects_bool_as_number():
    with pytest.raises(ValueError):
        _coerce_observation(dict(_VALID_OBS, confidence=True))


def test_coerce_normalises_country_case():
    obs = _coerce_observation(dict(_VALID_OBS, arrival_country="us", nationality="jp"))
    assert obs["arrival_country"] == "US"
    assert obs["nationality"] == "JP"


def test_coerce_rejects_bad_country():
    with pytest.raises(ValueError):
        _coerce_observation(dict(_VALID_OBS, arrival_country="USA"))
