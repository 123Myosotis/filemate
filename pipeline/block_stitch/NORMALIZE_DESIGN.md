# 拼接按源列归一（R2 设计）

状态：已写码（`normalize_by_source_column`，插在 `stitch()` 写行/implied 之后）。不改 4b 旧项；R2 独立验收见 `acceptance/block_stitch_r2/`。  
显示名语言已由 R3 拍板：**接受英文**。本设计不改 2b prompt。

## 1. 归一键

键 = `(parent_table_id, source_col)`。

- `source_col`：桥解析后的列下标（`sources_resolved[].col`），不是列头字符串。
- 不用列头文本做键：同一表头改字、空表头、matrix 标签与表头不同字时，文本键会把不同列并错，或把同一列拆开。

### 1.1 `parent_table_id` 来源（写死）

`executable.sources_resolved[]` **只有** `block_id`，没有 `parent_table_id`。必须经 `block_plan` 查：

1. 用 `source.block_id` 找到 `blocks[]` 里对应块。
2. 读该块 `parent_table_ids[]`：
   - **长度 = 1** → `parent_table_id = parent_table_ids[0]`，该 source **参与** R2 归一。
   - **长度 > 1**（小表合并块）→ 该 mapping 的这个 source **不参与** R2 归一（保守，不误并）。  
     理由：小表合并块里 `source_col` 是相对整块 `col_span` 的绝对列号，各小表列数不同时不可比。
   - **长度 = 0 / 块找不到** → 同「不参与」。

一条 mapping 若有多个 source，每个 source 各自按上面规则决定是否入键。归一发生在「多个**已入键**的 source 落到同一键」时，不是按 `target_name` 字符串相等。

## 2. 跨表

不同 `parent_table_id` 的同一 `source_col` **不归一**。

例外：`alignments_resolved` 里同一 `align_id` 的槽，其 `block_id + col` 已被桥标成同一实体列键时，这些槽对应的归一键可以并成一组。没有这条对齐声明，不因为列号碰巧相同就合并。

## 3. 显示名（写死，R3 = P1 接受英文）

同一归一键上收集到的 `target_name` 候选，按下面顺序只选一个显示名（仅用于**值相等合成**后的那一列）：

1. 候选里只要有英文名（判定：去掉下划线后，剩余字符全部是 ASCII 字母数字），就只在英文候选里选。
2. 英文候选多于一个：取 `mapping_index` 最小的那条的 `target_name`。
3. 候选全是中文（或都不满足第 1 条的英文判定）：取 `mapping_index` 最小的那条的 `target_name`。
4. 中英混合：走第 1–2 条，英文优先，不取中文。

不按「看起来更像表头」「更短」「出现次数更多」挑选。大小写不同（`AssetName` / `asset_name`）算两个英文候选，仍按最小 `mapping_index` 留一个显示名。

## 4. 同一归一键、同一输出行上的多个值

先按现有拼接把行对齐（有 `alignments_resolved` 则横向，否则上下相接）。冲突只看**同一输出行**，不看整组值集合。空串和缺格不算「写了值」。

| 情况 | 处理 |
|------|------|
| 没有任何一行同时有 ≥2 个 mapping 的非空且不相等的值（含：各行只有一个 mapping 有值；同一行多个非空但 strip 后相等） | 合成一列。列名 = §3 选出的显示名。各 mapping 的值按自己的 `row_index` 纵向排进这一列。`cell_origin` 见 §4.1。 |
| 某一输出行上 ≥2 个 mapping 都写了非空值且 strip 后不相等 | 整组拆列，不丢值。列名 = **该条自己的** `target_name#mapping_index`。同一行上值相等的子集仍并进其中最小 `mapping_index` 的那一列，该列名用那条自己的 `target_name#mapping_index`（值不等分支里不使用 §3 显示名）。 |

不把不相等的值覆盖掉，也不静默取第一个。不同行的不同值不是冲突。

### 4.1 值相等合成时的 `cell_origin`（写死）

选 **B：主 origin + `also_from[]`**，不采用「同格多条平铺 origin」。

规则：

