# Word 规格落地状态（对照 `docs/specs/word_office_spec.md`）

> **两个维度**（**替代 L0–L3**）：
> - **任务**：A–E（见 `docs/architecture.md` §1）
> - **改写**：`op_type`（replace / insert / delete / format / move / tracked_change）
>
> **L0–L3 是规格历史名，已废弃**；**L2/L3 = `op_type` 的 `move`/`tracked_change` + 跨结构**。

---

## 一、规格条目对照

| 规格里的 | 状态 | 说明 |
|---|---|---|
| **L0**（确定性） | ✅ | = **A 级** + `replace` / `inspect` |
| **L1**（轻 AI） | ✅ | = **B 级** + `l1` |
| **L2**（跨结构语义） | ⚠️ **部分** | `move`/`tracked_change` 已做（**段级**）；**跨结构**（表↔叙述/大纲）未做 |
| **L3**（事务层） | ⚠️ **部分** | `tracked_change`（`w:del`/`w:ins`）+ **dry-run/回滚**（`.bak` 恢复）已做；**正式事务层**未做 |
| **九对象** | ✅ **9/9（全用）** | `objects.py`：Intent/DocumentState/Plan/**PatchSet**/**ToolRegistry**/**CapabilityManifest**/**ConfirmationOverlay**/**IntermediateArtifact**/**RedactionPolicy** |
| **七状态机** | ✅ **7/7** | `done`/`failed`/`partially_completed`/`plan_invalidated`/`failed_postcheck`/`compensation_failed`/`rollback_failed` |
| **决策表** | ✅ **规则表** | 契约 `word_office.decision_rules`（`_decide_tier` 读契约） |
| **Overlay** | ✅ **接进 `_apply_overlay`** | `delete`/`tracked_change` → `modal`（**并接 GUI 弹窗**） |
| preflight | ✅ **真校验** | 每个 op 必须在 `ToolRegistry`（`no_executor:<op>` 拒） |
| 分片（`route_before_scan`） | ✅ **`route` + `chunk`** | `route` 判大文档（>200 段）→ `stages` 提示；`chunk` 按标题切块 |
| **UI 冒烟** | ✅ **`test_ui_smoke.py`** | offscreen 构造 `MainWindow`（**只"起得来"**，不做交互自动化） |

---

## 二、Word op 落地（**6/6，全 run 级**）

| op_type | 状态 | 说明 |
|---|---|---|
| `inspect`（读） | ✅ | 结构检查 |
| `replace` | ✅ | run 级 + 跨 run 段落级 |
| `insert` | ✅ | **标题锚点 → 段前/后插新段；非标题锚点 → 内联插文字**（见下"双行为"） |
| `delete` | ✅ | **只删目标文字**（run 级，保留前后）；**危险 → 确认** |
| `format` | ✅ | **run 级**：粗体/斜体/**字号**/**字体**（跨 run 也支持） |
| `move` | ✅ | **段级移动**（同段/找不到拒绝） |
| `tracked_change` | ✅ | **run 级** `w:del`/`w:ins`（只包目标文字，保留前后格式） |

### `insert` 的**双行为**（**明确约定**）

| 锚点 | 行为 | 理由 |
|---|---|---|
| **标题**（`Heading*` / `Title`） | **段后插新段**（段级） | 标题后面**应该是新段落**（章节/条目） |
| **非标题**（正文/表格/列表项） | **内联插文字**（run 级） | 正文里**"在 X 后面"** → 通常**接在同一段** |

**判定**：看**锚点段落**的 `style.name`——`Heading*`/`Title` → 段级；否则 → 内联。
**代价**：**同一句 prompt 在两种段落上行为不同**——**有意，不是 bug**。

---

## 三、已落地清单（可验收）

| 模块 | 文件 | 测试 |
|---|---|---|
| 结构扫描 + 三态谓词 + 指纹 | `pipeline/word_office/state.py` | `test_word_office.py` |
| Pre-check（门 + 预算 + **能力门**） | `pipeline/word_office/gate.py` | 同上 |
| 统一返回 + **七状态机** | `pipeline/word_office/module.py` | 同上 |
| 6 种 op（run 级） | `pipeline/word_office/module.py` | 同上 |
| **九对象** | `pipeline/word_office/objects.py` | 同上 |
| AI 建议（L1） | `pipeline/word_office/ai.py` | 同上 |
| 证据（gzip + **脱敏**） | `pipeline/word_office/evidence.py` | 同上 |
| 记忆（表/术语/锚点/生命周期） | `pipeline/word_office/memory.py` | `test_word_memory.py` / `test_word_term.py` |
| 即时记忆 | `pipeline/word_office/session.py` | `test_word_memory.py` |
| 任务链（线性） | `pipeline/word_office/chain.py` | `test_word_chain.py` |
| E 分块 + 多文件 | `pipeline/word_office/chunk.py` / `batch.py` | `test_word_stage6.py` |
| **GUI 确认弹窗** | `ui/main_window.py` + `engine/dispatcher.py` | —（人工验证） |

---

## 四、没做（**诚实**）

| 项 | 说明 |
|---|---|
| **跨结构编辑**（表↔叙述/大纲） | 规格 L2 的「跨结构」部分 |
| **dry-run / 回滚** | 规格 L3 的「事务层」部分（现在只有**写前备份**） |
| **preflight** | 仍是 stub（Pre-check 已覆盖） |
| **分片路由**（`route_before_scan`） | 只有 `chunk` |
| **`format` 颜色** | 未做 |
| **`move` run 级** | 有意保留段级 |
| **UI 自动化测试** | 只人工验证"能启动 + 确认弹窗" |

---

## 五、结论

> **Word 模块 = 规格的"可写回部分"已落地**：
> **op 6/6（run 级）、九对象 9/9、七状态机 7/7、决策表规则化、记忆/召回、GUI 确认**。
> **没做的是"跨结构/事务层/preflight/分片路由"**——**规格自己也说"后置"**。
