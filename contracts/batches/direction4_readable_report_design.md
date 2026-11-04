# 方向 4 — 人机可读证据渲染（设计确认，确认前不写代码）

## 0. 上下文复核

我读过的 handbook hash = `d3d912a04c589cc477dde79dce309715c977b1a9b4fa19a3bd8f73ad6a36f08a`

已关闭批：

- 准确性硬修：`acceptance/accuracy_hardfix_20261018/`（只读）
- 方向 2：`acceptance/direction2_ops_20261018/`（只读）；`CURRENT=v1.1.0`；执行路径 = `ops[]` + `fixed_op_order` + `OPERATOR_DISPATCH`
- 方向 3：`acceptance/direction3_triage_20261018/`（只读）。已交付 `execution_summary`（`d3-summary-1`）与 `evidence_path`（`d3-evidence-1`）。`execution_status` 闭集见 `contracts/batches/direction3_triage_acceptance.md`

与本批相关的手册条目复述：

1. 裁定 1：强制层 / opt-in / 暂不强制仍成立。方向 4 不把 `row_accounting`、`deriv_edge`、`delivery_blocks` 改成全面强制。`delivery_blocks` 仍是 opt-in，本批不发明交付检查块（G3-2 留给方向 6）。
2. 裁定 2：方向 4 属于「可渐进」：先静默执行 + 摘要 + 抽样，一键证据后做。方向 3 已完成静默执行、摘要和 `sample_ref`。本批只做阅读层，不做一键打包的后半（多文件归档、签名）——那部分若要做，另开批次。
3. 裁定 3 / 方向 1 不可回退：无 `FILEMATE_CONTRACT_GATE`；无契约库 → `ContractLibraryError`；无运行时开关回到旧启发式。
4. 两条铁律：只动本方向相关文件。不兜底让用例过、不留逃生门、接口不出现业务字段名、不用 confidence 做分流、不用模板化问法向用户提问。本地填报告不是问法。
5. 固定队列中本批位置：方向 1（完成）→ 准确性硬修（完成）→ 方向 2（完成）→ 方向 3（完成）→ **方向 4 设计确认** → 写码 → 方向 5…
6. 本批允许改 / 不允许改（按模板）：

```text
本批名称：方向 4 人机可读证据渲染
本批目标（一句话）：把已交付的 execution_summary、evidence_path、sample_ref 渲染成业务人员能直接打开的 HTML 与纯文本，不重算表、不加信号、不改 triage。

允许改：
- 文件：新增 pipeline/synth_contract/report_render.py；triage_flow.py 仅在 execution_status=success 且 execution_summary 已通过结构校验之后调用渲染器
- 符号：render_report(summary, evidence_body) → {report_status, report_html_path, report_text_path, evidence_path}
- 契约：不改 v1.1.0 枚举，不发布新契约版本。报告 schema 为本地 d4-report-1，与 d3-summary-1 / d3-evidence-1 同级，不进 contracts/versions

不允许改：
- 方向 5–9
- triage.py 的信号、triage_confirm 的两个动作闭集、executor / ops / DISPATCH / fixed_op_order / key_norm / empty / agg_semantics.fail
- validate.py 的 confidence 路径
- d3-evidence-1 的字段（不回写报告路径）
- execution_status 闭集（不因报告失败改成 summary_failed 或 complex）
- 用调测试断言迎合坏实现
- 本地兜底 / 逃生门 / 业务字段名上接口 / confidence 分流 / 模板化问法

本批验收命令（可机械执行）：
- python -m unittest tests.contract.test_report_render -q
- python -m tests.acceptance.direction4_report
```

声明：以上复述与 `contracts/EXECUTION_HANDBOOK.md` 一致；hash 与 `contracts/HANDBOOK_HASH` 一致。若无法复述或 hash 不符，本批作废。

---

## 1. 目标（一句话）

方向 4 是阅读层：方向 3 已经算出的 `execution_summary`、`evidence_path`、`sample_ref` 投影成 HTML 和一段纯文本。不重新执行、不读表、不新增 triage 信号、不改分流。

---

## 2. 渲染是本地模板，不是 AI

报告由本地模板按固定字段填空。不调用模型。

模板放在 `report_render.py` 里的字符串常量，与 `d3-summary-1` 的字段一一对应。禁止在模板或填空代码里写 `if 字段名 == "..."` 这类按业务字段名分支的判断。多语言、多主题另起批次，本批只有这一套常量。

HTML 模板对 `human_text` 和所有字符串字段做 `html.escape()` 后再填入。纯文本不做 HTML 转义。

因此本批没有「AI 结构校验」。输入侧只复查方向 3 已经要求的 `d3-summary-1` 必填字段是否仍在（缺字段 → `report_status=input_invalid`，见 §5）。`human_text` 原样抄进报告（HTML 侧先转义），不重写、不翻译、不再次请求 AI。

