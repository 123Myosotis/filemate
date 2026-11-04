# 后续-2 第一批 — 路由 A' 契约变更方案（设计确认，确认前不改契约文件）

## 0. 上下文复核

handbook hash = `d3d912a04c589cc477dde79dce309715c977b1a9b4fa19a3bd8f73ad6a36f08a`（与 `contracts/HANDBOOK_HASH` 一致）

依据：

- `acceptance/cross_table/A_PRIME_CONTRACT.md`（已批准）
- `pipeline/block_analyze/ROUTING_IMPL_CHECKLIST.md` 后续-2 分批说明
- 当前权威契约：`contracts/CURRENT` → `v1.3.0`，`cross_table` 块已有错误码三件套与部分上限键

本批目标（一句话）：为 A' 收尾（不含轨迹落点）补齐契约上的状态枚举、载荷形状、阶段复核「是否需问用户」、A 写失败三码、三层重试/追问上限、完整表头判断二值枚举。

**本批不做（明确划出）：**

| 排除项 | 归属 |
|---|---|
| trace 种类名 / 五元组槽位 / 编排序号 / 审计结构无效具体码 | 后续-2 **第二批** |
| 复核输入容量键接线 | 后续-4（未接线前仍测试传参） |
| B/S/E 内部契约表 | 后续-12 |
| 改生产契约 JSON | **待本方案批准后**另开落地回合 |

---

## 1. 允许改 / 不允许改

允许改（批准后落地回合）：

- `contracts/versions/` 新版本文件（建议 `v1.4.0`，或你们惯用的小版本策略）
- `contracts/CURRENT` 指向新版本
- 仅 `cross_table` 下：新增枚举、新增能力上限键、新增/扩展载荷形状说明、必要时扩展 `error_codes` / 原因码列表
- 配套：契约校验测试若强制「未知键拒绝」则同步承认新键

不允许改：

- 第二批键与行为
- A' / I / S 生产代码（本方案回合只出方案）
- 验收脚本、既有跨表业务枚举语义回写（除下文「误用澄清」）

---

## 2. 误用澄清（落地时必须改读键，本方案先钉语义）

现状（实现侧，供对照，本回合不改代码）：

- A' 内部追问计数曾读 `cross_table.user_retry_max_rounds`
- 编排侧另有 `proposal_max_calls_per_kind`、`user_retry_max_rounds`

**本方案裁定：不得继续用「用户重试上限」冒充 A' 内部追问总上限，也不得冒充 A' 入口重试上限。**  
三者语义分离，见 §4。落地时 A' 改读新键；旧键保留原语义给既有用户重试路径。

---

## 3. 状态枚举与载荷字段

### 3.1 A' 对外状态枚举（新增）

建议键：`cross_table.enums.layer_recognize_status`

| 取值（建议字面量） | 语义（与已批 A' 契约表对齐） |
|---|---|
| `no_structure` | 无此结构（无草案/非矩阵路径；不编造层） |
| `ok` | 成功（含完整层结果，且完整表头判断为「完整」） |
| `review_failed` | 复核不通过（正式复核否决，**或**完整表头判断为非完整） |
| `conflict` | 冲突（结构/容量/内部追问耗尽等不可继续） |

硬约束：

- 禁止把 `review_failed` 与 `conflict` 并成同一取值
- `ok` 不得在完整表头判断为非完整时出现

### 3.2 复核不通过载荷形状（新增 schema 块）

建议键：`cross_table.LayerRecognizeReviewFailed`

必含抽象字段（落名建议）：

| 建议字段名 | 类型 | 含义 |
|---|---|---|
| `status` | 固定 `review_failed` | 状态标记 |
| `rejection_reason` | string | 模型否决原因或完整表头判断原文 |
| `review_locator_ids` | string[] | 正式复核实际覆盖定位（未缩则为已识别全集） |
| `ask_rounds_used` | integer ≥0 | 本入口共用追问计数已用 |
| `layer_result_empty` | boolean（恒 true） | 已按识别回滚点丢弃 |
| `draft_retained` | boolean（恒 true） | 上游草案保留 |

### 3.3 冲突载荷形状（新增 schema 块）

建议键：`cross_table.LayerRecognizeConflict`