1. 在该 `row_index` + 合成后列名上，只保留 **一条** 主 `cell_origin`：取参与合成的 mapping 里 `mapping_index` 最小的那条的 origin 字段（`block_id` / `mapping_index` / `source_row` / `source_col` 等与现网一致）。
2. 在该主 origin 上增加数组字段 `also_from`：其余参与合成的来源各一项  
   `{ "block_id", "mapping_index", "source_row", "source_col" }`，按 `mapping_index` 升序。
3. 若只有一条来源：`also_from` 为 `[]` 或不写该键（实现时二选一，写码批次定一种并测）；语义都是「无旁路来源」。
4. 跨表对齐合并（§2 例外）且值相等时：同样走本条，主 origin 取最小 `mapping_index`，其它表来源进 `also_from`，**不丢源信息**。

值不相等、拆成多列时：每列各自一条 origin，**不**写 `also_from`（各列本来就一对一）。

## 5. 与 `build_columns` 的关系

**新增一步，不替换 `build_columns`。**

现有 `stitch()` 顺序：校验对齐 → `build_columns`（按 `target_name` 精确字符串）→ 按连通段写行 → 同名同格不同值时记 `stitch_implied_conflicts` 并拆 `#index`。

R2 插在「行已经写完、implied 冲突已经记完」之后，作为 `normalize_by_source_column(stitched, executable, alignments_resolved, block_plan)`：

1. 按 §1.1 从 `block_plan` 解析 `parent_table_id`；建立可参与归一的 `mapping_index → (parent_table_id, source_col)`。
2. 按 §2 把可合并的键收成组（默认同表同列；跨表仅当对齐槽证明）。
3. 按 §3 / §4 / §4.1 改 `columns` / `rows` / `cell_origin`。
4. `unresolved_conflicts` 与 `stitch_implied_conflicts` 保留原样，不删桥的记录；归一只改最终列与格子。若某 implied 组在归一后变成「同值已合并」，列上不再出现重复列，冲突数组仍留作审计。

不放在 `build_columns` 之前：那时还没有「同一输出行上的值」，无法执行 §4 的相等/不相等分支。

## 6. 验收与观察

### 6.0 观察（诊断事实，写死预期）

B 组 286 条 = 13 包 × 22 条：模型每包都吐全表映射，不是只吐本块。  
C 组 44 条 = 2 包 × 22 条：两包名字完全不复用。  
这是正常行为（模型按整表理解）。R2 归一化后应收敛到 **约 22 列**（接近源列数），不是 63 列。这是 R2 的核心预期。

### 6.1 与 4b

- **R2 未写码时**：4b 现有项仍按现在的 `stitch()` 跑，归一不存在，**不因本设计而失败**。不重跑、不改 4b。
- **R2 写码之后**：4b 原项仍应过。另加验收（独立跑，不替换 4b 旧项）：
  1. 同表同列、两个英文 `target_name`、同行同值 → 一列，显示名取较小 `mapping_index`；主 origin + `also_from` 含另一条。
  2. 同表同列、中英混合、同行同值 → 一列，显示名取英文。
  3. 同表同列、同行值不同 → 两列，列名分别为各自的 `target_name#mapping_index`，两值都在。
  4. 不同表、列号相同、无对齐 → 仍是两列。
  5. 不同表、对齐槽声明同一列键、同行同值 → 合成一列，`also_from` 含另一表来源。
  6. `parent_table_ids` 长度 > 1 的块上的 source → 不参与归一（列保持 `build_columns` 结果）。
  7. 真实表全量归一化后列数收敛：用真实漏扫表复现 B 组形态（约 13×22 条、约 63 唯一名）走 R2，`columns` 数应接近源列数（22 左右）。不允许仍是 63（未生效），也不允许低于 10（误并）。报告实测列数，不要求恰好 22。

## 7. 本文件不做的事

不改 prompt、桥、2b、1a；不改 5b/4b 现有验收项；不改 contracts/、方向 3.5；不把行业列名写进规则。  
（`stitch.py` 仅增加对 `normalize_by_source_column` 的调用，不改 `build_columns` 与其它步骤。）
