# 2b matrix 层级表头：诊断与设计

状态：**已确认写码。** 拍板已并入：C1 + D1（双方都有独占 value_rows 才拆子表；门店侧栏与店名行有交集时仍拆）+ 长表 + Z。跨表合并见 `MULTI_TABLE_DESIGN.md`，本轮不改 R2 的 `parent_table_id` 隔离。

`冷链抽查/门店对应店长.xlsx` 仓库里没有这个文件。同构表在 `零售抽查/门店对应店长.xlsx`（sheet「片区」，5×14，3 处合并）。下文用这张。

---

## 2b-D1 诊断

### 1. 2b 现在对 matrix 做了什么

`analyze.py` 不看 `layout`。它只调用 `build_prompt`。matrix 与 ordered 走同一套拼装。

`build_prompt` 对每个块只多打一行元数据：`layout=matrix|ordered`。后面的【生成指导】和【输出形状】两套文案对两种 layout **完全相同**。文件头注释写明了设计意图：matrix 的合并标签在 blob 里只是普通单元格字，列名质量靠 5a，不靠 2b。

单元格 blob（`block_cell_blob`）：按 `row_span × col_span` 把格子用制表符/换行摊平。合并格只在左上角有字，其余格是空。没有「这是表头行」标记。`repeat_header` 只在切行块时把第 0 行再贴一次，冷链这种整表一块用不到。

合并进 prompt 的形式（`context_blob`）：

```
merge <table_id> <r0> <c0> <r1> <c1>
```

只有表 id 和半开坐标，**没有合并格上的文字，没有「分组 / 表头」角色**。

行 0：和后面每一行一样写进 blob。1a 的 `classify_layout` 已经判定冷链 `looks_like_field_header=False`（行 0 密度 0.45，顶上合并比 1.0），所以 layout=matrix。2b **不读这个判定的含义**，也不把行 0 标成表头或数据。

没有任何「层级表头」或「分组标签」识别。1a 只负责把表标成 matrix 并记下合并矩形。

### 2. 为什么把格子当字段（冷链 24 条）

冷链「库区」5×11，3 处横向合并（A1:B1、D1:E1、G1:H1）。行 0 是「华北仓储一部 / 华南仓储一部 / 西南仓储一部 / 总机 / 分机」。下面两行在每个合并跨度内重复人名、温区，再下面两行才是各库名。2b 打出约 24 条 mapping，一格一条。

prompt 里促成这件事的句子（`prompt.py` 的【生成指导】和【输出形状】）：

| 句子 | 作用 |
|------|------|
| 「对照本包【块】里已经给出的单元格文本（含表头行或块内可见的列题/标签）；使列名与这些源侧题名/数据所表达的字段一致」 | 每个看得见的字（部门名、人名、库名、总机）都是列名素材。没有说合并标签是分组、不是字段。 |
| 「块内有多个不同数据列需要输出时，应各输出一条 mapping；一条 mapping 对应一个输出列」 | 「列」没有定义成「表的一列」。模型把每个不同的格子文本当成一列。 |
| 「mappings 的条数由块内实际需要输出的列决定；块内有多个不同数据列时，应各输出一条」 | 同上，鼓励按看见的不同文本加条数，而不是按源列数封顶。 |
| 「sources[].locator 应能对准你声称要输出的那一列在块内的位置（列题或可区分的列定位）」 | 允许用单个格子做定位。对齐槽才写了「不要用单格坐标当对齐键」，mappings 没有这条限制。 |
| blob 把行 0 放在第一行，无表头标记 | 没有任何一句说「matrix 的行 0 是分组标题，不要当数据字段，也不要当 ordered 那种字段表头」。`layout=matrix` 只是一个词，指导里没解释。 |
| `merge t0 0 0 1 2` 这种纯坐标 | 模型看不出 A1:B1 是「华北仓储一部」盖住两列。合并文字已经在 blob 里当普通字出现了，于是被当成字段值/字段名。 |

没有「一列一个语义、格是该列的值」这种指导。现有第 1 条只禁止把用户原话当列名，不禁止把格子当列名。

### 3. ordered 为什么没有这个问题

漏扫「数据列表」10031×22，**0 个合并**。行 0 密度 1.0，22 个短字段名（资产名称、状态、漏洞类型…），`looks_like_field_header=True`，layout=**ordered**。

差在材料，不在另一套 prompt：

- 行 0 本身就是「一列一个字段名」，和指导第 2 句「对照表头行」对得上，所以 22 条 mapping、一列一条。
- 没有横向合并，不存在「一个标签盖住多列」。
- 第 1 行起是长数据，和行 0 的短标签分开，模型不会把数据格再当成新字段。

