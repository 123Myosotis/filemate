# 后续-2 第二批 — 路由 A' / 全链 trace 契约变更方案（设计确认，确认前不改契约文件）

## 0. 上下文复核

handbook hash = `d3d912a04c589cc477dde79dce309715c977b1a9b4fa19a3bd8f73ad6a36f08a`（与 `contracts/HANDBOOK_HASH` 一致）

依据：

- `acceptance/cross_table/A_PRIME_CONTRACT.md` 图 5 + 补充（五元组 /【7】【8】【9】【21】【22】/ 后患 5–6 / 阻3）
- `pipeline/block_analyze/ROUTING_IMPL_CHECKLIST.md` 后续-2 第二批范围、后续-8 阻断说明
- 当前权威契约：`contracts/CURRENT` → **v1.4.0**（第一批已批已落）
- 既有机制：`join_hypothesis.write_trace(kind, body, clock)`；键 `trace_dir`、`trace_filename_pattern`、错误码 `trace_io_failed`

本批目标（一句话）：为 **后续-8（A' 三类轨迹落点）** 与后续 I/B/S/E 接线补齐 trace **种类落名、五元组/独立主键槽位、编排序号、冲突会话、缺运行标识之审计结构无效具体码**；禁止占位名冒充。

**本批不做（明确划出）：**

| 排除项 | 归属 |
|---|---|
| 改生产契约 JSON / `CURRENT` | **待本方案批准后**另开落地回合 |
| 后续-8 业务写入实现 | 第二批落地后的下一顺序任务 |
| A / I / B / S / E 接线代码 | 各自段 |
| 复核输入容量键 | 后续-4 |
| B/S/E **完整**对外契约表正文 | 后续-12（本批只落 **trace 主键槽位名**，不代替 B/S/E 契约表） |
| 第一批已落键的语义回写 | 禁止（ask/entry/write、状态枚举等保持 v1.4.0） |

---

## 1. 允许改 / 不允许改

允许改（批准后落地回合）：

- `contracts/versions/` 新版本（建议 **v1.5.0**）
- `contracts/CURRENT` 指向新版本
- 仅 `cross_table` 下：新增枚举、schema 块、错误码、（若需）文件名模式占位说明
- 配套最小契约测：新枚举可加载；缺第二批键时读取方 → 契约无效类 / 审计结构无效码（与落地读取方一并测）

不允许改：

- 第一批已批数值（8/3/2）与状态/载荷语义
- A' / I / 生产路径代码（本方案回合只出方案）
- 另造第二套 trace 目录机制（必须复用既有 `write_trace`）

---

## 2. 与既有键的关系

| 既有键（v1.4.0） | 本批处置 |
|---|---|
| `trace_dir` | **保留**；继续为目录来源 |
| `trace_filename_pattern` | **保留**默认 `{kind}_{seq}_{ts}.json`；§6 钉死占位语义；**不**把五元组塞进文件名 |
| `trace_io_failed` | **保留**；仅 IO/配置缺失；与「审计结构无效」分码（§5） |
| `user_retry_max_rounds` / 三层上限 | **不动** |

分批期间规则（清单已钉死，本方案重申）：第二批键缺失 → **结构错误**（缺键=契约无效类或 §5 专用码）；**禁止**临时代码硬编码种类字符串冒充契约枚举。

---

## 3. Trace 种类落名

### 3.1 A' 模型调用种类（后续-8 必用）

建议键：`cross_table.enums.layer_recognize_trace_kind`

| 取值 | 语义 | 是否计入追问轮数（N+M+K） |
|---|---|---|
| `layer_propose` | 提案（含内部结构重试的每一次实际模型调用） | **是**（计入 N） |
| `layer_ask_subset` | 要子集 / 缩范围请求 | **是**（计入 M） |
| `layer_formal_review` | 正式复核 | **是**（计入 K） |

硬约束：

- 内部结构重试**不得**另起种类；一律 `layer_propose`，靠五元组「种类内序号」区分。
- 要子集请求体超容量、**未发起**模型调用：无对应落点；冲突原因用第一批已有码（如 `ask_subset_request_over_capacity`）。
- 写入时 `write_trace` 的 `kind` 参数**必须**取自本枚举成员（字符串全等），禁止自由字符串。

