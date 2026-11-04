# 多表合并（跨 parent_table_id）

状态：**设计待拍板，不写码。** 独立于 2b matrix。不改 `pipeline/block_analyze/` 实现，不改 R2 的 `(parent_table_id, …)` 隔离，不改 `contracts/versions/v*.json` 已有键（只允许新增）。

本次交付包含：函数签名 **21** 个、数据结构 **12** 个、契约新增字段 **15** 个、配置新增项 **6** 个。

阶段位置：2b matrix 已交出单表 `executable` / `subtables` / `dimensions_by_block` / `matrix_draft` 之后，复核之前。本阶段只读那些产物，另写跨表产物。R2 键保持原样。

下文「设计意图」里的具体说法只说明意图，不进代码、不进契约、不进配置。函数签名、数据结构、契约、配置只用抽象标记：`CONCEPT_A`、`FIELD_SET_B`、`DIMENSION_D`、`ROLE_R`、`TABLE_P`、`TABLE_Q`。

## 铁律确认

上游 2b 的 `value_samples` 条数是代码常量，不是配置、也不是契约。产生位置在几何草案函数内写死截断，没有 key 路径。

这是上游 2b 的硬编码，已登记为 2b 回炉批次待修项；跨表阶段不得以任何方式依赖该条数做判断，不得以「样本太少 / 太多」为由分流或重问。也不得把条数写进配置、契约或本阶段代码常量。`value_samples` 只原样放进字段摘要，当作运行时输入。条数变化不得改变精确分组、近义是否调用、概念是否调用、对齐键、输出形态或失败行处置。

### 交付说明（交接待办）

- **2b 回炉批次：** 把 `value_samples` 条数改为配置项（本阶段不实现、不预留该配置键）。

---

## 设计意图（仅说明意图，不进代码、不进契约、不进配置）

当前 R2 用 `(parent_table_id, source_col)` / `(parent_table_id, field_id)` 隔离。不同表的同名列会各出一列；近义词不合并。这不是 2b 的缺陷，是隔离设计。用户要的一对多 / 多对一关联在本阶段解决。

### M1. 触发条件（原文）

用户原话提到两张表关系才合并，还是默认尝试，还是每次问？

**初步倾向（待拍板）：** 默认不合并；当两张表出现同名或近义字段时，经 2a 人机提示「是否合并？」；若用户原话已明确写「按 XX 关联 / 合并两张表」，可自动进入合并候选，仍须确认近义与对齐。

**补齐：** 设计按推荐项写死为 `trigger_mode=ask_on_candidate`。无候选则本阶段空操作，不调用模型。有候选则一律进人机，未确认不得合并。用户原话若包含关系陈述，只把对应候选的 `recommended_reply` 标成 `confirm`，仍要一次确认。不存在「读到原话就直接合并」的路径。

### M2. 合并键（原文）

- 字符完全相等 → 可自动列为合并候选（仍建议用户确认一次，见 M6）。
- 近义词 → AI 判断 + 用户确认。
- 完全不相关的词 → 不合。

**待定原文：** 相等判定是 `target_name` 相等，还是源表头原文相等，还是二者之一？规范化（去空格、全半角）规则？

**补齐：** `equality_basis=either`。规范化后的 `target_name` 相等，或规范化后的 `source_label` 相等，都进入精确候选；两组不自动并成一个候选，各自带 `matched_on`。`source_label` 为空串时不参与 `source_label` 分支（只用 `target_name`），见补丁 2。规范化只有三步且顺序固定：去首尾空白、大小写折叠、全半角折叠。空串不相等。近义不走这一步。不相关：精确步无组，且近义步没有把它们放进同一候选，则保持隔离。

### M3. 行对齐（原文）

两张表同名列精确匹配（字符串相等）可对齐；别名（如短名与长名）需要 AI 对齐。

**待定原文：** 对齐结果是写进 alignments，还是独立的 `join_key` / `row_links`？一对多时输出行如何膨胀？匹配失败的行是保留空侧还是丢弃？

**补齐：** 不写入既有 `alignments`（那是块间列槽）。对齐键见 M8 `derive_align_key_specs`。值链接写入新结构 `row_links[]`。一对多膨胀与失败行见 M8，不在本条另定。

### M4. 合并后输出（原文）

一张宽表 / 一张长表 / 多张长表（按来源分开）。

**初步倾向原文：** matrix 子表已是长表；跨表合并后默认仍长表，并带 `source_table_id`。

**补齐：** 该倾向作废。输出形态不默认长表，由 M8 `resolve_output_grain` 按字段角色决定。`source_table_id` 只在 `output_grain=per_source` 时作为分表标识，不是每行强制列。

### M5. 近义词裁决（原文）

AI 输出「两列可能是同一字段」→ 走 2a 人机中断问用户；用户回答后合并或保留。裁决记录落盘。

**待定原文：** 落盘格式与是否可在复核后改口重拼。

**补齐：** 落盘为 `DecisionLog`（sidecar），契约用 `cross_table.decision_status` 描述单条状态。复核阶段不得改裁决、不得重拼。改口只允许在拼接前，对同一 `question_id` 再答一次；答后整条决策链从日志重放。拼接已发生后再改口：拒绝本次重放，要求新指纹重开阶段，不回写已交付表。

### M6. 防误合并（原文）

默认不合并；提示用户选择。不自作主张合并近义词。

**硬约束：** 无 `decision_status=confirmed` 不得改写 R2 键，不得把不同 `parent_table_id` 收成同一 R2 列。本阶段新产品与 R2 输出并列，不替换 R2 键。

---

## M7. 语义概念归类

用户原话里的上位概念（不是列名、不是表名）要映射到跨表字段集合。

分工（无 confidence 分流）：

| 步骤 | 谁做 | 读什么 |
|------|------|--------|
| 收集字段摘要 | 本地 `collect_cross_table_field_refs` | executable、matrix_draft、dimensions_by_block |
| 精确同名候选 | 本地 `group_exact_name_matches` | 摘要 + 契约 `equality_basis` |
| 近义候选 | AI `propose_synonym_candidates` | 摘要；不读值的全列。调用次数见配置 `proposal_max_calls_per_kind` |
| 上位概念候选 | AI `propose_concept_bindings` | 用户原话 + 字段摘要。调用次数同上 |
| 提案是否可问人 | 本地 `validate_ai_proposal` | 提案结构，不看分数 |
| 人机 | 本地 `on_cross_table_interrupt` | 新问题通道，不复用既有 `questions.scope` |
| 落盘 | 本地 `write_decision_log` | 确认后的绑定 |

禁止概念词表。模型不得从配置或契约读到一份「概念 → 列」清单；配置里没有这类列表。

模糊边界（成员为空、同一 `FieldRef` 落入两个概念、提案缺必填、`proposal_status=ambiguous`）一律 `interrupt_kind=concept_bind`，不自动取第一个概念，不自动丢弃。

### M7 开放项（设计按推荐写死，改选只改枚举默认）

**输入。** 选项：只给 `target_name` 清单 / 只给角色清单 / 只给值样本 / 只给字段摘要 / 组合。

