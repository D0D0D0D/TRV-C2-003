# Template Design Specification — TRV-C2-003 Travel Inquiry Triage & Routing Agent

> 本設計書は実装が一意に従える粒度で書く。
> 本文（散文）は日本語、技術 ID・クラス名・config キー・YAML・enum 値・state キーは英語のまま。

## 0. 設計と実装の境界（scope of this design）

本設計書は **構造・契約・enum・shape・失敗規則** を確定する。
**具体的なデータ値（R1〜R6 の閾値・routing_table・取消料バンド）は DATA であり実装フェーズで
`config/config.yaml` と `config/domain_rules.baseline.yaml` に確定する**（100% を設計書に列挙する
過剰仕様は狙わない — 「機械保証の追い過ぎ」を避ける）。設計フェーズの差分は原則
`docs/` `config/` `README.md` `pyproject.toml`。**例外として `src/graph/graph.py` の outer class を
identity 改称（`Graph`→`TravelInquiryTriageAgent`・後方互換エイリアス付き・振る舞いは 1 行も足さない）まで
実施**し、`config/agent.yaml` の manifest エントリポイント（`class: TravelInquiryTriageAgent`）を merged branch 上で
実在させる（設計レビュー **H-1** 対応 — さもなくば lazy-load import がマージ直後に失敗する）。
ロジック実装（inner 5 ノード + outer 3 スロット）と `tests/` は変更しない。

**実装フェーズが担う範囲（設計フェーズの対象外）**: ①inner 5 ノード + outer 3 スロットの実装・`src/api/server.py` の
namespace / agent_name 実値化（outer class の identity 改称は設計フェーズで実施済み・後方互換 `Graph` エイリアスにより
server.py の `Graph` import は当面継続動作） ②`config/domain_rules.baseline.yaml` の作成
③`src/api/server.py` adapter の envelope 対応 ④`docs/03_test_spec.md`（テストフェーズ成果物）の TC/PB
⑤`src/graph/graph.py` `name` プロパティのエージェント名プレースホルダを実値化。

## 1. Position in AgentCore Architecture

- **Agent Class**: `TravelInquiryTriageAgent`（outer graph・`src/graph/graph.py`。設計フェーズで雛形の `Graph` から identity 改称済み・後方互換 `Graph` エイリアス維持。inner graph 差し込み等のロジック実装は実装フェーズ）
- **L1 Base**: `AgentBaseGraph`（L1 直継承。`AutonomousBaseGraph` ではない — 自律ループなし）
- **Three-Layer Separation**:
  - State: flat TypedDict（primitives + JSON-serializable のみ。Pydantic/dataclass 禁止＝msgpack 非互換）
  - Node: L1 継承（Template Method: `execute(self, state) -> dict` のみ override・変更キーのみ返却）
  - Graph: composition（`register_nodes()` で `main` スロットに GraphNode を差し込む）

> **継承関係の明確化（設計チェックリスト #4）**: 継承先は **`AgentBaseGraph` (L1 direct) のみ**。
> `GraphNode`・inner graph は「継承」ではなく「構成（composition）」であり、下記 §2 で扱う。

## 2. Architecture Overview

Cat 2 標準の二層構成。**outer graph** は固定 5 ノード backbone（framework 注入・`add_edges()` override 禁止）。
ドメインの多段処理は `main` スロットの `TravelTriageGraphNode`（`GraphNode` サブクラス）が内包する
**inner graph** に隠蔽する。

```
Outer backbone (fixed):
  START → initialize → pre_process → main → {route} → post_process → finalize → END
                                          ↓ (RETRY, max 3)
                                       pre_process

Inner graph (src/graph/domain_workflow_graph.py, BaseGraph 直継承):
  START → context_seed → intent_context → domain_rule → routing_decision → handoff_summary → END
          [決定論]        [LLM]            [決定論]        [決定論]           [LLM+narration]
```

**判定エンジンの定石（プロジェクト整合・Financial Document Classification & Routing Agent `resolve_routing()` 系）**:
- **ルーティング・優先度・緊急区分の「決定」はすべて決定論ルール**（表引き）＝reproducible & auditable。
- **LLM は「観測（observation）」だけを担う** — 自然文から意図・感情・緊急シグナルを構造化フィールドとして
  抽出し、ハンドオフを多言語で文面化する。**LLM は routing_target を選ばない**（後述 §5 参照）。
- **緊急・低信頼度は決定論で必ず安全側フォールバック**（`immediate_escalation` / `manual_triage`）。

### 2-1. adapter ↔ agent ↔ inner のペイロード契約（敵対レビュー Critical #1 対応）

SDK 1.0.0 の `AgentBaseGraph.invoke(user_input: str, ctx=...)` は **単一文字列**を受ける。
構造化コンテキスト（予約状況・会話履歴・基準日時）は次の envelope 経路で運ぶ:

1. **`/invoke` request body**: `{input: str, session_id?: str, context?: dict, reference_datetime?: str}`。
2. **adapter（`src/api/server.py`・実装フェーズで実装）**: サーバ側で JSON envelope を組み立て、単一 `input` 文字列として
   `agent.invoke()` に渡す:
   `{"text": <input>, "context": <context or {}>, "reference_datetime": <server 生成の TZ 付き UTC ISO-8601>}`。
   **`reference_datetime` はサーバが生成する。クライアント供給値は無視する**（決定論性・後述 §4-1）。