matrix 缺的是这三样：稠密字段表头、合并的分组角色、以及「格是值不是字段」的说明。指导文案相同，输入几何不同，所以只有 matrix 炸成一格一条。

### 4. 另外几张 matrix 会怎样

都用现有 `classify_layout` 看过结构，没有跑 2b。

| 表 | 结构 | 现有 2b 会怎样 |
|----|------|----------------|
| 库位对应仓管 5×11，3 合并，layout=matrix | 行 0 横向合并 = 部门组；组内下一行人名重复、再下一行温区重复；再下面才是各库名。右侧「总机/分机」是真列。 | **格子当字段**（已发生，约 24 条）。部门、人名、温区、每个库名都会变成 mapping。 |
| 零售「门店对应店长」片区 5×14，3 合并，layout=matrix | 与库位同构，跨度是 3 列：行 0 营运部，行 1 店长名重复 3 次，行 2 店型重复，行 3–4 才是各店名。右侧仍是总机/分机。 | **同一形态**：每个店名、每个重复的店长名都可能成为一条字段。预期条数随非空格走，不是 6 个语义列。 |
| 变电站「管理所」61×21，21 合并，layout=matrix | 多层：行 0 三个管理所各盖 4～5 列；行 1 负责人（有的再二合一）；行 2 电压；行 3 起站点名。中部还有横跨多列的分段标题（电厂、合计）。右侧 17–19 列是通信/电话，不在管理所合并里。 | **格子当字段，再加分段标题当字段**。不是「只是冷链的缩小版」：多了一层电压、一段段的小标题、一块侧栏。不修的话输出是几十到上百条单元格 mapping，不是「站点 / 负责人 / 电压 / 管理所」。 |
| 干扰名单.xlsx | 与「管理所」同一张几何（61×21，同样 21 处合并）。行 1 把部分人名换成「干扰甲/乙/丙」。 | **同一形态**。干扰字只是格里的值，现有 2b 仍会把它们当成新字段名，不会因为「干扰」两个字改变结构。 |

要覆盖的变体就这三类，不是四套无关布局：

1. **分组矩阵**（库位、门店）：顶栏横向合并 = 组；组内重复格 = 组属性；组内不再重复的格 = 同一字段的多个值；组外未合并列 = 独立字段。
2. **多层分组 + 段标题 + 侧栏**（管理所、干扰名单）：在 1 上再加「中部横跨标题不是字段」和「合并范围之外的侧栏是另一组真列」。
3. **ordered 稠密表头**（漏扫）：保持现状，22 列。

---

## 2b-D2 设计

### 1. matrix 的正确理解

几何规则，不写行业词。

- **横向合并**（同一行、跨多列，尤其落在表的顶带）：分组标签。它描述下面这一组列，**不是**独立输出字段，也不是这一列的单元格值。
- **组内、合并跨度上逐行都相同的非空字**（库位的人名、温区；门店的店长、店型）：该组的属性，每个属性一个字段，值在组内复制，不按格拆字段。
- **组内、跨度上开始互不相同的格子**（库名、店名、站名）：同一个列表字段的多个值，**一个字段**，按行/按组内位置铺开。
- **不被顶栏合并盖住的列**（总机、分机、通信电话）：普通列。若该列自己像「标签在上、值在下」，标签是字段名，下面是值。
- **中部横跨很多列、上下都是另一类内容的合并**（管理所里的电厂标题、合计）：段标题。不产出字段，只断开上下两段，每段各自用同一套分组规则。
- **行 0 是不是字段表头**：沿用 1a 已有判定。`layout=ordered` 且 `looks_like_field_header` → 行 0 是字段表头，一行一个字段。`layout=matrix` → 行 0 **不是**字段表头；它是分组标签行或段标题行，具体由「是不是横向合并 / 是不是稀疏标签」决定，不靠词表。

输出单位是**字段（列语义）**，不是格子。matrix 上一条 mapping 对应草案里的一个 `field_id`，覆盖该字段的列范围（见缺口 1）。locator 不能是单个库名格子。

### 2. 修复放在哪

**两处都要，只改 prompt 或只加预处理都不够。**

| 只做一处 | 为什么不够 |
|----------|------------|
| 只改 prompt，仍喂摊平 blob + 纯坐标 merge | 模型仍要自己从空格里猜合并盖住了谁。冷链 24 条就是这么来的。指导再写「不要一格一条」，没有结构化列模型时不稳定，也覆盖不了管理所的段标题。 |
| 只在 2b 前算出结构，prompt 仍是现在的【生成指导】 | 指导第 2、3 句仍要求按看见的格子文本各出一条。模型会忽略预处理，或把预处理和 blob 各出一套 mapping。 |

