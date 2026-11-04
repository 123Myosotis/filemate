# 准确性硬修批 — 设计确认（修订版 R2，确认前不写代码）

## 本批页眉硬约束（补充）

- 交付报告必须含：验收脚本**原始** stdout/stderr、真实文件端到端结果、每个变红测试的「代码错/测试错」判定及修哪边。  
- **不接受**「单测绿」作为交付证据；`python -m tests.acceptance.accuracy_hardfix` 必须由实现方跑过并贴原始输出。  
- 合并后用户将亲自跑该验收脚本与真实文件回归，任一 FAIL 即退回。  

---

## 0. 上下文复核

我读过的 handbook hash = `e54cfac4f54ebe6b842c4efa7cb7fe10365cee1f762cf7b88c599f432dc78484`

与本批相关的手册条目复述：

1. **裁定 1**：强制层含比较/聚合字段的 `empty_token_map`、join/对账/去重的 `key_norm_id`；未声明 → `contract_incomplete`。opt-in / 暂不强制本批不扩。  
2. **裁定 2**：本批 = 必须修三项（join key_norm 真比较、order/dedupe/agg 真执行、空值挂门禁），位于方向 1 之后、方向 2 之前。  
3. **裁定 3 / 方向 1**：无 GATE 逃生门；不得用本地 strip/lower 冒充 key_norm。  
4. **铁律**：只改本批文件；不兜底、不逃生门、无业务字段名上接口、无 confidence 分流、无模板化问法；不改断言迎合坏实现。  
5. **手册 §7（本修订写入）**：`steps` 禁止默认可空；identity = 唯一一项 `{"op":"identity"}`；empty_token_map 按角色枚举表；executor 单一比较路径。  
6. **队列位置**：方向 1 完成 → **本批** → 方向 2。  
7. **允许改/不允许改**：见 §6（含：旧结构测不得保留）。  

声明：以上与 `contracts/EXECUTION_HANDBOOK.md` 一致；hash 与 `contracts/HANDBOOK_HASH` 一致。不符则本批作废。

---

## 1. 本批范围（三项，不扩不缩）

| # | 目标 |
|---|------|
| ① | join 运行时真正按 `key_norm` 比较 |
| ② | order / dedupe / agg 真正机械执行 |
| ③ | 空值三分类挂进门禁 |

**不做**：方向 2–9；性能；前处理；分流；假设裁决。不改契约**业务枚举**；写码阶段仅允许：① 将 `identity` 登记进 `norm_ops`；② 必要时补 `authority_scan` 规则。

---

## 2. 补充裁定（R2 六项）— 写进本设计，写码必须遵守

### ① `key_norm_id` 的 `steps` 语义（禁止默认可空）

| 规则 | 内容 |
|------|------|
| 空 steps | **非法**。`steps` 缺省 / `[]` / `null` → `contract_incomplete` |
| Identity join | **唯一**表达：`steps == [{"op": "identity"}]` |
| `identity` 重放 | `norm_value =` 对 raw 的机械字符串化（`None`→`""`，否则 `str(raw)`），**禁止** trim/lower/casefold/本地规范化 |
| 双侧 | 左右 `key_norm_id` 解析出的 steps **逐项相等**；不等 → 契约不完整，退回 AI，不本地凑合 |
| 登记 | 写码时把 `identity` 加入契约 `norm_ops`（操作登记，非业务枚举） |

权威条文同时在手册 §7.1；本批不得发明第三种「空 steps = identity」。

### ② 「参与比较/聚合的字段」× `empty_token_map` 枚举表

与手册 §7.2 一致，写码扫描**只按此表**，不许看情况：

| 角色 ID | 需要 empty_token_map？ |
|---------|------------------------|
| `join_key` | **是** |
| `dedupe_key` | **是** |
| `agg_group` | **是** |
| `agg_input` | **是** |
| `order_key` | **是** |
| `filter_eq` | **是**（op∈{eq,neq} 的列） |
| `filter_in` | **是**（成员/多值比较列） |
| `order_tie_break` | **否**（仅当值为唯一 `cell_coord` 或 `input_row_id`）；若为 catalog 列 → **是**且应判契约倾向错误 |

