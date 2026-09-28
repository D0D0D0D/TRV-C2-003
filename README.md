# TRV-C2-003 — Travel Inquiry Triage & Routing Agent

> **Category**: Cat 2 (domain workflow)
> **Industry**: TRV

## Overview

This template triages inbound travel enquiries from email or web forms for internal operations teams.
It extracts intent, urgency, sentiment, and available booking context, then applies deterministic
travel-domain rules to select a routing target and priority. It produces a concise handoff summary so
staff can continue the case with the relevant context. It does not reply to travellers, determine visa
eligibility, or replace staff review of routing decisions.

It suits a travel operations desk with an inbox: enquiries arrive as free text, someone has to
decide which queue each belongs in and how urgent it is, and that decision is repetitive but not
mechanical. The agent reads the message, extracts what it can, and hands the case on with the
context already assembled.

The split between the model and the rules is the whole design, because the routing decision is
the part that must not be invented:

- **The model reads; the rules decide.** Intent, sentiment and urgency signals are extracted from
  free text by the model. The routing target, the priority and the cancellation-fee band are then
  computed by deterministic rules from those observations. The model never chooses a queue.
- **Both are closed enumerations.** Routing target and priority are validated against fixed sets
  before the response leaves. An intent the model returns that is not in the known set — including
  a fabricated one — falls to manual triage rather than being routed somewhere plausible.
- **`handoff_summary` is the only free-text field the model writes**, and it is guarded
  accordingly. It is replaced by a fixed withheld notice, with `handoff_summary_withheld`
  recorded, if a credential pattern appears in it, or if it names a *different* routing desk
  than the one that was computed. A personal-information scan also runs over it, for the audit
  trail only — it does not withhold, because customer identifiers are what makes an internal
  handoff useful. The routing decision is unaffected either way: it was never derived from the
  prose.
- **The reference time is generated server-side, once per request.** A client-supplied
  `reference_datetime` is ignored, and inner nodes never call `datetime.now()`. The day-count that
  drives the cancellation-fee band is therefore the same regardless of host timezone or where in
  the run it is evaluated.
- **Visa questions are never answered.** The agent holds a complexity matrix and no external
  lookup at all. Any missing observation is treated as complex and routed to the immigration desk.
  Eligibility is a human specialist's call, and the template has no fetch path that could go
  stale or be redirected.
- **Missing input degrades a rule, not the run.** A rule with no data to work on is skipped and
  recorded; the reason for any reduced result is folded into `degradation_reason`.

The agent does not reply to travellers, determine visa eligibility, quote a binding cancellation
fee, or replace staff review of the routing decision.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- a `BaseLLM` implementation as `config["llm"]`. None is bundled and no model is named. Without
  one the agent still answers: the deterministic rules run on whatever context the request
  supplies, and the result carries `llm_unavailable` in `degradation_reason`;
- your own tariff and routing table, as an operator rule file — see below;
- caller authentication in front of the entry adapter.

Runtime parameters read from `config/config.yaml`:

| Key | Meaning |
|---|---|
| `domain_rules.operator_kb_path` (`null`) | Your own rule file. When unset, the bundled `config/domain_rules.baseline.yaml` is used |
| `domain_rules.baseline_seed_path` | The bundled fallback rule file |
| `operator_timezone` (`Asia/Tokyo`) | Timezone the reference instant is converted into before the local date is taken |
| `thresholds.low_confidence` (`0.55`) | Below this the result is marked low-confidence |
| `thresholds.medium_sentiment_severity` (`0.5`), `high_sentiment_severity` (`0.75`) | Sentiment severity cut points |
| `thresholds.max_input_chars` (`8000`) | Input ceiling |
| `hitl.enabled` (`false`), `max_retry` (`3`), `timeout_seconds` (`30`) | No human-in-the-loop; framework retry budget and timeout |

The rule file (`config/domain_rules.baseline.yaml`) is data, not code: the intent-to-queue routing
table, the cancellation-fee day bands (stated as min-inclusive / max-exclusive so a boundary day is
unambiguous), and the visa complexity matrix. The bundled values are generic standard
travel-agency terms — swap them for your own without touching the implementation.

**A broken rule file fails at start-up, not at request time.** An operator file that is present but
unparseable, or the wrong shape, stops the server; it never falls through silently to the bundled
baseline. Only an absent or unreadable file degrades to the baseline. The same applies to
`config.yaml`: absent is fine (code defaults apply), present-but-malformed stops start-up rather
than leaving stale defaults quietly in force.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is the agent's import-time dependency: `src/` imports `framework.*` and `shared.*`
directly, so with no SDK installed the process does not start at all. That is what "does not run
standalone" means here — there is no degraded mode that runs without the framework.

