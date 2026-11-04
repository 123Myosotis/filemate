# 真实端到端 demo — 验收基线

本批不新增 triage 信号，不改 `d3-summary-1` / `d3-evidence-1` / `d4-report-1`。

## 源表

`网安行业各大公司真实评价（2024） .xlsx`

- 多 sheet、中文列名、有合并单元格。
- 主表稠密数据行约 98（`max_row` 含空行到 312），未落在偏好的 400–1000 带内。见 backlog `P2-E2E-1`。

## PASS

1. `real_table_opens`
2. `simple_path_silent_delivery`
3. `report_browser_readable`（优先 Tabbit；失败则回退源码文本检查）
4. `complex_path_awaits_user_yes`
5. `phase_timings_recorded`

## 耗时字段

`timings_ms.json`（人读，毫秒浮点，**不**进 `outputs.sha256`）：`read_ms` / `gate_ms` / `triage_ms` / `execute_ms` / `render_ms` / `report_ms`。`execute_ms` 含首次嵌入渲染；`render_ms`/`report_ms` 为再次调用 `render_report` 的单独计时。

`timings_schema.json`（进 `outputs.sha256`）：只钉阶段名是否齐全、是否全为正。墙钟浮点不可复现，与方向 2 的 `PHASE_MS` 同类处理。
