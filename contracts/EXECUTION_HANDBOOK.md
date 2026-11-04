# FileMate 执行手册（强制）

本文件是每批设计确认的**唯一权威上下文**。  
每批设计确认文档第一节必须「上下文复核」：逐条复述与本批相关的条目，并声明已读 handbook hash。  
复述不出或 hash 对不上 → **本批作废，不得写码**。

---

## 1. 三条裁定

### 裁定 1 — opt-in 分层强制

| 层 | 规则 | 对象 |
|----|------|------|
| **强制层** | 未声明 → `contract_incomplete`，拒绝执行 | `row_accounting`；输出格的 `deriv_edge`；字段参与比较/聚合时的 `empty_token_map`；join/对账/去重时的 `key_norm_id`；**有预算声明时**的 budget 时序/`actual` 数据 |
| **opt-in 层** | 声明才查；未声明不发明、不拒绝 | `entity_reg` / `xref` / `caliber` / `delivery_blocks` |
| **暂不强制** | 本阶段不因缺失拒绝 | `review_anchor_mode` 等复核层 |

方向 4–6 落地时地基依赖本分层。强制层条目随相关批次接入，不得一刀切「全声明」或「全 opt-in」。

### 裁定 2 — 无法落地条目三类

| 类 | 内容 | 排期 |
|----|------|------|
| 真不改架构落不了 | 方向 2 / 3 / 5 / 6 / 7 / 8 | 各方向单独设计确认后再写码 |
| 可渐进 | 方向 4（先静默+摘要+抽样，一键证据后做）；方向 9（先框架与规则，用例逐步补） | 各方向设计确认 |
| **必须修、不能等架构** | ① join 运行时按 `key_norm` 比较 ② order/dedupe/agg **真执行** ③ 空值三分类挂门禁 | **方向 1 完成 → 准确性硬修批 → 再进方向 2** |

### 裁定 3 — 方向 1 已收窄目标（已完成，不可回退）

1. 删除 `FILEMATE_CONTRACT_GATE` 环境变量语义；无契约库 → 启动失败（`ContractLibraryError`，由调用方转非 0）  
2. 删除旧路径残骸（`fast_plan`、死代码等）  
3. **一份**权威扫描 `pipeline.synth_contract.authority_scan`，规则版本化在契约 `authority_scan` 块  
4. 无任何运行时开关可回到旧启发式执行  

---

## 2. 两条铁律（每一批页眉默认成立）

1. **每批只动该方向相关文件**；不新增 `purge*.py` / 根目录临时扫描分叉堆积。允许的新生产文件仅该批设计确认写明的模块。  
2. **不接受**：本地兜底让用例过；保留逃生门；接口出现业务字段名；用 confidence 做分流；模板化问法。

---

## 3. 方向 1 之后固定队列

```text
方向 1（已完成）清场 + 启动强校验
    ↓
准确性硬修批（三项，不扩不缩）：
    ① join 运行时 key_norm
    ② order / dedupe / agg 真执行
    ③ 空值三分类挂门禁
    ↓
方向 2 设计确认 → 写码 → …
方向 3 → 4（渐进）→ 5 → 6 → 7 → 8 → 9（渐进）
```

准确性硬修与强制层中与这三项重叠的部分同批落地；强制层其余条目（如 `row_accounting` 全面强制）可同批或紧随，但**不得借机做方向 2–9**。

---

## 4. 每批「允许改 / 不允许改」边界模板

复制到每批设计确认，填空：

```text
本批名称：________
本批目标（一句话）：________

允许改：
- 文件：…
- 符号：…
- 契约：仅当设计确认写明（默认：不改业务枚举；authority_scan 规则可补）

不允许改：
- 方向 2–9 未批准部分
- executor/accuracy/… 中非本批符号（若本批未点名）
- 用「调测试断言」迎合现实现
- 本地兜底 / 逃生门 / 业务字段名上接口 / confidence 分流 / 模板化问法

本批验收命令（可机械执行）：
- …
```

---

## 5. 每批页眉必写的「上下文复核」格式

设计确认文档**第一节**必须原样包含下列块（填空）：

```text
## 0. 上下文复核

我读过的 handbook hash = <HANDBOOK_HASH 文件全文，小写 hex>

与本批相关的手册条目复述：
1. 裁定 1（若相关）：…
2. 裁定 2（若相关）：…
3. 裁定 3 / 方向 1 不可回退（若相关）：…
4. 两条铁律：…
5. 固定队列中本批位置：…
6. 本批允许改 / 不允许改（按模板）：…

声明：以上复述与 contracts/EXECUTION_HANDBOOK.md 一致；hash 与 contracts/HANDBOOK_HASH 一致。
若无法复述或 hash 不符，本批作废。
```

---

## 6. 附加硬约束（跨批仍成立）

- **A** `ensure_contract_library` 抛 `ContractLibraryError`，库内不 `sys.exit`  
- **B** `reject_payload` 与 `execute_mechanical` 失败返回形状一致  
- **C** 涉及执行路径的批必须有真实端到端回归，不得仅靠单测数量  
- **D** boot 不在每次 `execute_plan` 重复读盘；运行中用计划 `contract_version`+`contract_hash`  
- **E** 清场批不扩业务语义；发现「死链其实被间接调用」→ 停下来报告，不顺手修越界项  
- **F** 从本批起，任何涉及执行的批次，验收包必须包含 plan dump（文件名以 `plan.json` 结尾，如 `real_table_plan.json`）。该文件列入 `outputs.sha256`，并由 rerun 复算验证。没有 plan dump 的验收包视为不完整。  