3. **outer `pre_process`（`FunctionNode`）**: `state["user_input"]`（＝envelope 文字列）を JSON parse し、
   §3 の Executable Contract を適用して `validated_input`・`booking_context`・`history_context`・
   `reference_datetime`・`rejected_context` を outer State に確定する。**parse 失敗時**（raw invoke 直叩き等で
   envelope が JSON でない）は文字列全体を `text` とみなし `context={}`・`reference_datetime=null`（→R2/R5 は
   §4-1 で決定論 skip）の決定論 fallback（`status="success"`＋`degradation_reason="context_envelope_unparsable"`。
   pre_process は `now()` で補完しない）。
4. **`TravelTriageGraphNode.extract_input()`**: 検証済みフィールドを JSON 直列化した文字列を返す
   （`{"validated_input":..,"booking_context":..,"history_context":..,"reference_datetime":..}`）。
   KB（`domain_rules`/`routing_table`）は入力文字列でなく `_parent_config()` 経由で inner に渡す。
5. **inner `context_seed`（決定論）**: extract_input が渡した JSON を deserialize し inner State に載せる
   （再検証は不要 — 検証は outer pre_process 済み）。
6. **`merge_output()`**: inner の `get_output()` が返す `routing_decision`/`handoff_summary`/`triage_meta`/`status`
   だけを outer State に写す（変更キーのみ・None 埋め禁止・node_history は返さない）。

### 2-2. Node Configuration

**Outer graph（5 ノード backbone）**

| Node | 種別 | Responsibility | 主要 Input | 主要 Output |
|------|------|---------------|-----------|------------|
| initialize | default | framework 既定（S-4 node_start） | — | — |
| pre_process | `FunctionNode` | envelope parse＋Executable Contract 適用＋既定値注入 | `user_input` | `validated_input`, `booking_context`, `history_context`, `reference_datetime`, `rejected_context`, `status` |
| main | `GraphNode` | inner graph を呼ぶ（extract_input→invoke→merge_output） | 上記 | `routing_decision`, `handoff_summary`, `triage_meta`, `status` |
| post_process | `FunctionNode` | `formatted_output` envelope 組立＋degradation 畳み込み＋S-3 出力ゲート | main 出力 | `formatted_output`, `status` |
| finalize | default | framework 既定（S-4 node_complete） | — | — |

**Inner graph（domain workflow・5 ノード linear・`BaseGraph` 直継承）**

| Node | 種別 | Responsibility | 主要 Input | 主要 Output（inner State delta） |
|------|------|---------------|-----------|------------|
| `context_seed` | 決定論 | extract_input の JSON を inner State へ deserialize | (envelope str) | `validated_input`, `booking_context`, `history_context`, `reference_datetime` |
| `intent_context` | LLM | 意図/緊急度/感情/予約整合の**観測**を構造化して返す（判断はしない） | 上記 | `intent`, `urgency_signal`(bool), `sentiment_score`(float 0–1), `confidence`(float 0–1), `emergency_signal`(bool) |
| `domain_rule` | 決定論 | R1〜R6 を適用（緊急/取消料区分/ビザ複雑度/感情強度/予約整合/低信頼度） | `intent`ほか, `booking_context`, `reference_datetime`, `domain_rules`(KB) | `rule_findings`(dict), `escalation_required`(bool), `fallback_reason`(enum\|None) |
| `routing_decision` | 決定論 | `routing_table` 表引きで宛先＋優先度決定。未知/低信頼度→`manual_triage`、緊急→最優先固定 | `rule_findings`, `escalation_required`, `routing_table`(KB) | `routing_decision`(dict: `{routing_target, priority, urgent}`) |
| `handoff_summary` | LLM+narration | 決定論の `routing_decision` を**書き換えず**状況サマリを多言語文面化 | `validated_input`, `rule_findings`, `routing_decision` | `handoff_summary`(str), `triage_meta`(dict) |

> **LLM 必須性**: `intent_context`（自然文の意図・感情・緊急シグナル抽出）と `handoff_summary`
> （多言語自然文生成）は決定論では代替困難＝LLM が要る。**判断中枢（`domain_rule`/`routing_decision`）は
> 非 LLM で単体テスト可能**＝再現性・監査性を担保。

### 2-3. Data Flow（inner の分岐・型注釈・error）

- inner は基本 linear。`routing_decision` の後、`escalation_required==True`（R1 緊急 / R6 低信頼度）でも
  `handoff_summary` へ進む（サマリは常に生成、宛先だけ `immediate_escalation`/`manual_triage` に倒す）。
- inner の `route()` / `get_output()` は **inner 自身の State schema `InnerTriageState` で型注釈**する
  （`AgentState` 注釈だと射影でカスタム欄が消える — Business Travel Hotel & Plan Recommendation Agent 実害）。`route()` シグネチャ =
  `def route(self, state: InnerTriageState) -> str`、`get_output(self, state: InnerTriageState) -> dict`。
