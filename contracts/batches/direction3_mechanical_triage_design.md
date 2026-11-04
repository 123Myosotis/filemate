# 方向 3 — 机械分流（设计确认修订，确认前不写代码）

## 0. 上下文复核

我读过的 handbook hash = `d3d912a04c589cc477dde79dce309715c977b1a9b4fa19a3bd8f73ad6a36f08a`

已关闭批：

- 准确性硬修：`acceptance/accuracy_hardfix_20261018/`（只读）
- 方向 2：`acceptance/direction2_ops_20261018/`（只读）；`CURRENT=v1.1.0`；执行路径 = `ops[]` + `fixed_op_order` + `OPERATOR_DISPATCH`

与本批相关的手册条目复述：

1. **裁定 1**：强制层 / opt-in / 暂不强制分层仍成立。方向 3 **不**把 `row_accounting` / `deriv_edge` / budget 时序改成全面强制。  
2. **裁定 2**：方向 3 须设计确认后再写码。方向 4 一键证据、方向 5 假设裁决本批不做。  
3. **裁定 3**：方向 1 不可回退：无 GATE 逃生门；无契约库 → `ContractLibraryError`。  
4. **两条铁律**：只动本方向相关文件；不兜底、不逃生门、无业务字段名上接口、**无 confidence 分流**、无模板化问法。  
5. **固定队列**：方向 1 → 准确性硬修（关）→ 方向 2（关）→ **本批设计确认** → 写码 → 方向 4…  
6. **手册 §6 F**：执行验收包须含 plan dump，进 `outputs.sha256`，rerun 复算。  
7. **手册 §7**：key_norm / empty / ops[] / fixed_op_order **只读不改**。  
8. **本批允许改 / 不允许改**：见 §7。

声明：以上与 `contracts/EXECUTION_HANDBOOK.md` 一致；hash 与 `contracts/HANDBOOK_HASH` 一致。不符则本批作废。

本修订相对上一稿的 P0 裁定：

- **删除**信号 `declared_trigger`（`plan.triggers` 是预案不是已触发）。缺口 **G3-7** 进 backlog。  
- **G3-3 本批拆除**：`validate_binding` 不得再因 confidence 低于阈值写出 `state_id=await_user`。  
- **`plan.status=draft`**：gate **拒绝**，不进 triage、不静默执行。

---

## 1. 目标（一句话）

用契约里已有字段判定 `simple` / `complex`：简单静默执行并交付结构校验过的摘要；复杂进入一次是/否确认。判定不读 confidence、不读业务字段名、不读表、不调 AI。

---

## 2. 分流信号

### 2.1 总则

- 闭集 = §2.2。禁止为凑数新增信号常量。  
- 「非空」= 字符串 **strip 后**非空；列表 = `len>0`；数值比较见该行。空白串不算触发。  
- triage **不读** `confidence` / 阈值 / `confidence_low` 笔记内容（笔记可存在，不参与 fired）。  
- 两阶段：  
  - `triage_plan(plan)`：执行前，只读 plan。  
  - `triage_after_execute(plan, execution_result)`：摘要提交前再扫 §2.2 中标注「执行后」的信号。  
- `impact_scope` 由 triage 按 `impact_scope_pattern` **机械替换下标**，禁止 AI 填写。

### 2.2 信号表（本批）

