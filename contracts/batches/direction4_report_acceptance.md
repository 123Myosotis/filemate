# 方向 4 可读报告 — 验收基线

## contract_hash 显示

选定方案 A。报告里的 `contract_hash` 是摘要字段的原文，渲染器不截断。验收 fixture 与方向 3 写入摘要的值都是 `contracts/versions/v1.1.0.json` 的 64 位文件哈希。不用方案 B 的截断加路径。

## 返回字段

`render_report` 的返回值只有这四个键，缺一不可，顺序固定：

`report_status`、`report_html_path`、`report_text_path`、`evidence_path`

`write_failed` 时，没写成的那一份路径是 `null`，键仍然在。不允许改成空 dict，也不允许省掉没写成的键。

`report_status` / `report_html_path` / `report_text_path` 挂在 `triage_flow` 的返回值上，不写入 `execution_summary`。

## P2-2 / P2-3

正文（HTML、纯文本）或 `report/{version}/{plan_id12}.meta.json` 任一份没写成，`report_status=write_failed`。已经写成的文件保留，不删除。没写成的那份路径返回 `null`。HTML 写成而 txt 没写成时，`report_html_path` 有值，`report_text_path` 为 `null`。meta 没写成时，两份正文路径仍返回已写成的相对路径。

实现注释在 `pipeline/synth_contract/report_render.py` 的 `render_report`。

## 验收 11 条与 fixture

| PASS | fixture |
|------|---------|
| `report_projects_summary_fields` | `fixtures/d4/01_projects_summary.json`，plan dump `fixtures/d4/simple.plan.json` |
| `report_omits_cell_values` | `fixtures/d4/02_cell_rows.json`（单元格只在这个文件里） |
| `report_input_invalid_keeps_success` | `fixtures/d4/03_missing_evidence_path.json` |
| `report_not_called_on_complex` | `fixtures/d4/04_complex_pre_execute.plan.json` |
| `evidence_file_unchanged` | `fixtures/d4/05_evidence_bytes.json` |
| `report_echoes_evidence_path_verbatim` | `fixtures/d4/06_old_evidence_path.json`。落盘 `report/v1.1.0/63aff0f8db2c.html` 与 `.txt`，页眉为 `evidence/v1.1.0/63aff0f8db2c.json`，两份都进 `outputs.sha256` |
| `report_body_idempotent_across_rerenders` | `fixtures/d4/07_idempotent_summary.json`。正文进 `outputs.sha256`，`.meta.json` 不进 |
| `report_html_escapes_special_chars` | `fixtures/d4/08_script_summary.json`。可见文本用 `html.unescape(源码字段值)` |
| `report_status_is_separate_from_execution_status` | `fixtures/d4/09_write_fail_summary.json`（挡住 txt） |
| `report_html_escapes_evidence_path_but_preserves_visible` | `fixtures/d4/10_amp_evidence_path.json` |
| `report_marks_empty_output` | `fixtures/d4/11_empty_manifest.json` 与 `fixtures/d4/11_coords.json` |

`inputs.sha256` 纳入上表 fixture、`fixtures/d4/simple.plan.json`、`contracts/versions/v1.1.0.json`、`contracts/CURRENT`。设计确认文档不纳入，避免改设计文字就让已生成的输入哈希失效。
