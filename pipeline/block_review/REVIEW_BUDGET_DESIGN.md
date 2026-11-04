# 复核切包预算与请求节奏设计

状态：**已落地**（默认 `review.model` 仍空=沿用 api.model；验收用 `GLM-4-Flash-250414`；错误码 `analysis_too_many_packages`）  
范围：复核切包预算动态化、按 prompt 度量、包数上限、去掉验收外层连打、串行包间间隔、模型可配置切换。  
不做：改 prompt / 1a–R2 / 桥 / 拼接；不动 `contracts/`、方向 3.5；不加长等待（内层 `ai.retry` 另线）。

依据：冷链 5×11 诊断——本次 1305 是 `GLM-4.7-Flash` 限流 + 验收外层 `range(8)+sleep(45)` 连打，不是切碎；但 `review_budget_tokens=8000` 对大表是真隐患。

---

## 1. 配置项表 + 默认值

| 参数 | 默认 | 含义 | 备注 |
|------|------|------|------|
| `review.model` | `""`（沿用 `api.model`） | 复核专用模型 | **已有**；可设 `GLM-4-Flash-250414`。空字符串语义不变。 |
| `review.context_tokens` | `null` / 缺省 | 复核专用上下文窗口 | **新增**。空则：先 `llm.context_by_model[review.model]`（若有），再 `llm.context_tokens`。用户可显式设 `110000`。 |
| `review.fill_ratio` | `null`（沿用 `llm.context_fill_ratio`，现 0.8） | 复核填充比 | **新增** |
| `review.reserved_output_tokens` | `null`（沿用 `llm.reserved_output_tokens`，现 4000） | 复核输出预留；同时作 `_call_review_model` 的 `max_tokens` | **新增**（调用侧与预算侧同源，避免两套数） |
| `review.prompt_overhead_tokens` | `null`（沿用 `llm.prompt_overhead_tokens`，现 1500） | 复核模板/系统开销预留 | **新增**（预算公式扣项；与 prompt 内维度文案的 `estimate_tokens` 独立：前者是配置预留，后者计入 prompt 实测） |
| `review.small_table_ratio` | `0.5` | 整表单包 prompt ≤ `review_B × 该比例` → 整包不切 | **新增** |
| `review.max_packages` | `30` | 单次复核包数上限；超过报错 | **新增**（与 `run_review(..., max_packages=)` 调用参数区分见下） |
| `review.inter_package_delay_sec` | `1` | 串行包与包之间的间隔秒数 | **新增** |

### 废弃 / 停止依赖

| 项 | 处置 |
|----|------|
| `runtime.review_budget_tokens`（含写死 `8000` / 验收里的 `1800`） | **不再作为切包依据**。实现后忽略该字段；验收脚本删除赋值。过渡期可读但不生效，避免两套预算。 |
| `run_review` 内 `or 8000` | **删除**，改为始终算 `review_B`。 |

### 与已有 `run_review(max_packages=)` 的关系

- 配置 `review.max_packages`：切包结果长度的**硬上限**（防上千包）。
- 函数参数 `max_packages`：调用方「本次只跑前 N 包」（5b 材料/单测用）。  
  生效顺序建议：先切包 → 若 `len(packages) > review.max_packages` 报错 → 再按函数参数截断发送。

### 解析函数（设计名，实现时自定）

`resolve_review_budget(runtime=None) -> (review_B: int, details: dict)`  
优先级：`review.*` 显式值 > `runtime` 同源覆盖（若保留调试入口）> `llm.*` / 模型表。  
`details` 至少含：`window`、`fill_ratio`、`reserved_output_tokens`、`prompt_overhead_tokens`、`review_B`、`model`。

---

## 2. 预算公式（不再写死 8000）

与 2b `_budget_B` 对齐：

```
usable   = review.context_tokens × review.fill_ratio
review_B = usable − review.reserved_output_tokens − review.prompt_overhead_tokens
```

约束：

- `review_B ≤ 0` → 报错（建议码 `review_budget_non_positive`），带 `details` 六个数，**不**静默回退 8000。
- 上界防御：若配置异常使 `review_B > window − reserved`，截到 `max(1, window − reserved)`（与 1a/2b 一致）。
- **不**再读 `review_budget_tokens`。

### 示例（用户拟用模型）

| 输入 | 值 |
|------|-----|
| `review.model` | `GLM-4-Flash-250414`（拍板后） |
| `review.context_tokens` | `110000`（或写入 `llm.context_by_model`） |
| fill / reserved / overhead | 0.8 / 4000 / 1500 |

→ `usable = 88000`，`review_B = 88000 − 4000 − 1500 = 82500`。

冷链整表 prompt ≈ 2017 token ≪ 82500 → 一整包。

1M token 内容口径：`≈ 1e6 / 82500 ≈ 12–13` 包（落在「约 12–15、远小于几百」目标）。  
（若含密集 `cell_origin` 导致实测密度更高，仍靠 `review.max_packages=30` 兜底报错，而不是默默切上千包。）