### 3.2 全链其它节点种类（槽位预落名；接线时用，本批写入契约以免后续-8/接线再开契约轮）

建议键：`cross_table.enums.routing_trace_kind`（**并集**；实现按节点过滤合法子集）

| 取值 | 节点 | 备注 |
|---|---|---|
| `layer_propose` / `layer_ask_subset` / `layer_formal_review` | A' | 与 §3.1 相同字面量（可在落地时让 `layer_recognize_trace_kind` 为子集引用，或单枚举复用——**默认单枚举 `routing_trace_kind` 含全部，另用 `layer_recognize_trace_kind` 列出 A' 三元子集** 便于 A' 校验） |
| `stage_plan_generate` | B | 计划生成 |
| `stage_plan_review` | B | 计划复核 |
| `stage_exec_model` | S | 阶段内 AI 调用（种类再细分属后续-12） |
| `conflict_four_part_ask` | E | 四段追问 |
| `conflict_user_reply` | E | 用户回复 |
| `orchestrate_decision` | I | 编排决策（【9】） |

**方案默认：** 契约同时承载：

1. `enums.routing_trace_kind` = 上表全集  
2. `enums.layer_recognize_trace_kind` = A' 三元（后续-8 校验只认这三元）

---

## 4. 五元组元数据槽位（A'）

建议键：`cross_table.LayerRecognizeTraceMeta`

写入每个 A' trace **文件内容**的元数据对象（与业务 body 并列或包一层；落地时钉死包装键名 `meta`）。

| 字段名 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `run_id` | string，非空 | **是** | 运行标识（公共锚点） |
| `block_id` | string 或 integer（与块计划一致；落地读取方归一为 string） | **是** | 块标识 |
| `entry_seq` | integer ≥1 | **是** | 本块入口执行序号（I 每次调 A' +1） |
| `call_kind` | string ∈ `layer_recognize_trace_kind` | **是** | 调用种类 |
| `kind_seq` | integer ≥1 | **是** | **该入口执行内**该种类下的第几次（【21】：新入口从 1 起重算） |
| `counts_toward_ask` | boolean | **是** | 是否计入追问；A' 三类恒 `true`（冗余字段，防消费端猜） |

硬约束：

- 缺 `run_id` → §5 审计结构无效，**不落库**。
- 排序键：（`entry_seq`, `call_kind`, `kind_seq`）。
- 文件名仍只含 `{kind}_{seq}_{ts}` 粗筛；`seq` 为全局写盘序号，**不等于** `kind_seq` / `entry_seq`（【22】）。

---

## 5. 缺运行标识 / 审计结构无效 — 具体码

### 5.1 新增错误码

建议加入 `cross_table.error_codes`：

| 码 | 错误类归属 | 何时抛 |
|---|---|---|
| `cross_table_audit_structure_invalid` | **契约无效类同级处置**（调用方按结构错误处理；**不写入** trace） | 落点元数据缺 `run_id`；或 I 编排落点缺「触发源」；或必填五元组/独立主键槽位缺失 |

与 `trace_io_failed` 分界：

| 码 | 场景 |
|---|---|
| `trace_io_failed` | 目录/模式配置缺失、磁盘 IO、空文件 |
| `cross_table_audit_structure_invalid` | 元数据结构不满足审计主键规则（毒数据） |
| `cross_table_contract_invalid` | 契约键缺失/非法（含第二批枚举/schema 键本身缺失） |

**方案默认：** 缺 `run_id` 用 **`cross_table_audit_structure_invalid`**，不用把所有审计问题挤进 `cross_table_contract_invalid`（便于审计扫描）。若你更想少增码，可改 Q2 选复用契约无效类。

### 5.2 读取方缺省行为（落地时实现，本方案钉语义）

| 情况 | 行为 |
|---|---|
| 契约缺 `enums.layer_recognize_trace_kind` 或 `LayerRecognizeTraceMeta` | 任何 A' trace 写入尝试 → `cross_table_contract_invalid`（缺键）；**不**静默跳过、**不**用字面量顶 |
| `run_id` 空/缺 | `cross_table_audit_structure_invalid`；不调用写盘 |
| `call_kind` ∉ 枚举 | `cross_table_audit_structure_invalid`（或提案无效类——**默认审计结构无效**，因已进入落点组装） |