做法：

1. **2b 调用前的结构预处理**（纯几何，不调用模型）：输入 grid + `context.merges` + layout。输出「字段草案」：每个字段的角色（分组属性 / 列表值 / 独立列）、覆盖的列范围、哪几行是值、段边界。不写 target.name 的英文，名字仍由模型根据这些材料起。
2. **prompt 增加 matrix 专节**（仅 `layout=matrix` 时插入）：把字段草案写成模型可读的材料，并改掉「每个不同格子一条 mapping」。ordered 不插入这节，【生成指导】对 ordered 的四条保持原样。

不放在 1a：1a 已经正确标了 matrix 和合并矩形。不放在 R2：R2 只能合并「同一源列、同一行冲突」，消不掉「每个格子一个 mapping、源列还不同」这种 2b 误识。

### 3. 覆盖范围

同一套几何规则覆盖第 4 节的三类：

- 分组矩阵：库位、门店。
- 多层 + 段标题 + 侧栏：管理所、干扰名单（干扰甲/乙/丙只是属性值）。
- ordered：不进入该预处理的字段重写，blob 与指导与现在一致。

不把「华北」「店长」「管理所」写进规则。

### 4. ordered 不回归

漏扫仍是 layout=ordered、行 0 密度 1、无合并。预处理看到 ordered 就原样把块交给现有 `build_prompt`。验收锁：22 个字段名列，不出现「一格一条」，也不把行 0 的「资产名称」当成数据值。

### 5. 验收设计（写码后才跑）

判定见缺口 7。列数区间不够，还要核对草案 `field_id` 和「格子文本没有变成列名」。

---

## 连带改动总表（缺口 1–4）

选定 **选项 B**：一条 mapping 覆盖一个字段的列范围。不选 A（一列一条再靠分组标识拼回去，会把「一格一条」换成「一列一条」，R2 仍要猜哪些是同一字段）。不选 C（不给桥单独开一条旁路；列范围就是 `resolve_locator` 的一种 kind）。

| 模块 | 改不改 | 改成什么 |
|------|--------|----------|
| 1a 块计划 | **不改** | 仍只产出 layout、grid、`context.merges` |
| 2b 预处理 | **新增** | 分段 → C1 判定是否跨组合并 → D1 拆子表 → 每子表字段草案 + 候选维度 |
| 2b prompt / 二次调用 | **新增** | matrix：字段命名（约束草案）+ Z 选维度（候选 + 用户原话）；ordered 不动 |
| 2b mapping JSON | **matrix 扩展** | `sources[]` 带 `field_id`；结果带 `subtables` / `dimensions` |
| 桥 `resolve_locator` | **扩展** | kind=`field`；见缺口 2 |
| 桥 `sources_resolved` | **扩展** | `segments[]`、`role`、`subtable_id` |
| 拼接写值 | **扩展** | 缺口 B 宽表骨架 → 长表熔成（维度列 + attribute_name + attribute_value） |
| R2 归一 | **有 field_id 时改键** | `(parent_table_id, field_id)`；跨组合并不在 R2 |
| 复核 prompt / 路 A | **不改** | 已知限制保留 |
| contracts / 方向 3.5 | **不改** | |

---

## 缺口 1：mapping 的 source 结构

选项 **B**（一条 mapping ↔ 一个 `field_id`）。跨组同角色合成后，一个字段可含**多段** `col_range`（缺口 A），仍是一条 mapping。

预处理给每个（合成后的）字段一个稳定 id（块内从 0 递增）。模型只许引用它，不许自造列范围或改段。

```json
{
  "sources": [{
    "block_id": 0,
    "field_id": "f2",
    "locator": "field:f2"
  }],
  "target": {"name": "<模型起的字段名>"}
}
```

桥从草案写入 `sources_resolved`（多段）：

```json
{
  "block_id": 0,
  "locator": "field:f2",
  "field_id": "f2",
  "kind": "field",
  "role": "list",
  "attribute_slot": null,
  "col": 0,
  "row": 3,
  "segments": [
    {
      "group_index": 0,
      "col_range": [0, 2],
      "value_rows": [3, 4]
    },
    {
      "group_index": 1,
      "col_range": [3, 5],
      "value_rows": [3, 4]
    },
    {
      "group_index": 2,
      "col_range": [6, 8],
      "value_rows": [3, 4]
    }
  ]
}
```

