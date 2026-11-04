# 方向 3 补丁 — evidence 落盘（1 页说明，非完整设计确认）

## 根因

`execution_summary.evidence_path` 由 `evidence_relpath(plan)` 算出并写入摘要。`evidence_body` 只放进返回值内存字段 `out["evidence"]`，从未写成 `evidence/{contract_version}/{plan_id12}.json`。方向 4 按路径找文件时得到 `evidence_file_missing: true`。

## 三条裁定（本补丁）

1. `run_triaged` 简单路径、摘要已通过结构校验之后、调用 `render_report` **之前**，必须把 `d3-evidence-1` 写到 `evidence/{contract_version}/{plan_id12}.json`。`plan_id12` 用去掉 `_gated_*` 的算法（与方向 4 §4.1 / 方向 3 §5.3 一致）。该相对路径即 `execution_summary.evidence_path`。
2. 落盘失败：`execution_status=summary_failed`（闭集不变），`execution_summary=None`，`delivery_success=false`，`plan.runtime_notes` 追加一行前缀 `evidence_write_failed`（审计，**不是** triage 信号）。不调用渲染。
3. 写入路径必须等于摘要里的 `evidence_path` 字符串（逐字节）。路径中的 12 位 id 必须等于 `plan_id12(plan)`（去 `_gated_*`）。

## 不改

信号表、`d3-summary-1` / `d3-evidence-1` / `d4-report-1` 字段、`execution_status` 闭集、方向 4 模板。

## 验收增补（3 条）

- `evidence_file_written_on_simple_path`
- `evidence_file_missing_never_in_success_report`
- `evidence_write_failure_degrades_to_summary_failed`

### 第 3 条断言清单（`evidence_write_failure_degrades_to_summary_failed`）

fixture：mock `write_evidence_file` 抛异常（`OSError`）。

必须全部成立：

1. `execution_status == "summary_failed"`
2. `execution_summary is None`
3. `plan.runtime_notes` 含至少一项以 `evidence_write_failed` 为前缀的字符串
4. `report/` 下不存在该 `plan_id12` 对应的 `.html` / `.txt`（不调用渲染）

包：`acceptance/direction3_triage_20261018_evidence_fix/`（只含上述 PASS，不重跑原 28 条全量）。
