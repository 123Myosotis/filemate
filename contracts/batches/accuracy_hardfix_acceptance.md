# 准确性硬修批验收（已关闭 / 脚本 DEPRECATED）

> **活门禁勿再跑** `python -m tests.acceptance.accuracy_hardfix`：默认 SKIP。  
> 正文冻结在 v1.0.0 / 已删 API；强制旧跑需 `FILEMATE_RUN_DEPRECATED_HARDFIX=1`（不推荐）。重写另排期。

```text
run_id: 20261018
path: acceptance/accuracy_hardfix_20261018/
exit_code: 0
rerun_hash_match: true
rerun_input_hash_match: true
real_table_plan: real_table_plan.json
empty_force_reject: empty_force_reject.json
status: CLOSED
```

关闭时 handbook hash 见当时 `contracts/HANDBOOK_HASH`。本文件后续可追加语义澄清，不重开本批写码。

## 验收结果：PASS（关闭依据）

见同目录 `stdout.txt`：`failed=0 / total=18`，`验收结果: PASS`。  
强制层 9 条缺 map / 缺 semantics / unclassified fail 均在 `empty_force_reject.json`，并纳入 `outputs.sha256`。

---

## v1.0.0 语义澄清（P1，关闭后写入）

### P1-1 `agg_semantics[op][empty_class] = fail`

触发时：**整份 plan** 进入 `contract_incomplete`（执行路径拒绝，输出零行）。  
**不是**该组局部失败、其他组继续。  
若需要局部失败，须 **v1.1.0** 扩展契约后再实现。方向 2 实现时不得「看情况」改成局部失败。

### P1-2 `map_id` / `shared_map_id`（与 handbook §7.4 一致）

- 执行器使用该条目 **自己的** `map_id` 上的 `mapping` / `candidates`。  
- `shared_map_id` 仅用于溯源与一致性报告，**不**替换本条目内容。  
- 被指向 map 的 `mapping` 变更后，本条目 `mapping` 不变；不一致时 `plan_notes` 出现 `shared_map_mismatch …`。  
- 指向不存在的 id → `contract_incomplete`。

### P1-3 `rerun.sh` UTF-8

`acceptance/accuracy_hardfix_20261018/rerun.sh` 已 `export PYTHONUTF8=1`（并保留 `PYTHONIOENCODING=utf-8`）。  
重跑时 `_rerun_body_stdout.txt` 中文应与 `stdout.txt` 可读一致；不改变本批已记录的 hash 结论。

---

## 真实 plan map 摘要（关闭时）

列仅有：`账号`、`状态`、`金额`、`产品`。左右 join 两条 map，右侧显式 `shared_map_id`。