- `segments[].col_range` 半开 `[c0, c1)`。
- `col` / `row` = 第一段的左端与第一段 `value_rows[0]`，只做锚点。
- `role`：`group` | `attribute` | `list` | `column`。
- `attribute_slot`：仅 `attribute` 使用，组内第几个属性行（0-based）；`group`/`list`/`column` 为 `null`。
- 单段字段（侧栏）`segments` 长度为 1。

ordered 的 mapping 没有 `field_id`，source 仍是今天的 `{block_id, locator}`。

---

## 缺口 2：桥怎么解析列范围

`resolve_locator` 增加一种输入：`field:<field_id>`，或 source 上带 `field_id`。

解析次序插在现有坐标 / A1 / merge 之前，且**只在该块 `layout=matrix` 且该 id 存在于当次草案时**成功：

| 结果 | reason | 含义 |
|------|--------|------|
| 草案命中 | `ok`，kind=`field` | 带上草案的 `segments` / `role` / `attribute_slot` |
| locator 不是坐标、不是表头、也不是 `field:`，格子文本也对不上 | `locator_not_on_grid` | 与今天相同：定位串无法落到网格 |
| `field:` 的 id 不在草案，或 source.field_id 与 locator 不一致 | `column_range_invalid` | 列范围不合法（id 不是已知字段） |
| 模型自带 `col_range` / `segments` 且与草案不一致 | `column_range_invalid` | 不接受模型私自改范围 |
| matrix 块上的 mapping 没有 `field_id` | `field_id_required` | 禁止退回「按格子文本对上某一格」 |
| 同子表内重复 `target_name`（第二条起） | `duplicate_target_name_within_subtable` | 该条不进 executable；同子表其余映射照常。结果对象记 `duplicate_names`。第一次出现的那条保留，熔长表 `attribute_name` 用保留的那条 |
| 同一 `field_id` 出现 ≥2 条 mapping | `duplicate_field_id` | **该 field_id 下所有映射都不执行**，全部进 rejected。不静默取第一条 |

`field_id_required`：matrix 上若允许无 field_id 的文本匹配，模型仍会把「亦庄库」对成单格，预处理等于没做。

处理顺序（写死）：先按 `field_id` 扫重复（命中则该 id 全拒）→ 再解析 locator → 再在同子表已接受的 `target_name` 集合上查重（只拒第二条起）。

ordered 块不要求 `field_id`，不产生上表 matrix 专有 reason。

---

## 缺口 3：R2 归一键

跨组合同一角色在**预处理**完成（缺口 A 选 a）。R2 **不**按 role 合并多个 `field_id`。

| mapping | 归一键 |
|---------|--------|
| kind=`field` | `(parent_table_id, field_id)` |
| 旧的单列 source | `(parent_table_id, source_col)`，与现在相同 |

多段字段仍只有一个 `field_id`，键仍是 `(t, f2)`。不展开成每段一个键。

部门 / 仓管 / 库位是不同 `field_id`，键不同。值怎么铺到同一批输出行，见缺口 4 与缺口 B。

同一 `field_id` 不应出现两条 mapping。若出现，R2 仍按「同一输出行上非空值不等才拆」。无 `field_id` 的漏扫列继续单值 `source_col`。

---

## 缺口 4：拼接写值

只对 kind=`field` 的 mapping。ordered 仍走 `binding()` 的 column / cell。

一个字段 → **一个输出列**。值从各 `segments[]` 的 `value_rows × col_range` 取，不从锚点 `col` 单独取。

| 角色 | 段内格子 | 怎么写 |
|------|----------|--------|
| `group` / `attribute` | 段内取值行上非空且 strip 后全相等 | 该段得到**一个**标量。重复格不写两遍 |
| `group` / `attribute` | 段内取值行上出现两个不同非空值 | `stitch_implied_conflicts`（`field_span_inconsistent`），该段标量视为空，两值进冲突记录 |
| `list` | 某一 `value_rows` 行、段内多个不同非空格 | **正常**。按列从左到右，每个非空格产生一条**输出行**（见缺口 B） |
| `column` | 单段单列 | 标签行不算值；`value_rows` 上每个非空格对应其源行上的侧栏值（缺口 B） |

`binding()` 不解释 `segments`。field mapping 不调用 `binding()`。

---

## 缺口 5：纯几何规则（写死）

输入：grid、半开合并矩形、1a 的 `layout`。系数用 `LayoutBounds` 里已有的 `sparse_row_density=0.5`，不新写行业阈值。

**顶带。** 从行 0 往下，一行计入顶带当且仅当：非空密度 &lt; 0.5，或该行存在横向合并。遇到第一行「密度 ≥ 0.5 且该行没有横向合并」停止。库位的行 0 入顶带；行 1 人名铺满则停。顶带只用来找分组标签，不是字段表头。

