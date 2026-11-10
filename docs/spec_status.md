# Word 规格落地状态（对照 `docs/specs/word_office_spec.md`）

> **两个维度**（**替代 L0–L3**）：
> - **任务**：A–E（见 `docs/architecture.md` §1）
> - **改写**：`op_type`（replace / insert / delete / format / move / tracked_change）
>
> **L0–L3 是规格历史名，已废弃**；**L2/L3 = `op_type` 的 `move`/`tracked_change` + 跨结构**。

| 规格里的 | 状态 | 说明 |
|---|---|---|
| **L0**（确定性） | ✅ | = **A 级** + `replace` / `inspect` |
| **L1**（轻 AI） | ✅ | = **B 级** + `l1` |
| **L2**（跨结构语义） | ❌ | = **跨结构 `move` / `replace`**（未做） |
| **L3**（事务层） | ❌ | = **`tracked_change`**（未做） |
| 九对象 | ⚠️ **3/9** | 只有 Intent / DocumentState / Plan；其余 6 个**占位** |
| 七状态机 | ⚠️ **3/7** | `done` / `failed` / `partially_completed`；缺 4 个 |
| 决策表 | ⚠️ **关键词版** | `_decide_tier` 按词判，不是规则表 |
| Overlay / preflight | ⚠️ **stub** | 占位函数 |
| 分片（`route_before_scan`） | ⚠️ **只做 `chunk`** | 按标题切块，没有路由 |
| 事务层（L3） | ❌ | = `tracked_change`（未做） |

---

## Word op 落地（**4/6**）

| op_type | 状态 | 说明 |
|---|---|---|
| `inspect`（读） | ✅ | 结构检查 |
| `replace` | ✅ | run 级 + 跨 run 段落级 |
| `insert` | ✅ | 锚点后插新段 |
| `delete` | ✅ | 删含目标文本的**整段**（粒度粗） |
| `format` | ✅ | **仅 run 级粗体/斜体** |
| `move` | ❌ | 未做 |
| `tracked_change` | ❌ | 未做 |

**4/6 已做**；`move` / `tracked_change` 未做。

---

## 规格 v1 / v2 / v3 对照

| 版本 | 规格要求 | 本项目 |
|---|---|---|
| **v1** | L0 + L1 + 方案卡片 + 轻量确认 + 审计 | ✅ **已完成** |
| **v2** | L2（复合锚点 + 对称性校验 + 质量闭环） | ❌ 未开始 |
| **v3** | L3（PatchSet + dry-run + 回滚 + 事务层） | ❌ 未开始 |
| **v4** | 企业级（五角色 / 双人确认） | ❌ 不做 |

---

## 已落地清单（可验收）

| 模块 | 文件 | 测试 |
|---|---|---|
| 结构扫描 + 三态谓词 + 指纹 | `pipeline/word_office/state.py` | `test_word_office.py` |
| Pre-check（门 + 预算） | `pipeline/word_office/gate.py` | 同上 |
| L0 结构检查 + 确定性替换 | `pipeline/word_office/module.py` | 同上 |
| L1 AI 建议 + 校验 | `pipeline/word_office/ai.py` / `module.py` | 同上 |
| 证据（gzip） | `pipeline/word_office/evidence.py` | 同上 |
| 记忆（表/检索） | `pipeline/word_office/memory.py` | `test_word_memory.py` |
| 即时记忆 | `pipeline/word_office/session.py` | 同上 |
| 任务链（线性） | `pipeline/word_office/chain.py` | `test_word_chain.py` |
| E 分块 + 多文件 | `pipeline/word_office/chunk.py` / `batch.py` | `test_word_stage6.py` |

---

## 结论

**规格是 v1–v3 的完整设计；本项目实现到 v1。**
**v2/v3 未开始，不是"缺失"，是"未排期"**——下一步应明确：**继续 L2，还是冻结在 v1**。
