# 方向 2 — 受限操作集（设计确认，确认前不写代码）

## 0. 上下文复核

我读过的 handbook hash = `d3d912a04c589cc477dde79dce309715c977b1a9b4fa19a3bd8f73ad6a36f08a`

（写码前已写入 handbook §7.6 / §7.7；`column_origins` **不引入**；`empty_role_coverage` dump 字段与硬修包一致。）

准确性硬修批（已关闭）：

- 验收包：`acceptance/accuracy_hardfix_20261018/`
- 验收文件：`contracts/batches/accuracy_hardfix_acceptance.md`
- backlog：`contracts/batches/BACKLOG.md`（P2-1 / P2-2）

与本批相关的手册条目复述：

1. **裁定 1**：强制层含比较/聚合字段的 `empty_token_map`、join/对账/去重的 `key_norm_id` 等；opt-in 为 entity/xref/caliber/delivery_blocks。本批**不**把尚未强制的 `row_accounting` / `deriv_edge` / budget 时序改成全面强制。  
2. **裁定 2**：方向 2 属「不改架构落不了」，须单独设计确认后再写码。准确性硬修三项已关闭。  
3. **裁定 3**：方向 1 不可回退：无 GATE 逃生门；无契约库 → `ContractLibraryError`。  
4. **两条铁律**：只动本方向相关文件；不兜底、不逃生门、无业务字段名上接口、无 confidence 分流、无模板化问法。  
5. **固定队列**：方向 1 → 准确性硬修（关闭）→ **本批设计确认** → 写码 → 方向 3。  
6. **手册 §6 F**：执行批验收包须含 plan dump，进 `outputs.sha256`，rerun 验证。  
7. **手册 §7**：key_norm / empty；§7.4 `shared_map_id`；§7.5 `fail`=整 plan；§7.6 参数对象 `id`/`calc_id`/`norm_id`/`order_id`；§7.7 固定执行序；**无** `column_origins`。  

声明：以上与 `contracts/EXECUTION_HANDBOOK.md` 一致；hash 与 `contracts/HANDBOOK_HASH` 一致。不符则本批作废。

---

## 1. 目标

AI 能交给执行器的操作，在计划结构上只能是契约版本登记过的操作。集合外操作没有第二条解释器。新增操作只能随契约版本发布。

本批发 **v1.1.0**（见 §9）。旧 v1.0.0 自由键计划被新执行器拒绝（见 §9），不维护兼容解释器。

---

## 2. 裁定表（二选一已选定）

| ID | 裁定 |
|----|------|
| **P0-A** | **方案 1**：`filter` 与 `derive` **共用**枚举 `cmp_op`。v1.1.0 将现 `filter_op` **重命名为** `cmp_op`（成员集与 v1.0.0 `filter_op` 相同）。`operator_set` 里 `filter` / `derive` 的 `cmp_enum` 字段均为 `"cmp_op"`。验收 `derive_unknown_cmp_rejects` 的枚举源 = 契约 `enums.cmp_op`。 |
| **P0-B** | **方案 2：固定执行顺序**。`ops[]` 只声明「哪些操作参与」；执行器按固定序执行：`join → filter → derive → map_values → order → dedupe → aggregate → project`。`ops[]` 数组顺序不改变执行序；若 `ops` 含某 `op`，则对应参数对象必须存在。验收：`ops_order_ignored_but_present_checked`。**实现（非第二解释器）**：`iter_ops_fixed_order` 按契约 `fixed_op_order` 产出 `(op, dispatch_fn, param_object)`，`execute_mechanical` 对每个元素调用静态 `OPERATOR_DISPATCH` 指向的 `apply_*`；**禁止**把 `ops[]` 转成 v1.0.0 顶层 `joins[]`/`keep_row_when` 等旧 DSL 再执行。 |
| **P1-1** | 发 **v1.1.0**。旧 v1.0.0 自由键计划（顶层直接 `joins[]` 而无 `ops[]` 等）→ `version_rejected` / `contract_incomplete`。`accuracy_hardfix_regression` 使用**重新生成的 v1.1.0 计划**，plan dump 进 `acceptance/direction2_ops_<date>/`；`acceptance/accuracy_hardfix_20261018/` 保留为历史，不重跑。无兼容层。 |
| **P1-2** | 同一 `param_ref` 值在 `ops[]` 中出现两次 → **拒绝**（`duplicate_param_ref` / `contract_incomplete`）。`ops: []` → **拒绝**（`empty_ops` / `contract_incomplete`）。验收：`duplicate_param_ref_rejects`、`empty_ops_rejects`。 |
| **P1-3** | 分派表是源码里的**静态** `dict`（字面量键）。`authority_scan` 用 **AST** 提取该 dict 的键集合，与 `operator_set` 的 `op` 集合做相等；**不**在运行时读 `DISPATCH.keys()` 作为权威。防「运行时从 operator_set 动态拼表再声称扫描通过」。 |