**分组。** 顶带里每个横向合并（列跨度 ≥ 2）是一个组，跨度即 `col_range`。顶带里没被任何这种合并盖住、且有字的列，各是一个单列组（总机、分机）。侧栏 = 这些单列组，没有第二套判定。没被合并盖住就不是「漏掉的分组」；分组只来自横向合并。

**段标题。** 同时满足才算，缺一不可：

1. 行号在顶带之下；
2. 该行有一个横向合并，列宽 ≥ 3，且 ≥ 该表已用列数的一半；
3. 这个合并之外的格子都是空的；
4. 紧挨着的上一条非空行、下一条非空行都不是段标题，且那两行的非空格数量都多于本行。

「另一类」就是第 4 条：上下是更密的普通内容行，不是另一条同样的横幅。段标题不产生 `field_id`。它把表切成上下两段，每段各自做分组（下段若没有新的横向合并，沿用顶带的列分组，只是值行被标题行隔断）。

**组内一行是属性还是列表。** 只看该组 `col_range` 里的非空格（strip 后）：

- 全部相等 → 这一行是该组的 **attribute**（或合并标签所在行是 **group**，不进入值）；
- 出现至少两个不同的非空字 → 这一行是 **list**。

行 1 全相等、行 2 出现不同：**行 1 算属性，行 2 不算。** 按组单独判，不要求整表同一行同一个角色。

**列表从哪一行开始。** 该组标签行之后，第一行被判成 list 的那一行。从这一行起直到下一个段标题（或表尾），该组这些行都算 list，即使其中某一行碰巧又全相等（那是两个相同的列表值，不再改回属性）。「开始不同」= 这一处第一行 list，不是「多数行不同」的投票。

**字段草案（两步，见缺口 A）。**

步骤 1 — 每组先产**段**（还不是最终 field_id）：

- 1 个 role=`group` 段；
- 每个 attribute 行 1 个 role=`attribute` 段，并记 `attribute_slot` = 该组内属性行序号（0,1,2…）；
- 1 个 role=`list` 段，`value_rows` = 该组列表行；
- 每个单列组 1 个 role=`column` 段（侧栏不参与跨组合并）。

步骤 2 — 按缺口 A 合成最终字段并分配 `field_id`。

---

## 缺口 A：跨组同角色字段合成一列（选 a，受 C1 约束）

选 **(a) 预处理合并**。不选 (b)/(c)。

### C1：组间属性 slot 对齐检查（写死）

只对「多列组」（顶带横向跨度 ≥ 2 的组）做检查。侧栏单列组不参与。

对每个多列组算出属性行个数 `n_attr`（`attribute_slot` 个数）。

- **同构**：所有多列组的 `n_attr` **相等** → 允许按下面合成键跨组合并。
- **非同构**：存在两个多列组 `n_attr` 不同 → **整块不跨组合并**。每个多列组的 group / 各 attribute / list 段各自成为独立 `field_id`（列名带组区分由模型起名；几何上一段一字段）。侧栏 `column` 仍各一字段。

不做「部分 slot 合并、部分不合并」。不对齐就全拆。

几何同构 ≠ 语义同构，见文末「已知限制：C1」。

### 同构时的合成键

| role | 合成键 | 结果 |
|------|--------|------|
| `group` | `(role=group)` | 全块多列组的 group 段 → **一个**字段 |
| `attribute` | `(role=attribute, attribute_slot)` | 各组第 k 个属性段 → **一个**字段 |
| `list` | `(role=list)` | 全块多列组的 list 段 → **一个**字段 |
| `column` | **不跨组合并** | 每个单列组各自一个字段 |

`attribute_slot` 按组内属性行从上到下编号。同构时 slot 号对齐。某组缺少某 slot（同构下不应发生）则该 `group_index` 无段。

### 最终字段结构（多段，同构合并后）

```json
{
  "field_id": "f0",
  "role": "group",
  "attribute_slot": null,
  "subtable_id": 0,
  "segments": [
    {"group_index": 0, "col_range": [0, 2], "value_rows": [0]},
    {"group_index": 1, "col_range": [3, 5], "value_rows": [0]},
    {"group_index": 2, "col_range": [6, 8], "value_rows": [0]}
  ]
}
```

冷链三组皆 `n_attr=2` → 同构 → 合成后主区 4 字段 + 侧栏另计（见 D1）。模型对每个 `field_id` 一条 mapping。
---

## 缺口 B：组内属性对齐到 list 多行