推荐：**字段摘要组合**。摘要含 `field_id`、`role`、`target_name`、`source_label`、`source_labels`、`parent_table_id`、`subtable_id`、`value_samples`（沿用字段上已有样本，本阶段不另截断）、以及该字段是否在 `dimensions_by_block` 里。给 AI 时同时给 `source_labels` 与 `source_label`，不选、不截断。理由：只给名字无法知道它是维度还是属性；只给角色无法对应原话里的概念；只给样本无法绑定字段身份。值样本只作摘要里的已有字段，不单独当输入。

**裁决存哪。** 选项：仅 session sidecar / 仅 executable 新字段 / 独立 `concept_bindings[]` / 仅契约新块。

推荐：**契约描述 `ConceptBinding`，实体落在 sidecar 的 `concept_bindings[]`，executable 只新增可选引用 `concept_binding_ids`。** 理由：只放 executable 会在重拼时丢掉提案原文；只放契约没有实例；只放 sidecar 则重跑没有稳定类型。引用不改 R2 键。

**人机钩子。** 不扩展既有 `scope`（`block|mapping|value_domain`）。新钩子 `on_cross_table_interrupt`。问题结构见数据结构 `CrossTableQuestion`。

---

## M8. 跨表关系保持

归类确认之后：

| 步骤 | 谁做 |
|------|------|
| 从角色推导对齐键 | 本地 `derive_align_key_specs` |
| 对齐键上的值精确相等 | 本地 |
| 对齐键上的非相等别名 | AI `propose_value_alias_links`，再人机。调用次数见 `proposal_max_calls_per_kind` |
| 一对多膨胀、失败行 | 本地 `expand_matched_rows` |
| 输出形态 | 本地 `resolve_output_grain` |
| 写出 | 本地 `project_cross_table_outputs` |

推导规则（本地，不调用模型）：

1. 某 `parent_table_id` 上，若该概念成员里恰好有一个字段出现在该子表的 `dimensions_by_block` 中，它就是该表的对齐键。
2. 若没有维度成员：该表上角色为 list 的成员恰好一个 → 它是对齐键。list 成员多于一个 → `cross_table_align_key_ambiguous`，`interrupt_kind=align_key_pick`，不取下标最小者。
3. 只有 group / attribute、没有 list、也没有维度成员 → 这些成员不是对齐键。`interrupt_kind=align_key_pick`。本地不指定其中一个。
4. 别名提案只允许发生在已确认的对齐键上。属性列不得当对齐键，除非用户在 `align_key_pick` 里点名该 `field_id`。

ordered 表的 `FieldRef.role` 为 `column`，且 `dimensions_by_block` 对其为空，因此按规则 1/2/3 通常落到 `status=ambiguous`，走 `interrupt_kind=align_key_pick` 人机。此行为是预期，不是缺陷。禁止为 ordered 表设默认对齐键（如取首列、取下标最小列、取字符最短列），禁止在推导函数内对 ordered 表放宽规则。

一对多：对齐键值在 `TABLE_P` 命中 1 行、在 `TABLE_Q` 命中 N 行 → 输出 N 行，1 侧复制。`cardinality=one_to_many`。不设行数上限。多对多同样按命中笛卡尔展开，并记 `cardinality=many_to_many`。零命中不展开进主结果。

输出形态由 `resolve_output_grain` 决定，不默认长表：

- 概念成员都在同一个 `parent_table_id`：本阶段不产出跨表表，`output_grain=noop`。
- 用户回复把该概念标成按来源保留：`output_grain=per_source`，每张来源表各一份，不合并行。
- 概念跨表，且每张表都有对齐键，并且这些键不与另一个已确认概念共用：`output_grain=per_concept`，一个概念一份输出。
- 两个已确认概念共用同一 `(parent_table_id, field_id)` 对齐键（B4）：`output_grain=shared_key`，合成一份输出。
- 宽或长：没有任何一侧已带 `cross_table.reserved_long_columns` 中的任一列，且非键成员全是 group 或 attribute，且对齐键角色是 list 或 column → `row_shape=wide`（一成员一列）。任一侧已带该列表中的任一列，或两侧一个宽一个长 → `row_shape=long`。`long` 时：非键成员若 `role` 是 list 或 column，该成员保留原列，不熔成属性对；`role` 是 group 或 attribute 的成员才熔成属性对。属性对的列名只取契约 `cross_table.reserved_long_columns`，函数内不写列名字面量。模型不选择 wide/long。不为此新增人机问题。

匹配失败行：设计按 `unmatched_disposition=retain_unmatched`。失败行留在所属概念输出的末段，对侧单元格为空，并写入 `unmatched_rows[]`。不丢弃（丢弃不可复现谁被删）。不进入桥的 `rejected`（那是定位失败，不是对不齐）。

`output_grain=noop` 不产出 `CrossTableOutput`。下游区分见 B5。

---

## B1–B14 规则

### B1. ordered 字段全集

字段全集只从 `executable` 取，不从 `mappings`，不从 `block_plan`。

理由：`mappings` 含桥未接受的条；`block_plan` 只有块几何，没有字段身份。`executable` 是已接受、带 `target_name` 的集合。

桥 `rejected` 的字段不算入全集。没有 `target_name` 的条不得进入跨表候选。

ordered 的 `role` 固定为 `column`。`target_name` 取该 executable 项的 `target_name`。`source_labels`：遍历该字段全部 `sources_resolved`，收录非空 `cell_text`，去重，按源行升序、同行内源列升序；仅当绑定是列题（源行是表头行，或 `kind=column`）时收录，否则该 source 不贡献。`source_label`：仅当 `source_labels` 长度恰为 1 时取该唯一值，否则为空串。禁止取下标 0、取最短、取最长。没有 `field_id` 时，身份用结构串 `col:{parent_table_id}:{col}`，不使用单元格原文当 id。`in_dimensions=false`。`value_samples=[]`（ordered 草案不产样本；不另截一段值来充数）。

### B2. align_key_pick 答复

不新增 reply 枚举。仍是 `confirm` / `reject` / `defer`。

新增 `selected_field_id: str | null`，写在 `UserReplyRecord`，并由 `apply_cross_table_reply` 接收。

关系：

- `interrupt_kind=align_key_pick` 且 `reply=confirm`：`selected_field_id` 必填，且必须属于该问题的 `field_refs`。返回的 `AlignKeySpec.status=resolved`，`rule=user_picked`，`field_id=selected_field_id`。
- `reply=reject`：`selected_field_id` 必须为 null。该表没有对齐键，概念 `decision_status=rejected`，不进入拼接。
- `reply=defer`：`selected_field_id` 必须为 null。`decision_status=deferred`，不进入拼接。
- 其他 `interrupt_kind`：`selected_field_id` 必须为 null，忽略以外的值视为 `cross_table_reply_invalid`。

### B3. keep_sources

不新增 `interrupt_kind`。不放进 `concept_bind` 的二级答复。

用已有 `interrupt_kind=output_grain`。概念已 `confirmed` 且成员的 `parent_table_id` 不少于 2 时，本地生成这一问（不调用模型）。

问句字段仍是 `CrossTableQuestion`。`if_holds` 固定为动作名 `set_keep_sources`。

答：

- `confirm` → `ConceptBinding.keep_sources=true` → `resolve_output_grain` 对该概念给出 `output_grain=per_source`。
- `reject` → `keep_sources=false` → 不走 `per_source`，继续 B4 与其余形态规则（`per_concept` 或 `shared_key`）。
- `defer` → `decision_status=deferred`，`keep_sources` 保持 false，不投影。