| signal_id | 来源 | 触发 | impact_scope_pattern | 阶段 |
|-----------|------|------|----------------------|------|
| `binding_ambiguous_reason` | `bindings[i].ambiguous_reason` | strip 后非空 | `bindings[{i}]` | 执行前 |
| `binding_alternatives` | `bindings[i].alternatives` | list 且 len>0 | `bindings[{i}]` | 执行前 |
| `status_await_user` | `plan.status` | `== await_user` | `plan` | 执行前 |
| `status_contract_incomplete` | `plan.status` | `== contract_incomplete` | `plan` | 执行前 |
| `status_version_rejected` | `plan.status` | `== version_rejected` | `plan` | 执行前 |
| `status_over_budget` | `plan.status` | `== over_budget` | `plan` | 执行前 |
| `status_unanchored` | `plan.status` | `== unanchored` | `plan` | 执行前 |
| `status_fail` | `plan.status` | `== fail` | `plan` | 执行前 |
| `status_rollback` | `plan.status` | `== rollback` | `plan` | 执行前 |
| `status_non_idempotent_rerun` | `plan.status` | `== non_idempotent_rerun` | `plan` | 执行前 |
| `status_review_reread` | `plan.status` | `== review_reread` | `plan` | 执行前 |
| `status_stop` | `plan.status` | `== stop` | `plan` | 执行前 |
| `budget_actual_over` | `budgets[k].limits` / `actual` | 同键且两边均为有限数值且 `actual > limits` | `budgets[{k}]` | 执行前 |
| `row_cover_incomplete` | `row_cover` + `source_row_ids` | 已声明 row_cover 且差集非空 | `plan` | 执行前 |
| `shared_map_mismatch_note` | `plan.runtime_notes[i]` | 字符串以 `shared_map_mismatch` 开头 | `plan` | 执行前若已写入；执行后必扫 |
| `execution_contract_issues` | `execution_result.contract_issues` | list 且 len>0 | `plan` | **仅执行后** |

`plan.runtime_notes`：字符串数组。执行器把 handbook §7.4 的 `shared_map_mismatch …` 行 **追加**到该数组（同时可留在 `execution_result.notes`，triage **只读** `plan.runtime_notes` 与 `execution_result.contract_issues`）。形状在写码发布的 **v1.1.1** 登记为可选字段 `runtime_notes: string[]`（不改 ops / cmp_op / empty / key_norm）。v1.1.0 计划缺该键视为 `[]`。

### 2.3 不进本批信号

| 项 | 裁定 |
|----|------|
| `plan.triggers` / `declared_trigger` | **删除**。预案 ≠ 已触发。见 G3-7 |
| `confidence*` / `plan_notes` 中 `confidence_low` | 不触发。G3-3 本批拆路由副作用 |
| `enums.trigger` 各成员 | 本批 **零个** 作为 triage 信号。见 §2.4 |
| `conflict_kind` | 无 `conflicts[]` 载体。G3-1 |
| 表内容 / 业务列名 | 禁止 |

### 2.4 `enums.trigger`（v1.1.0 现状，本批不消费）

当前成员：`compare_fail`、`set_mismatch`、`cycle`、`over_budget`、`anchor_miss`、`version_reject`、`user_deny`、`contract_incomplete`、`row_cover_miss`、`normalize_miss`、`duplicate_key`、`cross_txn_misalign`、`meter_mismatch`、`calc_replay_fail`。

与其它信号的关系（**不**把 trigger 字面量并入 fired）：

| trigger | 若将来 G3-7 落地时可能重叠的现信号 | 本批处理 |
|---------|-----------------------------------|----------|
| `over_budget` | `status_over_budget`、`budget_actual_over` | 只用后两者 |
| `contract_incomplete` | `status_contract_incomplete` | 只用 status |
| `version_reject` | `status_version_rejected` | 只用 status |
| `anchor_miss` | `status_unanchored` | 只用 status |
| `user_deny` | `status_await_user`（确认拒绝后） | 只用 status / lock_record |
| `row_cover_miss` | `row_cover_incomplete` | 只用集合差 |
| `compare_fail` | `status_fail` / `rollback` / `non_idempotent_rerun` | 只用 status |
| 其余（`set_mismatch`、`cycle`、`normalize_miss`、`duplicate_key`、`cross_txn_misalign`、`meter_mismatch`、`calc_replay_fail`） | 无本批信号 | 留 G3-7，不硬塞 |

### 2.5 `budget_actual_over` 与不可比较

有 `budgets[]` 时，对每个同时出现在 `limits` 与 `actual` 的键：