对齐模式只有一种：**list 先展开，其它字段往展开后的输出行上填。** 组内 `group`/`attribute` 与侧栏 `column` 都遵守，但填值键不同。

### B1. list 展开（按段、按源行）

按 `group_index` 升序处理 list 字段的每一段。对段内每一个 `r ∈ value_rows`，按列从左到右扫 `col_range` 内非空格，每个非空格产生一条输出行，记下：

- `out_row` 序号；
- `group_index`（当前段）；
- `source_row` = `r`；
- `source_col` = 该格列号；
- list 列的值 = 该格文本。

冷链华北段 `col_range=[0,2)`、`value_rows=[3,4]` → 4 条输出行（亦庄库、通州库、大兴库、顺义库——以格内实字为准）。华南、西南段接着往下排。

### B2. group / attribute：按 **group_index** 广播

对每条由 B1 产生的输出行，取其 `group_index = g`：

- 在 group 字段中找 `group_index=g` 的段，取出该段标量（缺口 4），写入该输出行的部门列；
- 对每个 attribute 字段，同样取 `group_index=g` 的段标量，写入该行。

同一 `g` 拉出的全部 list 行（上例 4 行）拿到**同一份**部门/仓管/温区，不是只写第一行、其余留空。

华北 4 行：皆为「华北仓储一部 / 沈照 / 冷藏」（以格内实字为准）。华南段拉出的行：皆为华南对应标量。依此类推。

某字段在 `g` 上没有段，或标量为空：该输出行该列写空。

### B3. 侧栏 column：仅当与主区同属一张子表时

若 D1 已把侧栏拆成独立子表，主区 B1 骨架**不**填侧栏列（侧栏在自己的子表里按自己的 list/行展开）。

若未拆子表（侧栏 `value_rows` 与主区 list 有交集），对每条 B1 输出行取其 `source_row = r`，写入侧栏在 `(r, 侧栏列)` 的非空值；同一 `r` 拉多行则复制。

### B4. 宽表骨架顺序（每张子表各自做）

1. B1 展开该子表的 list（若无 list：见 Z 步骤 4 退化）。
2. B2 填 group/attribute。
3. B3 填同子表内的 column。
4. 得到**宽表骨架**（尚未成长表）。
5. Z 选定维度后，按「长表结构」熔成最终输出（见下）。不在宽表形态交付给用户验收。

---

## 缺口 D1：侧栏与主区拆成独立子表

在缺口 A（含 C1）得到字段段之后、分配最终 `field_id` 之前：

1. **主区域** = 所有多列组（横向合并跨度 ≥ 2）上的 group / attribute / list 段。
2. **侧栏区域** = 所有 role 将为 `column` 的单列组段。
3. 令  
   `R_main` = 主区域所有 list 段的 `value_rows` 的并集；  
   `R_side` = 侧栏各段的 `value_rows` 的并集（侧栏取值行：顶带之下、该列非空的行；标签行若等于顶带合并行则排除）。
4. **判定（写死）**：若 `R_main ∩ R_side = ∅` 且 `R_main`、`R_side` 均非空 → 拆成 **两张子表**：
   - `subtable_id=0`：主区域字段（再跑 A 的跨组合并，受 C1 约束）；
   - `subtable_id=1`：侧栏字段（每个 column 一个 field，不与主区合并）。
5. 若交集非空，或任一侧 `value_rows` 为空 → **不拆**，一张子表 `subtable_id=0`，侧栏字段留在同一子表（B3 按源行填）。

冷链：主区 list 行 {3,4}，侧栏电话行 {1,2}，交集空 → **两张子表**。门店同构同理。管理所侧栏若与站点行有交集则不拆。

每张子表独立：`field_id` 空间、候选维度、AI 选维度、宽表骨架、长表输出。`field_id` 在块内全局唯一（可 `s0_f0` / `s1_f0`，或全局递增并带 `subtable_id` 字段）。

---

## 长表结构（写死）

每张子表交付形态固定为长表，列如下：

| 列 | 内容 |
|----|------|
| 维度列（≥1 个） | 列名 = 该维度字段的 `target_name`；值 = 宽表骨架上该维度组合的值 |
| `attribute_name` | 非维度字段的 `target_name`（模型起的） |
| `attribute_value` | 同一维度组合下该非维度字段的值 |

规则（写死）：