### B4. shared_key 判定

判定对象是 `(parent_table_id, field_id)` 二元组，不是单独的 `field_id`，也不是值集合。

函数是独立本地 helper `concepts_share_align_key`，由 `resolve_output_grain` 调用，不写在该函数体内联一套第二套规则。

规则：两个已 `confirmed` 的概念，各自取 `status=resolved` 的对齐键集合。两集合的 `(parent_table_id, field_id)` 交集非空 → 共用，`output_grain=shared_key`。交集为空 → 不共用。

### B5. noop 不产出

`output_grain=noop` 不生成 `CrossTableOutput`，不生成 `output_id`。

`project_cross_table_outputs` 只返回 `output_grain!=noop` 的项。返回空列表时：

- `DecisionLog.grain` 为空，且没有 `confirmed` 概念 → 无概念。
- `DecisionLog.grain` 中每条都是 `noop` → 有概念但 noop。

调用方必须读 `grain`，不得把空列表单独理解成无概念。

### B6. 精确链接

独立函数 `derive_exact_links`（本地，不在 `expand_matched_rows` 内部生成）。`expand_matched_rows` 仍接收 `exact_links`，调用方传入该函数的返回值。

见函数 18。精确对不进人机。别名仍由函数 11 提出再确认。

### B7. long 保留列的列名冲突

冲突范围：同一份 `CrossTableOutput` 内。跨 `parent_table_id` 也算，同一 `parent_table_id` 内重名也算。与 `reserved_long_columns` 中任一字符串相同也算冲突。

处理：不走人机，不单写一条冲突日志就停。保留列的输出列名改为 `{parent_table_id}:{原 target_name}`。前缀只许用 `parent_table_id` 这种结构标识，不许用业务词，不许用 `concept_id`（同一来源表在一份输出里应只有一个前缀）。熔出的属性对列名保持契约列表原文，不加前缀。

下游识别：`CrossTableOutput.column_origins[]`。每列一条：`column`（写入 `columns` 的最终名）、`parent_table_id`、`field_id`、`melted`（bool，属性对为 true）。复核与下游只读 `column_origins`，不从列名反推来源。

### B8. fingerprint 不含 value_samples

不含。

理由：样本条数是上游硬编码，放进指纹会让回炉改条数之后全部重放失效，而字段身份并没变。

风险：样本变了仍重放旧的 `ai_raw` 与用户确认。接受该风险，因为确认针对的是当时的字段身份与原话；样本不是裁决键。指纹含：`parent_table_id`、`field_id`、`role`、规范化后的 `target_name`、规范化后的 `source_label`、规范化后的 `source_labels`（按列表顺序拼接）、用户原话、契约版本。以上任一变化则指纹不同，拒绝重放。不含 `value_samples`。

### B9. concept_binding_ids 回写

浅拷贝，不原地写。

本地函数 `copy_executable_with_bindings`：新列表。每个 executable 项拷贝全部顶层键（浅拷贝值）。`sources_resolved` 换成新列表，元素仍是原 source 对象，不往 source 上加键。只在新项顶层写 `concept_binding_ids`。未参与任何已确认概念的项，该字段为 `[]`。

进入前的 executable 对象保持不变。本阶段结束时调用方改用这份新列表。R2 键集合必须与进入前相同。

### B10. R2 快照

快照函数 `snapshot_r2_keys(executable, block_plan) -> list[list[str]]`。不读 `subtables[].columns`（那是熔列之后的显示名，不是 R2 键）。

每个 source 一条二元组，排序后作为快照：有 `field_id` 则为 `[parent_table_id, "field:"+field_id]`，否则 `[parent_table_id, "col:"+col]`。`parent_table_id` 从 `block_plan` 用 `block_id` 查。这是只读投影，不改 R2 实现。

`before_keys`：本阶段第一个函数运行之前，对输入 executable 取一次。`after_keys`：`copy_executable_with_bindings` 与 `project_cross_table_outputs` 都结束之后，对新 executable 再取一次。

断言失败：丢弃本次 `CrossTableOutput`、丢弃本次若已写出的决策日志文件、丢弃新 executable。保留进入前的 executable、既有 `subtables`、以及本次之前已经存在的日志文件。

### B11. key_values 顺序

按源表行序，不是字典序，也不是集合遍历序。

定义：对该对齐键的 `col_range`，按 `row` 升序、同一行内按 `col` 升序扫描网格。非空单元格原文第一次出现则追加。重复值丢弃。空串不进入列表。

理由：行序与列序来自已经载入的表，不依赖语言环境的排序，两次扫描结果相同。

### B12. 部分重叠

成员集合的元素是 `(parent_table_id, field_id)`。

部分重叠：候选集合与某个精确组集合的交集非空，且两集合不相等。有一个二元组相同即算。交集为空 → 不是重叠。两集合相等 → 不是部分重叠，而是重复精确组，同样 `proposal_status=invalid`（不把精确组再当近义）。

prompt 约束字段（不给业务例子），放进 `propose_synonym_candidates` 的输入说明，键名 `member_set_rule`，正文固定为：

`Each candidate member set is pairs of parent_table_id and field_id. The set must be disjoint from every exact group. A non-empty intersection is partial overlap. Equality with an exact group is also invalid. One shared pair is enough.`

### B13. per_source 的 output_id

稳定公式，写码时照此实现，此处不给代码：

`sha256_hex(utf8("concept_ids=" + join(sorted(concept_ids), ",") + "|parent_table_id=" + (parent_table_id or "") + "|output_grain=" + output_grain + "|row_shape=" + row_shape))`

`per_source` 的 `parent_table_id` 必填，因此同一概念的两张来源表得到不同 id。`shared_key` 的 `concept_ids` 是被合并概念 id 的升序。`noop` 没有 id（B5 不产出）。

### B14. 无跨表候选不写日志

无候选的判定：`field_refs` 里不同的 `parent_table_id` 少于 2。与精确组是否为空无关。两张表以上但精确组为空，仍要走近义与概念，不是无候选。

无候选：不调用模型，不写 `DecisionLog`。

审计理由：没有裁决可重放。写一份空日志会和「写失败」无法区分；不写文件则下次仍走同一空操作。有两张表以上时，即使用户全部 `reject`，也写完整日志（各组状态为 `rejected`，`ai_raws` 保留）。

---

## 2.1 函数签名

类型用 JSON 风格。不设业务默认值。每个函数标明 AI 或本地。

### 1. `collect_cross_table_field_refs`（本地）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| executable | `list[ExecutableItem]` | 桥已接受的映射 |
| matrix_draft | `dict[block_id, MatrixDraft]` | 单表字段草案；ordered 块可无草稿 |
| dimensions_by_block | `dict[block_id, dict[subtable_id, list[field_id]]]` | 已确认维度 |
| block_plan | `BlockPlan` | 提供 `parent_table_id` |

返回：`list[FieldRef]`。无跨表字段时返回空列表，不抛错。不调用模型。

全集只来自 `executable`（B1）。桥 `rejected` 不进入。ordered / matrix 均按补丁 2 填 `source_labels` 与 `source_label`（禁止硬取下标 0）。`value_samples` 原样拷贝，不按条数截断、不按条数分流。

拒绝：`executable` 不是数组 → `cross_table_input_invalid`，不调用模型，不写日志。

