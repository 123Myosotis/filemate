# 方向 3.5 — 结构信号（设计确认，确认前不写代码）

## 0. 上下文复核

我读过的 handbook hash = `d3d912a04c589cc477dde79dce309715c977b1a9b4fa19a3bd8f73ad6a36f08a`

已关闭：准确性硬修、方向 2、方向 3、方向 4、evidence 落盘补丁。真实 e2e demo 未关。

与本批相关的手册条目复述：

1. 裁定 1：强制层 / opt-in / 暂不强制仍成立。结构信号是分流输入，不把 `delivery_blocks` 改成强制。
2. 裁定 2：方向 3 已关；本批是方向 3 与方向 5 之间的独立小批，**不是**方向 8 前处理整包。
3. 裁定 3：无 `FILEMATE_CONTRACT_GATE`；无契约库 → `ContractLibraryError`。
4. 两条铁律：只动本方向相关文件；不兜底、不逃生门、无业务字段名上接口、**无 confidence 分流**、无模板化问法。
5. 固定队列：… → 方向 3（关）→ evidence 落盘补丁（关）→ **方向 3.5 设计确认** → 写码 → 用户指定业务表重做 e2e → 再讨论方向 5。
6. 本批允许改 / 不允许改：

```text
本批名称：方向 3.5 结构信号
本批目标：读表产出的结构元信息进入 plan，使 triage_plan 能把脏表挡出简单路径。

允许改：
- 读表 → 本地结构观测；AI 规划 → header_layers；合并写入 plan.structural_signals（§3、§5）
- triage.py：新增 structural_* 信号（闭集 16 → 16+6）
- gate：未知 metric / 缺阈值键拒绝
- 契约：发布带 structural_thresholds 的 v1.1.2；不改 ops / empty / key_norm

不允许改：
- 方向 5–9；方向 8 完整前处理模块
- 现有 16 信号语义；d3-summary-1 / d3-evidence-1 / d4-report-1
- 对单元格内容做威胁词 / URL / 隐私扫描
- 代码 hardcode 阈值；任何 AI 自评 confidence / 分数进分流
```

声明：以上与 `contracts/EXECUTION_HANDBOOK.md` 一致；hash 与 `contracts/HANDBOOK_HASH` 一致。不符则本批作废。

---

## 0.1 为什么方向 3.5 必须在方向 5 之前（非可选）

方向 5 的假设裁决建立在 **triage 已经分好 simple / complex** 之上。若 triage 对脏表仍判 `simple`，方向 5 会在错误前提上做假设冲突处理，投入白做，并把结构问题伪装成「假设冲突」。

方向 3 的 16 个信号只读契约字段、**故意不读表结构**。真实 e2e 已证明无结构信号时异常表可静默 `simple`。因此方向 3.5 是方向 5 的前置闸门，不是可选 neatness。

---

## 1. 目标

让 `triage_plan` 感知「表结构异常」，脏表进 `complex`，不静默成功。不重算表、不改三份交付 schema、不扫单元格内容。

---

## 2. 数据来源（已定）与 gate 前合成顺序（P1-3）

**读表阶段扩展 + AI 规划阶段的非 confidence 结构化声明**，不是方向 8 前处理整包。合成细节见 §5（缺口 3）。

**合成发生在 gate 之前**（定死，整条路径）：

1. 读表生成 `plan.structural_signals_local[]`
2. AI 生成 `header_layers`（IR 或 plan 独立字段）
3. 合成到 `plan.structural_signals[]`（`header_unclear` 冲突以 AI 为准；审计行写 `runtime_notes`，见 §5.3）
4. **gate** 校验 `plan.structural_signals[]` 内所有 `metric` 属于闭集
5. **triage** 读 `plan.structural_signals[]` + 契约 `structural_thresholds` 分流

因此 `structural_metric_unknown` 的 `path` 统一指向 `plan.structural_signals[i].metric`，无歧义。

---

## 3. `structural_signals` 形状

### 3.1 一条还是多条（按 metric×scope）

同一 `scope` 下每个 **metric 最多一条**。合并单元格是 **1 条**，`observed_value` = 该 scope 内合并区个数 N，不是 N 条「第 i 处合并」。

不同 sheet 可各有一条（`scope=sheet:<name>`）。workbook 级度量用 `scope=workbook`。

### 3.2 每项字段与 `observed_value` 定义（含缺口 1）

```text
plan.structural_signals: [   # 数组；缺键 = []
  {
    "metric": "<metric_name>",
    "observed_value": <number|object>,   # 仅观测，不含阈值
    "scope": "workbook" | "sheet:<sheet_name>"
  },
  ...
]
```

禁止项内字段：`confidence`、`score`、`threshold_value`、`signal_id`。

**缺口 1 — `too_few_rows` / 稠密行数（选定 B）**

`metric=too_few_rows` 的 `observed_value` = **该 scope 内总非空行数**（行内任一单元格 strip 后非空即计 1）。不是连续非空段最大长度（A），也不是「有数据最大列的行数」（C）。

其它 metric 的 `observed_value`：

