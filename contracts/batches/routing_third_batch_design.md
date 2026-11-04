# 后续-2 第三批 — 路由契约变更方案（设计确认，确认前不改契约文件）

## 1. 上下文复核

handbook hash = `d3d912a04c589cc477dde79dce309715c977b1a9b4fa19a3bd8f73ad6a36f08a`（与 `contracts/HANDBOOK_HASH` 一致）

当前权威契约：`contracts/CURRENT` → **v1.5.0**（第二批已批已落）

依据：

- `acceptance/cross_table/B_CONTRACT.md` **【8】缺项汇总**（三项待第三批：结构状态枚举 / 计划再生成上限键 / 阶段计划正式 Schema）
- `pipeline/block_analyze/ROUTING_IMPL_CHECKLIST.md` **后续-16** / 与 P3-3 同批说明
- `contracts/batches/BACKLOG.md` **P3-3** / **P3-6**（本方案回合**不改** BACKLOG；批准落地后再同步状态）
- 语义权威（落地前）：`B_CONTRACT.md` **§2.1 / §2.3 / §3 / §5**；画像结构标记语义：`A_PRIME_CONTRACT.md` **§2.4**（仓库无独立 `A_CONTRACT.md`；A 段写画像映射以 A' 表 §2.4 + A 段证据映射为准）

本批目标（一句话）：把 B 已实现钉死、但仍缺契约承载的 **结构状态两值枚举**、**计划再生成上限能力键**、**阶段计划正式 Schema（含回滚点①–③枚举）** 一次写入契约方案；批准后再落地 JSON。

**与 B_CONTRACT §8 三项一一对齐（不多不少）：**

| §8 缺项 | 本方案节 |
|---|---|
| 结构状态进契约枚举 | §2 项 1 |
| 计划再生成上限键 | §2 项 2 |
| 阶段计划正式 Schema（含回滚点①–③、决策种类引用） | §2 项 3 |

**本批不做（明确划出）：**

| 排除项 | 归属 |
|---|---|
| 改生产契约 JSON / `CURRENT` | **待本方案批准后**另开落地回合 |
| S / E 契约表正文与实现 | 后续-12 / **下一轮 S**；本方案回合不动 S |
| 数值表（各上限具体正整数） | **另批**（同第一批：本批只落键名+类型+校验规则） |
| 用户介入点强于布尔的表达 | backlog；本批不扩 |
| P3-4 转换层裁定 | 不顺手塞入 |
| 改 A' / A / I / B / trace 生产代码 | 落地回合按清单改 B 读契约等；本方案回合不动 |

---

## 2. 三项逐项

### 项 1：结构状态枚举进契约

| 维 | 内容 |
|---|---|
| **抽象名**（建议） | `cross_table.enums.structure_status` |
| **类型** | 契约枚举（两成员，封闭） |
| **语义** | 画像切片上的结构标记：区分「整块未做层识别」与「已识别层」。**禁止**第三值；**禁止**自由串；**禁止**用空层列表冒充「无此结构」 |
| **成员（建议字面量；与 B/A 实现已钉死对齐）** | `upstream_absent` = 上游无此结构；`layers_recognized` = 已识别层 |
| **引用方** | A 写画像；B 读画像；未来 S/E 读画像（本批不为 S/E 另加字段） |
| **消费方** | A（写时取值必须 ∈ 本枚举）；B（读时校验 ∈ 本枚举）；S/E（未来读，同一枚举） |
| **缺键行为** | 缺本枚举键（或成员集非法）→ `cross_table_contract_invalid`（A/B 侧读时视为结构错误 / 契约能力未落地） |
| **与既有键关系** | **新增**；不改写第一/二批已落枚举。与画像切片上的结构标记字段（实现侧已用）成对：枚举在契约，字段值必须成员全等 |
| **与谁对齐** | `A_PRIME_CONTRACT.md` **§2.4**（枚举型结构标记、与空层列表区分）；`B_CONTRACT.md` **§2.1**（结构状态直接读）、**§2.3**（空层列表 vs 无此结构） |