### 2. `normalize_match_text`（本地）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| text | `str` | 待比较文本 |
| ops | `list[Literal["trim","casefold","width_fold"]]` | 规范化步骤，顺序即执行顺序 |

返回：`str`。空输入返回空串。不调用模型。无抛错。

只执行 `ops` 里的步骤。禁止在本函数或任何调用方对文本做额外预处理（去标点、去数字、截断等）。扩展方式：新增规范化步骤只能走契约新版本新增枚举值；禁止在 `normalize_match_text` 或任何调用方内硬编码额外步骤。当前默认三步即全部可用值。

### 3. `group_exact_name_matches`（本地）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| field_refs | `list[FieldRef]` | 摘要 |
| equality_basis | `Literal["target_name","source_label","either"]` | 契约项 |
| ops | `list[Literal["trim","casefold","width_fold"]]` | 同函数 2 |

返回：`list[ExactMatchGroup]`。只保留成员 `parent_table_id` 不少于 2 的组。空串不进组。`equality_basis` 含 `source_label` 或 `either` 时：`source_label` 为空串的字段不进入 `source_label` 分支（`either` 下仍可只靠 `target_name` 成组）。不调用模型。

拒绝：`equality_basis` 不在枚举 → `cross_table_contract_invalid`，返回前不产生组。

### 4. `propose_synonym_candidates`（AI）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| field_refs | `list[FieldRef]` | 摘要，不含全列值 |
| exact_groups | `list[ExactMatchGroup]` | 已占用的精确组。约束见 B12 |
| user_utterance | `str` | 用户原话 |
| member_set_rule | `str` | prompt 固定句，正文即 B12 的英文约束。不是词表 |

返回：`list[SynonymCandidate]`。本函数每次调用只调模型一次，不内部重试，不读配置。重试由调用方按 D2 顺序控制：`propose_*` → `validate_ai_proposal` → 仅当 `needs_retry.synonym=true` 且累计次数未超 `proposal_max_calls_per_kind` 时再调本函数。禁止因模型自述不确定或任何分数触发重试。总调用仍受既有 wall time 与 token 预算约束。

拒绝：引用未知 `field_id`、与精确组部分重叠或集合相等（B12）→ 由 `validate_ai_proposal` 标为结构不完整（`needs_retry.synonym`）。调用方耗尽上限仍无效 → `cross_table_proposal_invalid`。不改用精确组顶上。

### 5. propose_join_hypotheses（AI）

输入：user_utterance, field_refs, exact_groups, value_samples_by_field
输出：list[JoinHypothesis]

JoinHypothesis = {
  hypothesis_id: str,
  left: {parent_table_id, block_id, field_id},
  right: {parent_table_id, block_id, field_id},
  join_type: "left" | "inner" | "full",
  rationale: str
}

约束（全部由 AI 自己负责，本地不强制）：
- AI 应提出所有合理的关联假设，并按其自身的合理性排序。
- 允许返回 0 条（明确表示「找不到合理关联」）。
- rationale 是给用户和审计看的解释，不是结构性证据；本地不校验内容，只校验非空。
- 每次调用只调模型一次，不内部重试，不读配置。

### 5b. rank_hypotheses（本地）

输入：list[JoinHypothesis], value_samples_by_field, top_n（从契约读）
输出：list[RankedHypothesis]（附加 coverage_ratio）

对每条 hypothesis：
- left_vals = set(value_samples_by_field[left.field_id])
- right_vals = set(value_samples_by_field[right.field_id])
- common = len(left_vals & right_vals)
- coverage_ratio = common / max(len(left_vals), 1)
- 按 coverage_ratio 降序排，取前 top_n

本地不做任何「这两个值是否应该关联」的判断。top_n 从契约 cross_table.rank_top_n 读，代码零字面量。

### 5c. validate_join_hypothesis（本地）

输入：list[JoinHypothesis], field_refs
输出：{ok: bool, needs_retry: bool, reason_codes: list[str]}

结构校验（纯机械，无阈值）：
- left.field_id 在 field_refs 的 field_id 集合里
- right.field_id 在 field_refs 的 field_id 集合里
- left.parent_table_id != right.parent_table_id
- join_type 在枚举 {left, inner, full} 里
- hypothesis_id 在本次返回内唯一
- rationale 非空

任一不满足 → needs_retry=true，reason_codes 追加对应枚举（unknown_field / same_table / invalid_join_type / duplicate_hypothesis_id / empty_rationale）。

本函数不校验「至少一条跨表」「evidence 唯一」「concept 是否合理」。

### 6. `validate_ai_proposal`（本地）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| bindings | `list[ConceptBinding]` | 函数 5 的返回 |
| synonyms | `list[SynonymCandidate]` | 函数 4 的返回 |
| field_refs | `list[FieldRef]` | 合法引用全集 |

返回：`{ok: bool, questions: list[CrossTableQuestion], needs_retry: {concept: bool, synonym: bool}}`。不调用模型。

`needs_retry.concept` / `needs_retry.synonym` 分别对应函数 5 / 函数 4。对应位为 true 仅当该类提案结构不完整：缺必填、成员为空、或 `proposal_status=invalid`。重试仅当对应位为 true（D1）。`proposal_status=ambiguous`、成员重叠 → `ok=false`，对应 `needs_retry` 位为 false，变成问题走人机，不触发补充 AI 调用。结构非法且无可问对象 → 抛 `cross_table_proposal_invalid` 且 `questions=[]`。禁止把任何分数或「不确定」自述写成 `needs_retry`。本函数不校验别名提案；别名重试见函数 11。

concept 分支随 ConceptBinding 废除；synonym / alias 分支保持不变。

concept 类 needs_retry 的四类原因：成员结构不完整 / 成员未知 / 成员跨 binding 重叠 / utterance_evidence 缺失、非子串或重复。

### 7. `build_cross_table_questions`（本地）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| exact_groups | `list[ExactMatchGroup]` | 精确候选也要问 |
| synonyms | `list[SynonymCandidate]` | 近义候选 |
| bindings | `list[ConceptBinding]` | 概念候选 |
| align_ambiguities | `list[AlignKeySpec]` | 推导失败的键 |

返回：`list[CrossTableQuestion]`。同一 `candidate_id` 只一条。不调用模型。

### 8. `on_cross_table_interrupt`（本地）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| questions | `list[CrossTableQuestion]` | 待答 |
| prior_log | `DecisionLog | null` | 已有日志则走重放，不再问 |

返回：`{paused: bool, open_questions: list[CrossTableQuestion], released: bool}`。`released=true` 仅当问题数组为空，或日志重放且全部 `decision_status=confirmed|rejected`。有开放问题则 `paused=true`，`released=false`。不调用模型。不调用桥。

拒绝：`prior_log` 指纹与当前摘要不一致 → `cross_table_fingerprint_mismatch`，不部分采用旧裁决。

### 9. `apply_cross_table_reply`（本地）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| question | `CrossTableQuestion` | 原问题 |
| reply | `Literal["confirm","reject","defer"]` | 用户答复。不扩展此枚举 |
| reply_text | `str` | 原文，只存档，不解析出新字段 |
| selected_field_id | `str \| null` | 仅 `align_key_pick` 且 `confirm` 时必填。其余必须为 null |

