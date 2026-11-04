# 跨表合并写码前 PRECHECK

状态：**只回答 Q1–Q4，不写码。** 等审。依据：当前仓库只读检查（`gridio` / `plan` / `acceptance` / `ai.client` / `ai.retry` / `block_ingest`）。

---

## Q1. `load_tables` 现状

**签名：**

```text
load_tables(path, sheet_name=None) -> list[dict]
```

定义在 `pipeline/block_plan/gridio.py`。

**输入形态：**

| 形态 | 是否支持 | 说明 |
|------|----------|------|
| 单文件 `.xlsx` / `.xls` | 是 | 唯一正式入口后缀 |
| 同文件多 sheet | 是 | `sheet_name=None` 时每个 sheet 一张表；`sheet_name` 非空时只保留该 sheet |
| 一次调用多文件 | 否 | `path` 只能是一个路径；无 `paths=[]` 之类 API |
| SQLite / `.db` | 否（本函数） | `load_tables` 不读库。SQLite 在 `pipeline/block_ingest/loader.py` 的 `load_sqlite_one_table(path, table_name)`，一次一张表 |
| 调用方拼多文件 | 惯例存在 | 验收 `acceptance/block_plan_1b` 的 C 组：对每个文件调 `load_tables`，取 `loaded[0]`，改写 `table_id` 后塞进 `plan_blocks({"tables": ...})`。这是调用方组合，不是 `load_tables` 原生多文件 |

**`table_id` / 与 parent 的关系：**

- 每个元素有 `table_id`，由本函数赋值为 `t0`、`t1`、…（按该次返回列表下标）。
- 另有 `sheet`（sheet 名）。**不用文件名当 `table_id`。**
- 多文件时若调用方不改写，两次 `load_tables` 都会从 `t0` 起编号 → 冲突；现有验收用手动改写 `table_id` 避免。

**结论（Q1）：** 同 Excel 多 sheet **支持**；多文件需调用方拼接；SQLite 走 ingest 单表 API，不是 `load_tables`。跨表验收优先形态：**同文件多 sheet**，或 **多文件由验收脚本拼 `tables[]` 并重编号 `table_id`**。不视为前置阻塞（组合路径已有先例）。

---

## Q2. `plan_blocks` 现状（补答三子问；只读代码，不猜）

**输入：** `plan_blocks({"tables": [...], "runtime": {...}})`。多表可装进同一块（`_pack` → `_make_block`）。

**块级已有字段（与「列级映射」区分）：**

- `parent_table_ids: list` — 本块涉及的表 id。
- `parent_spans: list[{table_id, row_span, col_span, repeat_header}]` — **每张父表各一块矩形**，不是「块内每个 col / 每个 field 一条归属」。
- 块顶层另有 `row_span` / `col_span`，来自 `_make_block` 的 **primary = spans[0]**（第一张父表的矩形），不是跨表统一列坐标系。
- `context.merges|fills|comments` 元素带 `table_id`（组装时按各父表矩形过滤），仍是单元格侧注，**不是 field / col_range → 表 的映射表**。
- 块上 **没有** `field_id`、没有「每列一条 `parent_table_id`」、没有 `col_to_table` 之类键。

实测（同 xlsx 两 sheet 进 plan）：一块可出现 `parent_table_ids=["t0","t1"]`，`parent_spans` 两条各自 `col_span` 都从 `0` 起（例如 t0 `[0,21)`、t1 `[0,15)`），列下标在不同父表上 **重叠且各自独立**。

---

### Q2.1 有没有「列级 parent_table_id 映射」？

**无。**

| 对象 | 是否记录「属于哪张表」 | 字段名与层级（只读所见） |
|------|------------------------|---------------------------|
| 块内每个 `col` / `col_range` | 无独立映射 | 无 |
| 块内每个 `field` / `field_id` | 无 | `block_plan` 阶段根本不产 `field_id` |
| 每张父表的矩形 | 有 | `blocks[].parent_spans[].table_id` + 同元素 `row_span` / `col_span` |
| context 合并格等 | 有单元格级 `table_id` | `blocks[].context.merges[].table_id`（及 fills/comments 同级） |

`matrix_draft.fields[]`（2b，不在 1a）有 `field_id` / `segments[].col_range`，**字段对象上无 `parent_table_id` / `table_id`**。几何草案读网格时写死取 `(block.get("parent_table_ids") or [None])[0]`（`matrix_geom.draft_matrix_block` / `dimension_candidates`）——那是实现里的 `[0]` 猜测，**不是** plan 提供的列级映射。