1. 宽表骨架上每一行 = 一个**维度组合**（该行上所有维度列取值的元组）。
2. 对该组合，每一个**非维度**字段产生长表的**一行**：维度列重复该组合的值，`attribute_name` / `attribute_value` 为该字段名与值。
3. 一个维度组合有 N 个非维度字段 → N 行长表。
4. 非维度字段值为空：仍输出一行，`attribute_value` 为空串（不丢属性名）。
5. 列顺序：维度列按 AI 给出的 `field_id` 顺序；然后固定 `attribute_name`、`attribute_value`。
6. 多子表：每个子表一张长表（或带 `subtable_id` 列拼在同一结果里——实现选一种，验收按子表分别数行）。**默认**：结果里 `subtables[i].rows` 分表存放，不强制竖拼。

`attribute_name` / `attribute_value` 这两个列名写死为英文蛇形，不翻译、不用用户原话替换。

---

## Z：几何候选维度 + AI 选维度（完整流程，不许视情况）

在字段草案完成、模型已为每个 `field_id` 起好 `target_name` 之后执行。每张子表独立跑步骤 1–4。

### 步骤 1：几何产出候选维度

对子表内每个字段，判定是否进入 `dimension_candidates`（只放「能区分行」的字段）：

| 条件 | 是否候选 |
|------|----------|
| `role=list` | **是**（组内逐格不同） |
| `role=group` | **是**（组间标签不同；若子表只有一组则仍候选，因可区分「是否该组」） |
| `role=attribute` | 取各段标量；若存在至少两个段的标量 strip 后**不相同** → **是**；若所有段标量相同（含仅一段）→ **否** |
| `role=column` | 取其 `value_rows` 上非空值集合；若集合大小 ≥ 2 → **是**；若只有 0/1 个不同值 → **否** |

候选集可以是空：进入步骤 2 的失败路径。

### 步骤 2：AI 选维度

输入（一次调用可带齐所有子表，但输出必须分子表）：

- 用户原话；
- 每张子表：`subtable_id`、字段摘要列表。每项含 `field_id`、`role`、`target_name`、`value_samples`（最多 3 个非空样例）、以及是否在 `dimension_candidates` 里。

AI **只**从该子表的 `dimension_candidates` 里选，每张子表 ≥ 1 个 `field_id`。

输出格式（写死）：

```json
{
  "subtable_0_dimensions": ["f3", "f1"],
  "subtable_1_dimensions": ["f0"]
}
```

键名：`subtable_<id>_dimensions`，值为 `field_id` 数组，顺序即长表维度列顺序。

### 步骤 3：校验 AI 输出（本地，不重问业务）

对每个 `subtable_id`：

1. 键必须存在；缺键 → 该子表失败，reason=`dimensions_missing`。
2. 数组长度 ≥ 1；否则 `dimensions_empty`。
3. 每个 id 必须 ∈ 该子表 `dimension_candidates`；否则 `dimensions_not_candidate`。
4. 禁止重复 id；重复 → `dimensions_duplicate`。
5. 不在草案中的 id → `dimensions_unknown_field`。
6. 任一维度字段的 `target_name`（strip 后）∈ {`attribute_name`, `attribute_value`} → **整次** matrix 分析失败，reason=`dimension_name_conflicts_reserved`。不静默改名；由调用方让模型重跑并改维度字段的 `target_name`（或改选其它候选维度）。

任一张子表失败 → 整次 matrix 分析失败，保留已有 mappings / rejected，**不**用「全员当维度」或「第一个 list 当维度」静默兜底。

候选集为空：不调用 AI，直接失败，reason=`no_dimension_candidates`。

### 步骤 4：熔成长表

对该子表宽表骨架：

1. 维度字段 = 步骤 3 通过的列表；非维度字段 = 子表其余字段。
2. 若子表**没有** list 角色字段：宽表骨架退化为「每个 group_index（或侧栏每个 source_row）一行」，用 B2/B3 能填的标量填齐，再熔成长表。
3. 按「长表结构」生成 `subtables[i].columns` / `rows`。
4. `cell_origin`：长表每一行对应宽表骨架一行 + 一个非维度 `field_id`；origin 取该字段在对应 `group_index`/`source_row` 上的来源。

R2：长表阶段在 R2 **之后**或把 R2 限制在宽表骨架上（写码时二选一，推荐宽表骨架上按 `field_id` 做完 R2 再熔长表，避免 `attribute_name` 列被归一）。**拍板：先宽表 R2，再熔长表。**

### 与 2b 调用次数

1. 第一次模型调用：按草案为每个 `field_id` 起 `target_name`（缺口 6），产出 mappings。  
2. 第二次模型调用：Z 步骤 2 选维度（可与第一次合并成一次 JSON 若实现方便，但语义上是两段；若合并，响应必须同时含 mappings 与 `subtable_*_dimensions`，校验仍按步骤 3）。  
**不允许**第三次「视情况再问」。