硬约束（落地必须测）：

- 封闭两值；出现任何其它串 → 契约/输入无效（实现侧归入既有无效类，不得静默当已识别）
- 「无此结构」与「已识别且某维空层列表」不得互相改写（对齐 B §2.3）

---

### 项 2：计划再生成上限能力键

| 维 | 内容 |
|---|---|
| **抽象名**（建议） | `cross_table.plan_regenerate_max` |
| **类型** | 正整数，**≥ 1**（校验规则同前批次能力上限键：缺键/非正整数/非整数 → 契约无效） |
| **语义** | B 主流程在「计划结构无效 / 复核未通过 → 退回模型重出」路径上，允许的再生成次数上限。达上限仍失败 → **路径乙**（冲突包交 I→E），对齐 B §5 |
| **持有方** | 契约（`cross_table` 根下能力描述）；落地后 **B 改契约读** |
| **消费方** | B 主流程 |
| **过渡态收口** | 本轮 B 实现靠调用方传入；第三批落地后取消「代传必填」依赖，改为契约读；落地前缺传参仍按「契约能力未落地」→ `cross_table_contract_invalid` |
| **缺键行为** | 缺键 → `cross_table_contract_invalid`（能力未落地） |
| **与既有键关系** | 与 `layer_recognize_ask_max_rounds` / `layer_recognize_entry_retry_max` / `portrait_write_retry_max` **同级**，同在 `cross_table` 根下能力描述；**互不冒充**（不得用追问上限或写画像重试上限代替本键） |
| **数值** | **本方案不写死**；数值表另批 |
| **与谁对齐** | `B_CONTRACT.md` **§5**（达再生成上限 → 路径乙）；`B_CONTRACT.md` **【8】**（本键缺项行）；清单 **后续-16** |

---

### 项 3：阶段计划正式 Schema + 回滚点枚举

#### 3.1 回滚点枚举（新增）

| 维 | 内容 |
|---|---|
| **抽象名**（建议） | `cross_table.enums.stage_rollback_point` |
| **类型** | 契约枚举（三成员，封闭） |
| **成员（抽象语义；字面量落地回合定）** | 识别层回滚（图 4 之①）/ 阶段起点回滚（②）/ 上游布局回滚（③） |
| **硬约束** | **④ 不进**本枚举、不进阶段声明；模型给出④ → 计划结构无效（提案无效类语义），退回重出 |
| **缺键行为** | 缺本枚举 → `cross_table_contract_invalid` |
| **与谁对齐** | `B_CONTRACT.md` **§3.3**（回滚点 ↔ 图 4；仅①–③可进阶段声明） |

#### 3.2 阶段计划 Schema（新增）

| 维 | 内容 |
|---|---|
| **抽象名**（建议） | `cross_table.StagePlanSchema` |
| **类型** | 契约 schema 块（`required` + `properties`；字段字面量落地回合定，本方案只钉抽象槽位） |
| **权威切换** | **落地前**：`B_CONTRACT.md` **§3** 为权威；**落地后**：本 Schema 为权威，B_CONTRACT §3 同步标记「已由契约 Schema 承载」（落地回合改契约表文档，本方案回合不改 B_CONTRACT） |

**顶层必含（抽象槽位）：**

| 抽象槽位 | 类型意图 | 与谁对齐 |
|---|---|---|
| 阶段列表 | 非空有序数组；元素服从「每阶段」形状 | B §3.1 |
| 计划世代 | 整数 ≥ 1（审计世代） | B §3.1；与已落 `StagePlanTraceMeta` 世代语义一致，本批不改 Meta |
| 复核结果 | 对象；至少承载「是否需问用户」布尔（见下） | B §3.1；与 v1.4.0/`v1.5.0` 已有 `StageReviewResult.needs_user_ask` 对齐 |

**每阶段必含（抽象槽位）：**