### ③ 旧结构测不得保留

- 涉及 order/dedupe/agg 的测试，合并后**必须**经 `execute_plan` / `execute_mechanical` **真执行**并断言**结果行/列**。  
- 凡「只校验规格对象完整、不执行也能 PASS」的用例（典型：`test_order_dedupe_agg_structural`）→ **删除，或改造成真执行测**。  
- **不允许**保留结构测当作本批通过证据。  
- 写入 §6「不允许改」的对偶：**不允许保留结构-only 绿测**。

### ④ 验收脚本真实可执行

新增：`tests/acceptance/accuracy_hardfix.py`  

```text
python -m tests.acceptance.accuracy_hardfix
```

必须输出：

1. 每项 **PASS/FAIL**（至少：D1 基线逐格、T1、T2、T3、真实文件回归、单一路径断言、authority_scan）  
2. **差异表**：每行标注 `准确性修正` / `引入新错`  
3. **分阶段耗时**：读表 / 校验 / 执行 / 汇总（ms）  
4. **exit code** 0 = 全 PASS，否则 1  

交付报告必须粘贴 Cursor **本机跑过的原始 stdout/stderr**。不接受「设计里应该 PASS」。

### ⑤ 至少一张真实文件回归

在 T1/T2/T3 **之外**：

- 至少一张**真实来源**表（非纯手工迷你 fixture）：规模约**几百行**，场景混合 **join + order + agg + 三类空值**。  
- 走完整 `execute_plan`，输出（摘要哈希 + 行数/列/抽样或全量路径）附验收报告。  
- 来源候选（写码时择一存在者）：`模拟/`、`测试/` 下真实业务导出；若无合适文件 → **生成自真实 CSV 采样的派生文件仍不够**；必须能指出磁盘上的原始路径。若仓库无 ≥数百行混合场景文件，写码前在设计确认已注明的路径下**用仓库已有真实表拼接/抽取**（仍算真实来源单元格），并在报告写明源路径。  
- **全部是手工几行 fixture ≠ 满足约束 C/⑤**。

### ⑥ executor 单一路径

| 保留（executor 唯一比较栈） | 处理 |
|------------------------------|------|
| `accuracy.classify_empty` / `compare_empty` + `norm_spec` 重放（`replay_norm_steps`，含 `identity`） | **唯一**键/比较路径 |
| `normalize.is_null` / `compare_with_null_policy` | **executor 禁止调用**；标 deprecated 或加模块级注释；验收用静态/调用探针断言 `execute_mechanical` 调用图不含二者 |

`normalize.validate_normalize_spec` / `apply_ops`（非比较）可保留供规格校验；**比较语义不得双轨**。

---

## 3. 三项改造 — 接入点清单

### ① join 运行时 key_norm

```text
joins[] → 强制左右 key_norm_id
  → 解析 norm_spec；steps 非空；双侧 steps 相等
  → 键值：classify_empty → 非 value 则按计划状态中止
  → replay steps（含 identity）→ 仅 norm_value 建索引/探测
  → 禁止键值 strip/lower
  → duplicate_key → 计划声明状态；禁止本地取第一条装成功
```

| 文件 | 符号 |
|------|------|
| `executor.py` | `apply_contract_joins`（主改）、`execute_mechanical` |
| `accuracy.py` | `enforce_declared_accuracy`、`validate_key_norm_pair` |
| `accuracy.py` | `replay_norm_steps` 增加 `identity` |

### ② order / dedupe / agg 真执行

| 文件 | 符号 |
|------|------|
| `executor.py` | 新建 `apply_order_spec` / `apply_dedupe_spec` / `apply_agg_spec`；`execute_mechanical` 接入 |
| `calc.py` | 校验升级；执行由 executor 调 `replay_calc_*` |

顺序（计划声明时）：… → join → filter → **order → dedupe → agg** → project。

### ③ 空值挂门禁

| 文件 | 符号 |
|------|------|
| `accuracy.py` | `enforce_declared_accuracy`：按 §2.② 角色表扫字段，强制 `empty_token_map` |
| `executor.py` | 所有键/比较路径调用 `classify_empty`/`compare_empty` |
| `normalize.py` | `is_null`/`compare_with_null_policy`：deprecated + executor 不调用 |