### P0-B 与准确性硬修语义等价

硬修时代机械序为 join → filter → derive → order → dedupe → agg → project。  
v1.1.0 固定序与之对齐，但**只走** `ops[]` + `OPERATOR_DISPATCH`。`accuracy_hardfix_regression`：仅执行 v1.1.0 计划（乱序书写 vs 正序书写 CELL_EQ）；对照历史包 `rows_out`；**不**再调用 v1.0.0 执行路径。

---

## 3. `operator_set`（v1.1.0 契约块形状）

```json
"operator_set": [
  {"op": "join",       "param_object": "joins",          "param_ref": "join_ref",   "cmp_enum": null},
  {"op": "filter",     "param_object": "filters",        "param_ref": "filter_ref", "cmp_enum": "cmp_op"},
  {"op": "derive",     "param_object": "derives",        "param_ref": "derive_ref", "cmp_enum": "cmp_op"},
  {"op": "map_values", "param_object": "value_maps",     "param_ref": "map_ref",    "cmp_enum": null},
  {"op": "order",      "param_object": "order_specs",    "param_ref": "order_ref",  "cmp_enum": null},
  {"op": "dedupe",     "param_object": "dedupe_specs",   "param_ref": "dedupe_ref", "cmp_enum": null},
  {"op": "aggregate",  "param_object": "agg_specs",      "param_ref": "agg_ref",    "cmp_enum": null},
  {"op": "project",    "param_object": "project_specs",  "param_ref": "project_ref","cmp_enum": null}
]
```

- `param_ref`：**每个 op 有固定键名**（上表），不是统一的泛名 `param_id`。`ops[i]` 上只允许出现该 op 对应的那一个 ref 键。  
- `cmp_enum`：非 null 时，该 op 参数对象内的比较符必须 ∈ `enums.<cmp_enum>`。  
- 改名：`keep_row_when` → 参数对象数组 `filters`；`derived_columns` → `derives`；`value_mappings` → `value_maps`；`output_columns` 收进 `project_specs`（见示例）。

首版闭集仅上表 8 个 `op`。不在表内 → `version_rejected`。

---

## 4. 完整 plan JSON 示例（5-step，可执行形状）

字段约定：参数对象数组里每项有稳定 `id`；`ops[].<param_ref>` 解析到对应数组中 `id` 相等的对象。