---

## 3. 切包度量改按 prompt token

| 现状 | 目标 |
|------|------|
| `estimate_tokens(json.dumps(package)) ≤ budget` | `estimate_tokens(build_review_prompt(package)) ≤ review_B` |

流程（按块，沿已有 `row_span` / `source_row`，不新造轴）：

1. 先构造「该块全部行」的 package → `build_review_prompt` → `need = estimate_tokens(prompt)`。
2. 若 `need ≤ review_B`（且过小表门槛，见 §4）→ 一包。
3. 否则沿 `source_row` 累加：每次试加一行后重建 prompt 再估；超 `review_B` 则封上一段、新开桶。
4. 单行 prompt 已 `> review_B`：与现状一致——仍产出该行一包，由包数上限或调用失败暴露；**不**截断单元格、不改 prompt 文案。

冷链已知差：包 JSON 1494 vs prompt 2017（约 +523）。按 prompt 度量消除大表低估。

---

## 4. 小表整包阈值

在「整块试装」之前或同时：

```
whole_need = estimate_tokens(build_review_prompt(整块单包))
if whole_need ≤ review_B × review.small_table_ratio:
    → 整块一包，不进入按行累加
elif whole_need ≤ review_B:
    → 仍整块一包（未超预算，只是超过「小表」优惠带）
else:
    → 按 source_row 切
```

说明：

- `small_table_ratio` 默认 `0.5`：明确「明显小于半窗输入预算则不切」。
- 冷链：`2017 ≤ 82500 × 0.5` 成立 → 整包；与今日「1 包」行为一致，阈值写清楚。
- 不引入按行数/列数的第二套启发式，避免与 token 度量分叉。

---

## 5. 大表包数上限和报错

切包完成后、发请求前：

```
if len(packages) > config.review.max_packages:  # 默认 30
    raise ReviewError(
        "analysis_too_many_packages",  # 用户指定错误码；实现可用此字符串
        "...",
        {"packages": len(packages), "max_packages": 30, "review_B": ..., ...},
    )
```

- **不**静默合并、不静默丢行、不抬高预算偷跑。
- 调用方（GUI/验收）应提示：增大 `review.context_tokens` / 减小表范围 / 调高 `max_packages`（需用户知情）。
- 目标包数：1M 内容 + `review_B≈8.25万` → **约 12–15 包**；默认上限 30 留余量，禁止默认出几百包。

---

## 6. 外层重试去掉

| 位置 | 现状 | 目标 |
|------|------|------|
| `acceptance/final_full_pipeline/run.py` | `for attempt in range(8): run_review; sleep(45)` | **单次** `run_review`；失败即记入 summary / REVIEW_AUDIT |
| `acceptance/final_full_pipeline/resume_review.py` | 同上 | 同上 |
| 其它验收若有同类外层连打 | 一并去掉 | 只依赖 `ai.retry` |

规则：

- 内层：`chat_completion` → `run_with_retry`（429 分类、冷却、墙钟、空正文降 token）。
- 内层耗尽 → 抛 `ReviewError` / `ChatRetryError` 映射错误给调用方。
- **禁止**验收脚本再套固定次数 + 固定 sleep 连打同一模型。
- 本设计**不加长**内层等待；是否再调退避表另线审。

---

## 7. 串行 + 包间间隔

- `run_review` 保持 `for pack in packages` **串行**（不引入线程池并发复核）。
- 相邻两包之间：`sleep(review.inter_package_delay_sec)`，默认 **1** 秒。
- 第一包前不睡；仅包间。
- `max_packages` 函数截断后的实际发送序列同样尊重间隔。
- 空正文强制行对半的**同包重试**不算「下一包」，不额外加间隔（仍走内层 retry）。

---

## 8. 默认模型切换（等用户拍板）

切模型**只需改配置，不改业务代码**：

1. `review.model = "GLM-4-Flash-250414"`
2. `review.context_tokens = 110000`  
   **或** 在 `llm.context_by_model` 增加 `"GLM-4-Flash-250414": 110000`，且 `review.context_tokens` 留空走表。

| 选项 | 说明 |
|------|------|
| A. 默认仍空/`api.model` | 行为与今一致；用户本地/验收显式设复核模型 |
| B. 默认改为 `GLM-4-Flash-250414` + 上下文 110k | 新机开箱即用宽松复核；需同步改验收里写死的 `GLM-4.7-Flash` |

**本设计不擅自改默认**；实现 PR 可把「配置键与解析」落地，默认值等拍板后一行配置切换。

建议验收在拍板后：最终全链路 / api_retry 项 6 改用新模型 id，不再绑死 `GLM-4.7-Flash`。

---

## 9. 对现有 5b / 4b / 最终跑通验收的影响（预期不回归）