- 两边均为有限数值且 `actual > limits` → 触发 `budget_actual_over`。  
- 任一缺失、非数值、NaN/Inf → **gate 拒绝**（`budget_comparison_error`，`contract_incomplete`），**不进入 triage**。禁止「不可比较就当没触发」。  
- 同时在 `plan.runtime_notes` 追加一行前缀 `budget_comparison_error`（gate 拒绝路径也写，便于审计）。该前缀 **不是** triage 信号。

### 2.6 `draft`

`plan.status == draft` → **gate 拒绝**（`status_not_executable`），零行，**不**调用 triage，**不**静默执行。不新增 `status_draft` 信号。可进入 triage 的入口 status **仅** `ready`（以及用户 `yes` 之后、lock 为 `accepted` 的同一次执行，status 仍须先被置回可执行态，见 §6.3）。

---

## 3. 判定规则

### 3.1 二元

```text
fired = 本阶段信号触发集
空 → simple
非空 → complex
```

返回值只有 `simple` | `complex`。附带 `fired_signals` 只作诊断，不另算分。

### 3.2 与 gate

| 条件 | 结果 |
|------|------|
| gate 失败（含 draft、budget 不可比较、契约不完整） | 拒绝执行。不是 complex |
| gate 过且 triage simple | §5 |
| gate 过且 triage complex | §6。仅当当前 status 为 `ready` 时写入 `await_user`；**已是 `await_user` 则不重复写**（保留原 status） |

### 3.3 G3-3 本批拆除（路由副作用）

`validate_binding` 与 `validate_global_judgment` 两处都删：

- 两处函数内的 confidence **阈值比较代码完全删除**。不产生 `await_user`，不写低置信度笔记。  
- `validate_global_judgment` 原先在 `confidence < threshold` 时发 issue，`state_id` 取 `on_low_confidence`，缺省就是 `await_user`。那不是 fail 级旁路，所以与 binding 同一刀拆除，不保留阈值比较。  
- `on_low_confidence` 仍是判断对象的必填枚举字段（结构校验），不再用来改 plan 状态。  
- `confidence` 字段仍保留，仅供展示/审计。  
- `forbid_degenerate`（0.0/1.0）仍是结构校验失败（gate 拒绝），不改成 await_user，也不进 triage。  
- 这是拆「confidence → 状态 → triage」的旁路，**不**改 key_norm / empty / ops 语义。

---

## 4. 缺口（backlog，不进信号表）

| ID | 内容 | 本批 |
|----|------|------|
| G3-1 | 无 `conflicts[]` | backlog |
| G3-2 | delivery 检查项无计划块 | backlog |
| G3-3 | confidence→await_user | **本批拆除**（§3.3），不再当未做缺口 |
| G3-4 | 语义未覆盖无非分数载体 | backlog |
| G3-5 | 不发明 `budget_unsatisfiable` | 只用现有 budget/status 信号 |
| G3-6 | 除 mismatch 前缀外无通用 runtime_signals 枚举 | backlog；本批只登记 `runtime_notes: string[]` |
| **G3-7** | 需要 `triggers_fired[]`（已触发），不能用 `plan.triggers` 预案 | **backlog。本批无 declared_trigger** |

---

## 5. 简单路径

静默 `execute_mechanical`。成功且执行后 triage 仍为 simple，才交付。

### 5.1 `execution_summary` 必填

```text
schema_version = "d3-summary-1"
contract_version / contract_hash    # 与 plan 一致
triage = "simple"
fired_signals = []
primary_table                       # strip 非空
ops_present                         # plan.ops[].op 去重排序
row_count_out / column_count_out    # int >= 0
sample_ref                          # sample_manifest_id 非空 或 sample_coords 非空列表
evidence_path                       # 符合 §5.3
human_text                          # 非空字符串；只查类型与非空
```

结构失败 → 不交付成功摘要。见 §5.4（生成失败）与 §8（执行中信号）。

### 5.2 摘要生成失败（AI 超时 / 非 JSON / 结构校验失败）

