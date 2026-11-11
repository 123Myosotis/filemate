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
| **L2**（跨结构语义） | ✅ **已做** | **表↔叙述 双向**（`table_to_text`/`text_to_table`）+ **表↔大纲 双向**（`table_to_outline`/`outline_to_table`）+ `outline` |
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
| **`outline`** | ✅ | **改标题层级**（`Heading N` / `Normal`）——**跨结构最小版** |
| **`table_to_text`** | ✅ | **跨结构（表→叙述）**：表格逐行转段落，表格删除；**`find` 不在格子里 → 向前找表标题（caption，最多 5 段），再兜底转全部** |
| **`text_to_table`** | ✅ | **跨结构（叙述→表）**：`键:值` 连续段落 → 2 列表格（分隔符 `：`/`:`/`=`；**跳过空行**，遇非键值对停） |
| **`table_to_outline`** | ✅ | **跨结构（表→大纲）**：首列 → `Heading level`（默认 1），**其余列 → 正文段**；表格删除 |
| **`outline_to_table`** | ✅ | **跨结构（大纲→表）**：标题 → 2 列表格（标题 + **数字层级**，可回表）；**空 find → 全篇标题**；**默认保留原标题**，显式「删除原标题/搬走」才删 |

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
| **正式事务层**（L3） | `dry_run` 是"先改再还原"的别名，非真 dry-run；回滚依赖 `.bak`/独立备份 |
| **批注 / 控件 / 交叉引用**（读能力） | 未做（`tracked_change` 已做，但批注 API 等未做） |
| **记忆语义召回** | 无 embedding 时 `recall` 两路字面（FTS5 + LIKE） |
| **记忆学习**（用户纠正→记） | 未做 |
| **`move` 带完整句 / 跨段** | 未做（现在 run 级带标点） |
| **`outline` 自动重排** | 未做（只提示"编号需手工核对"） |
| **UI 自动化（pytest-qt）** | 只有"构造 + dispatch worker"冒烟 |

---

## 五、结论

> **Word 模块 = 规格的"可写回部分"已落地**：
> **op 6/6（run 级）、九对象 9/9、七状态机 7/7、决策表规则化、记忆/召回、GUI 确认**。
> **没做的是"跨结构/事务层/preflight/分片路由"**——**规格自己也说"后置"**。