```json
{
  "contract_version": "v1.1.0",
  "contract_hash": "<v1.1.0 file sha256>",
  "status": "ready",
  "primary_table": "flow",
  "ops": [
    {"op": "join", "join_ref": "j1"},
    {"op": "filter", "filter_ref": "f1"},
    {"op": "order", "order_ref": "o1"},
    {"op": "dedupe", "dedupe_ref": "d1"},
    {"op": "aggregate", "agg_ref": "a1"},
    {"op": "project", "project_ref": "p1"}
  ],
  "joins": [
    {
      "id": "j1",
      "right_table": "acct",
      "left_column": "账号",
      "right_column": "账号",
      "how": "left",
      "key_norm_id": "kn_id"
    }
  ],
  "filters": [
    {
      "id": "f1",
      "column": "状态",
      "op": "eq",
      "value": "成功"
    }
  ],
  "derives": [],
  "value_maps": [],
  "order_specs": [
    {
      "id": "o1",
      "keys": [{"column": "账号", "dir": "asc"}],
      "tie_break": "input_row",
      "null_order": "last"
    }
  ],
  "dedupe_specs": [
    {
      "id": "d1",
      "key_norm_id": "kn_id",
      "key_column": "账号",
      "keep": "first_in_order",
      "order_id": "o1"
    }
  ],
  "calc_specs": [
    {
      "calc_id": "c1",
      "calc_version": "1",
      "result_column": "cnt",
      "steps": [{"op": "count", "column": "金额"}],
      "agg_null_policy": "skip"
    }
  ],
  "agg_specs": [
    {
      "id": "a1",
      "group_columns": ["产品"],
      "group_catalog_ids": [],
      "calc_id": "c1",
      "order_id": "o1",
      "agg_semantics": {
        "count": {
          "vacant": "skip",
          "unknown": "skip",
          "na": "skip",
          "incomparable": "incomparable",
          "unclassified_empty": "fail"
        }
      }
    }
  ],
  "project_specs": [
    {
      "id": "p1",
      "output_columns": ["产品", "cnt"]
    }
  ],
  "norm_specs": [
    {
      "norm_id": "kn_id",
      "idempotent": true,
      "steps": [{"op": "identity"}]
    }
  ],
  "empty_token_maps": [
    {
      "map_id": "em_flow_left_账号",
      "source_table": "flow",
      "side": "left",
      "column": "账号",
      "mapping": [
        {"raw": "", "enum": "vacant"},
        {"raw": "未知", "enum": "unknown"},
        {"raw": "不适用", "enum": "na"}
      ],
      "candidates": ["", "未知", "不适用", "N/A", "n/a"]
    },
    {
      "map_id": "em_acct_right_账号",
      "source_table": "acct",
      "side": "right",
      "column": "账号",
      "shared_map_id": "em_flow_left_账号",
      "mapping": [
        {"raw": "", "enum": "vacant"},
        {"raw": "未知", "enum": "unknown"},
        {"raw": "不适用", "enum": "na"}
      ],
      "candidates": ["", "未知", "不适用", "N/A", "n/a"]
    },
    {
      "map_id": "em_flow_na_状态",
      "source_table": "flow",
      "column": "状态",
      "mapping": [
        {"raw": "", "enum": "vacant"},
        {"raw": "未知", "enum": "unknown"},
        {"raw": "不适用", "enum": "na"}
      ],
      "candidates": ["", "未知", "不适用", "N/A", "n/a"]
    },
    {
      "map_id": "em_flow_na_金额",
      "source_table": "flow",
      "column": "金额",
      "mapping": [
        {"raw": "", "enum": "vacant"},
        {"raw": "未知", "enum": "unknown"},
        {"raw": "不适用", "enum": "na"}
      ],
      "candidates": ["", "未知", "不适用", "N/A", "n/a"]
    },
    {
      "map_id": "em_acct_na_产品",
      "source_table": "acct",
      "column": "产品",
      "mapping": [
        {"raw": "", "enum": "vacant"},
        {"raw": "未知", "enum": "unknown"},
        {"raw": "不适用", "enum": "na"}
      ],
      "candidates": ["", "未知", "不适用", "N/A", "n/a"]
    }
  ],
  "empty_role_coverage": [
    {"role": "join_key", "side": "left", "field": "账号", "source_table": "flow", "map_id": "em_flow_left_账号", "shared_map_id": "", "has_empty_token_map": true},
    {"role": "join_key", "side": "right", "field": "账号", "source_table": "acct", "map_id": "em_acct_right_账号", "shared_map_id": "em_flow_left_账号", "has_empty_token_map": true},
    {"role": "filter_eq", "side": "", "field": "状态", "source_table": "flow", "map_id": "em_flow_na_状态", "shared_map_id": "", "has_empty_token_map": true},
    {"role": "order_key", "side": "", "field": "账号", "source_table": "flow", "map_id": "em_flow_left_账号", "shared_map_id": "", "has_empty_token_map": true},
    {"role": "dedupe_key", "side": "", "field": "账号", "source_table": "flow", "map_id": "em_flow_left_账号", "shared_map_id": "", "has_empty_token_map": true},
    {"role": "agg_group", "side": "", "field": "产品", "source_table": "flow", "map_id": "em_acct_na_产品", "shared_map_id": "", "has_empty_token_map": true},
    {"role": "agg_input", "side": "", "field": "金额", "source_table": "flow", "map_id": "em_flow_na_金额", "shared_map_id": "", "has_empty_token_map": true}
  ]
}
```