- inner でハード error（LLM 例外・KB 読込不能など）→ inner が `status="error"` を立て、
  `get_output()` は `{"status":"error", ...}` を返す。outer `main`/`post_process` は
  **error を passthrough**（handoff は生成しない）。degradation（LLM 不在の縮退）は error ではなく
  `status="success"`＋`degradation_reason` に倒す（両者を明確に区別）。

### 2-4. State Definition（`src/schemas/state.py` — `AgentState` 継承 flat TypedDict）

authoritative shape は 1 つ: 判断は `routing_decision: dict` に集約し、post_process だけが
`formatted_output` へ flatten する（敵対レビュー Critical #8 の節間不整合を解消）。

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `user_input` | `str` | envelope 文字列（framework 標準・S-2 PII スキャン対象） | Yes |
| `validated_input` | `str` | 検証済み問い合わせ本文 | 派生 |
| `booking_context` | `dict` | 検証済み予約状況（allowlist キーのみ・欠落時 `{}`） | 派生 |
| `history_context` | `list[dict]` | 検証済み会話履歴（欠落時 `[]`） | 派生 |
| `reference_datetime` | `str\|null` | 基準日時（ISO-8601 UTC・TZ 付き。adapter が確定。raw invoke 経路では null） | 派生 |
| `rejected_context` | `list[dict]` | `{path: str, reason: enum}`（drop/reject した入力の監査） | 派生 |
| `routing_decision` | `dict` | `{routing_target: enum, priority: enum, urgent: bool}`（決定論・authoritative） | 派生 |
| `handoff_summary` | `str` | LLM 生成サマリ（narration・非 authoritative） | 派生 |
| `triage_meta` | `dict` | `{confidence: float, fallback_reason: enum\|null, degradation_reason: str\|null}` | 派生 |
| `formatted_output` | `dict` | 最終出力 envelope（post_process 組立・S-3 対象） | 派生 |
| `status` | `str` | `AgentStatus` の値（小文字 "success"/"error"/"retry"） | Yes |

**enum 定義（敵対レビュー Critical #5/#7）**:
- `routing_target`（closed enum）: `reservations` / `cancellation` / `rebooking` / `complaints` /
  `visa_immigration` / `emergency_desk` / `immediate_escalation` / `manual_triage`
- `priority`（closed enum）: `urgent` / `high` / `normal` / `low`
- `intent`（closed enum）: `reservation` / `cancellation` / `rebooking` / `complaint` / `visa` /
  `emergency` / `other`
- `fallback_reason`（closed enum\|null）: `emergency_safeside` / `low_confidence` / `unknown_intent` /
  `booking_mismatch` / null
- `degradation_reason`（str\|null・下記 **closed token 集合**の "+" 結合。S-3 は "+" 分割した各 token が
  集合に属することを検証し、非集合 token は `SecurityViolationError`）: `domain_rules_baseline_seed` /
  `llm_unavailable` / `context_envelope_unparsable` / `handoff_summary_withheld`

**State Constraints（mandatory）**:
- Flat TypedDict（primitives + JSON-serializable）のみ。JWT/API key/credential/Pydantic/InvocationContext 非格納。
- `InvocationContext` は `config["configurable"]` 経由（State 非格納）。node 内で `InvocationContext.from_state(state)`。
- 本 AG は永続 KB／checkpoint への機微本文格納を行わない（§6-4 永続化統治）。

### 2-5. Inner State schema と rule_findings の shape（敵対レビュー対応・inner の型注釈）

inner の `route()`/`get_output()` が型注釈に使う `InnerTriageState`（`AgentState` 継承 flat TypedDict）:
`validated_input:str` / `booking_context:dict` / `history_context:list[dict]` / `reference_datetime:str\|null` /
`intent:enum` / `urgency_signal:bool` / `sentiment_score:float` / `confidence:float` / `emergency_signal:bool` /
`arrival_country:str\|null` / `nationality:str\|null` / `purpose:enum\|null` /
`rule_findings:dict` / `escalation_required:bool` / `fallback_reason:enum\|null` /
`routing_decision:dict` / `handoff_summary:str` / `triage_meta:dict` / `status:str`。

`rule_findings` の **closed nested shape**（各ルールの結果・型のみ固定。バンド境界値は実装フェーズの config）:
`{r1_emergency:bool, r2_cancel_band:enum\|null, r3_visa_complexity:enum\|null,
r4_sentiment_band:enum\|null, r5_booking_consistent:bool\|null, r6_low_confidence:bool}`。
`r2_cancel_band`/`r3_visa_complexity`/`r4_sentiment_band` の enum メンバー集合は baseline seed schema に定義。

## 3. 🔴 Executable Contract — optional 入力（設計チェックリスト原因 A・レビュー High 5/7 の型）

proposal が「任意」「未接続なら degrade」と宣言した各点に、**欠落時の既定値・allowlist・reject 規則・
決定論性・証明テスト**を確定する。**書き込み（write/状態変更）入力は無い**（本 AG に `/ingest`・`/feedback`
は無く、実行時に永続層へ書かない）＝mode 別 RBAC は不要（§6-4）。

**top-level allowlist**: `context` 直下は `{history, booking}` のみ許可。`context` 自体が dict でない
場合は `{}` に置換し `rejected_context+=[{"path":"context","reason":"type_mismatch"}]`。未知 top-level キーは drop。