测试变红时必须逐条判定：**代码错 / 测试错 / 修哪边**；禁止改断言迎合坏实现。

---

## 7. key_norm / empty_token_map 机械语义（准确性硬修起生效）

### 7.1 `key_norm_id` 的 `steps` — 禁止默认可空

- `norm_spec.steps` **必须非空**（长度 ≥ 1）。空列表 / 缺省 / `null` → `contract_incomplete`。  
- **不允许**「无 steps 表示 identity」。  
- **Identity join** 的唯一合法表达：`steps` 恰好为一项 `{"op": "identity"}`（写码阶段把 `identity` 登记进契约 `norm_ops`；重放：`norm_value = raw` 原样字符串化，无 trim/lower/本地规范化）。  
- 两侧 join 的 `key_norm_id` 所解析出的 `steps` 必须逐项相等（含 `identity`）。  

### 7.2 「参与比较/聚合的字段」与 `empty_token_map` 需求表

| 角色 ID | 含义 | 需要 empty_token_map？ |
|---------|------|------------------------|
| `join_key` | join 左右键列 | **是** |
| `dedupe_key` | dedupe 键列 | **是** |
| `agg_group` | 聚合分组列 | **是** |
| `agg_input` | 聚合输入度量列 | **是** |
| `order_key` | order_spec.keys 中的列 | **是** |
| `filter_eq` | keep_row_when / filter op∈{eq,neq} 的列 | **是** |
| `filter_in` | filter 多值成员（contains_any/contains_all 等成员比较）的列 | **是** |
| `order_tie_break` | tie_break | **否**，当且仅当值为唯一 `cell_coord` 或 `input_row_id`；若误用 catalog 列当 tie_break → **是**（并视为契约错误倾向，应改用坐标/行号） |

扫描计划时按上表枚举角色；任一「是」且无 map → `contract_incomplete`。写码时不得「看情况」。

### 7.3 比较路径单一性

- executor 键/比较路径**只走**：`classify_empty` / `compare_empty` + `norm_spec` 重放。  
- `normalize.is_null` / `compare_with_null_policy`：**executor 不得调用**；保留仅用于规格校验或标 deprecated，验收须断言调用图为零。  

### 7.4 `map_id` 与 `shared_map_id`

- 执行器读取的是该条目 `map_id` 上的 `mapping` / `candidates`。  
- `shared_map_id` 只做两件事：溯源（指向另一条 map），以及一致性报告。它不替换本条目的 `mapping`。  
- 被指向的 map 后来改了 `mapping`，本条目自己的 `mapping` 保持不变。  
- 两边 `mapping` 不一致时，运行时在 `plan_notes` 写 `shared_map_mismatch map_id=… shared_map_id=…`，执行仍用本条目自己的 mapping，不因此改写内容。  
- `shared_map_id` 指向不存在的 `map_id` → `contract_incomplete`（溯源断裂）。  
- `authority_scan` 不读计划数据，这份比对在运行时做。  

### 7.5 `agg_semantics` 动作 `fail`（v1.0.0 起）

- `agg_semantics[op][empty_class] = fail` 一旦触发，**整份 plan** 进入 `contract_incomplete`（或执行路径上等价拒绝态），输出零行。  
- **不是**「该组局部失败、其他组继续」。局部失败若需要，须后续版本扩展后再实现。  

### 7.6 参数对象引用规则（v1.1.0 起）

- 计划执行体为 `ops[]`。每个元素：`op` ∈ 契约 `operator_set`，以及该 op 唯一的 `param_ref` 键（如 `join_ref`），值为参数对象主键。  
- **被 `ops[]` 直接引用**的对象数组：`joins` / `filters` / `derives` / `value_maps` / `order_specs` / `dedupe_specs` / `agg_specs` / `project_specs`。主键字段一律为 **`id`**。`ops[i].<param_ref>` 必须等于其中某项的 `id`。  
- **不被 `ops[]` 直接引用**、由其他参数对象引用的规格：`calc_specs` 主键 **`calc_id`**；`norm_specs` 主键 **`norm_id`**。  
- `dedupe_specs` / `agg_specs` 引用排序规格时，字段名为 **`order_id`**，指向 `order_specs[].id`。禁止在 `order_specs` / `dedupe_specs` / `agg_specs` 上再写冗余的 `order_spec_id`。  
- **不引入**计划字段 `column_origins`。非 join 角色的 `source_table` 在 dump/执行解析中默认为 `primary_table`；join 左右侧由 join 参数对象的表字段决定。  
- dump 字段 `empty_role_coverage`（验收 plan dump 必需）字段名与语义与硬修包 `acceptance/accuracy_hardfix_20261018/real_table_plan.json` 一致：`role` / `side` / `field` / `source_table` / `map_id` / `shared_map_id` / `has_empty_token_map`。不改名、不改语义。  

### 7.7 固定执行顺序（v1.1.0 起）

- `ops[]` 只声明参与哪些操作；执行器按契约 `fixed_op_order` 执行（默认：`join → filter → derive → map_values → order → dedupe → aggregate → project`）。  
- `ops[]` 书写顺序不改变执行序。  

---

## 8. Handbook 自身变更规则

- 修改本文件 → 必须重算 SHA-256 → 更新 `contracts/HANDBOOK_HASH`（单行小写 hex，无空格/注释，仅允许末尾一个换行）  
- 手册变更本身算独立「手册批」，需用户确认后再改队列或裁定  