With the SDK installed but no platform around it, the standalone server in `src/api/server.py`
does come up, and — unusually for a template of this kind — it is genuinely usable:

- `/health` answers `{"status": "ok", "agent": "trv-c2-003"}`.
- It reads `config/config.yaml` **relative to the working directory**, so start it from the
  repository root.
- It injects no `config["llm"]`, so the model-driven extraction is unavailable. The deterministic
  rules still run against whatever `context` the request supplies, and every response says so
  through `degradation_reason: llm_unavailable`. This is a real fallback, not an error path.
- `INVOKE_AUTH_TOKEN`, when set on the server environment, promotes an anonymous caller to
  `VERIFIED_EXTERNAL` on a matching `Authorization: Bearer` header, and rejects a wrong token with
  **401**. This is the one value read directly from the process environment, because it
  authenticates the caller before any invocation context exists. When it is not set, the caller
  stays `ANONYMOUS` and is refused by the node trust gate instead.

The test suite runs without a platform connection.

## Input

`POST /invoke` takes a JSON body:

| Field | Meaning |
|---|---|
| `input` | The enquiry text, up to `thresholds.max_input_chars` |
| `session_id` | Optional; generated when absent |
| `context` | Optional structured context: `{"history": [...], "booking": {...}}` |

Unknown keys inside `context` are dropped and a value of the wrong type is rejected, rather than
being coerced — the accepted shape is an allowlist. **`reference_datetime` is deliberately not
accepted from the caller**: the server generates it, so the day-count driving the cancellation-fee
band cannot be steered from outside.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "input": "We need to cancel our trip departing next week — what will it cost?",
    "session_id": "triage-001",
    "context": {"booking": {"departure_date": "2026-09-12"}}
  }'
```

## Output

The standard AgentCore envelope is preserved; `output` carries the triage envelope:

```json
{
  "status": "success",
  "output": {
    "routing_target": "cancellation",
    "priority": "high",
    "urgent": false,
    "handoff_summary": "Traveller asks about the cost of cancelling a departure in 7 days.",
    "triage_meta": {
      "confidence": 0.82,
      "fallback_reason": null,
      "degradation_reason": null
    }
  }
}
```

`routing_target` and `priority` come from the closed enumerations. When the free-text guard
withholds the summary, `handoff_summary` carries a fixed notice
(`[triage summary withheld — see routing_target]`) rather than being empty, and
`triage_meta.degradation_reason` says `handoff_summary_withheld`; the routing decision stands
regardless. `confidence` below `thresholds.low_confidence` is the signal to look at the case by
hand.

Values above are illustrative; real output depends on the enquiry and the configured rules.

## Security and Limitations

Every node requires verified-external trust, so an anonymous caller is stopped before any node
body runs. The agent makes **no outbound requests at all** — the visa matrix replaced what would
otherwise have been a live lookup, which removes that attack surface rather than defending it.
Personal information is detected in the one model-written field before it is returned, and the
whole output envelope is validated against a closed allowlist of keys and closed enumerations, so
an unexpected field cannot ride out on the response.

Known limits, recorded rather than hidden:

- **The contradiction check is a bounded mitigation, not a guarantee.** It only fires on the
  underscore-form identifier of a *different* desk appearing in the summary; single-word desk
  names are deliberately excluded because they collide with ordinary language and would withhold
  far too often. It does not verify that the summary is a faithful account of the enquiry, and no
  keyword lexicon is chased here — that path ends in an unwinnable game of adding words.
- **The cancellation-fee band is an indication.** It is computed from configured bands and the
  day-count; it is not a quote, and it does not account for fare rules, insurance, or supplier
  terms outside the configured tariff.
- **Visa complexity is not visa eligibility.** Any missing observation is treated as complex and
  routed onward. The agent has no authority on entry requirements and no way to check them.
- **Nothing is persisted.** No checkpointer is configured and no case history is retained;
  `context.history` is whatever the caller passes in the same request.
- **Rules are only as current as the file.** Tariffs and routing tables are operational data that
  someone has to maintain; the template has no mechanism to notice that they have gone stale.

Output is machine-generated and assistive. It does not substitute for staff review, and the
routing decision should be treated as a proposal.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), with the domain workflow nested in the
  `main` slot as a subgraph
- Generation mode: `llm` (extraction and the handoff summary; routing and banding are computed)
- HITL: not enabled
- Persistence: none — no checkpointer is configured
- External API clients: none, by design. The language-model client is deployment-injected

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not — see
*Behaviour without the platform* above.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the payload contract, the
determinism contract for the reference time, and the security design. `docs/03_test_spec.md`
records the test specification and the verification results.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