| # | optional | ①欠落/型不正時の既定値（allowlist 上の正当値） | ②未知キー・型違いの reject（層＝outer pre_process） | ③決定論性 | ⑤証明するテスト |
|---|---|---|---|---|---|
| C-1 | 会話履歴 `history` | 欠落 or 非 list → `history_context=[]`（空は正当値）。空でも intent_context は `validated_input` 単独で成立 | 各要素は `{role, text}` のみ抽出。`role`∈`{customer, staff}` 外・`text` 非 str・要素が非 dict → 当該要素 reject（`rejected_context`）。未知キー drop | 純関数 | no-history が success／非 list history が `[]` 化／不正要素が reject |
| C-2 | 予約状況 `booking` | 欠落 or 非 dict → `booking_context={}`。R5 は空なら「照合スキップ」に決定論的に倒す（捏造しない） | `{booking_id, departure_date, status}` のみ抽出。`booking_id` 非 str→drop、`departure_date` は ISO-8601 date として parse 検証（不正→drop＋reject）、`status`∈`{confirmed, pending, cancelled}` 外→drop。未知キー drop | 純関数 | no-booking が success（R5 スキップ）／不正 departure_date drop で R2/R5 が安全側 |
| C-3 | ドメインルール KB | 未接続 → `config/domain_rules.baseline.yaml`（`from_path()` 固定テーブル）へ decode し `degradation_reason="domain_rules_baseline_seed"`。baseline は標準旅行業約款ベースの取消料バンド・汎用カテゴリ→担当マップ・優先度/SLA を格納 | KB は deploy 時 config（実行時クライアント入力でない）。起動時 schema 検証（下表） | 静的ファイル＝決定論 | KB 未接続で baseline により success かつ degradation_reason／operator KB schema 不正で起動 `ConfigError` |

**KB 読込の失敗規則（起動時・一意化。敵対レビュー Medium 対応）** — silent ignore を作らない:

| ケース | 挙動 |
|---|---|
| `operator_kb_path` が null / 未読込（ファイル無し・読取不能） | baseline seed へ **degrade**（`degradation_reason=domain_rules_baseline_seed`）。運用上の正常 |
| operator KB は在るが **schema 不正**（必須キー欠落・型違い） | **`ConfigError`（fail-closed）** — 誤設定の operator KB を黙って無視して baseline に落とさない |
| baseline seed が無い / schema 不正 | **`ConfigError`（fail-closed）** — 最終フォールバックが壊れている＝起動不能にする |

## 4. 決定論ルールの構造（R1〜R6・enum・競合優先）

**具体的な閾値・表は実装フェーズで `config/config.yaml`（thresholds）＋ baseline YAML に確定**（§0 境界）。
設計で確定するのは以下の構造・失敗規則:

| # | 判断軸 | 入力 | 決定論ルール（構造） |
|---|---|---|---|
| R1 | 緊急度（安全側固定） | `emergency_signal`(LLM) **かつ** 決定論キーワード層 | いずれか真 → `escalation_required=True`・`routing_target=immediate_escalation`・`priority=urgent`。**判定が割れたら緊急扱い** |
| R2 | 取消料区分 | `booking.departure_date`, `reference_datetime`, 取消料バンド表 | 出発日−基準日時の差を `datetime` 比較でバンド化（境界は「以上/未満」を表で明示）。booking 欠落→スキップ |
| R3 | ビザ複雑度 | `arrival_country`, `nationality`, `purpose`（intent_context の LLM 観測から・§5 schema・欠落可） | 複雑度マトリクス表引き→複雑 or **いずれか null/未知は必ず** `visa_immigration`（専門担当）へ fallback。**可否は回答しない・外部参照しない**（§4-2） |
| R4 | 感情・クレーム強度 | `sentiment_score`(LLM), `thresholds.high_sentiment_severity` | 閾値以上→ベテラン/SV（`priority` 引き上げ） |
| R5 | 予約整合 | `booking_context`, `intent` | 予約 ID 照合（任意連携）・直近出発は時間優先度引き上げ。booking 欠落→スキップ |
| R6 | 低信頼度フォールバック | `confidence`(LLM), `thresholds.low_confidence` | 閾値未満→`routing_target=manual_triage`（捏造振り分けしない）・`fallback_reason=low_confidence` |

**競合優先順（決定論・固定）**: R1 緊急 > R6 低信頼度 > R2/R3/R4/R5。緊急が立てば他区分に関わらず
`immediate_escalation`＋`priority=urgent`。低信頼度が立てば（緊急でない限り）`manual_triage`。

### 4-1. 決定論性 — 基準日時と R2 の時刻解釈契約（敵対レビュー Critical #2/#4・設計チェックリスト原因 A #3）

**基準日時の生成**: 現在時刻は **adapter（`src/api/server.py`）で TZ 付き UTC ISO-8601 として 1 回生成**し
envelope の `reference_datetime` に載せる。**クライアント供給値は無視**（改竄・TZ 不一致を排除）。
**inner ノードは `datetime.now()` を呼ばない**（リクエスト内・ホスト間 TZ で結果が変わる。実装フェーズで monkeypatch/AST 証明）。