返回：更新后的 `ConceptBinding | ExactMatchGroup | SynonymCandidate | AlignKeySpec`（与问题的 `interrupt_kind` 对应）。协议见 B2、B3。`confirm` → `decision_status=confirmed`。`reject` → `rejected`。`defer` → `deferred`，不得进入拼接。不调用模型。

拒绝：`reply` 不在 `allowed_replies`，或 `selected_field_id` 违反 B2 → `cross_table_reply_invalid`，对象状态不变。

### 10. `derive_align_key_specs`（本地）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| binding | `ConceptBinding` | 已 `confirmed` 的一个概念 |
| field_refs | `list[FieldRef]` | 摘要 |
| dimensions_by_block | `dict[block_id, dict[subtable_id, list[field_id]]]` | 单表维度 |

返回：`list[AlignKeySpec]`，每个涉及的 `parent_table_id` 至多一条 `status=resolved`。无法唯一确定时该表一条 `status=ambiguous`，不返回猜测键。不调用模型。

拒绝：`binding.decision_status!=confirmed` → `cross_table_unconfirmed`，返回空且不写键。

### 11. `propose_value_alias_links`（AI）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| align_keys | `list[AlignKeySpec]` | 只含 `status=resolved` |
| key_values | `dict[field_id, list[str]]` | 每个 `status=resolved` 的对齐键必须有条目。值去重与顺序按 B11。全空列 → `[]`（D3，不视为缺条目）。缺条目 → 调用前即 `cross_table_input_invalid` |
| user_utterance | `str` | 原话 |

返回：`list[RowLink]`，`link_kind=alias`，`decision_status=proposed`。精确相等的对不由本函数产生。本函数每次调用只调模型一次，不内部重试，不读配置。重试不走 `validate_ai_proposal`：调用方检查返回列表是否含 `proposal_status=invalid`（或等价结构错误）；若是且累计次数未超配置上限，再调本函数（D1/D2）。禁止置信度分流。总调用仍受 wall time 与 token 预算约束。`key_values` 某键为空列表 → 返回空列表，不视为失败（D3）。无别名则返回空列表，不视为失败。

拒绝：链接两端不是对齐键、或值不在对应 `key_values` 条目中 → `cross_table_proposal_invalid`，整批别名丢弃，精确链接仍由函数 18 产生。调用方耗尽上限仍无效同此。

### 12. `expand_matched_rows`（本地）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| align_keys | `list[AlignKeySpec]` | 已解析键 |
| exact_links | `list[RowLink]` | 函数 18 的返回。本函数不自己生成精确对 |
| alias_links | `list[RowLink]` | 仅已确认的别名 |
| unmatched_disposition | `Literal["retain_unmatched","drop_unmatched","route_unmatched"]` | 契约项 |
| tables_by_id | `dict[parent_table_id, TableGrid]` | 读单元格 |

返回：`{rows: list[dict[str, str]], unmatched_rows: list[UnmatchedRow], cardinality: Literal["one_to_one","one_to_many","many_to_many"]}`。不调用模型。传入的 `exact_links` / `alias_links` 可为空列表（D3：空键列合法）；空链接不报错，按无可对齐对处理。

拒绝：disposition 不在枚举 → `cross_table_contract_invalid`，不写出行。`route_unmatched` 在本设计不把行放进桥 `rejected`；若拍板仍选它，行只进入 `unmatched_rows` 且 `disposition=route_unmatched`，主 `rows` 不含它们。

### 13. `resolve_output_grain`（本地）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| bindings | `list[ConceptBinding]` | 仅 `confirmed` |
| align_keys | `list[AlignKeySpec]` | 已解析 |
| field_refs | `list[FieldRef]` | 角色来源 |
| reserved_long_columns | `list[str]` | 从契约 `cross_table.reserved_long_columns` 读入。判定一侧是否已是长表时只用这份列表，不从代码常量读 |
| source_shapes | `dict[parent_table_id, Literal["wide","long"]]` | 上游形态。调用方用 `reserved_long_columns` 与上游列名求交得到；函数内不得写死列名 |

返回：`list[OutputGrainDecision]`。不调用模型。`shared_key` 只通过函数 19 判定。规则即 M8 四条形态加宽/长规则。`keep_sources=true` 时该概念为 `per_source`（B3）。`long` 的熔列范围按角色：list / column 不熔，group / attribute 才熔。属性对列名只来自 `reserved_long_columns`。

拒绝：同一概念同时满足 `per_source` 与 `per_concept`（回复冲突）→ `cross_table_grain_conflict`，不挑选其中一个。

### 14. `project_cross_table_outputs`（本地）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| decisions | `list[OutputGrainDecision]` | 函数 13 |
| expanded | `dict[concept_id, ExpandedRows]` | 函数 12 的返回按概念索引 |
| bindings | `list[ConceptBinding]` | 已确认。成员 `role` 决定 long 形态下谁熔、谁留原列 |
| reserved_long_columns | `list[str]` | 从契约读。熔出的属性对列名只用此列表，不写字面量 |

返回：`list[CrossTableOutput]`。不调用模型。不修改输入的 executable 列集合。`output_grain=noop` 的决策不产生元素（B5）。

写出：`row_shape=wide` 时每个成员一列。`row_shape=long` 时，非键成员 `role` 为 list 或 column 的保留该成员原列；`role` 为 group 或 attribute 的熔进 `reserved_long_columns` 给出的属性对。`columns` 允许同时含这两部分。列名冲突按 B7 加 `parent_table_id` 前缀，并写入 `column_origins`。`output_id` 用 B13 的公式。不新增契约字段，不改 `row_shape` 枚举，不把该选择放进人机。

拒绝：任一决策 `row_shape` 缺失 → `cross_table_grain_conflict`，整批不写。

### 15. `write_decision_log`（本地）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| log | `DecisionLog` | 完整链 |
| session_dir | `str` | 会话目录 |

返回：`{path: str, fingerprint: str}`。不调用模型。无跨表候选时不得调用本函数（B14：不同 `parent_table_id` 少于 2 则不写文件）。

拒绝：目录不可写 → `cross_table_log_io`，内存中的确认状态保留，但 `released` 保持 false（没落盘不得拼接）。

### 16. `replay_decision_log`（本地）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| log | `DecisionLog` | 上次落盘 |
| fingerprint | `str` | 当前摘要指纹 |

返回：`DecisionLog`。指纹一致则不调用模型，AI 段使用日志里的 `ai_raw`。不调用模型。

拒绝：指纹不一致 → `cross_table_fingerprint_mismatch`，不返回部分绑定。

### 17. `assert_r2_keys_unchanged`（本地）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| before_keys | `list[list[str]]` | 进入本阶段前的 R2 键快照 |
| after_keys | `list[list[str]]` | 本阶段结束后的 R2 键快照 |

返回：`{ok: bool}`。快照语义见 B10。集合不等 → 抛 `cross_table_r2_key_mutation_forbidden`。不调用模型。

### 18. `derive_exact_links`（本地）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| align_keys | `list[AlignKeySpec]` | 只含 `status=resolved` |
| key_values | `dict[field_id, list[str]]` | 每个 `status=resolved` 的对齐键必须有条目。顺序与去重同 B11。全空列 → `[]`（D3，不视为缺条目） |
| ops | `list[Literal["trim","casefold","width_fold"]]` | 值比较用的规范化，与函数 2 相同 |

