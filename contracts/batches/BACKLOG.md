# FileMate 批次 backlog

按排期先后；不影响已关闭批的验收结论。

## P2 — 准确性硬修 / 方向 2 之后（不阻塞方向 3）

### P2-1 数据路径验证 `agg_semantics.count.vacant=skip`

在真实数据路径上：金额列插入空值（vacant），跑 count，结果应等于非空行数。证明 `agg_semantics.count.vacant=skip` 在执行路径生效，而不只是契约校验。

### P2-2 真实表全量输出进验收包

`acceptance/accuracy_hardfix_20261018/` 增加 `real_table_full_output.json`，把真实表回归的全部输出行落地，作为逐格对账的完整依据。现有 `real_table_result.json` 的 `sample` 不够。

### P2-3 `real_table_result.json` 带契约钉

方向 2 包（及后续执行回归产物）的 `real_table_result.json` 必须含 `contract_version` / `contract_hash`，与当时执行计划一致，便于跨包对账。

### P2-4 `_rerun_body_stderr.txt` 纳入 `outputs.sha256`

方向 2 起验收包：`_rerun_body_stderr.txt` 与 `_rerun_body_stdout.txt` 一并进 `outputs.sha256` 并由 rerun 复算（注意 PHASE_MS 等非稳定行需先规范化或迁出，避免假失败）。

### P2-5 `accuracy_hardfix_regression` PASS 行含 `cell_eq=True`

方向 2 回归 PASS 明细须显式带 `cell_eq=True`（或失败时 `cell_eq=False` + 差异），避免只报 `path=DISPATCH rows=…` 无法一眼确认单元格等价。

### P2-6 `REQUIRED_PASS_TAGS` 清单来源说明

验收模块内 `REQUIRED_PASS_TAGS`（或等价清单）必须在 handbook 或对应批次设计/验收文件中有权威来源说明：清单与 §验收 PASS 行一一对应，禁止只存在于测试源码。

---

## P2 — 方向 3 设计盘点的契约缺口（不阻塞方向 3 写码启动；信号不得临时凑数）

来源：`contracts/batches/direction3_mechanical_triage_design.md` §4。

### G3-1 `conflicts[]` 计划载体

`enums.conflict_kind` 已有，缺稳定 `plan.conflicts[]`（kind / scope / impact）供机械枚举。

### G3-2 delivery 检查项载体

`delivery_check_item` 无对应强制计划块；随方向 4/6。

### G3-3 （已收回本批，不再挂 backlog）

方向 3 设计 §3.3：本批拆除 `validate_binding` 的 confidence→`await_user`。不在此重复排期。

### G3-4 语义未覆盖的非分数载体

`semantic_cover` 目前绑 confidence；需非分数声明字段后再信号化。

### G3-5 `budget_unsatisfiable` 字面量

不发明；本批仅用已有 `over_budget` / `budget_actual_over`。

### G3-6 运行时信号回写形状

除 `shared_map_mismatch` 前缀外，缺通用 `runtime_signals[]` 枚举。方向 3 只登记 `plan.runtime_notes: string[]`（v1.1.1），不扩展通用枚举。

### G3-7 `triggers_fired[]`

`plan.triggers` 是预案，不能当已触发。需要契约字段 `triggers_fired[]` 之后才能做信号。方向 3 **不**使用 `declared_trigger`。

---

## P2 — 方向 3 设计已裁定、仍记一笔（不阻塞）

### P2-7 「非空」是否 strip

已裁定：字符串信号一律 strip 后判空。见方向 3 设计 §2.1。

### P2-8 `await_user` 重复写保护

已裁定：仅当 status 为 `ready` 时写入 `await_user`；已是 `await_user` 不重写。见 §3.2。

### P2-9 `inputs.sha256` 是否含设计确认文档

本批验收 **不**把 `direction3_mechanical_triage_design.md` 列入 `inputs.sha256`。若后续批次要钉设计文件，另开条目。

---

## P2 — 方向 4 关闭后（不阻塞关闭）

### P2-D4-1 文件存在时的完整报告示例

`d4aaaa000001.html` 因 `evidence/v1.1.0/d4aaaa000001.json` 不在验收包内而带 `evidence_file_missing: true`。补一份最小 `d3-evidence-1` 到该路径，让这份 HTML 展示证据文件存在时的报告。

### P2-D4-2 `evidence_file_missing` 放进段落标签

该行目前是裸文本。改为 `<p>evidence_file_missing: true</p>`（或放在 `<footer>` 里的同一段落）。

---

## P2 — 真实端到端 demo（不阻塞本 demo 关闭）

### P2-E2E-1 语料缺「≥400 稠密行 + 合并单元格」同表

`网安行业各大公司真实评价（2024） .xlsx` 有合并单元格、中文列名、多 sheet，但主表稠密数据行约 98（`max_row` 含空行到 312）。`刁难抽查/已下发任务详情.xlsx` 等有 2900+ 行与中文列名，但合并单元格为 0。本 demo 选用前者以覆盖合并单元格。需要一份同时满足 400–1000 稠密行与合并单元格的真实业务表后再补回归。

### P2-E2E-2 合并表头污染列名

同上表：合并单元格把长说明文字写进第 1 行，openpyxl 读出的「列名」含大段非字段文本。本 demo 未改读表策略（不顺手修）。方向 5 前若要做真实表，需要独立的表头/合并单元格规范化设计确认。

---

## P3 — 契约结构整理（不阻塞后续-8 / A 段）

### P3-1 `layer_recognize_kinds` 位置与引用列表统一