**R2 の day-count 契約（date と datetime の突き合わせ）**: `booking.departure_date` は**カレンダー日付**
（時刻なし）。`reference_datetime`（UTC）を `config.operator_timezone`（既定 `Asia/Tokyo`・deploy 固定）へ
変換し、その**現地日付**を取る。`days_until_departure = (departure_date − reference_local_date).days`（整数）で
取消料バンドを引く（バンド境界は「以上/未満」を baseline seed schema に明示）。ISO 文字列は**文字列比較でなく
`date`/`datetime` に parse して比較**（オフセット表記で静かに壊れる — SupplyDataQualityPrecedentAgent 実害）。

**null 時の決定論 skip**: `reference_datetime` が null（envelope parse 失敗・raw invoke 経路）または
`booking.departure_date` 欠落なら、**R2 は決定論的に skip**（`r2_cancel_band=null`・捏造しない・安全側）。R5 も
booking 欠落なら skip（§3 C-2）。pre_process が `now()` で補完することはしない（敵対レビュー指摘）。

テスト: 固定 clock で R2 バンド境界（例: ちょうど 7 日）を両側検証＋null reference_datetime で R2 skip＋
inner が `now()` を呼ばないことを証明。

### 4-2. R3 ビザ複雑度 — 外部ライブ参照を持たない（レビュー指摘 F-2 の解消）

R3 は「到着国 × 国籍 × 滞在目的」の**複雑度マトリクス（決定論・deploy 時 config）で専門担当へ振り分ける**
だけで、**ビザ可否そのものを回答しない・外部 API を叩かない**。SSRF・鮮度切れ・外部依存の攻撃面を
**構造的に持たない**（設計チェックリスト #6「フェッチを持たない選択＝攻撃面ゼロ」）。可否判断は人間の専門担当の責務。

## 5. LLM の役割と不在時挙動（敵対レビュー Critical #6）

**主張の精密化**: 「LLM は判断に使わない」は不正確だった。正確には —
**LLM は観測（intent/urgency/sentiment/confidence/emergency の構造化シグナル）を産出し、
決定論ルールがその観測ベクトルから決定を下す。観測が固定されれば決定は再現・監査可能で、
LLM は routing_target を選ばない。**

- **LLM 出力 JSON schema**: `{intent: enum, urgency_signal: bool, sentiment_score: float 0–1,
  confidence: float 0–1, emergency_signal: bool, arrival_country: str(ISO-3166 alpha-2)\|null,
  nationality: str(ISO-3166 alpha-2)\|null, purpose: enum{tourism,business,transit,other}\|null}`。
  末尾 3 項目は **R3（ビザ複雑度）の入力源**（問い合わせ本文から観測抽出。読めなければ null）。
  プロンプトに要素例と allowlist（enum 値）を含める（空配列/型のみの提示は LLM を空応答に倒す —
  先行テンプレートでの実害）。指示形で書く（許可形は落ちる）。
- **LLM 出力の schema 違反 = parse 失敗と同一に扱う（strict）**: 欠落キー・未知キー・enum 外・型違い・
  範囲外（score が 0–1 外）はフィールド単位で救わず、**観測ベクトル全体を決定論 fallback に倒す**（安全側で明快）。
- **parse 失敗・schema 違反・LLM 不在時の決定論 fallback**: `intent=other`・`confidence=0.0`（→R6 で
  `manual_triage`）・`sentiment_score=0.0`・`arrival_country=nationality=purpose=null`（→R3 は `visa_immigration`）。
  **`emergency_signal` は LLM に依存させず、R1 の決定論キーワード層で必ず二重化**（LLM 不在でも緊急キーワードで
  `immediate_escalation` に倒す＝取りこぼし 0 の安全側）。`degradation_reason=llm_unavailable`。
- **LLM 注入シーム**: `config["llm"]`（`BaseLLM` インスタンス）取り出しのみ。モデル ID・具体クライアントを
  `src/` に書かない。llm は runtime 注入（`config/config.yaml` にモデル ID や鍵は置かない）。

## 6. Security（5 層）

### 6-1. S-1 Trust Level
全 `FunctionNode`（pre_process/post_process/inner nodes）と `TravelTriageGraphNode`（GraphNode）で
`required_trust_level = TrustLevel.VERIFIED_EXTERNAL` を**明示宣言**（受付担当の標準業務操作）。
GraphNode 既定 ANONYMOUS のまま放置すると inner S-1 と不整合（088 実バグ）＝明示必須。
有効値は `ANONYMOUS`/`VERIFIED_EXTERNAL`/`INTERNAL` のみ（文字列 enum・`int()` 比較不可）。

### 6-2. S-2 Input Gate
`pre_process`/`context_seed` の `FunctionNode` で framework 既定 PII スキャンが `user_input`/`validated_input` に
自動適用。ドメイン拡張は `_extra_security_gate_input(state)` で `booking_context`/`history_context` の追加
PII スキャンと `validated_input` 長さ上限。`@final` の `_security_gate_input()` は触らない。
`GraphNode`（main）の S-2 は deliberate no-op（inner の各 FunctionNode gate が適用済み）。