返回：`list[RowLink]`。`link_kind=exact`，`decision_status=confirmed`。两侧规范化后相等则各生成一条链接；不相等的不在这里产生。某键对应空列表 → 该键不产生链接，不报错（D3）。不调用模型。不进人机。

拒绝：某个 `resolved` 键没有 `key_values` 条目 → `cross_table_input_invalid`，不返回部分链接。

### 19. `concepts_share_align_key`（本地）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| left | `list[AlignKeySpec]` | 概念甲已解析的键 |
| right | `list[AlignKeySpec]` | 概念乙已解析的键 |

返回：`bool`。两侧 `status=resolved` 的 `(parent_table_id, field_id)` 交集非空则为 true。不比较值集合，不比较裸 `field_id`。不调用模型。无抛错。

### 20. `snapshot_r2_keys`（本地）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| executable | `list[ExecutableItem]` | 输入列表或 B9 的新列表 |
| block_plan | `BlockPlan` | 用 `block_id` 查 `parent_table_id` |

返回：`list[list[str]]`。每条 source 一个二元组，按 B10 排序。不读 `subtables`。不调用模型。缺 `block_id` 或查不到表 → `cross_table_input_invalid`。

### 21. `copy_executable_with_bindings`（本地）

参数：

| 名 | 类型 | 语义 |
|----|------|------|
| executable | `list[ExecutableItem]` | 进入本阶段的原列表，函数不得改它 |
| bindings | `list[ConceptBinding]` | 仅 `confirmed` |

返回：`list[ExecutableItem]`。浅拷贝规则见 B9。不调用模型。原列表与返回值不是同一对象。

---

## 2.2 数据结构

### FieldRef

从既有层投影，不替换那些层。

| 字段 | 类型 | 必填 | 语义 |
|------|------|------|------|
| field_id | `str` | 是 | 引用 matrix 草案或 ordered 映射上的字段身份 |
| block_id | `str \| int` | 是 | 引用块 |
| parent_table_id | `str` | 是 | 引用表。跨表判断用它，不改 R2 |
| subtable_id | `int \| null` | 否 | 有子表时引用；ordered 为 null |
| role | `str` | 是 | 引用草案 `role`；ordered 无角色时为 `column` |
| target_name | `str` | 是 | 引用 executable 上的名字 |
| source_labels | `list[str]` | 是 | 该字段全部 source 的非空 `cell_text`，去重，按源行序（升序行、同行内升序列）。可为空数组。禁止取最短 / 最长 / 下标 0 |
| source_label | `str` | 是 | 仅当 `source_labels` 长度恰为 1 时取该值；否则空串。不参与「从多条里挑一条」 |
| in_dimensions | `bool` | 是 | 该 `field_id` 是否出现在 `dimensions_by_block` |
| value_samples | `list[str]` | 是 | 原样引用上游已有样本。ordered 为空数组。本阶段不按条数截断 |

关系：只读 `mappings`→桥→`executable`、`matrix_draft`、`dimensions_by_block`。不写入 `alignments` / `conflicts`。`concept_binding_ids` 不在本结构，只写在 B9 浅拷贝的 executable 顶层。

### ExactMatchGroup

| 字段 | 类型 | 必填 | 语义 |
|------|------|------|------|
| candidate_id | `str` | 是 | 稳定 id，由成员 field 引用排序后生成 |
| member_refs | `list[FieldRef]` | 是 | 至少两个不同 `parent_table_id` |
| matched_on | `Literal["target_name","source_label"]` | 是 | 哪一种文本相等 |
| decision_status | `Literal["proposed","confirmed","rejected","deferred"]` | 是 | 精确组仍须确认 |

关系：不进入 `mappings`。确认后可被 `ConceptBinding.member_refs` 引用。

### SynonymCandidate

| 字段 | 类型 | 必填 | 语义 |
|------|------|------|------|
| candidate_id | `str` | 是 | 提案 id |
| member_refs | `list[FieldRef]` | 是 | 模型认为可归并的字段 |
| proposal_status | `Literal["ok","ambiguous","invalid"]` | 是 | 结构状态，不是分数 |
| ai_raw | `object` | 是 | 模型原文，供重放 |
| decision_status | `Literal["proposed","confirmed","rejected","deferred"]` | 是 | 人机结果 |

关系：与 `ExactMatchGroup` 并列。确认前不影响 executable。

### ConceptBinding

| 字段 | 类型 | 必填 | 语义 |
|------|------|------|------|
| concept_id | `str` | 是 | 概念实例 id，不是词表项 |
| utterance_span | `str` | 是 | 原话中被模型标出的片段原文 |
| member_refs | `list[FieldRef]` | 是 | 跨表字段集合 |
| proposal_status | `Literal["ok","ambiguous","invalid"]` | 是 | 模糊则为 ambiguous |
| decision_status | `Literal["proposed","confirmed","rejected","deferred"]` | 是 | 人机结果 |
| keep_sources | `bool` | 是 | 只由 `interrupt_kind=output_grain` 的答复写入（B3）。缺省 false，表示尚未要求按来源分表 |
| ai_raw | `object` | 是 | 模型原文 |

关系：独立数组 `concept_bindings[]`。executable 仅通过浅拷贝顶层的 `concept_binding_ids` 引用（B9）。不写入 `matrix_draft.fields`。

### CrossTableQuestion

人机问题。不使用既有 `questions[]`。

| 字段 | 类型 | 必填 | 语义 |
|------|------|------|------|
| question_id | `str` | 是 | 稳定 id |
| interrupt_kind | `Literal["concept_bind","synonym_merge","exact_merge","alias_link","align_key_pick","unmatched_policy","output_grain"]` | 是 | 新通道上的问题类型 |
| hypothesis | `str` | 是 | 模型或本地给出的判断句 |
| evidence | `str` | 是 | 引用了哪些 field_id / candidate_id |
| if_holds | `str` | 是 | 确认之后本地将执行的动作名 |
| ask_user | `str` | 是 | 呈现给用户的问句 |
| candidate_id | `str` | 是 | 指向组、近义、概念或对齐键 |
| field_refs | `list[FieldRef]` | 是 | 问题涉及的字段 |
| allowed_replies | `list[Literal["confirm","reject","defer"]]` | 是 | 合法答复 |
| recommended_reply | `Literal["confirm","reject","defer"] \| null` | 否 | 原话已点名关系时可为 confirm；仍须用户提交 |

关系：`on_cross_table_interrupt` 的暂停列表。不进入 `on_analysis` 的 `pause.blocks`。

### UserReplyRecord

| 字段 | 类型 | 必填 | 语义 |
|------|------|------|------|
| question_id | `str` | 是 | 对应问题 |
| reply | `Literal["confirm","reject","defer"]` | 是 | 结构化答复 |
| reply_text | `str` | 是 | 原文 |
| selected_field_id | `str \| null` | 是 | 见 B2。非点名对齐键时为 null |
| answered_at | `str` | 是 | 时间戳 |

关系：只进 `DecisionLog.replies`。

### AlignKeySpec

| 字段 | 类型 | 必填 | 语义 |
|------|------|------|------|
| concept_id | `str` | 是 | 所属概念 |
| parent_table_id | `str` | 是 | 这一侧 |
| field_id | `str \| null` | 否 | `resolved` 时必填 |
| rule | `Literal["dimension_member","sole_list","user_picked"]` | 是 | 推导依据 |
| status | `Literal["resolved","ambiguous"]` | 是 | 能否当键 |