---

## 6. 文件名模式与粗筛（【22】）

既有：`trace_filename_pattern` = `{kind}_{seq}_{ts}.json`

本批**新增说明块**（非改默认值）：

建议键：`cross_table.trace_filename_placeholders`

| 占位 | 类型说明 | 含义 |
|---|---|---|
| `{kind}` | string | = `call_kind` / 节点种类字面量 |
| `{seq}` | integer 字符串 | **写盘全局序号**（现有 `_SEQ`），非五元组字段 |
| `{ts}` | string | 时钟格式化时间戳 |

五元组**禁止**进入文件名必填占位；消费路径：文件名粗筛 → 读 JSON `meta` 重建。

---

## 7. I 编排决策落点（编排序号 + 决策种类）

### 7.1 元数据槽位

建议键：`cross_table.OrchestrateDecisionTraceMeta`

| 字段名 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `run_id` | string 非空 | **是** | 公共锚点 |
| `orchestrate_seq` | integer ≥1 | **是** | **编排序号**（整次运行内 I 决策落点递增） |
| `decision_kind` | string ∈ §7.2 | **是** | 决策种类 |
| `trigger_source` | string ∈ {`user`,`orchestrator`} | **是** | 触发源（缺 → 审计结构无效） |
| `block_id` | string \| null | 否 | 涉及块 |
| `entry_seq` | integer \| null | 否 | 涉及入口执行序号 |
| `trigger_status` | string | 条件必填 | 机械摘录：如 `review_failed` / `conflict` / … |
| `decision_params` | object | **是** | 有理由：含 `rationale_text`（仅用户/模型已给原文）；机械：`{"mechanical": true}` 固定形 |

### 7.2 决策种类枚举

建议键：`cross_table.enums.orchestrate_decision_kind`

| 取值 | 语义 |
|---|---|
| `retry_a_prime` | 重试 A' 入口 |
| `hand_off_e` | 转 E |
| `stop_workbook` | 整表停 |
| `continue_next_block` | 继续下一块 |
| `rerun_all_a_prime` | 全量重跑 A' |
| `retry_portrait_write` | A 写重试 |
| `enter_b` | 齐备→进 B |
| `enter_s` | 进 S / 下一阶段（接线细化时可拆） |

`write_trace` 的 `kind` 固定为 `orchestrate_decision`；细分在 `meta.decision_kind`。

---

## 8. E 冲突会话主键落名（【26】）

建议键：`cross_table.ConflictSessionTraceMeta`

| 字段名 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `run_id` | string 非空 | **是** | 公共锚点 |
| `conflict_session_seq` | integer ≥1 | **是** | 冲突会话序号（跨块/跨阶段可递增） |
| `call_kind` | `conflict_four_part_ask` \| `conflict_user_reply` | **是** | 种类 |
| `kind_seq` | integer ≥1 | **是** | 会话内该种类序号 |
| `block_ref` | string \| null | 否 | 块引用 |
| `stage_ref` | string \| null | 否 | 阶段引用 |

说明：E 完整四段协议形状属后续-12；本批只钉 **trace 主键槽位**，解锁「会话可重建」。

---

## 9. B / S 独立主键槽位（预落名，供后续-12 引用）

### 9.1 B

建议键：`cross_table.StagePlanTraceMeta`

| 字段 | 类型 | 必填 |
|---|---|---|
| `run_id` | string | 是 |
| `plan_generation` | integer ≥1 | 是 |
| `call_kind` | `stage_plan_generate` \| `stage_plan_review` | 是 |
| `kind_seq` | integer ≥1 | 是 |

### 9.2 S

建议键：`cross_table.StageExecTraceMeta`

| 字段 | 类型 | 必填 |
|---|---|---|
| `run_id` | string | 是 |
| `stage_id` | string | 是 |
| `call_kind` | string ∈ routing 枚举之 S 子集 | 是 |
| `kind_seq` | integer ≥1 | 是 |
| `input_block_ids` | string[] | 否（查询用） |

---

## 10. 版本与数值

| 项 | 提案 |
|---|---|
| 版本号 | **v1.5.0**（相对 v1.4.0 的审计/trace 能力扩展） |
| 新数值上限 | **本批不新增**数量型上限；序号规则用 schema `minimum: 1` |
| 与第一批数值 | 无关；8/3/2 保持 |
| `CURRENT` | 落地回合切换 |
| 缺省行为 | 见 §5.2；无「默认种类名」「默认 run_id」 |