| 抽象槽位 | 类型意图 | 与谁对齐 |
|---|---|---|
| 阶段标识 | 字符串主键；计划内唯一 | B §3.2 |
| 父表集合 | 标识集合 | B §3.2 |
| 块集合 | 标识集合；每块须有画像切片（一致性自检属 B §5，Schema 只定形状） | B §3.2 |
| 行范围 / 列范围 | 定位范围（可声明整表） | B §3.2 |
| 子表集合 | 标识集合 | B §3.2 |
| 维度集合 | 标识集合（条件必填规则实现侧按 B §3.2「条件」执行） | B §3.2 |
| 层集合 | 标识集合（条件同上；无此结构块不得点名层 — 自检属 B §5） | B §3.2 |
| 允许决策种类 | 数组；**每个成员 ∈ 既有** `cross_table.enums.orchestrate_decision_kind`（见 §2.3.3 判定） | B §3.2 / §3.4 |
| 用户介入点（计划侧权限） | 本批以 boolean **`allow_user_ask`** 承载（Q5 变体）；≠ `StageReviewResult.needs_user_ask`；S 触发 E = 二者 AND | B §3.2 / §3.4 |
| 回滚点标识 | 必须 ∈ 本批 `stage_rollback_point`（仅①–③） | B §3.2 / §3.3 |
| 依赖声明 | 必填（形状：落地回合定最小可校验结构；缺 → 结构无效） | B §3.2 |
| 是否需问用户 | boolean；与顶层/阶段复核侧布尔一致承载（可与「用户介入点」同槽或显式双槽 — 见 §6 待裁定） | B §3.2 末行 |

| 维 | 内容 |
|---|---|
| **引用方** | B（产出校验）；未来 S（执行读）；I（原样传递，不改形状） |
| **消费方** | B 主流程（交付前自检）；S（下一轮契约表引用本 Schema，本批不写 S 表） |
| **缺键行为** | 缺 Schema 块 / 缺枚举 → `cross_table_contract_invalid`；计划实例缺必填槽位 → B 侧结构无效（提案无效类重出），不得交付 S |
| **与既有键关系** | **新增** Schema + 回滚点枚举；**复用**已有 `orchestrate_decision_kind`、`StageReviewResult.needs_user_ask`；**不**改 `StagePlanTraceMeta`；**不**把④或 S/E 专有字段塞进本 Schema |

#### 3.3 允许决策种类：引用形式判定

**本方案选定：Schema 字段直接引用既有 `cross_table.enums.orchestrate_decision_kind`（成员全等校验），不另造独立枚举，也不另造「固定子集引用列表」。**

理由：

1. 与 `B_CONTRACT.md` **§3.4**「只允许契约已有枚举成员；计划填写的集合必须是其子集」字面一致——子集由**每份计划实例**填写，不是契约再钉一份静态允许表。
2. 与第二批 `layer_recognize_kinds` 模式对比：`layer_recognize_kinds` 是因为 A' **写 trace 的种类集合固定为三**、需防实现自由串；阶段「允许决策种类」是**计划可变集合**，固定子集列表会过早收窄或逼本批发明 S 尚未裁定的允许集。
3. 另造独立枚举会与 I 侧决策枚举漂移，违反 B「B 不得新增枚举成员」。

若审方要求「契约级固定允许子集」（类似 `layer_recognize_kinds`），记入批准后 backlog / 数值与允许表另批，**不**在本批扩。

---

## 3. 版本号提案

建议：**v1.6.0**（相对当前 v1.5.0）。

理由：本批新增封闭枚举 ×2、能力键 ×1、正式 Schema ×1，属契约能力扩展，与第一批（→v1.4.0）、第二批（→v1.5.0）同级的 **次版本** 跳号；不是仅文档/勘误级的 **v1.5.1**。

若审方坚持「无行为变更、仅补已实现钉死项」可用 v1.5.1——但本方案仍推荐 v1.6.0，因 Schema 权威切换会影响后续 S 读契约方式。

---

## 4. 三项之外不顺手加

本批**明确禁止**塞入：

