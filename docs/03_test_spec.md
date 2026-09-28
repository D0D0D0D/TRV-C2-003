# Test Specification — TRV-C2-003 Travel Inquiry Triage & Routing Agent

> Test-phase deliverable. Maps every mandatory TC/PB item to the test that covers it, plus
> the domain (business-logic) tests for the deterministic R1–R6 rules and the S-3 gate.
> Source of contract: `docs/02_design.md`.

## Test Strategy
- **Layers**: unit (per-node, pure/deterministic) + proof-of-boundary (framework contract) +
  a live-LLM smoke check (out-of-band, not in CI — see below).
- **Deterministic-first**: the routing DECISION is made by non-LLM rules (R1–R6), so the
  business logic is fully unit-testable with fixed inputs. The LLM only produces observations
  and narration; its absence collapses to a safe deterministic fallback.
- **Test types**: Unit / Proof-of-Boundary / (live-LLM smoke, manual).
- **Execution note (honest)**: the SDK-dependent suite (everything importing `framework.*` /
  `shared.*`) runs under `run-tests` in GitLab CI, which provisions the `agenticstar-agentcore==1.0.0`
  wheel. The two AST-only proof-of-boundary checks (PB-2/PB-5 state safety, PB-4 import isolation)
  were additionally verified locally with a bare interpreter (0 violations). The live-LLM smoke
  check requires an external CLI + an Azure key and is run out-of-band (see the PB-3 row).

## Framework Compliance Tests (Mandatory)

| TC-ID | Test | Covered by | Result |
|-------|------|-----------|--------|
| TC-01 | State contract: flat TypedDict, no Pydantic/dataclass | `src/schemas/state.py` (`State`/`InnerTriageState` inherit `AgentState`, primitives only); `test_state_safety.py` | PASS (AST, local) |
| TC-02 | `SecurityViolationError` fires on invalid output | `test_post_process_node.py` (closed-allowlist / enum / degradation-token violations raise) | Covered — CI |
| TC-03 | No JWT/Credential in State or `src/` | CI credential scan; secrets via `ctx.secrets` only, `requires.secrets: []` | Covered — CI gate |
| TC-04 | InvocationContext via `config["configurable"]` only | No node constructs `InvocationContext(...)`; adapter builds it in `server.py`; `test_state_safety.py` bans the annotation | PASS (AST, local) |
| TC-05 | S-4: no duplicate lifecycle events in `execute()` | No node emits `node_start/complete/error`; only domain events — verified by review across all 8 nodes | Covered — review |
| TC-06 | S-2 `_security_gate_input()` not overridable | `test_framework_compliance_tc06_tc07.py::test_tc06_...` | Covered — CI |
| TC-07 | S-3 `_security_gate_output()` not overridable | `test_framework_compliance_tc06_tc07.py::test_tc07_...` | Covered — CI |
| TC-08 | `required_trust_level` enforced | `test_pb_invoke_order.py` calls each node via `__call__` with matching trust; all nodes declare `VERIFIED_EXTERNAL` | Covered — CI |
| TC-09 | S-2 `_extra_security_gate_input()` non-trivial | `pre_process_node.py` raw-size DoS ceiling + audit-only domain PII scan; `test_pre_process_node.py` | Covered — CI |
| TC-10 | S-3 `_extra_security_gate_output()` non-trivial | `post_process_node.py` closed-allowlist + enum + degradation-token + free-text guard; `test_post_process_node.py` | Covered — CI |
| TC-11 | S-4: ≥1 domain `emit_trace_event()` per `execute()` path | every node emits domain events incl. reject/degrade/passthrough branches (docs §6-5) | Covered — review |

## Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Covered by | Result |
|-------|----------|-----------|--------|
| PB-1 | BaseNode → AuditLogger | domain `emit_trace_event()` in every node (incl. reject/degrade); framework `node_start/complete` auto-emitted | Covered — CI |
| PB-2 | State serialization | `test_state_safety.py` (AST: primitives only, no Pydantic/dataclass) | PASS (AST, local) |
| PB-3 | L1 → External service | This agent has **no external live service** (R3 visa is a deterministic matrix; no fetch, docs §4-2). The only external dependency is the injected LLM. Exercised by the **live-LLM smoke check** (`work/scripts/mrb_live_llm_check.py`, out-of-band): the observation/narration prompts embed a concrete JSON schema + allowlists, and `_coerce_observation` is strict (a `{}` response → key mismatch → safe fallback vector, never a silent empty-valid object), so the MunicipalContractVariationEvidenceBriefAgent empty-prompt failure mode is structurally avoided. | N/A (no live svc) — LLM seam smoke out-of-band |
| PB-4 | Import isolation | `test_import_isolation.py` (AST: no `agenticstar` import) | PASS (AST, local) |
| PB-5 | Checkpoint safety | This agent does not persist sensitive content to State/checkpoint (HITL disabled, no runtime persistence, docs §6-4); `test_state_safety.py` confirms no credential fields | PASS (AST, local) |
| PB-6 | Invoke execution order | `test_pb_invoke_order.py` (S-1 → node_start → S-2 → execute → S-3 → node_complete for every node under `src/nodes/`) | Covered — CI |
| PB-7 | HITL interrupt propagation | **Auto-waived — non-HITL** (`config/config.yaml` sets `hitl.enabled: false`; `config/agent.yaml` has no `hitl.enabled: true`). Scaffold stub `test_pb7_hitl_interrupt_propagation.py` auto-skips. | Auto-waived — non-HITL |

> **Pre-review gate checklist:** PB-1–PB-6 mandatory (covered above). PB-7 auto-waived (non-HITL).

## Business Logic Tests (deterministic rules R1–R6 + gates)

| ID | Test | Covered by |
|----|------|-----------|
| BL-01 | R1 emergency: LLM signal OR deterministic keyword → `immediate_escalation` @ `urgent`; missed-by-LLM emergency still caught by keyword layer | `test_domain_rule_node.py` |
| BL-02 | R2 cancellation-fee band by day-count (tz-aware parse, boundary min-incl/max-excl); booking/reference missing → skip (null) | `test_domain_rule_node.py` |
| BL-03 | R3 visa complexity: any missing observation → `complex` → `visa_immigration` (safe side); never answers eligibility | `test_domain_rule_node.py` |
| BL-04 | R4/R6 sentiment band & low-confidence → `manual_triage` (no fabricated routing); conflict order R1>R6>R2/R3/R4/R5 | `test_domain_rule_node.py`, `test_routing_decision_node.py` |
| BL-05 | Routing table: unknown/`other` intent → `manual_triage`; defensive closed-enum fallback | `test_routing_decision_node.py` |
| BL-06 | LLM observation strict coercion: out-of-range/unknown-enum/missing-key collapses whole vector to safe fallback | `test_intent_context_node.py` |
| BL-07 | Handoff narration: LLM absent → deterministic template summary (never silent); prompt provides the FINAL decision as fixed fact | `test_handoff_summary_node.py` |
| BL-08 | S-3 output gate: closed allowlist + closed enums + degradation-token set; credential in summary → withheld | `test_post_process_node.py` |
| BL-09 | KB loader fail-closed: operator KB schema error / non-mapping / unparseable / empty emergency-keywords / non-string visa element → `ConfigError`; absent → degrade to baseline | `test_service.py` |

## Test Execution Summary
- Test files: 9 unit + 4 proof-of-boundary (PB-7 auto-skips).
- Local (bare interpreter, SDK-free): PB-2/PB-5 (state safety) + PB-4 (import isolation) = 0 violations.
- SDK-dependent suite: executed by GitLab CI `run-tests` (provisions the framework wheel).
- Live-LLM smoke (`mrb_live_llm_check.py`): run out-of-band (external CLI + Azure key); the empty-prompt
  failure mode is structurally precluded (concrete-schema prompt + strict coercion → safe fallback).
- PB-7: Auto-waived — non-HITL.

## 実 LLM 1 回転検証

モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している（`gitproject-dev-flow.md` §14-1-bis）。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-18 |
| 使用 LLM | Azure OpenAI の実モデルを `BaseLLM` 実装として注入 |
| 注入シーム | `config[llm]` |
| 実 LLM 呼び出し | 2 回 |
| 出力 | ['routing_target', 'priority', 'handoff_summary', 'triage_meta'] が非空。縮退理由なし |

本番の LLM は `AnthropicClient` 等で、これは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。CI には入れていない（外部 CLI と API 鍵が要るため）。