| 建议字段名 | 类型 | 含义 |
|---|---|---|
| `status` | 固定 `conflict` | 状态标记 |
| `reason_codes` | string[]（见 §3.4） | 原因列表；禁止空 |
| `ask_rounds_used` | integer ≥0 | 可为 0 |
| `layer_result_empty` | boolean（恒 true） | 已回滚 |
| `draft_retained` | boolean（恒 true） | 草案保留 |
| `portrait_metric_hint` | object（可选） | 层计数归零类提示 |

### 3.4 冲突原因码枚举（新增）

建议键：`cross_table.enums.layer_recognize_conflict_reason`

至少包含（可增不可语义合并）：

| 取值 | 对应场景 |
|---|---|
| `proposal_retries_exhausted` | 提案结构重试用尽 |
| `ask_subset_request_over_capacity` | 要子集请求体本身超容量 |
| `shrink_failed` | 缩范围失败 |
| `already_minimal_still_over_capacity` | 已最小仍超容量 |
| `ask_budget_exhausted_proposal` | 内部追问耗尽 — 提案类 |
| `ask_budget_exhausted_subset` | 内部追问耗尽 — 要子集类 |
| `ask_budget_exhausted_review` | 内部追问耗尽 — 正式复核类 |
| `success_payload_incomplete` | 成功路径缺 8 字段之一 |

说明：笼统「内部追问用尽」**不得**作为唯一合法码；必须用上表三类之一。

### 3.5 成功载荷：层结果必含字段清单（登记，非业务词）

建议键：`cross_table.LayerRecognizeOk.required_layer_fields`（字符串列表）

抽象八项（契约用中性标识，映射实现字段名在 A 段映射表）：

1. `rect_to_layer`
2. `layer_order`
3. `layers`（含定位包络）
4. `subtable_boundaries`
5. `nesting_facts`
6. `group_label_declaration`
7. `dimension_membership`（允许空层列表）
8. `header_completeness`（取值见 §7）

缺一 → 不得发 `ok`，发 `conflict` + `success_payload_incomplete`。

---

## 4. 三层上限（能力键）

均放在 `cross_table` 根下，正整数（或 0 表示「禁止该层重试」，若选 0 语义须在落地注释写清；**建议最小为 1 的键在校验里 `>=1`**，由批准时二选一）。

| 建议键 | 语义 | 持有/消费方 | 缺省后果 |
|---|---|---|---|
| `layer_recognize_ask_max_rounds` | **追问总上限**：单次 A' 入口内提案+要子集+正式复核实际次数之和 N+M+K | A' 读并计数 | 契约无效类 |
| `layer_recognize_entry_retry_max` | **A' 入口重试上限**：每块各自累计；仅 `review_failed` 后再调入口时累加 | I 读并累计 | 键缺失则**禁止**写入口重试循环 |
| `portrait_write_retry_max` | **A 写重试上限**：含用户确认的每一次重写；仅「写入拒绝」路径 | I 读并累计 | 未落地时用户确认重写最多一次；落地后按本键；缺键且仍写循环 → 契约无效类 |

与既有键关系：

| 既有键 | 本批处置 |
|---|---|
| `user_retry_max_rounds` | **保留**；语义仍是既有「用户重试」路径，**不再**被 A' 内部追问读取 |
| `proposal_max_calls_per_kind` | **保留**；不承担 A' 共用追问总上限 |

数值：本方案**不写死数字**；落地时从当前环境/既有合理默认迁入或单独批数值表。方案只钉「键存在 + 类型 + 缺省行为」。

---

## 5. 「是否需问用户」（阶段复核）

建议键：

- 枚举或布尔承载位置：`cross_table.StageReviewResult.properties.needs_user_ask`
- 类型：`boolean`（二值即可；不引入第三态「未知」——未知=字段缺失）

语义（已批图 6 / 后患 4）：

| 情况 | S 行为 |
|---|---|
| 复核不通过且 `needs_user_ask=false` | 回滚本阶段并重跑 |
| 复核不通过且 `needs_user_ask=true` | 触发 E |
| 字段**缺失** | 结构无效 → 回滚本阶段 + 触 E；S 交齐「缺字段事实」四要素 |

本批只钉契约形状；S 实现属接线，不阻塞 A' 收尾单测。

---

## 6. A 写失败三码