桥 `sources_resolved` 对 `kind=field` 回写含 `field_id` / `segments`，**不含** `table_id` / `parent_table_id`（只读 `bridge.py` 组装项）。

---

### Q2.2 复数 `parent_table_ids` 时，从 `(block_id, field_id)` 怎么解析单一 `parent_table_id`？

**解析不出来（在禁止取 `[0]`、禁止猜的前提下）。**

只读路径推演：

1. `block_id` → 块 → `parent_table_ids` 长度 ≥ 2。
2. `field_id` → 查 `matrix_draft[block_id].fields` → 得到 `segments[].col_range`，**仍无表 id**。
3. 试图用 `col` 对上 `parent_spans[].col_span`：各父表 `col_span` 均从 0 起，同一列下标可同时落在多张表的矩形内 → **无法唯一**。
4. ordered / 非 field：`sources_resolved` 的 `row`/`col` 同样缺少表 id；多亲块下同样歧义。
5. `len(parent_table_ids)==1` 时，单一值可直接取该唯一元素——这不是复数情形。

**错误码（本批次若实现 `resolve_parent_table_id` 时用，尚未写码）：** `cross_table_parent_table_unresolvable`。  
（仓库现有桥/plan **没有** 这个码；不是已存在的拒绝 reason。）

---

### Q2.3 是否前置阻塞？→ 用户拍板：走路径 1

原判「前置阻塞」成立（无列级归属 + 复数亲不可唯一解析）。用户拍板 **路径 1**，不选路径 2（不补列级 / field 级归属）。

**路径 1 的语义（本段拍板，其他 Q 段不动）：**

- `plan_blocks` 的 **token 预算切分保留不变**：超大表仍切成多块，每块属于同一张表。
- **「顺带合小表」禁用**：每块 `parent_table_ids` 长度 **恒为 1**。不再出现跨表块。
- **2b matrix 零改动**：每块产一个 `matrix_draft`，`FieldRef.parent_table_id` 天然单值。
- **桥 `sources_resolved` 零改动。**
- **跨表阶段**：每张表独立跑 1a → 2b → 桥 → 拼接，得多个 `executable`；跨表合并读多个 `executable`，用 `collect_cross_table_field_refs` 做概念归类 + 对齐 + 合并。
- **「分块整合统合」发生在跨表阶段**（`propose_synonym_candidates` / `propose_concept_bindings` 的输入本来就是多表字段摘要一起给 AI），不在 1a 合块。
- **代价明确**：API 次数从 2 次升到 N 次（N=表数）。这是 **可度量代价**；未来可用「同一张表的多块合一次 2b 调用」优化，**不是本次范围**。不写成「已知限制」。

**路径 1 明确不是：**

- 不是禁止 token 切分（超大表仍照切）
- 不是禁止同一张表切多块
- 不是改 2b matrix
- 不是改桥

**1a 唯一改动（进入 1a 最小改动批次后才写码，本 PRECHECK 阶段不写）：**  
`plan.py` 的 `_pack` / `_make_block` 加约束：每块 `parent_table_ids` 长度恒为 1。不改预算公式、不改切分逻辑、不改 1a 其他部分。回归 `acceptance/block_plan_1b` 全过；`acceptance/final_full_pipeline` 记录调用次数升到 N 的事实，不要求回退或优化。

**结论（Q2）：** 路径 1 已拍板。补完「步骤 A 前置」后停下等审；审通过 → 1a 最小改动 → 再开跨表步骤 A。

---

## 步骤 A 前置（同一张表多块；不改 MULTI_TABLE_DESIGN.md）

路径 1 下，同一张表仍可被 token 切成多块。跨表阶段处理依据如下（实现 `collect_cross_table_field_refs` / `derive_align_key_specs` 时遵守；设计已通过正文不动）：

1. **`FieldRef.parent_table_id`**  
   同一 `parent_table_id` 下，多块产出的字段共同归属该 `parent_table_id`。`block_id` 保留在 `FieldRef`（及定位信息）上，用于回源定位，不另造「块合并表 id」。

2. **`derive_align_key_specs` 的「恰好一个字段出现在 `dimensions_by_block`」**  
   以 **`(parent_table_id, block_id, subtable_id)`** 为分组键。每个子表只检查 **其自身块** 的 `dimensions_by_block`，**不跨子表并集**。同一张表被 token 切成多块时，每块独立判子表，各自出各自的 `AlignKeySpec`。设计原文规则 1 的「该子表」即此粒度；本节与设计原文对齐。