观察（BATCH2 审后 backlog）：`layer_recognize_kinds` 落在 `cross_table` 根下而非 `enums` 内。未来引入更多「引用列表」类结构时，考虑统一放置约定，或采用 JSON Schema `$ref` 机制，避免根级/enums 混放。**不阻塞**后续-8 与 A 段画像映射。

### P3-2 subset 类 `kind_seq` 多轮递增测（可选）

后续-8 批准时登记。当前 `test_kind_seq_increments_within_same_entry` 已证 **propose** 同入口递增；**不阻塞**。可选补：同一 `entry_seq` 内多次 `layer_ask_subset` 时 `kind_seq` 为 `[1,2,…]`（与 propose 同源计数器，单独验证可选）。

### P3-3 `structure_status` 枚举进契约 — **已关闭**

后续-2 第三批已落 `cross_table.enums.structure_status` = `{upstream_absent, layers_recognized}`（v1.6.0）。证据：`acceptance/cross_table/EVIDENCE_CONTRACT_BATCH3.md`。

### P3-4 A→画像是否引入转换层

A 段批准观察。现状：镜像映射（同名 1:1）正确。决策时点：**等 B 契约表裁定**（B 读画像结构定型时；B_CONTRACT 已钉镜像）。若 B 需不同画像结构，A 应变真正转换层。归属：B 定型后（第三批未改映射；仍开放至明确需要转换层时）。

### P3-5 画像持久化 — **已关闭**

I 接线批次已落地 `FilePortraitStore`；证据见 `EVIDENCE_I_ORCHESTRATE.md`。不再开放。

### P3-6 `plan_regenerate_max` + B 计划 Schema — **已关闭**

后续-2 第三批 / 后续-16 已落：`plan_regenerate_max`（暂挂值 3）、`StagePlanSchema`、`enums.stage_rollback_point`（仅①–③）。证据：`EVIDENCE_CONTRACT_BATCH3.md`。数值表另批可改暂挂值。

### P3-7 阶段内重试上限专用能力键（后续-2 第四批）

S 契约表批准登记。现状：S 阶段 AI 结构无效重试用 `plan_regenerate_max` **代位**。须落专用正整数键（≥1）；落地后 S 改契约读专用键，停止代位。归属：后续-2 **第四批**。证据指向：`S_CONTRACT.md` §9。

### P3-8 阶段 AI 用尽时 `needs_user_ask` 语义借用（非阻塞）

S 批准澄清 1。现状：用尽后门闸借用计划顶层 `review_result.needs_user_ask`（B 计划级判断 ≠ 本次 AI 失败是否需问）。风险：B 判无需问时可能沉默回滚。候选：A 强制触 E / B 只看 `allow_user_ask` / C 用尽后模型追问一次。归属：后续-2 **第四批**或 S 后续优化。见 `S_CONTRACT.md` §4。

### P3-9 `review_failed` 增加 I 入口重试累计次数（审计）

E 定稿登记。可选审计字段：I 转入时附带本块入口重试累计次数。不阻塞 E 实现。归属：后续-2 第四批或 I/E 接线批。

### P3-10 B 侧「找不到块集合」缺失行为测

E 定稿登记。`input_block_ids` 已强制；补：B 组装上报时块集合不可得 → 结构无效/不上报。不阻塞 E。归属：B 测补强。

### P3-11 I 转交 B 上报包时的信封组装

E 定稿登记。I 附加 `run_id` / `entry_kind=from_b_via_i_ownership` / `conflict_session_seq`。不阻塞 E 本体。归属：I→E 接线。

### P3-12 E 对「主动放弃 vs 用尽转 E」的四段语义区分

E 定稿登记。用户 `stop_workbook` 与追问用尽兜底同名 decision，四段/说明措辞应可区分。不阻塞首版实现。归属：E 实现后续优化。

### P3-13 A' trace 目录清理 / 总容量上限

来源：`CONTRACT_CHANGE_TRACE_AUDIT.md` 提醒 C。审计落盘补强后，单块入口最坏约 7×(prompt+raw)≤64KiB 级文件，多块时目录可达数十 MB。需生产清理策略或总容量上限。**不阻塞**第 1 步审计落盘；与第 2 步「提案输入容量」为兄弟问题。

### P3-14 T10 应验证 `prompt_out` 每次新建

来源：第 1 步落地审后非阻塞建议。现状 `test_t10_review_two_calls_independent_prompt_out` 只断言 ask_subset / formal_review 的 `prompt_hash` 不同，**不证**调用方每次 `prompt_out = {}` 独立新建（细节 B）。可选补：断言两次调用收到的 dict `id(...)` 不同，或哨兵污染不被第二次覆盖。**不阻塞**第 2 步。

### P3-15 `raw_chars` 与非 dict `raw_full` 口径一致性

来源：第 1 步落地审后非阻塞建议。当 `raw="hello"`（非 dict）时：计长用 `json.dumps(..., ensure_ascii=False)` → 含引号为 7 字符，落盘 `raw_full` 可保留裸字符串 5 字符，口径不一致。需裁定：非 dict 时 `raw_chars` 跟 dumps 串还是跟落盘形态。**不阻塞**第 2 步。

### P3-16 大规模分片下合并输入 vs `review_input_capacity`

来源：第 2 步实现方案审后建议 D。更大规模表（例如 100+ 矩形、8 片）时，合并输入（`layer_summaries` + `header_band_texts` + boundary + 跨片 contains）可能逼近 `review_input_capacity`。第 2 步落地验收阶段 D 须**记录**各表合并输入实际大小，供未来容量数值表参考。本条不阻塞第 2 步方向；不在本步拍新容量值。
