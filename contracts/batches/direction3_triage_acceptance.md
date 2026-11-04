# 方向 3 机械分流 — 验收基线

`execution_status` 本批不写入契约 schema。闭集语义以本文为验收基线，待 v1.1.2 或下一批正式登记进契约。

闭集（仅此三值）：

| 值 | 含义 |
|----|------|
| `success` | 执行后 triage 仍为 simple，摘要已写出，且 `evidence/{version}/{plan_id12}.json` 已落盘。`execution_summary` 存在，`delivery_success=true`。 |
| `summary_failed` | 行结果保留（`rows` 不改成空）。不标成功。不写 `execution_summary`（值为 null）。含摘要 AI 失败，或 evidence 落盘失败（`runtime_notes` 追加 `evidence_write_failed*`，非 triage 信号）。不进入执行中漏检（设计 §8）。 |
| `complex` | 执行前分流或执行后升级。不交付成功表。 |

不允许出现这三值以外的 `execution_status`。

evidence 落盘补丁说明：`contracts/batches/direction3_evidence_write_fix.md`。增补包：`acceptance/direction3_triage_20261018_evidence_fix/`（2 条 PASS）。

## P1-1 `inputs.sha256` 完整条目

已关闭包 `acceptance/direction3_triage_20261018/inputs.sha256` 只有下面三行，关闭后不改哈希：

| 条目 | 纳入 | 依据 |
|------|------|------|
| `fixtures/d3/adversarial_ambiguous_looks_normal.plan.json` | 是 | §9 PASS 6 的 fixture。`confidence` 0.99 与 `ambiguous_reason` 必须能对文件字节复算。 |
| `contracts/versions/v1.1.0.json` | 是 | 本批执行与阈值（binding 0.75、region_cover 0.8、退化值）的契约文件。 |
| `contracts/CURRENT` | 是 | 钉住当时当前版本指针，避免只哈希版本文件却换了指针。 |
| `contracts/batches/direction3_mechanical_triage_design.md` | 否 | backlog P2-9 已裁定本批验收不把设计确认文档列入 `inputs.sha256`。设计变更不应当让已关闭的输入哈希失效。 |

## P1-2 `plan_id12` 与 `_gated_*`

关闭时的实现是在 `attach_contract_meta` **之后**对 plan 做 `plan_id12`，`_gated_contract_version` / `_gated_contract_hash` 会进入哈希。`sort_keys=True` 只固定键顺序，挡不住多出来的键。

已改为：计算源是去掉 `_gated_*` 顶层键之后的 plan，等价于 gate 附加这两键之前的 plan。两键仍写在 plan 上，只作审计，不进 `plan_id12`。设计 §5.3 已补这句。

已关闭包里的 `evidence/v1.1.0/63aff0f8db2c.json` 是改前哈希，不重跑、不改 `outputs.sha256`。

## P1-3 本批验收不覆盖真实 AI

本次验收覆盖 `d3-summary-1` 结构校验，以及摘要失败时的 `summary_failed`（`rows` 保留、`execution_summary` 为 null）。摘要文本来自验收桩，不覆盖真实 AI 调用。真实 AI 摘要的超时和非 JSON 在集成测试里验证，不进入方向 3 验收包。