字段形状裁定（写码前定死）：

- **empty_role_coverage**：字段名/语义照抄硬修包 v1.0.0 dump：`role` / `side` / `field` / `source_table` / `map_id` / `shared_map_id` / `has_empty_token_map`。v1.1.0 契约 `empty_role_coverage_fields` 与此一致。
- **column_origins**：**选项 A** — 不引入。示例与 dump 均无此字段；非 join 角色 `source_table` 默认为 `primary_table`。
- **id 规则**：ops 直引对象主键 `id`；`calc_specs`/`norm_specs` 用 `calc_id`/`norm_id`；`dedupe`/`agg` 用 `order_id` → `order_specs[].id`；禁止冗余 `order_spec_id`。

解析规则：

- `ops[0].op=join` + `join_ref=j1` → 在 `joins` 中找 `id=j1`。找不到 → `contract_incomplete`。  
- `filters[].op`（比较符）必须 ∈ `enums.cmp_op`。  
- `derives` 内条件比较符同属 `enums.cmp_op`（本例无 derive 步；若 `ops` 含 `derive`，必须有对应 `derives[].id`）。  
- 即使 `ops` 写成 `[project, join, …]`，执行仍按固定序；验收用 `ops_order_ignored_but_present_checked` 证明乱序书写与正序书写输出 CELL_EQ。

### v1.1.0 下 `real_table_plan.json` 形状

与上例同一骨架；dump 须含：`ops[]`、各 `*_ref`、参数对象 `id`、双侧 join `empty_token_maps`、`empty_role_coverage`（含 `side` / `map_id` / `shared_map_id`）、`cnt_reducer`。文件落在 `acceptance/direction2_ops_<date>/real_table_plan.json`，**不**覆盖硬修历史包。

---

## 5. 参数对象允许的引用

- `catalog_id` / 已绑定列名  
- `key_norm_id`、按角色挂的 `empty_token_map`（`map_id` / `source_table` / `side`）  
- `order_id`、`calc_id`  
- 契约枚举：`how`、`dir`、`null_order`、`dedupe_keep`、`cmp_op`、`calc_op`、`norm_op`、`agg_semantics_action`  

不允许：任意表达式字符串、未登记 `op`、本地函数名、业务字段名作为操作种类。

---

## 6. 集合外操作如何进不去

1. `ops[].op` ∉ `operator_set` → `version_rejected`，零行。  
2. 执行器静态分派表；未知 `op` 无分支可走。  
3. 无 v1.0.0 自由键旁路（§9）。  
4. `derive` / `filter` 比较符 ∉ `enums.cmp_op` → `version_rejected`，无本地 `else: False`。

---

## 7. 新增操作走版本发布

1. 新版本 `operator_set` 增加该 `op` 与 `param_ref` / `param_object` / `cmp_enum`。  
2. 源码静态分派表增加同名实现；AST 扫描键集合必须相等。  
3. 旧版本计划带新 `op` → 按声明的 `contract_version` 拒绝。

只改 Python、不改版本闭集 → `authority_scan` 失败。

---

## 8. 分派表与 `dispatch_equals_operator_set`（P1-3）

```text
# 示意（写码时落地，本设计确认不写码）
DISPATCH = {
  "join": apply_join_op,
  "filter": apply_filter_op,
  ...
}
```

- 键是源码字符串字面量，不是 `for op in operator_set: DISPATCH[op]=…`。  
- `authority_scan` 规则（例如 `R_OPSET`）：AST 定位模块级 `DISPATCH`（或约定名 `OPERATOR_DISPATCH`），收集 `keys`；与 CURRENT 契约 `operator_set[].op` 做集合相等。  
- 验收 `dispatch_equals_operator_set`：扫描 exit 0，且故意删/加一个源码键时扫描非 0（可用夹具或文档化的负例脚本，不得只 assert 运行时 `.keys()`）。

---

## 9. 版本与旧计划（P1-1）