建议键：`cross_table.enums.portrait_write_failure_code`

| 取值 | 语义 | I 行为（契约层声明，实现属 A/I） |
|---|---|---|
| `field_missing` | 字段缺失 | 整表停→E；禁止重写 |
| `mapping_undefined` | 映射未定义 | 整表停→E；禁止重写 |
| `write_rejected` | 写入拒绝 | 可按 `portrait_write_retry_max` 用户确认重写该块切片；达上限整表停→E |

硬约束：写失败进 I 必须带上列三者之一；自由字符串仅附注。

是否同时加入 `error_codes` 顶层列表：**不强制**。建议失败码走专用枚举，外层错误类仍用已有 `cross_table_contract_invalid` / `cross_table_input_invalid` / `cross_table_proposal_invalid` 按清单 §7 边界映射（映射不一致类→契约无效类；写入拒绝运行时→可由输入/契约类承载，落地时写清一行映射表）。

建议映射（待批）：

| 写失败码 | 抛给上层的错误类 |
|---|---|
| `field_missing` | `cross_table_contract_invalid` |
| `mapping_undefined` | `cross_table_contract_invalid` |
| `write_rejected` | `cross_table_input_invalid`（运行时通道/存储拒绝，非契约文本缺键） |

---

## 7. 完整表头判断枚举

建议键：`cross_table.enums.header_completeness`

| 取值 | A' 映射 |
|---|---|
| `complete` | 允许进入 `ok`（尚须正式复核通过等其它条件） |
| `incomplete` | **必须** `review_failed`；否决原因含模型原文 |
| `not_header` | 同 `incomplete`：一律视为「非完整」侧 |

A' 实现只需二分：`complete` vs 非 `complete`（`incomplete` ∪ `not_header`）。  
不要求本批再拆「结构缺陷 vs 判断为否」冲突细分（可选，属后续扩展）。

---

## 8. 版本与兼容

| 项 | 提案 |
|---|---|
| 版本号 | `v1.4.0`（相对 `v1.3.0` 的能力扩展；若团队惯例用 patch，可改为 `v1.3.1`，批准时选定） |
| 兼容 | 旧调用方不读新键则行为不变；A' 收尾代码**必须**读新键，缺则契约无效类 |
| `CURRENT` | 落地回合切换 |
| 回滚 | 若撤销本批，A' 收尾不得已合并进主路径 |

---

## 9. 批准后落地清单（下回合机械执行，本回合不做）

1. 复制 `v1.3.0.json` → 新版本，写入 §3–§7 全部键与枚举  
2. 更新 `CURRENT`  
3. 跑契约库既有校验测试；补「新枚举可加载 / 缺上限键可被读取方判无效」的最小测（若仓库惯例要求）  
4. **不**在本落地回合改 A' 业务逻辑（A' 收尾是顺序第 2 步）  
5. 出短 EVIDENCE：键列表 + 版本号 + 测试通过数  

---

## 10. 与 A' 收尾的衔接（批准本方案后）

第一批落地后**可立即做**（不依赖第二批）：

- 后续-9：两状态分路径单测  
- 后患 1：完整表头非完整 → `review_failed`  
- 【11】(b) 回调缺省防御测  
- 【16】【18】【19】、预留策略、后续-7 空层列表  

**仍阻塞**（等第二批）：

- 后续-8 轨迹落点  

---

## 11. 待你批的显式选择题

| # | 题 | 方案默认 |
|---|---|---|
| Q1 | 新版本号用 `v1.4.0` 还是 `v1.3.1`？ | **v1.4.0** |
| Q2 | 上限键允许 `0`（表示禁止该类重试）还是校验 `>=1`？ | **校验 `>=1`**；禁止重试用「不写循环」文档约束，不用 0 |
| Q3 | `write_rejected` 外层错误类用输入无效类还是契约无效类？ | **输入无效类**（§6 表） |
| Q4 | `header_completeness` 是否保留三值（complete/incomplete/not_header）还是压成两值？ | **三值**（契约可区分）；A' 逻辑二分 |

无其它开放题。默认可按上表执行；你可只回「批 + 改 Qx」或「整批按默认批」。

---

## 12. 本方案状态

**已批准并落地。** 见 `acceptance/cross_table/EVIDENCE_CONTRACT_BATCH1.md`。