- S / E 契约表字段、执行协议、④ 上报包 Schema（④ 仍只在 B 发现路径，不进阶段 Schema）
- 数值表中的具体正整数
- 用户介入点枚举化 / 多点位结构
- P3-4 转换层、画像字段改名
- 新错误码（沿用已有 `cross_table_contract_invalid` / 提案无效类语义）
- 改 `routing_trace_kind` / `StagePlanTraceMeta` / 第一二批已落键语义

S 契约表：**下一轮**做；本方案回合不动 S。

---

## 5. 批准后落地清单（机械执行）

**状态：已批准（见 §6 裁定）。本落地回合执行。**

1. 自 v1.5.0 复制 `v1.6.0.json`。
2. 在 `cross_table.enums` 新增：
   - `structure_status` = `{upstream_absent, layers_recognized}`
   - `stage_rollback_point` = 英文蛇形三值（与 B 实现对齐：`a_prime_recognition` / `stage_start` / `upstream_layout`）；④ 不出现
3. 在 `cross_table` 根下新增能力键 `plan_regenerate_max`（正整数 ≥1；**暂挂值**由落地回合定，数值表另批）。
4. 新增 `cross_table.StagePlanSchema`（顶层 + 每阶段 `required`/`properties`；决策种类 items 直接 ∈ `orchestrate_decision_kind`；回滚点 ∈ `stage_rollback_point`；阶段侧 boolean **`allow_user_ask`**）。**须在 Schema `description` 写清：** `allow_user_ask`（计划侧权限）≠ `StageReviewResult.needs_user_ask`（复核侧判断）；**S 触发 E 的条件 = `allow_user_ask` AND `needs_user_ask`**。
5. 更新 `contracts/CURRENT` → `v1.6.0`。
6. 契约测（三条硬测必须全绿）：
   - 新枚举可加载：`structure_status` 恰两值；`stage_rollback_point` 恰三值且无 ④
   - `plan_regenerate_max` 缺键 / `0` / 非整数 → 各抛 `cross_table_contract_invalid`
   - `StagePlanSchema`：**计划实例**回滚点填 ④ → **计划校验时拒绝**（不是 Schema 加载拒绝）
7. B 实现：改契约读 `plan_regenerate_max`；计划产出按 Schema 校验；`structure_status` 读枚举；阶段字段用 `allow_user_ask`。
8. 文档同步（**本步必须真做完，不许留 backlog**）：
   - `B_CONTRACT.md` §3 标记「已由契约 Schema 承载」
   - `B_CONTRACT.md` §8 三项改「已补」
   - `BACKLOG` P3-3 / P3-6 关闭或改指向证据
   - `ROUTING_IMPL_CHECKLIST` 后续-16 状态更新
9. 出 `EVIDENCE_CONTRACT_BATCH3.md`；**不**改 S/E 契约表与实现。

---

## 6. Q1–Q6 裁定（已批准）

| # | 裁定 |
|---|---|
| Q1 | **v1.6.0** |
| Q2 | **A** 英文蛇形；落地字面量 = `a_prime_recognition` / `stage_start` / `upstream_layout`（与 B 已钉实现对齐） |
| Q3 | **A** 维持 `upstream_absent` / `layers_recognized` |
| Q4 | **A** 直接 ∈ `orchestrate_decision_kind` |
| Q5 | **A 变体**：阶段字段名 = **`allow_user_ask`**（boolean，权限上限）；与 `StageReviewResult.needs_user_ask`（复核判断）区分；S 触发 E = `allow_user_ask` AND `needs_user_ask`；须写入 Schema description |
| Q6 | **A** 落地回合定暂挂值（保守占位；数值表另批）→ 本落地选用 **`3`** |

---

## 7. 不新增数值（数值表另批）

本批契约只保证键存在与校验规则。`plan_regenerate_max` 暂挂值 **3** 仅占位，正式数值表另批可改。

---

## 8. 方案回合停点（历史）

方案回合只出本文件；批准后进入落地（见 §5）。S 契约表方案在审过本落地证据后下一轮做。
)