- 最多 **2 次**尝试（首次 + 1 次重试）。  
- `execution_status` 闭集：`success` | `summary_failed` | `complex`。  
- `summary_failed`：`rows` **保留**，不标成功，不写 `execution_summary`。用户可重试、接受无摘要、改计划或 abort。不写入 `fired_signals`，不进 §8。  
- **不等同** §8 漏检。

### 5.3 `evidence_path`（写码前定死）

- 相对验收/会话目录：`evidence/{contract_version}/{plan_id12}.json`  
- `plan_id12` = `json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")` 的 sha256 hex 前 12 位。五个参数不改。  
- plan_id12 基于 gate 附加 `_gated_*` 之前的 plan。`_gated_contract_hash` / `_gated_contract_version` 只留在 plan 上作审计，不进入 dumps。  
- 文件内容（本地写，非 AI）：

```text
schema_version = "d3-evidence-1"
contract_version
contract_hash
triage
fired_signals
ops_present
row_count_out
column_count_out
sample_ref
```

不含业务单元格、不含 `human_text`、不含 confidence。

---

## 6. 复杂路径

### 6.1 `complex_confirmation`

```text
schema_version = "d3-complex-1"
contract_version / contract_hash
triage = "complex"
fired_signals                 # 非空
conflicts[]:
  signal_id
  source_path                 # 机械路径，如 bindings[0].ambiguous_reason
  impact_scope                # 由 §2.2 pattern 替换得到的字符串
  suggested_action            # confirm_execute | edit_plan | abort
user_prompt                   # 一条非空字符串（单次是/否）
lock_record
```

`impact_scope` 禁止 AI 生成。

### 6.2 `suggested_action` 与 `edit_plan` 流转

`triage_plan` / `triage_after_execute` **只**返回 `{triage, fired_signals}`。`suggested_action` 与 `user_prompt` 由独立步骤生成，**禁止**在 `triage.py` 写信号→动作映射。

执行前 complex 的动作闭集：`confirm_execute` | `edit_plan` | `abort`。  
§8 升级后的动作闭集：**仅** `accept_current_result` | `abort`（不含 `confirm_execute`，不含 `edit_plan`）。要改计划必须先 `abort` 再重新提交。

AI 失败、超时、或返回值不在该阶段闭集内 → 整单 fallback `abort`（不是按信号查表）。

`edit_plan`（仅执行前确认）：

1. `lock_record` 重置为 `pending`，`decision=null`。  
2. 改计划后必须重新 gate，再 `triage_plan`。不得跳过 gate。  
3. `confirm_execute` + 用户 `yes` → `accepted`，然后才执行。  
4. `abort` 或用户 `no` → `rejected`，不执行。

### 6.3 `lock_record.status` 闭集

`pending` | `accepted` | `rejected` | `executed` | `failed` | `rolled_back`

| 时机 | status |
|------|--------|
| 进入确认、尚未答 | `pending` |
| 用户 yes | `accepted`（尚未跑完执行） |
| 用户 no / abort | `rejected` |
| accepted 且执行成功且执行后 triage 仍 simple | `executed` |
| accepted 且执行被契约拒绝 | `failed` |
| §8 升级回滚 | `rolled_back` |

不另造「沿用 accepted 表示已执行」。

### 6.4 与方向 5

本批只有一次是/否 + lock_record。不做假设树、不做分项裁决。

---

## 7. 允许改 / 不允许改

```text
本批名称：方向 3 — 机械分流
允许改（批准写码后）：
- pipeline/synth_contract/triage.py（新）
- pipeline/synth_contract/validate.py：仅拆除 confidence→await_user（§3.3）
- 执行器：向 plan.runtime_notes 追加 shared_map_mismatch 行（不改比较语义）
- table_synth 入口：gate → triage → simple/complex
- 契约 v1.1.1：runtime_notes: string[]；可选 enums.triage_result = [simple, complex]
- tests + acceptance/direction3_triage_<date>/

不允许改：
- ops[] / DISPATCH / fixed_op_order / v1.0 拒绝路径
- key_norm / empty / classify_empty / agg_semantics.fail
- 用 confidence 或业务字段名做 triage
- 方向 4–9；用 triggers 预案当已触发
```