方向 3 的真实 AI 摘要（超时 / 非 JSON）仍不在本批。摘要失败时方向 3 不会交出 `execution_summary`，渲染器不会被调用。

---

## 3. 报告不含业务单元格

报告允许出现的值只有摘要和证据里已有的标识与计数：

- `contract_version` / `contract_hash`
- `triage` / `fired_signals`
- `primary_table`
- `ops_present`
- `row_count_out` / `column_count_out`
- `evidence_path`（见 §4，原样抄字符串）
- `human_text`（方向 3 已生成的那一段；HTML 中已转义）
- 抽样显示 `sample_size: <int>`（抽样条数）和 `sample_ref_kind: manifest|coords`（类型）；空输出再加 `output_is_empty`（见下）

`sample_coords` 的具体坐标值、`sample_manifest_id` 的具体 id 不出现在报告正文。它们留在 `d3-evidence-1` 里供机器审计。若将来需要「详细模式」把坐标打进报告，另起批次。

抽样两行的取数只看 `sample_ref` 的形状，不读表：有非空 `sample_coords` 时 `sample_ref_kind=coords`，`sample_size` 等于该列表长度，坐标字符串本身不打印。只有 `sample_manifest_id` 时 `sample_ref_kind=manifest`，不把 id 解析成条数；方向 3 当前只用 manifest 表示空输出，此时 `sample_size` 为 0。两者都没有则 `input_invalid`。

`sample_ref_kind=manifest` 且 `sample_size=0` 时，报告额外加一行 `output_is_empty: true`（方向 3 空输出场景的唯一标记）。`sample_ref_kind=coords` 时这行为 `output_is_empty: false`，或不出现该行。不改方向 3 的 manifest 语义。

不允许读取 `rows`、表网格、单元格原文。因此本批不定义脱敏规则。以后若有批次要把单元格写进报告，由该批定义脱敏，不在本批预埋。

---

## 4. 和 `evidence_path` 的关系

`evidence_path` 指向的 `d3-evidence-1` 文件是机器证据，本批不改它的 schema，也不把报告路径写回去。

报告是阅读证据，单向引用机器证据：HTML 与纯文本的页眉写出 `contract_version`、`schema_version=d4-report-1`，以及从 `execution_summary.evidence_path` **原样抄来的那一串**。方向 4 不调用 `plan_id12`，不重新计算 id。

### 4.1 `plan_id12` 跨批一致性（P0，写码前定死）

选定 **A**。不采用 B（双算法是暗门），不采用 C（不迁移已关闭包）。

- 方向 4 以及此后新批次，凡是自己计算 `plan_id12` 的，统一用「去掉 `_gated_*` 再 dumps」的算法（方向 3 设计 §5.3）。
- 方向 3 已关闭包里的 `evidence/v1.1.0/63aff0f8db2c.json` 是历史保留。不重跑、不改名、不改 `outputs.sha256`。
- 方向 4 的报告文件名和页眉都不重算。从 `execution_summary.evidence_path` 原样取出 `evidence/{contract_version}/{plan_id12}.json` 这一段，报告写成：

```text
evidence/{contract_version}/{plan_id12}.json    # 只读；路径字符串来自摘要，不重算
report/{contract_version}/{plan_id12}.html
report/{contract_version}/{plan_id12}.txt
report/{contract_version}/{plan_id12}.meta.json # 见 §5，不进 outputs.sha256
```

页眉里的 `evidence_path` 与传入字符串的关系（与 §2 的 `html.escape()` 一起读）：

「逐字符相同」指：页眉 HTML 源码里 `evidence_path` 字段值 = `html.escape(execution_summary.evidence_path)`；用户浏览器渲染后看到的文本 = `execution_summary.evidence_path` 原文。纯文本报告里是原文。「不含任何新算出的 id」指可见文本里没有任何 `plan_id12()` 调用产物。

旧 id `63aff0f8db2c` 被传入时，可见文本仍是 `evidence/v1.1.0/63aff0f8db2c.json`，不出现另算出来的 id。路径里的 `&` 等字符只在 HTML 源码中转义，不改变可见文本，也不改变纯文本。

指向的证据文件不存在时（旧包已清理）：`evidence_path` 字段照抄，页脚追加 `evidence_file_missing: true`。文件存在则不写这一行。不因此把 `report_status` 改成失败。

PDF 不在本批。HTML 与纯文本已覆盖「打开就能看」；PDF 需要额外依赖和字体，排到本批之后，不在这里预写渲染器。

---

## 5. 失败时回退到什么

报告失败不改变方向 3 的交付：