### 6-3. 🔴 S-3 Output Gate（敵対レビュー Critical #7・裏口対策）
outer `post_process`（`FunctionNode`）で最終 S-3 を必ず通す。`_extra_security_gate_output(result)` は
**`result` を受け取り `state` を受け取らない**（SDK 実機・裏口 #3）。分岐に要る値は execute() が
`formatted_output` に**宣言**し、ゲートはその値で判定する（設計に `state.get()` と書かない）。

1. **closed allowlist（構造で非結論性を担保）**: `formatted_output` の top-level キーを固定 allowlist
   `{routing_target, priority, urgent, handoff_summary, triage_meta}` に限定。`triage_meta` の nested キーも
   `{confidence, fallback_reason, degradation_reason}` に固定。**未宣言キー・型不正・enum 不正は
   `SecurityViolationError`（fail-closed・単一の失敗規則。削除でも代替出力でもない）**。
   `routing_target`/`priority` は §2-4 の closed enum のみ（自由文の宛先を作れない）。
   `fallback_reason`/`degradation_reason` は enum（自由文でない）＝自由文の密輸経路を構造的に断つ。
2. **handoff_summary 自由文ガード（裏口 #4 対策）**: LLM が書く**唯一の自由文フィールド**。
   ①framework 標準 credential/PII 再スキャンを `handoff_summary` に明示適用（脇の自由文を素通りさせない）
   ②`routing_target` と矛盾する宛先指示が summary に無いことを確認する**製品安全の緩和策**（対象は
   routing_target enum の別名・多言語表記の照合。検出時は summary を固定安全文
   `"[triage summary withheld — see routing_target]"` に置換し `degradation_reason` を立てる）。
   **これは保証でなく緩和策**であり、非結論性の主保証は closed allowlist ＋ enum 固定（断定 lexicon の
   whack-a-mole に入らない — 同系テンプレート Return Fraud Investigation Memo Drafting Agent / Logistics Dangerous-Goods Document Consistency Agent の教訓）。

### 6-4. 永続化統治（設計チェックリスト #2・該当なしの明記）
本 AG は **`/ingest`・`/feedback` を持たず、実行時に永続 KB／checkpoint へ機微本文を書き込まない**。
KB（`domain_rules`/`routing_table`）は **deploy 時 read-only config**（`from_path()`）であり、実行時の
マルチテナント永続インデックスではない。よって「tenant_id/org_id fail-closed・スコープ引数必須・永続層 PB」は
**該当なし**。機微値（問い合わせ本文・氏名・予約 ID）は inner ノードの戻り値に載せず、S-2 マスク後の値のみ
State に流す（checkpoint 平文残留の回避）。

### 6-5. S-4 Audit
各ノード（決定論ノード含む）で `emit_trace_event()` を最低 1 回。fallback/escalation/reject/degradation の
各分岐にもドメインイベント（成功経路だけに置かない — 設計チェックリスト原因 B #5）。`node_start`/`node_complete`/
`node_error` は framework が自動発行＝execute 内で重複させない。

## 7. Framework Utilization / SDK 整合（doc ベーススパイク 2026-08-03 確認）

- `InvocationContext`: `correlation_id`/`session_id`/`caller_trust_level` を SDK 1.0.0 reference で実在確認。
  **`tenant_id` は無い** → 本 AG はマルチテナント分離を必要としない設計（§6-4）。
- `AgentBaseGraph.get_output()` = `formatted_output` or `result`（reference で確認）＝post_process で
  `formatted_output` に畳み込む（framework は inner のカスタム欄を透過しない）。
- `_extra_security_gate_output(self, result)` は `result` を受領（`state` 非受領）を reference で確認（§6-3）。
- 返す `status` は `AgentStatus` の小文字値・変更キーのみ返却・`node_history` は返さない。

### Import Isolation Confirmation
- [x] Level 0（`agenticstar`）を import しない
- [x] import 先は `framework/` と `shared/` のみ（`agents/base/` 不要）

## 8. EU AI Act Art.13 Design-Time Evidence
**Annex III Not in scope** と判定済み（TRV は必須宣言セット外・advisory）。よって
Art.13 設計時エビデンスは N/A。本 AG は人への不利益判定を自動化せず、最終判断は受付担当者（人間）。

## 9. 適用部品（parts — 共通部品ライブラリ・実装フェーズで `apply_part.py` を流す）

> parts は共通部品ライブラリ（本テンプレ repo 外）。**版は実装着手時に registry の実 version で確定**し、
> 適用後の adaptation 値を DDR に残す。骨格は再発明しない。

| part | 適用スロット | adaptation_points の埋め方 |
|---|---|---|
| `cat2-graph-skeleton` | `main` = `TravelTriageGraphNode` + inner 骨格 | lazy/cache subgraph・extract_input=検証済みフィールドの JSON 直列化・merge_output は `routing_decision`/`handoff_summary`/`triage_meta` を返す・upstream error passthrough・HITL propagate=False |
| `output-envelope` | `post_process` の `formatted_output` 組立 | allowlist キー §6-3 の通り・degradation 畳み込み・get_output() 透過問題対応（post で畳み込む） |
| `llm-draft-stage` | inner `handoff_summary` | プロンプト組立シーム・`config["llm"]` 注入・LLM 不在時 deterministic fallback（rule_findings から定型サマリ）・degradation_reason |