关系：不修改 `dimensions_by_block`。`user_picked` 只来自 `align_key_pick` 的确认。

### RowLink

| 字段 | 类型 | 必填 | 语义 |
|------|------|------|------|
| link_id | `str` | 是 | 稳定 id |
| concept_id | `str` | 是 | 所属概念 |
| left_field_id | `str` | 是 | 对齐键 |
| right_field_id | `str` | 是 | 对齐键 |
| left_value | `str` | 是 | 规范化前原文 |
| right_value | `str` | 是 | 规范化前原文 |
| link_kind | `Literal["exact","alias"]` | 是 | 精确为本地，别名为 AI |
| decision_status | `Literal["proposed","confirmed","rejected","deferred"]` | 是 | 精确链接写出时已是 confirmed |
| proposal_status | `Literal["ok","ambiguous","invalid"] \| null` | 否 | 仅 `link_kind=alias` 且尚未确认时必填。调用方以列表中出现 `invalid` 判定结构失败并按 D1 重试。精确链接为 null |

关系：替代「把对齐塞进 alignments」的做法。`alignments` 保持块间列槽语义。

### UnmatchedRow

| 字段 | 类型 | 必填 | 语义 |
|------|------|------|------|
| concept_id | `str` | 是 | 所属概念 |
| parent_table_id | `str` | 是 | 未命中的那一侧 |
| field_id | `str` | 是 | 对齐键 |
| value | `str` | 是 | 未命中的键值 |
| disposition | `Literal["retain_unmatched","drop_unmatched","route_unmatched"]` | 是 | 与契约默认一致，除非本概念被用户改过 |

关系：不写入桥 `rejected`，不写入 `conflicts`。

### OutputGrainDecision

| 字段 | 类型 | 必填 | 语义 |
|------|------|------|------|
| concept_id | `str` | 是 | 一份决策对应一个概念；`shared_key` 时用主概念 id，并填 `merged_concept_ids` |
| output_grain | `Literal["noop","per_concept","per_source","shared_key"]` | 是 | 分表方式 |
| row_shape | `Literal["wide","long"]` | 是 | 行内形状 |
| merged_concept_ids | `list[str]` | 否 | 仅 `shared_key` |
| align_field_ids | `list[str]` | 是 | 参与对齐的键 |

关系：决定 `CrossTableOutput` 的个数。不改单表 `subtables`。

### CrossTableOutput

| 字段 | 类型 | 必填 | 语义 |
|------|------|------|------|
| output_id | `str` | 是 | B13 的 sha256 十六进制。`noop` 不产生本结构 |
| concept_ids | `list[str]` | 是 | 覆盖的概念 |
| output_grain | `Literal["per_concept","per_source","shared_key"]` | 是 | 不含 `noop`（B5） |
| row_shape | `Literal["wide","long"]` | 是 | 同决策 |
| parent_table_id | `str \| null` | 否 | 仅 `per_source` 时必填 |
| columns | `list[str]` | 是 | `wide`：已确认成员列。`long`：可同时含未熔成员的原列，以及已熔属性对的列。重名按 B7 加前缀 |
| column_origins | `list[{column: str, parent_table_id: str, field_id: str, melted: bool}]` | 是 | 与 `columns` 等长。下游只靠它识别来源 |
| rows | `list[dict[str, str]]` | 是 | 膨胀后的行 |
| unmatched_rows | `list[UnmatchedRow]` | 是 | 失败行；`retain_unmatched` 时这些键也出现在 `rows` 里且对侧为空 |

关系：与 `subtables` 并列的新产品。复核若要读它，只能读这份产物；本设计不改复核材料组装。

### DecisionLog

落盘 sidecar。重放的唯一来源。

| 字段 | 类型 | 必填 | 语义 |
|------|------|------|------|
| fingerprint | `str` | 是 | B8：不含 `value_samples`。含表 id、字段 id、角色、规范化 `target_name` / `source_label` / `source_labels`、原话、契约版本 |
| contract_version | `str` | 是 | 写出时的契约版本 |
| field_refs | `list[FieldRef]` | 是 | 当时摘要 |
| exact_groups | `list[ExactMatchGroup]` | 是 | 含状态 |
| synonym_candidates | `list[SynonymCandidate]` | 是 | 含 `ai_raw` |
| concept_bindings | `list[ConceptBinding]` | 是 | 含 `ai_raw` |
| questions | `list[CrossTableQuestion]` | 是 | 问过的问题 |
| replies | `list[UserReplyRecord]` | 是 | 答复 |
| align_keys | `list[AlignKeySpec]` | 是 | 推导结果 |
| row_links | `list[RowLink]` | 是 | 精确与别名 |
| grain | `list[OutputGrainDecision]` | 是 | 形态 |
| ai_raws | `list[object]` | 是 | 按调用顺序：近义、概念、别名。某次未调用则该位为 `{}` |

关系：不写入 `conflicts`。指纹变了就整份作废。

---

## 2.3 契约新增字段

新增块写在**新版本文件**的新键 `cross_table` 下。不改 v1.0.0 / v1.1.0 已有键，不改已有枚举的成员。

| key 路径 | 类型 | 默认值 | 默认值语义 |
|----------|------|--------|------------|
| `cross_table.schema_version` | `str` | `"1"` | 本块自身版本。与文件名版本独立，便于只增字段 |
| `cross_table.trigger_mode` | `Literal["ask_on_candidate","utterance_only","always_attempt"]` | `ask_on_candidate` | 有跨表候选才问；没有候选不调用模型。不是「检测到字面关系就合并」 |
| `cross_table.equality_basis` | `Literal["target_name","source_label","either"]` | `either` | 两种文本任一规范化相等即成精确候选，避免只认一种名字而漏掉另一种同指 |
| `cross_table.confirm_required` | `bool` | `true` | 未确认不得合并。关掉则与 M6 冲突，实现必须拒绝启动 |
| `cross_table.unmatched_disposition` | `Literal["retain_unmatched","drop_unmatched","route_unmatched"]` | `retain_unmatched` | 对不齐仍保留该侧行。丢弃不可从产物还原；路由到桥 rejected 会混进定位错误 |
| `cross_table.output_grain` | `Literal["noop","per_concept","per_source","shared_key"]` | `noop` | 契约默认是「尚未决定」。真正取值只由 `resolve_output_grain` 写入实例，禁止用这个默认直接交付 |
| `cross_table.row_shape` | `Literal["wide","long"]` | 无默认 | 实例字段必填。契约不提供默认，避免没跑推导就变成某一种表 |
| `cross_table.decision_status` | `Literal["proposed","confirmed","rejected","deferred"]` | `proposed` | 新建即未确认 |
| `cross_table.proposal_status` | `Literal["ok","ambiguous","invalid"]` | `ambiguous` | 模型没给状态时视为模糊，进人机，而不是视为成功 |
| `cross_table.candidate_kind` | `Literal["exact","synonym","concept","alias"]` | 无默认 | 实例必填，用来区分四条链路 |
| `cross_table.align_key_rule` | `Literal["dimension_member","sole_list","user_picked"]` | 无默认 | 只有推导或用户点名之后才有值 |
| `cross_table.interrupt_kind` | `Literal["concept_bind","synonym_merge","exact_merge","alias_link","align_key_pick","unmatched_policy","output_grain"]` | 无默认 | 问题实例必填 |
| `cross_table.cardinality` | `Literal["one_to_one","one_to_many","many_to_many"]` | 无默认 | 膨胀之后才写 |
| `cross_table.replay_policy` | `Literal["replay_on_fingerprint","refuse_on_mismatch"]` | `replay_on_fingerprint` | 指纹相同必须重放日志、不得再叫模型；不同则拒绝而不是混用 |
| `cross_table.reserved_long_columns` | `list[str]` | `["attribute_name", "attribute_value"]` | 判定「一侧是否已是长表」以及 long 形态属性对列名时只读此列表。值写在契约文件里，不写在函数里。列表成员是列名身份，不是概念词表 |