| metric | observed_value |
|--------|----------------|
| `multi_sheet` | workbook 的 sheet 个数（int） |
| `too_few_rows` | 总非空行数（int，定义见上 B） |
| `merged_cells` | 合并区个数（int） |
| `placeholder_columns` | 占位列数 / 总列数（float，匹配用 §4.2 `placeholder_patterns`） |
| `long_column_name` | 最长列名字符数（int） |
| `header_unclear` | `{layers, layer_count, has_gap}`（§5） |

### 3.3 `fired_signals`（含缺口 4）

- 只含 id 字符串，例如 `structural_multi_sheet`。禁止 `structural_multi_sheet:3`。
- 生成：`"structural_" + metric`。
- **去重（set 语义）**：3 个 sheet 都因 `header_unclear` 触发时，`fired_signals` 里 **只有 1 条** `structural_header_unclear`。`structural_signals[]` 仍保留每个 sheet 各一条观测项。
- 观测值只在数组项里，供审计；验收按 id 断言。

### 3.4 与 `runtime_notes`

分开。分流只读 `structural_signals` + 契约阈值。合成冲突审计行见 §5.3，写进 `runtime_notes`，**不是** triage 信号。

---

## 4. `structural_thresholds`（含缺口 2）

### 4.1 版本

写码时发布 **`v1.1.2`**，`CURRENT` 切到 `v1.1.2`。阈值不由 AI 在 plan 里声明。

### 4.2 契约块字段（定死；含 P1-1）

```text
"structural_thresholds": {
  "min_sheets_for_complex": <int>,
  "min_dense_rows": <int>,
  "max_merged_cells_simple": <int>,
  "max_placeholder_ratio": <float>,
  "max_column_name_length": <int>,
  "max_header_layers": <int>,
  "forbid_header_layer_gap": <bool>,
  "placeholder_patterns": ["col_\\d+", "Unnamed: \\d+", ...],
  "placeholder_match_mode": "fullmatch" | "search"
}
```

**缺口 2 — 占位模式表（选定 A）**

`placeholder_patterns` 与数值阈值同在 `structural_thresholds`。读表/合成阶段用这些正则判定某列名是否占位，再算 `placeholder_columns` 的比例写入 `observed_value`；triage **只比较**比例与 `max_placeholder_ratio`，不再自带模式表。

接受后果：**改正则列表或 `placeholder_match_mode` = 发新契约版本**（与改数值阈值相同）。禁止把模式表硬编码进 `triage.py`。

**P1-1 — 匹配模式拆细（定死 `fullmatch`）**

- `placeholder_patterns`：正则字符串列表。
- `placeholder_match_mode`：枚举，仅 `"fullmatch"` | `"search"`。
- **本批固定 `"fullmatch"`**：对列名整串匹配（等价 `re.fullmatch`）。理由：占位列名通常是完整的 `col_N` / `Unnamed: N`，不会嵌在其他文本里；若用 `search`，`col_\d+` 会误匹配 `column_123`。
- 缺键或非法枚举值 → gate 拒绝（与缺其它阈值键同级，走 `structural_threshold_missing` 或等价契约不全）。

触发规则：

| metric | fired id | 触发 |
|--------|----------|------|
| `multi_sheet` | `structural_multi_sheet` | `observed_value >= min_sheets_for_complex` |
| `too_few_rows` | `structural_too_few_rows` | `observed_value < min_dense_rows` |
| `merged_cells` | `structural_merged_cells` | `observed_value > max_merged_cells_simple` |
| `placeholder_columns` | `structural_placeholder_columns` | `observed_value > max_placeholder_ratio` |
| `long_column_name` | `structural_long_column_name` | `observed_value > max_column_name_length` |
| `header_unclear` | `structural_header_unclear` | §5 机械条件 |

闭集 **16+6**。

### 4.3 初始值；以后怎么改

写码时登记进 `v1.1.2.json`（建议起点，可在写码批准时改契约数字，不可改「来源=契约」）：

- `min_sheets_for_complex=2`
- `min_dense_rows=1`（防空表；400–1000 是 demo 选表要求，不是分流阈值）
- `max_merged_cells_simple=0`
- `max_placeholder_ratio=0.0`
- `max_column_name_length=64`
- `max_header_layers=1`
- `forbid_header_layer_gap=true`
- `placeholder_patterns=["col_\\d+","Unnamed:\\s*\\d+"]`
- `placeholder_match_mode="fullmatch"`

以后改阈值、正则或 match_mode：只发新契约版本并改 `CURRENT`。

### 4.4 未登记时

| 情况 | 行为 |
|------|------|
| plan 含 `metric` ∉ 闭集 | gate 拒绝 `structural_metric_unknown`（见 §7 缺口 5） |
| 契约缺整个 `structural_thresholds` | 库加载 / gate 失败 |
| 契约缺某一已知键（含 `placeholder_patterns` / `placeholder_match_mode`） | gate 拒绝 `structural_threshold_missing` |
| plan 无 `structural_signals` | 视为 `[]` |

禁止「未知 metric 就跳过」。

---

## 5. `header_unclear` 合成流程（缺口 3）与禁止 confidence