3. **`subtable_id` 不是全局唯一**  
   2b matrix 每块独立编号，`subtable_id` 从 0 起。跨表阶段所有以子表为键的索引，必须是 **`(block_id, subtable_id)`** 二元组；禁止裸 `subtable_id` 做键。`FieldRef` 的 `block_id` + `subtable_id` 两字段联合标识子表。

4. **`resolve_parent_table_id` 在路径 1 下的角色**  
   保留为边界防御函数。路径 1 落地后块 `parent_table_ids` 恒为 1，正常路径不抛 `cross_table_parent_table_unresolvable`，函数返回该唯一元素。错误码进契约 `cross_table` 错误清单，作为不可达但保留的防御码；不写成已知限制；不在正常路径加 try/except 把不可达当可达处理。1a 约束落地后，此函数实现即「返回 `parent_table_ids[0]` 且长度校验为 1」。

步骤 B 实现 `collect_cross_table_field_refs` 与 `derive_align_key_specs` 时以本节为准。步骤 A 骨架阶段只暴露签名（含 `resolve_parent_table_id`）+ docstring + `NotImplementedError`，不写解析逻辑。

---

## Q3. 验收包现状

**有没有「跨表合并」专用验收包：** 没有。`acceptance/` 下无 `cross_table/`。

**现有相关：**

| 包 | 与多表的关系 |
|----|----------------|
| `acceptance/matrix_2b/` | 单文件单 sheet，matrix / ordered 各跑，不做跨 `parent_table_id` 合并 |
| `acceptance/final_full_pipeline/` | 单文件 |
| `acceptance/block_plan_1b/` C 组 | 多文件拼进 `plan_blocks`，测的是分块/预算，不是跨表概念合并 |
| `acceptance/block_ingest_6b/` | SQLite 单表烟雾 |

**步骤 E 输入形态建议（待审确认，本 PRECHECK 不写码）：**

1. **主路径：** 同 `.xlsx` 多 sheet → `load_tables(path)` → 多 `table_id`。
2. **备路径：** 多文件 → 验收脚本多次 `load_tables` + 重编号 `table_id`（对齐 1b C）。
3. **SQLite：** 仅当明确要用 ingest；一次一表，多表需多次 `load_sqlite_one_table` 再拼——**本批次若未拍板，不做 SQLite 跨表验收。**

**结论（Q3）：** 缺专用包，步骤 E 新建 `acceptance/cross_table/`。输入形态依赖 Q1：**推荐同文件多 sheet；多文件用脚本拼接。** 不阻塞开始步骤 A（骨架），但步骤 E 前需你确认形态二选一或两者都要。

---

## Q4. AI 客户端现状

**`ai.client.chat_completion`：**

- 参数 `enable_retry: bool = True`。
- `enable_retry=False` → 只走 `_chat_completion_once`，**无** `run_with_retry`（无 429 退避、无空正文降 token 重试）。
- `enable_retry=True` → `ai.retry.run_with_retry`：受 `api.max_retry_seconds` 墙钟、空正文 `api.empty_retry_max_tokens` 等约束。这是 **传输/速率** 重试，不是「提案结构 needs_retry」重试。

**与 D2 对齐：**

| 层次 | 谁控制 | 现状 |
|------|--------|------|
| 提案结构重试（`needs_retry.concept/synonym`、别名 invalid） | 跨表调用方外层 | 客户端 **没有** 这类逻辑 → 符合「函数本身不读配置、不内部提案重试」 |
| HTTP/429/空正文 | `enable_retry` | 可关可开 |

**Q4 已拍板：** 保留 `enable_retry=True`。不改 `ai/client.py` / `ai/retry.py`。本批次只传参。

- `propose_*` 每次对 client **一次**调用；外层按 D2 累计 `proposal_max_calls_per_kind`。
- **不**在 `propose_*` 函数体内读 `proposal_max_calls_per_kind`。

**结论（Q4）：** **通过。** 不报前置阻塞。

---

## 总判

| 项 | 状态 | 说明 |
|----|------|------|
| Q1 | 通过 | 多 sheet 原生；多文件靠拼接 |
| Q2 | **路径 1 已拍板** | 禁用顺带合小表（每块 `parent_table_ids` 长度恒为 1）；保留同表 token 切分；2b/桥零改；跨表阶段整合；API 次数 N=表数（可度量代价） |
| Q3 | 暂缓 | 步骤 E 素材与输入形态待用户拍板；不阻塞 1a 最小改动与跨表 A–D |
| Q4 | 通过 | `enable_retry=True` 已拍板；只传参 |

**当前动作：** PRECHECK 已补 Q2.3 +「步骤 A 前置」。不写码。不开 1a、不开步骤 A。等你审本文后，再进入「1a 只加约束」批次。

---

停下报审。不写码。