`confirm_required=false` 不是可运行配置：启动检查视为契约不完整，阶段不开始。

枚举值都是结构状态，不是业务词。

---

## 2.4 配置项

读者都是本阶段函数，在进入该函数时读取。2b matrix 不读这些键。

| key | 类型 | 语义 | 谁读、何时 | 缺省行为 |
|-----|------|------|------------|----------|
| `cross_table.enabled` | `bool` | 本阶段是否运行 | `on_cross_table_interrupt` 入口 | 缺省视为 `false`：不调用模型，不写日志，R2 与单表产物原样离开 |
| `cross_table.replay_if_log_present` | `bool` | 指纹相同的日志是否禁止再叫模型 | `replay_decision_log` 之前 | 缺省视为 `true`：有日志且指纹相同则重放 |
| `cross_table.decision_log_filename` | `str` | sidecar 文件名 | `write_decision_log` / `replay_decision_log` | 缺省文件名 `cross_table_decision_log.json`，写在当次 `session_dir` |
| `cross_table.name_normalize_ops` | `list[Literal["trim","casefold","width_fold"]]` | 文本比较步骤 | `normalize_match_text` | 缺省为这三步全开，顺序固定。缺省不是「跳过规范化」。扩展方式：新增规范化步骤只能走契约新版本新增枚举值；禁止在 `normalize_match_text` 或任何调用方内硬编码额外步骤。当前默认三步即全部可用值。不允许在函数里对文本做额外预处理（去标点、去数字、截断等），除非契约新增枚举 |
| `cross_table.value_sample_policy` | `Literal["inherit_field_digest"]` | 摘要里的样本从哪来 | `collect_cross_table_field_refs` | 只有这一枚举值。缺省即继承字段上已有 `value_samples`，本阶段不另设条数 |
| `cross_table.proposal_max_calls_per_kind` | `int` | 每类提案（近义 / 概念 / 别名）的模型调用上限。由调用方持有，不传入 propose 函数体内读配置 | 调用方每次调用 / 每次重试 `propose_*` 之前重读 | 缺省视为 `1`。调用顺序见下「写码前明确 D2」。近义 / 概念：仅当 `validate_ai_proposal.needs_retry` 对应位为 true 时再调。别名：仅当返回含结构无效时再调。禁止置信度分流。仍受既有 wall time 与 token 预算约束；不新增行数 / 条数阈值 |

不新增分数阈值、不新增概念词表、不新增「超过 N 行就改策略」的参数。默认 `1` 进配置，不写进函数常量。

---

## 待用户拍板

8 项拍板已通过。R1–R4 已修正。B1–B14 已复审。补丁 1–3 已通过。D1–D3 已明确，设计终审通过，可进入写码。

写码前明确（D1–D3）已写入函数 4/5/6/11/12/18 与配置说明，不是新设计缺口。未接到写码批次指令前不写码。

下列 8 项按推荐通过。改选只替换契约默认枚举，不改函数个数与数据结构字段名。

1. **M1 触发**
   - `ask_on_candidate`（推荐）：有跨表候选就问，没有就跳过。
   - `utterance_only`：原话没提到关系则连候选问题都不出现。
   - `always_attempt`：无候选也调用概念模型。
   - 推荐理由：不漏掉同名候选，也不在没有候选时调用模型。

2. **M2 相等依据**
   - `either`（推荐）。
   - `target_name`。
   - `source_label`。
   - 推荐理由：两种文本都是字段身份，只认一种会把同指拆开。

3. **M7 摘要输入**
   - 字段摘要组合（推荐）：身份 + 角色 + 名字 + 已有样本 + 是否维度。
   - 仅 `target_name` / 仅角色 / 仅样本。
   - 推荐理由：概念映射需要身份和角色，样本只是附属。

4. **M7 存放**
   - sidecar 的 `concept_bindings[]` + 契约类型 + executable 只存 id（推荐）。
   - 只放 executable / 只放 sidecar / 只放契约。
   - 推荐理由：类型、原文、重放和 R2 隔离同时成立。

5. **M7 人机通道**
   - 新钩子 `on_cross_table_interrupt`（推荐）。
   - 把新 `scope` 塞进既有 `questions`。
   - 推荐理由：既有 scope 枚举是已发布字段，本阶段只新增。

6. **M8 失败行**
   - `retain_unmatched`（推荐）。
   - `drop_unmatched`。
   - `route_unmatched`（只进 `unmatched_rows`，仍不进桥 `rejected`）。
   - 推荐理由：保留才看得到没对上的键；丢弃和混进定位错误都不可审计。

7. **M8 输出由谁定**
   - 本地 `resolve_output_grain` 按角色与上游宽/长决定（推荐）。模型不选形态。
   - 模型直接指定 wide/long 与分表。
   - 推荐理由：形态是角色的函数，不是另一次生成。

8. **M4 旧倾向**
   - 作废「默认长表」（推荐），改由第 7 项。
   - 保留默认长表。
   - 推荐理由：与「由字段角色动态决定」冲突。

---

## 写码前明确（D1–D3）

### D1. `needs_retry` 分两类

`validate_ai_proposal` 返回 `needs_retry: {concept: bool, synonym: bool}`。`concept` 对应函数 5，`synonym` 对应函数 4。重试仅当对应位为 true。`propose_value_alias_links` 的重试由自身返回中的结构无效判定触发，不走 `validate_ai_proposal`。

### D2. `max_calls` 由外层控制

`propose_synonym_candidates` / `propose_concept_bindings` / `propose_value_alias_links` 每次调用只调模型一次，不内部重试，不读配置。调用方持有累计次数，每次重试前重读 `cross_table.proposal_max_calls_per_kind`。

调用顺序（无业务例子）：

1. 调用方读配置得到上限。
2. 调用 `propose_*`（一次模型调用）。
3. 结构校验：近义 / 概念走 `validate_ai_proposal`；别名由调用方检查返回结构。
4. 若对应 `needs_retry` 位为 true（或别名结构无效），且累计次数未达上限，回到步骤 1 再调同一 `propose_*`。
5. 达上限仍无效 → `cross_table_proposal_invalid`。

### D3. `key_values` 空列语义

每个 `status=resolved` 的对齐键，`key_values` 必须有对应条目。全空列 → `key_values[field_id] = []`，不视为缺条目。缺条目 → `cross_table_input_invalid`。空列表不报错：`derive_exact_links` 产出空链接列表；`propose_value_alias_links` 收到空列表返回空列表，不视为失败；`expand_matched_rows` 接受空链接列表。

---

停下，等写码批次指令。不写码。