---

## 缺口 6：草案是约束，不是建议

模型**必须**对草案里每个 `field_id` 恰好输出一条 mapping。只允许写 `target_name`（以及 questions / alignments）。不许拆并字段、不许改 `segments`。维度选择见 Z，不得在 mappings 里改 `field_id` 集合。

**同子表 `target_name` 唯一（选 A）：** 同一 `subtable_id` 内，各 `field_id` 的 `target_name`（strip 后）必须互不相同。桥按缺口 2：只拒重复的第二条起，保留第一次出现的；结果记 `duplicate_names`；熔长表 `attribute_name` 用保留那条。

**重复 `field_id`：** 桥层面拒绝——同一 `field_id` 出现 ≥2 条 mapping 时，该 id **全部**不进 executable（`duplicate_field_id`），**不**静默取第一条。

| 偏离 | 谁处理 | 结果 |
|------|--------|------|
| 未知 `field_id`、自改范围、matrix 无 `field_id` | 桥 | rejected，缺口 2 |
| 同子表重复 `target_name`（第二条起） | 桥 | `duplicate_target_name_within_subtable`；首条保留 |
| 同一 `field_id` ≥2 条 | 桥 | `duplicate_field_id`；该 id 全拒 |
| 草案字段无对应 mapping | 2b | `draft_fields_missing` |
| 维度校验失败（含撞保留列名） | Z 步骤 3 | 整次失败，不静默兜底 |
| 全部 mapping 被拒 | 调用方 | 分析失败 |

ordered 无草案、无 Z、无长表熔成。

---

## 缺口 7：验收判定（按长表 + 子表）

对每张 matrix 表：

1. **子表数**：冷链 / 门店在 D1 下必须为 **2**（主区 + 侧栏）；管理所按交集规则记录实测（常见 1）。漏扫无子表。
2. **字段草案**：每子表 `field_id` 与 executable 一致；C1 同构冷链主区为 4 字段（group+2×attr+list），侧栏 2 字段。
3. **长表列**：每子表列 = 维度列（≥1，名为模型 `target_name`）+ `attribute_name` + `attribute_value`。禁止再出现「一库一列」宽表当最终交付。
4. **维度合法**：最终维度 ∈ 步骤 1 候选，且通过步骤 3。
5. **维度列名不得撞保留列**：任一维度列的列名 ∉ {`attribute_name`, `attribute_value`}；撞了则本项失败（对应 `dimension_name_conflicts_reserved`）。
6. **同子表 target_name 唯一**：executable 内同 `subtable_id` 无重复 `target_name`；若桥记了 `duplicate_names`，重复条不得出现在 executable。
7. **格子不当列名**：任一字段的 `target_name` 不得等于段标题或该字段列表值原文（同前）。
8. **行对齐抽查**（主区子表）：同一库位维度值对应的属性行里，部门/仓管/温区（或模型起的对应 `attribute_name`）取值与缺口 B 广播一致（华北多名库位共享同一仓管）。
9. **ordered 漏扫**：仍 **恰好 22** 列宽表，不走长表、不走 Z。

| 表 | 子表 | 主区字段（合成后） | 最终形态 |
|----|------|--------------------|----------|
| 库位对应仓管 | 2 | 4 + 侧栏 2 | 两张长表；主区维度通常含库位 list（AI 按原话选） |
| 门店对应店长 | 2 | 同构 4+2 | 两张长表 |
| 管理所 | 按 D1 | C1 视 slot；多层+段标题 | 长表；段标题 0 字段 |
| 干扰名单 | 同管理所 | 同左 | 干扰字只在 attribute_value |
| 漏扫 | — | — | 22 列宽表，无长表 |

---

## 已知限制：C1 只保证几何同构，不保证语义同构

C1 的判定标准是「所有多列组 `n_attr` 相等」。几何相等 ≠ 语义相同。

两组都是 2 个属性行，可能是「人 / 类型」和「类型 / 人」，或「人 / 电压」和「人 / 地区」。这种表 C1 会合并，熔长表后属性值可能按 slot 错位叠在同一 `attribute_name` 下，用户需人工核查。

缓解路径（不在本轮）：

- 后续批次给预处理加语义信号（例如让模型在命名前先判断各组属性行语义是否一致）；
- 或在 5a 复核对 attribute 的「字段内多值 / 语义错位」做检查。

这不是实现 bug，是几何规则的能力上限。

---

停下等审。断链封口（重复 target_name / 重复 field_id / 维度撞保留列）与 C1 语义限制已写入；通过后再写码。