| 条件 | `report_status` | 执行结果 |
|------|-----------------|----------|
| 摘要缺必填字段，或 `evidence_path` 为空 | `input_invalid` | `execution_status` 保持 `success`；`rows` 不动；不进 §8；不改 triage |
| 目录或文件写失败 | `write_failed` | 同上。不把失败伪装成摘要成功的反面 |
| 两份文件都写完且字段齐 | `written` | 返回两个相对路径 |

没有 AI，所以没有超时分支。`report_status` 闭集只有 `written` | `input_invalid` | `write_failed`。它不是 `execution_status` 的第四个值。

`report_status`、`report_html_path`、`report_text_path` 是 `triage_flow` 返回值上的新字段。`execution_summary` 与 `execution_result` 完全不动，`d3-summary-1` 保持稳定。因此可以同时存在 `execution_status=success` 与 `report_status=write_failed`。

失败时不删除已有的 `execution_summary`，不把 `execution_summary` 改成 null，不把 `delivery_success` 改成 false。业务人员仍可看摘要 JSON 与证据 JSON。

同一 `plan_id12` 再次渲染是幂等覆盖：HTML 与纯文本正文不写时间戳，两次渲染的正文 sha256 相同。`rendered_at`（ISO 8601）只写入 `report/{contract_version}/{plan_id12}.meta.json`。该 meta 文件不进 `outputs.sha256`（时间会变）。报告正文进 hash。

---

## 6. 只覆盖 simple 成功路径

渲染器只在 `execution_status=success` 且 `execution_summary` 非空时调用。

complex 的 `user_prompt` / `conflicts[]` / `lock_record` 由方向 3 的输出方式直接呈现给用户。方向 4 不改变该呈现方式，也不额外生成 HTML 确认单。方向 5 或专门的 UI 批次再讨论。

`summary_failed` 与执行前/执行后的 `complex` 都不调用渲染器。

---

## 7. 验收（写码后，本文件批准前不写）

```text
python -m unittest tests.contract.test_report_render -q
python -m tests.acceptance.direction4_report
```

PASS 行（写码时按此实现，批准前不写）：

1. `report_projects_summary_fields` — 成功摘要渲染后，HTML 与 txt 都含 `evidence_path`、`primary_table`、`row_count_out`、`human_text`，且 `report_status=written`。
2. `report_omits_cell_values` — 传入带 `rows` 的执行结果时，两份报告正文都不含这些单元格字符串。
3. `report_input_invalid_keeps_success` — 摘要缺 `evidence_path` → `report_status=input_invalid`，且调用方不得改 `execution_status`。
4. `report_not_called_on_complex` — `execution_status=complex` 时渲染器不被调用，无 `report/` 文件。
5. `evidence_file_unchanged` — 渲染前后 `d3-evidence-1` 字节一致（不回写报告路径）。
6. `report_echoes_evidence_path_verbatim` — 传入 `execution_summary.evidence_path = "evidence/v1.1.0/63aff0f8db2c.json"`（旧 id，无特殊字符）。可见文本照抄该字符串，不出现任何 `plan_id12()` 产物。
7. `report_body_idempotent_across_rerenders` — 同一摘要渲染两次，HTML 与 txt 正文 sha256 一致；两份 `.meta.json` 的 `rendered_at` 不同。meta 不进 `outputs.sha256`。
8. `report_html_escapes_special_chars` — `human_text` 含 `<script>`。HTML 源码层是 `&lt;script&gt;`。可见文本不跑浏览器：`html.unescape(源码字段值)` 等于原文 `<script>`。txt 层是 `<script>`。
9. `report_status_is_separate_from_execution_status` — 构造 `execution_status=success` 且 `report_status=write_failed`。两者独立；`execution_summary` 仍是 `d3-summary-1`，不含 `report_status`。
10. `report_html_escapes_evidence_path_but_preserves_visible` — 传入 `evidence_path = "evidence/v1.1.0/a&b.json"`。选定方案 A，不用 BeautifulSoup/lxml，不用 headless browser。源码层查 `a&amp;b.json`；`html.unescape(源码字段值)` 反解层查 `a&b.json`；txt 层查 `a&b.json`。可见文本里没有另算的 id。
11. `report_marks_empty_output` — 传入 `sample_ref={"sample_manifest_id": "..."}`，报告正文含 `output_is_empty: true`。传入 `sample_ref={"sample_coords": ["row:0"]}`，正文不含该行，或该行为 `output_is_empty: false`。坐标字符串 `row:0` 不出现在正文。

验收包按手册 §6 F：`command.txt` / `stdout.txt` / `stderr.txt` / `exit_code.txt` / `env.txt` / `inputs.sha256` / `outputs.sha256` / `rerun.sh` / `_rerun_body_stdout.txt`，以及至少一份 `*.plan.json`。不调用真实 AI。

---

## 8. 确认前不写码

未批准前不新增 `report_render.py`，不改 `triage_flow.py`，不改证据 schema。