---

## 4. 受影响现有测试 + 变红处置

| 测试 | 预期 | 代码错/测试错 | 修哪边 |
|------|------|---------------|--------|
| `test_executor.test_join_exact` | 变红 | 代码曾无 key_norm；测试夹具不合法 | 代码接 key_norm；夹具补 `identity`/`trim` 等合法 steps + empty_token_map；**不断言无 norm 可 join** |
| `test_executor.test_filter_eq` 等 | 可能变红 | filter_eq 需 map | 夹具补 map |
| `test_calc_xref_budget.test_order_dedupe_agg_structural` | **删除或改造** | 结构-only 禁止保留 | 改为真执行断言结果，或删除 |
| `test_adversarial.test_empty_classes_*` | 绿 | 助手单测 | — |
| `test_diaonan.test_key_norm_mismatch_on_join` | 绿/更严 | — | 可加缺 id 用例 |
| `test_normalize` 中依赖 `compare_with_null_policy` 的比较测 | 保留为 **normalize 单元**；不得作为 executor 比较证据 | — | 验收脚本断言 executor 未调用 |

---

## 5. 端到端基线与验收资产

| 资产 | 作用 |
|------|------|
| `archive/d1_e2e_baseline.json` | 方向 1 小表；合并后逐格相等或差异表逐行标注 |
| T1/T2/T3 fixtures | 机械证明三项 |
| **真实文件**（§2.⑤） | 约束 C；路径写入验收报告 |
| `tests/acceptance/accuracy_hardfix.py` | 一键验收 |

D1 基线预判：无空白差异键时，声明 `identity` 或等价 trim 后应 **CELL_EQ**；若不等 → 差异表强制分类。

---

## 6. 允许改 / 不允许改

```text
本批名称：准确性硬修 R2
本批目标：三项准确性硬修 + 单一比较路径 + 可执行验收

允许改：
- executor.py, accuracy.py, calc.py, normalize.py（仅 deprecated/断调用）
- contracts/versions/v1.0.0.json：仅登记 norm_ops 增加 identity；必要时 authority_scan
- tests/contract/*：夹具改造；删除/改造结构-only 测
- 新增 tests/acceptance/accuracy_hardfix.py
- 新增 tests/contract/fixtures/accuracy_hardfix/*
- 真实回归输入路径说明（只读真实文件，不改业务数据语义）

不允许改：
- 保留 order/dedupe/agg 结构-only 绿测当证据
- 方向 2–9；GATE/boot 清场回退
- 双轨比较（executor 调用 is_null/compare_with_null_policy）
- 空 steps 当 identity；strip 当 key_norm
- 改断言迎合无 key_norm 的 join 成功
- 用「单测绿」代替 acceptance 脚本原始输出
```

---

## 7. 验收标准（可机械执行）

```text
# handbook
# 设计 §0 hash == contracts/HANDBOOK_HASH

python -m pipeline.synth_contract.authority_scan          # exit 0

python -m unittest discover -s tests/contract -q          # exit 0
# 且无结构-only order/dedupe/agg 测残留

python -m tests.acceptance.accuracy_hardfix             # exit 0
# 原始输出整段贴交付报告；含 PASS/FAIL、差异表、分阶段耗时
# 内含：D1 基线、T1、T2、T3、真实文件、executor 未调用 is_null/compare_with_null_policy
```

---

## 8. 明确不做方向 2–9

同前版；本修订不扩大范围。

---

## 9. 请最后确认后再写码

请确认 R2：

1. §0 hash = `e54cfac4f54ebe6b842c4efa7cb7fe10365cee1f762cf7b88c599f432dc78484`  
2. 空 steps 非法 + identity 唯一表达  
3. empty_token_map 角色表  
4. 结构-only 测删除/改造  
5. `tests.acceptance.accuracy_hardfix` 一键验收 + 原始输出  
6. 真实文件回归  
7. executor 单一路径  

**你回复批准 R2 之前，不写准确性硬修业务代码。**