`context_seed`・`intent_context`（構造化抽出）・`domain_rule`/`routing_decision`（決定論表引き）はドメイン固有で部品化対象外。

## 10. Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | `AgentBaseGraph` | `AutonomousBaseGraph` | **A** | 固定パイプライン・自律ループなし |
| Composition | GraphNode（inner subgraph） | Standalone（直置き） | **GraphNode** | Cat 2 標準。ドメイン多段を `main` に隠蔽 |
| 判断の担い手 | 決定論表引き | LLM 判断 | **決定論** | reproducible & auditable（Financial Document Classification & Routing Agent）。LLM は観測と文面化に限定（§5） |
| 設計の粒度 | 閾値・表まで設計書に列挙 | 構造・enum・契約は設計／データ値は実装フェーズの config | **B** | 機械保証の追い過ぎを避ける（§0 境界）。閾値は運用で調整する管理項目 |
| context 受け渡し | invoke に多引数 | JSON envelope（adapter→pre_process→extract_input→context_seed） | **envelope** | `invoke()` は単一 str（SDK 実機）。§2-1 |
| 基準日時 | 各ノード `now()` | adapter で 1 回確定し State 伝搬 | **adapter 確定** | 決定論性（TZ・リクエスト内不変・SupplyDataQualityPrecedentAgent 回避） |
| R3 ビザ | 外部ライブ参照 | 複雑度マトリクスで専門担当へ（回答しない） | **B** | SSRF/鮮度切れ回避・可否は人間専門担当（レビュー指摘 F-2 解消） |
| S-3 非結論性 | 自由文 denylist 主防御 | closed allowlist ＋ enum 固定 | **allowlist** | denylist は言い換えで抜ける（先行テンプレートで確認済み）。lexicon は製品安全の緩和策に留める |
| 緊急検知 | LLM 依存 | LLM ＋ 決定論キーワード二重化 | **二重化** | LLM 不在でも取りこぼし 0（安全側） |
| HITL | enabled | disabled | **disabled** | suspend/resume の対話がない。安全性は決定論フォールバックで担保 |

### 🔴 設計チェックリスト照合（設計フェーズ必須）

| 先回り項目 | 該当 | 本 AG での対処 |
|---|---|---|
| #1 provenance を「ID実在」で終わらせない | 部分該当 | LLM は判断せず handoff_summary は narration。判断は決定論の authoritative フィールドで、LLM が捏造しても routing_decision は不変。summary の忠実性は人間（受付担当）が最終担保＝100% 機械保証を追わない。捏造引用 ID 照合が要る蓄積型ではない |
| #2 永続 KB 統治契約 | **該当なし** | `/ingest`・実行時永続書込なし。KB は deploy 時 read-only config（§6-4） |
| #3 PoC 検証計画 | **不採用（新ルール上書き）** | 本テンプレートでは PoC を実施しない。販売実現性は別マターとして分離。旧チェックリスト #3 を上書き |
| #4 継承欄は継承クラスのみ | 該当 | §1 で「継承先 = `AgentBaseGraph` (L1 direct) のみ」・GraphNode/inner は composition と明記 |
| #5 外部ソース確認は実施日を明記 | 該当 | 外部ライブ参照を持たない（R3 解消）。SDK API は doc ベーススパイクで 2026-08-03 確認（§7） |
| #6 入力 allowlist ＋ 品質ゲート | 該当 | ファイルアップロード無し（text）。optional の allowlist 抽出＋未知キー drop＋型違い reject を §3 に定義。`user_input`/`validated_input` は長さ上限（§6-2） |
| S-3 裏口 #3（gate は state 非受領） | 該当 | `formatted_output` に分岐値を宣言しゲートは `result` で判定（§6-3） |
| S-3 裏口 #4（自由文は主フィールドだけ検査しない） | 該当 | 自由文は handoff_summary の 1 本のみ＝全列挙して同一ガード（§6-3） |
| 設計フェーズ必須の実値化 | 該当 | `agent.yaml` `base_type: AgentBaseGraph`/`class: TravelInquiryTriageAgent`・README `{{}}` 実値化・Inherits=`AgentBaseGraph (L1 direct)` |

### 判断確定記録（human gate・2026-08-03 ユーザー確定）

設計フェーズで洗い出した 4 判断項目は、いずれも **推奨既定（(a)）で確定**。
設計は当初から推奨案を反映済み＝**本確定による設計内容の変更なし**。確定を明示のため記録する。

| # | 論点 | 判断（確定） | 反映先（設計上の実体） |
|---|---|---|---|
| 1 (A-1/A-7) | R3 ビザ 外部ライブ参照の要否 | **(a) 外部参照なし**（複雑→専門担当へ回す） | §4-2・本 §10 DDR「R3 ビザ」= Option B。R3 は複雑度マトリクス表引きのみ・外部 API/フェッチを構造的に持たない（SSRF/鮮度切れ攻撃面ゼロ） |
| 2 (A-3) | HITL 要否 | **(a) 非採用**（`hitl.enabled: false`） | `config/config.yaml` `hitl.enabled: false`・本 §10 DDR「HITL: disabled」。suspend/resume 対話なし・PB-7 は auto-waived（非 HITL） |
| 3 (A-2) | 法的/リスク境界 | **(a) 判定を出さない**（ビザ可否・法的判定を出力せず振り分けに閉じる） | §4-2・§6-3・§8。断定は S-3 closed allowlist + enum で構造排除。最終判断は受付担当者（人間） |
| 4 (A-5) | 内部ハンドオフの PII 粒度 | **(a) FW標準 detect_pii のみ・顧客識別は保持** | §6-2（framework 標準 detect_pii を domain フィールドへ適用＝既存実装の再利用）・§6-3（handoff_summary の credential/PII 再スキャン）・§6-4（機微本文の checkpoint 非格納）。**日本語補完サニタイザは追加しない**（既定不採用） |