**铁律：不读 confidence、不比较任何 AI 自评分数。**

### 5.1 阶段顺序（定死）

1. **读表在前**：产出机械观测到临时缓冲 `plan.structural_signals_local[]`（不含 `header_unclear`，或仅含读表能算的度量）。读表 **不**猜表头层级。
2. **AI 规划在后**：产出 `header_layers`（结构化声明）到 plan 独立字段（如 `plan.header_layers` 或 IR 等价物）。**禁止** `header_confidence` / 任何分数。
3. **一次性合成**：读表收尾或 gate 前的合成函数读取 `structural_signals_local` + `header_layers`，写入最终 `plan.structural_signals[]`。临时键 `structural_signals_local` 合成后可删除或保留仅供审计，分流只认 `structural_signals`。

可选缓冲名 `structural_signals_ai[]`：若使用，只允许承载由 `header_layers` 派生的 `header_unclear` 观测项；合成后同样并入 `structural_signals[]`。

### 5.2 `header_unclear` 的 observed_value

由 `header_layers` 派生：

```text
header_layers: [
  {"level": 1, "rows": [1, 2]},
  {"level": 2, "rows": [3]}
]

→ structural_signals 项：
{
  "metric": "header_unclear",
  "observed_value": {
    "layers": [...],
    "layer_count": <int>,
    "has_gap": <bool>
  },
  "scope": "sheet:<name>"
}
```

机械触发（任一即 fire）：

- `layer_count > max_header_layers`
- 或 `forbid_header_layer_gap == true` 且 `has_gap == true`

不再采用单独的 `header_unclear: true/false` 作为本批主路径（布尔可作为缺 `header_layers` 时的退化，但写码优先强制 `header_layers`）。

### 5.3 同 metric 冲突与审计位置（P1-2）

对 `header_unclear`：**以 AI 的 `header_layers` 为准**（语义声明）。若读表本地曾写入同 metric 的占位项，合成时丢弃本地项，采用 AI 派生项。

**P1-2 — `structural_header_ai_overrides_local` 位置（选定 A）**

- **写进 `plan.runtime_notes` 的一行前缀**（审计用，与分流分离）。
- **不**写成 `structural_signals` 的字段或一项 metric（不选 B）。
- 理由：分流路径只读 `structural_signals` + 契约阈值，不读 `runtime_notes`（方向 3 已确立的审计与分流分离原则）。

其它纯机械 metric 无 AI 覆盖问题。

### 5.4 禁止

- 任何 `confidence` / `score` 进 `structural_signals`、gate、triage。
- 用分数阈值决定表头是否 unclear。

---

## 6. complex 动作闭集

仍用 `{confirm_execute, edit_plan, abort}`。不新增 `pick_different_sheet`。换 sheet 走 `edit_plan`。

---

## 7. 验收（写码后；本文件批准前不写码）

```text
python -m unittest discover -s tests/contract -q
python -m tests.acceptance.direction3_5_structural
```

1. 每个 `structural_*` 一条 `triage_signal_<id>_forces_complex`
2. `structural_all_clear_stays_simple`
3. **`structural_unknown_metric_gate_rejects`（缺口 5，断言定死）**
   - fixture：`structural_signals` 含 `metric` 不在闭集（如 `"not_a_metric"`）
   - 断言：
     - `contract_rejected == True`（或执行结果等价的 `rejected == True`）
     - `rows == []`
     - `contract_status == "contract_incomplete"`
     - `issues` 中存在 `code == "structural_metric_unknown"`
     - 该 issue 的 `path` 指向具体 metric（如 `structural_signals[0].metric`）
4. `structural_header_layers_no_confidence` — 夹具含 `confidence` 字段时 gate 拒绝或该字段不参与 fire；分流路径不读分数
5. **`structural_thresholds_from_contract`（缺口 6 + P1-4；fixture，不做 AST，不改契约文件）**
   - 契约 `min_sheets_for_complex=2` **保持不变**（不改磁盘契约、不清缓存、不恢复）
   - **plan A**：`structural_signals` 含 `multi_sheet` 且 `observed_value=3` → **fire** `structural_multi_sheet`
   - **plan B**：同契约，`multi_sheet` 且 `observed_value=1` → **不** fire
   - 证明：阈值来源是契约、读契约值分支正确；不依赖「改契约值」脚手架与不幂等性
   - 禁止源码 AST 扫描数字字面量

不调用真实 AI。真实业务表回归留到用户指定表后的 e2e。

---

## 8. 确认前不写码

未批准前不改 `triage.py` / `gate.py`、不发布 `v1.1.2`、不写 `structural_thresholds` 进契约、不新增 `structural_meta.py`、不动 demo。

**流程违规已入审计链（不可因回退抹掉）**：见 `contracts/batches/AUDIT_CHAIN.md` 条目「2026-10-18 — 流程违规：方向 3.5 未批准先写码」。实现侧已回退到 `CURRENT=v1.1.0`，无 `v1.1.2.json` / `structural_meta.py` / `pipeline/` 内 `structural_signals` 引用；回退证据见当次设计确认回复中的 P0 五项，不替代审计条目本身。