| 项 | 裁定 |
|----|------|
| 本批契约 | **发布 `v1.1.0`**（含 `operator_set`、`enums.cmp_op`、参数对象改名） |
| v1.0.0 计划 | 缺 `ops` 或使用顶层 `joins`/`keep_row_when`/`derived_columns`/`output_columns` 旧形状而无 `ops` → **拒绝** |
| 兼容层 | **无**。不做第二解释器 |
| 硬修回归 | 新生成 v1.1.0 计划 + dump 进 `acceptance/direction2_ops_<date>/`；历史包只读引用 |

---

## 10. `param_ref` 重复与空 `ops`（P1-2）

| 情况 | 裁定 | 理由 |
|------|------|------|
| 同一 ref 值出现两次（如两个 `join_ref: "j1"`） | **拒绝** `duplicate_param_ref` | 一步一引用，避免双重应用歧义 |
| `ops: []` | **拒绝** `empty_ops` / `contract_incomplete` | 方向 2 执行体是 ops；空列表不是「透传原表」 |

验收：`duplicate_param_ref_rejects`、`empty_ops_rejects`。

---

## 11. 允许改 / 不允许改

```text
本批名称：方向 2 受限操作集
本批目标：v1.1.0 闭集 ops + 静态分派；去掉集合外解释器与 v1.0.0 自由键旁路。

允许改：
- pipeline/synth_contract/executor.py：静态 DISPATCH；固定执行序；读 ops[]
- pipeline/synth_contract/gate.py：ops / param_ref / cmp_op / empty_ops / duplicate_param_ref
- pipeline/synth_contract/authority_scan.py：AST 提取 DISPATCH 键 vs operator_set
- contracts/versions/v1.1.0.json（新建）+ CURRENT 指针
- tests/contract、tests/acceptance/direction2_ops.py、验收包

不允许改：
- 方向 3–9；BACKLOG P2-1/P2-2（除非另批）
- 把 row_accounting / deriv_edge / budget actual 改成全面强制
- 改硬修已关闭的 key_norm / empty / shared_map / agg_semantics.fail 语义
- 运行时动态构建 DISPATCH；保留 v1.0.0 自由键第二解释器
```

---

## 12. 方向 2 完成后的验收标准（可机械执行）

```text
python -m pipeline.synth_contract.authority_scan          # exit 0；含 DISPATCH AST ↔ operator_set
python -m unittest discover -s tests/contract -q          # exit 0
python -m tests.acceptance.direction2_ops                 # exit 0
```

验收包 `acceptance/direction2_ops_<date>/`：`command.txt`、`stdout.txt`、`stderr.txt`、`exit_code.txt`、`env.txt`、`inputs.sha256`、`outputs.sha256`、`rerun.sh`、`rerun_stdout.txt`、至少一份 `*.plan.json`（含 `real_table_plan.json`）。

`stdout.txt` 必须出现（原文 PASS 行）：

1. `unknown_op_rejects`  
2. `derive_unknown_cmp_rejects` — 枚举源 = `enums.cmp_op`  
3. `dispatch_equals_operator_set`  
4. `ops_order_ignored_but_present_checked`  
5. `duplicate_param_ref_rejects`  
6. `empty_ops_rejects`  
7. `v1_0_0_plan_rejected` — v1.0.0 自由键计划 → `version_rejected` / 零行；产物 `v1_0_0_force_reject.json`  
8. `dispatch_ast_negative` — 源码删 `OPERATOR_DISPATCH` 一键 → scan 非 0 + `R_OPSET`；恢复后 0（`tests/acceptance/negative_dispatch_removed.py`）  
9. `accuracy_hardfix_regression` — 仅 v1.1.0 `fixed_op_order+DISPATCH`；乱序书写 CELL_EQ；对照硬修历史 `rows_out`  

`rerun_hash_match=true`，`rerun_input_hash_match=true`，`exit_code=0`。

---

## 13. 写码状态

已落地：`v1.1.0` + CURRENT；**无** v1.0.0 执行旁路；`iter_ops_fixed_order` + `OPERATOR_DISPATCH`（无 materialize→旧 DSL）；gate/executor/authority_scan AST；`tests/acceptance/direction2_ops.py` + `negative_dispatch_removed.py`；包 `acceptance/direction2_ops_20261018/`（§12 **九**条 PASS，含 `v1_0_0_plan_rejected` / `dispatch_ast_negative`，`rerun_hash_match=true`）。不借机做方向 3–9 或 BACKLOG P2。