---

## 11. 批准后落地清单（下回合机械执行，本回合不做）

1. 复制 `v1.4.0.json` → `v1.5.0.json`，写入 §3–§9  
2. 更新 `CURRENT`  
3. 补契约最小测：枚举可加载；缺 `LayerRecognizeTraceMeta` / 缺种类枚举 → 契约无效类；专用码在 `error_codes` 列表中  
4. **不**在本落地回合改 `layer_recognize.py` 写 trace（那是后续-8）  
5. 出 `EVIDENCE_CONTRACT_BATCH2.md`：键列表 + 哈希 + 测试数  

---

## 12. 与后续-8 / 线程 B 的衔接

第二批落地后**立即解锁**：

- 后续-8：A' 对 `layer_propose` / `layer_ask_subset` / `layer_formal_review` 各至少一落点 + 五元组 meta + 测试  

仍不代替：

- 后续-12：B/S/E 对外契约表（行为/载荷）；本批仅预落 trace 槽位名  
- A 段画像映射（不依赖 trace 写入完成与否的业务映射可先设计，但 A **开工**仍门禁后续-8）  

---

## 13. 待你批的显式选择题

| # | 题 | 方案默认 |
|---|---|---|
| Q1 | 新版本用 `v1.5.0` 还是 `v1.4.1`？ | **v1.5.0** |
| Q2 | 缺 `run_id` 用新码 `cross_table_audit_structure_invalid` 还是复用 `cross_table_contract_invalid`？ | **新码**（§5） |
| Q3 | A' 种类枚举单独键 vs 仅用全链 `routing_trace_kind`？ | **双键**（§3.2：全集 + A' 三元子集） |
| Q4 | `block_id` 类型坚持 string，还是允许 int 并由读取方归一？ | **允许 int/string，读取方归一 string** |

无其它开放题。默认可按上表执行。

---

## 14. 批准附带定稿（落地必须遵守；2026-10-20）

方案按默认批准，并钉死下列四点（落地证据逐条对账）：

### 定稿 1 — 审计码独立

`cross_table_audit_structure_invalid` 必须出现在 `error_codes` 列表中；抛出时 `code` 字符串全等该字面量，**禁止**包装进 `cross_table_contract_invalid`。契约内写清与 `cross_table_contract_invalid` / `trace_io_failed` 三码分界（见落地键 `error_code_boundaries`）。

### 定稿 2 — Q3 选用**方案甲**（单一真相）

**不采用**方案默认「双枚举」。

| 键 | 形态 |
|---|---|
| `enums.routing_trace_kind` | 全集（唯一种类字面量真相） |
| `layer_recognize_kinds` | **引用列表**（三元），每个成员必须 ∈ `routing_trace_kind`；**不是**第二枚举 |

理由：避免两份枚举漂移；A' 校验只读引用列表；新增全链种类只改一处全集。最小测断言 `set(layer_recognize_kinds) ⊆ set(routing_trace_kind)`。

### 定稿 3 — block_id 归一碰撞

归一规则：原始值允许 int/string → `str(raw)`（拒绝 bool）。**同一 `run_id` 内**两个不同原始 `block_id` 归一到同一字符串 → 抛 `cross_table_audit_structure_invalid`，不落库。契约见 `LayerRecognizeTraceMeta.block_id_normalize_rules`。

### 定稿 4 — decision_params 形态

见 `OrchestrateDecisionTraceMeta.decision_params_rules`：

- 机械决策若 `rationale_text` 非空 → 审计结构无效  
- 有理由决策若仅 `{mechanical: true}` → 审计结构无效  
- `rationale_text` 与 `mechanical: true` 不得同时非空  

### 提醒钩子（不阻塞本批）

`StageExecTraceMeta.subsequent_12_hook`：后续-12 S 契约表必须约束——S 触发跨块归属④回滚时落点必带 `input_block_ids`，否则 S 侧结构无效。

---

## 15. 本方案状态

**已批准（默认 + §14 定稿）。** 落地见 `acceptance/cross_table/EVIDENCE_CONTRACT_BATCH2.md`。