---

## 8. 执行中漏检

`triage_plan=simple` 之后、提交摘要之前，`triage_after_execute` 若 fired 非空（含 `shared_map_mismatch_note`、`execution_contract_issues`）：

1. 停止，禁止静默成功。  
2. 不交付 `execution_summary`。结果表 `contract_rejected=true` 或空交付。  
3. 改交 `complex_confirmation`；`lock_record.status=rolled_back`。`suggested_action` 只允许 `accept_current_result` | `abort`（AI 生成；非法或失败则 fallback `abort`）。不含 `confirm_execute`（不重跑）、不含 `edit_plan`（要改计划须先 abort 再重新开始）。  
4. 不自动重跑。

`execution_contract_issues` **是** §2.2 信号，来源是执行返回值，不是 gate 文本扫描。

---

## 9. 验收（写码后）

```text
python -m pipeline.synth_contract.authority_scan
python -m unittest discover -s tests/contract -q
python -m tests.acceptance.direction3_triage
```

包内文件含 `_rerun_body_stdout.txt` 且进 `outputs.sha256`。`inputs.sha256` **不**强制包含本设计 md（见 backlog P2-9，本批不纳入）。

PASS 行：

1. `triage_signal_<signal_id>_forces_complex` — §2.2 每一信号一条。  
2. `triage_all_clear_is_simple`  
3. `simple_path_summary_schema_ok`  
4. `simple_path_sample_and_evidence_present` — `evidence_path` 符合 §5.3  
5. `complex_path_awaits_confirm` — `lock_record.status=pending`；yes 之前无成功表  
6. `adversarial_ambiguous_looks_normal` — fixture：**`bindings[].confidence` 高于 binding 阈值（用 0.99，阈值见契约 `confidence_thresholds.by_judgment_type.binding`）且 `ambiguous_reason` strip 后非空**。断言 triage=`complex`，且 fired 含 `binding_ambiguous_reason`、**不含**任何 confidence 派生信号。与第 1 条的差别是：第 1 条只证明信号非空；本条证明 **高 confidence 不能洗白歧义**。  
7. `no_confidence_in_triage_source` — `triage.py` AST 无 `confidence` 标识符  
8. `late_signal_escalates` — 注入机制：先 `triage_plan` 得 simple，再构造 `execution_result.notes` 含一行 `shared_map_mismatch map_id=a shared_map_id=b`，由收口函数写入 `plan.runtime_notes` 后调用 `triage_after_execute`。断言不提交 summary，`lock_record.status=rolled_back`。不靠改生产源码临时删键。  
9. `draft_status_gate_rejects` — status=draft → gate 拒绝，triage 未被要求给出 simple  
10. `confidence_low_does_not_await_user` — binding confidence 低于契约阈值、无其它信号 → gate 不因此失败，triage=`simple`；`validate_binding` 无阈值比较  
11. `budget_incomparable_gate_rejects` — actual 非数值 → `budget_comparison_error`，不进 triage  
12. `triage_after_execute_all_clear_stays_simple` — `triage_plan=simple` 且 `execution_result.notes=[]` 且 `contract_issues=[]` → `triage_after_execute=simple` → 正常交付（`execution_status=success`）  
13. `global_judgment_confidence_low_does_not_await_user` — `judgments[].confidence` 低于该 `global_judgment_type` 的契约阈值，且 `on_low_confidence=await_user`，锚点合法。断言 gate 不因此拒绝，`contract_status` 不是 `await_user`，triage=`simple`，`execution_status=success`。  

`rerun_hash_match=true`，`rerun_input_hash_match=true`，`exit_code=0`。

---

## 10. 确认前不写码

未批准前不改 `validate.py` / 执行器 / 契约版本，不新增 `triage.py`。