**既定適用（聞かなかった項目）**: A-4 永続化 = 実行時に永続層へ書かない（§6-4 該当なし）／A-6 原則緩和 = なし（State 不変条件・closed allowlist・payload 非永続を遵守）。

### 🔴 実装で確定した設計差分（DDR 追記・実装が正・設計を追随更新）

実装時に、設計書の抽象記述を実装契約に落とす過程で確定した差分。**いずれも実装が正**で、
本設計書は追随済み（framework 適合と文書完全性の乖離を残さないため記録）。

| # | 論点 | 設計初版 | 実装で確定 | 根拠 |
|---|---|---|---|---|
| B-1 | 外部エンベロープ縮退の受け渡し | §2-4 表に該当欄なし | `State` に `envelope_degradation: str\|null` を1本追加し、post_process が inner の `triage_meta.degradation_reason` と `+` 連結で畳み込む | `merge_output` が `triage_meta` を inner 値で**上書き**するため、outer 層（pre_process）で立てた縮退トークンを triage_meta 内に置くと消える。State の独立欄に退避して post で合流するのが唯一の正しい経路 |
| B-2 | booking/history のドメイン PII スキャン位置 | §6-2「`_extra_security_gate_input(state)` で booking_context/history_context の追加スキャン」 | **execute() 内の audit-only スキャンに移動**。`_extra_security_gate_input` は raw エンベロープのサイズ上限のみ | S-2 ゲートは **execute() の前**に走る（実行順 §3）。booking_context/history_context は execute() が envelope を parse して初めて生成されるため、ゲート時点では存在しない。ゲートが execute 書込 state に依存すると fail-open になる（ゲートは execute() より前に走るため、execute() が書いたキーを読むゲートは、そのキーを書かない経路で黙って素通しになる）。A-5「FW標準 detect_pii のみ・顧客識別は保持」の方針は不変 |
| B-3 | main スロットの単体テスト | scaffold 既定 `test_main_node.py`（`MainNode` 前提） | `TravelTriageGraphNode` の契約テストへ全面置換（extract_input 直列化 / merge_output の schema 境界〔宣言フィールドのみ・raw 非透過〕/ upstream・no-input passthrough / trust・error_strategy） | outer main は scaffold の `MainNode`（FunctionNode）ではなく `GraphNode`。旧テストは存在しないクラスを import し収集エラーになるため置換必須。新規 inner ノードテスト群に main 専用テストが無かったギャップも同時に解消 |
| B-4 | KB / config の fail-closed 粒度（敵対レビュー 1 巡目由来） | §3 失敗規則の骨子のみ | ①`emergency_keywords: []` を **ConfigError**（R1 決定論バックストップの黙殺防止）②`visa.simple_purposes/simple_country_pairs` の**要素型**を検証（非 str→request 時 `set()` クラッシュを起動時 ConfigError 化）③operator KB が**在るが非 mapping / 解析不能**→ ConfigError（`未読込`＝OSError/ファイル無しのみ degrade）④present-but-corrupt `config.yaml`→起動時 ConfigError | いずれも「present-but-broken を silent degrade / request 時クラッシュにしない」= fail-closed の粒度確定。§3 の失敗規則表の意図を実装契約に厳密化。棄却したレビュー指摘 = post_process の1語 routing_target 矛盾チェック（§6-3 の「緩和策であって保証でない」設計判断のとおり・lexicon whack-a-mole 回避 Return Fraud Investigation Memo Drafting Agent / Logistics Dangerous-Goods Document Consistency Agent）と pyproject `dependencies=[]`（SDK wheel が推移供給する共通規約） |

## 11. Usability Review（受付担当のユースケース）

- **入力**: 受付が自然に持つ最小入力（問い合わせ本文）だけで成立。予約状況・会話履歴は任意で、無くても
  success（§3 の既定値）。過剰な構造化入力を要求しない。
- **出力**: 受付が次アクションに直結できる形 — `routing_target`（担当）＋`priority`＋`urgent`＋多言語
  `handoff_summary`。担当はサマリをそのまま引き継ぎに使える。
- **エラー/フォールバック**: LLM 不在・KB 未接続・低信頼度でも `status="success"`＋`degradation_reason`/
  `fallback_reason` を返し**沈黙しない**。緊急シグナルは必ず `immediate_escalation`（取りこぼし 0 最優先）。
- **修正した設計判断**: 当初「R3 で外部参照」を検討したが、鮮度切れ回答が受付を誤誘導するリスクと SSRF 面を
  避け「複雑＝専門担当へ回す」に閉じる設計へ修正（安全側・レビュー指摘 F-2 整合）。