| 验收 | 影响 | 预期 |
|------|------|------|
| **4b 拼接** | 不碰 stitch / R2 | **无回归** |
| **5b 行为项**（人机、reanalyze、invalid、model resolve、材料） | 不改 prompt / 维度文案；`max_packages=1` 的调用语义保留 | **无回归** |
| **5b 切包相关** | 删除 `runtime.review_budget_tokens=1800`；改靠 `review_B`。单项若断言「多包」需按新公式重算期望 | 冷链/最小块仍 **1 包**；漏扫全量若曾依赖 1800 碎包，包数会**变少**（更合理），用例期望跟着改数字，不是逻辑回退 |
| **final_full_pipeline** | 去掉外层 8 次；去掉 `review_budget_tokens: 8000`；冷链仍 1 包 | 链路上 **1a→R2 不变**；复核成败取决于模型限额与内层 retry，不再人为连打放大 1305 |
| **api_retry** | 项 1–5 mock 不变；项 6 真实复核建议随拍板换模型 | 重试层行为不依赖外层 for |
| **block_stitch_r2 / 2b** | 不改 | **无回归** |

回归底线：不改 prompt、不改桥/拼接/normalize；复核「有 findings / 打回语义」与模型有关，与切包预算放大方向一致（更不容易误碎）。

---

## 10. 硬编码自查

实现时禁止再出现：

| 禁止 | 应改为 |
|------|--------|
| `or 8000` / `review_budget_tokens: 8000` / `1800` 作为预算 | `resolve_review_budget()` |
| 代码里写死 `GLM-4-Flash-250414` / `110000` 为唯一路径 | `config` + 可选 `llm.context_by_model` |
| `sleep(45)` × 8 外层 | 删除；间隔只读 `review.inter_package_delay_sec` |
| `max_packages` 上限写死 30 在逻辑里不读配置 | `config.get_param("review.max_packages")` |
| `small_table_ratio` 写死 0.5 | 读配置 |
| 包间 `sleep(1)` 字面量 | 读 `review.inter_package_delay_sec` |
| 切包仍用 `json.dumps(package)` 估 token | 必须 `build_review_prompt` |

允许保留：错误码字符串、prompt 维度常量（本设计不改）、已有 `review.reanalyze_max` / `invalid_threshold`。

GUI（可选后续）：设置页增加 `review.context_tokens`、`review.small_table_ratio`、`review.max_packages`、`review.inter_package_delay_sec`；`review.model` 已有。非本设计强制项，可与写码同 PR 或随后。

---

## 实现落点（供审后写码，本轮不动手）

| 模块 | 改动摘要 |
|------|----------|
| `config.py` / `config.json` | 新增 §1 键；废弃说明 |
| `pipeline/block_review/review.py` | `resolve_review_budget`；`review_packages` 按 prompt；小表阈值；包数上限；包间 delay；去掉对 `review_budget_tokens` 依赖 |
| `acceptance/final_full_pipeline/run.py`、`resume_review.py` | 去外层 8 次；去硬编码 budget |
| `acceptance/block_review_5b/run.py` | 去掉 `review_budget_tokens: 1800` |
| `ui/main_window.py` | 可选暴露新配置 |
| `llm.context_by_model` | 拍板后可加 `GLM-4-Flash-250414: 110000` |

---

## 验收建议（设计过、写码后）

1. 单元/本地：`review_B` 在 context=110000、fill=0.8、res=4000、oh=1500 时 = **82500**。  
2. 冷链 artifacts：`packages==1`，`prompt_est ≤ review_B * 0.5`。  
3. 人造超预算多行表：按 prompt 切开；`len>30` 抛 `analysis_too_many_packages`。  
4. 最终脚本：日志中无 `review retry 0..7` + `sleep(45)` 外层循环。  
5. 包间：2 包时两次 API 间隔 ≥ 约 1s（mock clock 可测）。  
6. 硬编码扫：仓库内无 `review_budget_tokens` 生效路径。

---

## 拍板记录

- D1：`review.model` 默认 `GLM-4-Flash-250414`。
- D2：`review.context_tokens` 默认 `110000`；GUI 设置页可编辑。
- D3：路 A。R2 拆列复核盲区记为已知限制，本轮不改 `review.py` 材料组装。
- 错误码：`analysis_too_many_packages`。

---

## 已知限制

### R2 拆列的复核盲区（路 A，本轮不修）

R2 拆出的 `xxx#mapping_index` 列，其源列头是**共享源列头**（描述的是源列，不是这一拆列实际装的值）。

复核当前用「列名 vs 源列头」判语义是否一致。拆列场景下源列头不描述该列实际值，模型仍可能判 `acceptable=true`。

例：`on_duty#8` 的源列头是「总机」、实际值是「值班」；`dispatch#9` 同理。复核可能写成「列名与源列头一致」。

这是已知限制。缓解路径（后续独立批次，不在本轮做）：

- **路 B**：`review_packages` 组装时，拆列的源列头换成该列实际值样本。
- **路 C**：复核维度从「列名 vs 源列头」升级为「列名 vs（源列头 **且** 实际值）」，任一不匹配判 `acceptable=false`。

本轮先跑通。路 C 等真实场景积累后再评估。

---

停下等审。

