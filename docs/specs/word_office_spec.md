# FileMate Word 业务线接入设计草案（v0.3）

> 面向《Office Word 模块实现规格（FileMate 接入版）》（正式 1.1）的落地细化。
> 本草案覆盖**接入层 + 分流层 + Word 模块内部流程的工程化补全**，不重写 1.1 已写死的协议。
>
> **总原则（贯穿全文）**：
> - 功能重合处 → **照搬 FileMate 已验证实现**（标 🟢复用）
> - 相较 FileMate 有优化 → **保留 FileMate 的优化**（标 🟡保留优化）
> - 表格 vs Word **本质不同** → **单独重设计**（标 🔴重设计）

---

## 〇、修订历史

### v0.5（相对 v0.4，逐条对应第四轮评审）

| # | 评审意见 | v0.5 落地位置 |
|---|---|---|
| D1 | 模型是真正瓶颈（QP 一次通过率约 9%，硬数据），草案未覆盖 | §八.3 新增「模型能力 spike」，与扫描器并列为第 0 步 |
| D2 | 写回必须后置，v1 先只做「只读」 | §四 implemented_tiers=[]、implemented_capabilities=只读；§十.1 落地清单砍到只读 |
| D3 | 别一上来造 Word 编辑器（L0-L3+七状态机+事务=迷你编辑引擎） | 收口：先只读切片验证用户需求，再决定是否投入事务层 |

### v0.4（相对 v0.3，逐条对应第三轮评审）

| # | 评审意见 | v0.4 落地位置 |
|---|---|---|
| C1 | business_line_id / task_runner 节点状态 在真实代码 0 命中，§三.2 自相矛盾 | §三.2 删掉信号表，改写为 WordResult.status → dispatcher 渲染分支；word_office 不经过 task_runner |
| C2 | 「4 选 1 → 5 选 1 + prompt 裁决」前提不成立（file_organize 是正则兜底，不看 tool） | §二 写清 word_office 走 tool 短路（同 table_query），真实风险是正则兜底抢在 word_office 之前 |
| C3 | 「总结文档」是替换已有 cap_parse→cap_analyze 链路，不是纯新增 | §九 改为「v1 只做写回/编辑等 file_parser 不覆盖的新能力，避开文档摘要」 |

### v0.3（相对 v0.2，逐条对应第二轮评审）

| # | 评审意见 | v0.3 落地位置 |
|---|---|---|
| B1 | 版本号错位（草案 v2.0.0 vs 仓库 CURRENT=v1.9.0） | 全文改 v1.10.0/v1.11.0/v1.12.0，延续 v1.x 序列 |
| B2 | stage1-4 下线是独立决策，不与 Word 接入绑死 | §九 拆出单独立项，§十 移出落地清单 |
| B3 | 三处真·重设计单列成本 | §十.2 |
| B4 | 模型档位是新基建（当前仅单模型） | §十.3 |
| B5 | handle() 可中断回调是 v1 第一验收点 | §十.3 |
| B6 | 落地顺序调整 | §十 重排：spike 冻结 → 版本对齐 → 契约 → 5 选 1 → 句柄/信号表 |

### v0.2（相对 v0.1，逐条对应第一轮评审）

| # | 评审意见 | v0.2 落地位置 |
|---|---|---|
| A1 | status==needs_user 与返回结构不一致 | §三 信号流图：统一为 `partially_completed + sub_state=awaiting_user_decision`，不造独立 status |
| A2 | tiers_by_version 与 rules[].implemented 优先级未写死 | §四：`implemented_tiers` 是唯一事实来源，规则 `implemented` 是冗余标注，不一致 → 配置错误告警 |
| A3 | 落地清单缺 spike | §八 扫描器 spike 提到 v1 第 0 步；§十 落地清单补第 0 步 |
| Q1 | 契约 JSON 膨胀 | §四.2：分两步——先「分区懒加载」，破 6000 行再「版本目录多文件」 |
| Q2 | 整体失败判定信号 | §三.2：写死一张 status→回滚 映射表 |
| Q3 | handle() 同步/异步 | §三.3：v1 同步阻塞 + 可中断回调，v2 事件驱动 |
| Q4 | 扫描器 spike 提前 | §八：spike 范围表 + 结论直接决定 v1 范围 |
| Q5 | 与 file_parser 冲突 | §九：显式声明「不经过 file_parser/stage1-4」 |
| S1 | implemented_tiers 唯一事实来源 + 不一致告警 | §四.1（与 A2 合并落定） |
| S2 | 「已标？」改「有对应机制？」 | §五 表格列名与语义修正 |

---

## 一、三分类判定表（先定调，后文所有设计据此归类）

| 能力 | 表格/文件域（FileMate 现有） | Word 内容域 | 归类 |
|---|---|---|---|
| 意图路由 + 白名单消毒 | `intent_router` 4 选 1 | 5 选 1 + word_office 冲突裁决 | 🟢 复用结构，🔴 扩裁决 |
| 失败反馈闭环 | `_correct()` + correction_rounds | 同 | 🟢 复用 |
| 目标定位 | 目录/文件路径解析 | 「当前文档句柄」 | 🔴 本质不同，重设计 |
| 方案生成 | AI 出 JSON 计划 | AI 出 PatchSet/方案 | 🟢 复用「AI 出 JSON + 本地校验」 |
| 档位/算子判定 | 契约 operator_set | 决策表 L0–L3 | 🔴 本质不同（算子 vs 风险档位） |
| 语义对账 | `calc replay` + `row_accounting` | 效果重放 + 内容记账 | 🟢 复用（对账是通用的） |
| 空值三分类 | `classify_empty` | 谓词三态 | 🟢 复用「不确定即拒绝」模式 |
| 代价前置 | `gate_step_budget` | `estimate_cost` | 🟢 复用 |
| 分片 | `route_before_qp` I-slice | `route_before_scan` | 🟢 复用 |
| 记忆召回 | `hybrid_recall` XOR | Word 术语表召回 | 🟢 复用（表结构不同） |
| 执行链/状态机 | task_runner DAG | Word 独立执行器 | 🔴 本质不同，独立 |
| 回滚 | 文件移动撤回/表原子回滚 | 文档 op 级快照 + 事务 | 🔴 本质不同，重设计 |
| 取消/续跑 | `TaskControl` + checkpoint | 同 | 🟢 复用 |
| trace 落盘 | `QueryTraceSink` | Word trace | 🟢 复用 |
| 权威扫描 | `authority_scan` | 决策表防 if-else | 🟢 复用 |
| 契约兼容窗口 | `deprecated_enum_values` | 决策表规则兼容 | 🟢 复用 |

**一句话：编排层、校验层、安全层、韧性层 ≈ 90% 复用 FileMate；只有三处必须重设计（当前文档句柄的确定、Word 独立执行链与 task_id 关联、文档 op 级事务回滚）。**

---

## 二、word_office 冲突裁决 + mode 语义 prompt 草案

> 照 `agent_core/intent_router.py` 的 `_PROMPT_TEMPLATE` 结构扩写。`+` 标注新增行。

```
你是 FileMate 的唯一意图决策器（Agent Router）。本地程序只按你的 JSON 调度工具。只输出一个 JSON 对象，禁止输出 JSON 以外的任何文字、不要 markdown 代码块。

【用户指令】
{user_input}
【根目录】{target_dir}
【用户当前选中的文件】（若非空，跨表查询优先用这些路径）
{selected_files}
【根目录直接包含的文件】
{file_list}
【各子文件夹及其内部文件】
{subdir_detail}

【第零步：选择 tool（最重要，互斥优先）】
tool 只能是以下之一：
- table_query：……（原文不变，略）
- table_synthesize：……（原文不变，略）
- file_organize：……（原文不变，略）
- chat：……（原文不变，略）
+ - word_office：编辑/改写/排版/校对/翻译/审阅 **Word 文档内部的内容或格式**，或提取 Word 内嵌的段落/表格/图片。
+   强信号（命中任一条就选 word_office，禁止选 file_organize）：
+   · 目标带「这段/这章/这篇文档/这个段落/标题」等文档内定位词 + 编辑动词（改写、润色、删掉、插入、调整格式、统一术语、生成大纲、校对、翻译、审阅）
+   · 「把 Word 里的表格/图片/目录提取出来」且目标只是该 Word 文档内的对象
+   · 「给这篇文档做个摘要/总结/批注建议」且目标是文档内容本身
+
+ 【word_office 的分流位置（重要，写死）】：
+ word_office 和 table_query 一样，**只信任 tool 字段直接短路**；
+ file_organize / table_synthesize 是「非 table_query/word_office 时走本地正则兜底」，不看 tool 字段。
+ 因此「word_office vs file_organize」的真实风险不是 prompt 裁决，而是：
+ · 模型报 tool=word_office → 短路到 Word 模块（不受正则干扰）
+ · 模型没报 word_office → 「整理这个 Word」会被正则兜底接走（file_organize / content_organize）
+
+ 硬约束（写进强信号，防正则兜底抢跑）：
+ · 目标是「Word 文档内部的内容/格式编辑」→ **必须报 tool=word_office**，禁止报 file_organize 或留空
+ · 只有「移动/归档/重命名 Word 文件本身」才允许不报 word_office
+
+ 冲突裁决（word_office vs table_query）：
+ · 目标是「单个 Word 文档内的某段/某张内嵌表」→ word_office
+ · 目标是「多个 Excel/CSV 表之间的数据关联/筛选/汇总」→ table_query
+ · 「把 Word 里的表格提取出来」且只读该表 → word_office；「提取后还要和别的表联查」→ table_query
+ · 当前选中的若是 .docx → 优先 word_office；选中的是 .xlsx/.csv → 优先 table_query

【第一步：确定工作文件夹 subdir】
（……原文不变……）
+ word_office 不填 subdir：目标是「当前打开的 Word 文档」，以当前文档句柄为准。

【第二步：判断 mode】
- execute：祈使语气，要求现在就把事办了。
- consult：询问做法、建议、可行性，或明确说"先给方案、先别动、能不能、怎么、如何"。
- chat：问候、闲聊、与文件无关的问题。tool 也必须是 chat。
+ word_office 的 mode 语义（与 Word 模块「方案卡片」衔接）：
+ · execute：要求现在改文档 → 进 WordModule 出方案卡片，用户确认后执行
+ · consult：只想要方案/预览 → 进 WordModule 但只跑到「方案卡片」就停，不执行任何写入
+ · 无明确倾向默认 execute（继承 FileMate「执行者不是咨询顾问」基调）

【第三步：生成 actions 数组】
（……原文不变……）
+ 4) tool=word_office 时：不填 actions（Office 的 Plan 由 Word 模块内部生成）。
+   只填 {tool, mode, confidence, plan}；confidence 取意图识别的置信度，plan 用一句话中文说明要做什么。

只输出如下 JSON：
{{"tool":"file_organize|table_query|table_synthesize|word_office|chat","mode":"execute|consult|chat","subdir":"","actions":[...],"plan":"","confidence":0.0}}
```

**需改代码处**：`intent_router.VALID_TOOLS` 加 `"word_office"`；`_normalize()` 对 word_office 分支强制 `actions`/`subdir` 空、`confidence` 透传。

---

## 三、接入层 ↔ Word 模块 完整信号流图

### 3.1 信号流图

```
┌─ UI 层：_DispatchWorker(QThread) ──progress.emit──▶ 对话区 ──────────────┐
│    持有 TaskControl（cancel/pause）                                      │
└────────┬─────────────────────────────────────────────────────────────────┘
         │ dispatch(text, dir, file_paths, control, channel_id, resume)
         ▼
┌─ 接入层 IntentDispatcher.dispatch() ──────────────────────────────────────┐
│ ① 目录/Key 校验（复用现有）                                               │
│ ② intent_router.route(...)  ←【分流层】5 选 1 + 冲突裁决 + mode 语义      │
│ ③ tool==word_office → 短路（同 table_query），复用 channel_id 关联审计              │
│ ④ 解析「当前文档句柄」：优先 file_paths 里的 .docx → 兜底                 │
│    infer_work_dir_from_path → 校验存在/后缀/占用 → 失败返回提示           │
│ ⑤ 构造 WordRequest{utterance, doc_handle, selection_meta, mode,          │
│    confidence, channel_id, control, api_key}           │
└────────┬─────────────────────────────────────────────────────────────────┘
         │ WordModule.handle(request, ui_callback)   ←【同步阻塞 + 可中断】
         ▼
┌─ Word 模块内部（独立执行链，不经过 task_runner）────────────────────────┐
│  粗识别 → 只读结构探测 → 精绑定 → L0 前置路由                              │
│    → 完整扫描 → 方案构造(决策表) → Overlay → preflight                     │
│    → 方案卡片(ui_callback 阻塞等确认) → 逐 op 执行 → 写入后校验            │
│  每个 op 前：control.checkpoint()  ；每 op 后：save_checkpoint()          │
│  每步：trace_sink.write()  ；op 级日志带 channel_id                 │
└────────┬─────────────────────────────────────────────────────────────────┘
         │ 返回 WordResult（见 3.2 status 枚举）
         ▼
┌─ 接入层收口 ──────────────────────────────────────────────────────────────┐
│ 按 3.2 渲染分支表：status → dispatcher 渲染分支（不经过 task_runner）           │
│ ⑥ 反馈闭环：status==failed 且 fail_locus 可纠正 → _correct() 带反馈重路由 │
│ ⑦ 方案卡片渲染：partially_completed + awaiting_user_decision 作为消息类型 │
│ ⑧ cancelled → 清 checkpoint，提示可续跑（复用现有 cancelled 分支）         │
└──────────────────────────────────────────────────────────────────────────┘
```

### 3.2 WordResult.status → dispatcher 渲染分支（写死，对应评审 Q2/C1）

> **word_office 在 dispatcher 层短路，不经过 task_runner**（同 table_query）。因此**不存在**「task_runner 节点状态」和「review 回滚」这两件事——回滚是 Word 模块内部（文档快照）的事，与 task_runner 的文件级 `rollback_delivery` 完全无关。`needs_user` **不是** status，是 `partially_completed` 的 `sub_state`。

| WordModule 返回 status | dispatcher 渲染分支 |
|---|---|
| `done` | 组装完成消息 |
| `partially_completed`（sub_state=awaiting_user_decision） | 方案卡片作为新消息类型（复用 table_query 的 needs_user 返回结构 + ask_history 回传） |
| `failed` | fail_locus 可纠正 → `_correct()` 带反馈重路由；否则明确失败提示 |
| `plan_invalidated` | 提示「文档已变更，需重新生成方案」 |
| `split` | 提示「已拆分，重新生成方案」 |
| `cancelled` | 清 checkpoint，提示可续跑（复用现有 cancelled 分支） |

### 3.3 WordModule.handle() 同步模型（v1，对应评审 Q3）

**v1：同步阻塞 + 可中断回调。v2 再事件驱动。**

```python
def handle(request: WordRequest, ui_callback: Callable) -> WordResult:
    # ui_callback(event) 请求 UI 呈现；阻塞等用户响应
    plan = construct_plan(...)
    if plan.needs_confirmation:
        event = ui_callback({"type": "plan_card", "plan": plan})  # 阻塞等待
        if event.cancelled:
            return WordResult(status="cancelled")
        if event.modified:
            plan = regenerate_plan(event.modifications)
    ...
```

**关键约束（写死）**：

- `ui_callback` **必须可中断**：用户点取消时，阻塞的 `Event.wait()` 必须能被 `control.cancel()` 唤醒，返回 cancelled。**不用裸 sleep 或不可中断的 join。**
- 每个 op 执行前调 `control.checkpoint()`：复用 FileMate `TaskControl`，取消/暂停在 op 边界生效。
- **v1 不支持「等用户确认中途续跑」**：用户取消 = 放弃本次，checkpoint 清空。v2 再支持从「等确认」恢复（需持久化 Plan + 已执行 op 状态）。
- `_DispatchWorker` 每次任务用**独立 QThread**，不是线程池。一个 Word 任务占一个线程，用户不确认时线程阻塞，但不影响表格/文件任务。
- **超时保护**：handle() 整体超时上限 30 分钟，超时强制返回 cancelled 并清理 checkpoint，防线程永久占用。
- v2 事件驱动的触发条件：当「用户确认停留时间导致线程占用」成为实测问题，改成 `WordModuleSession + step()` 推进，状态存 session。**v1 不要提前做**——事件驱动要把状态机拆成可序列化状态，L2/L3 未实现时做会过度设计。

---

## 四、word_office 契约分区（含渐进填充 + 懒加载）

### 4.1 分区骨架 JSON

> 放 `contracts/versions/v1.10.0.json` 顶层，与 `cross_table` 平级。
> **渐进填充铁律**：`implemented_tiers` 是**唯一事实来源**；规则上的 `implemented` 字段是**冗余标注**，校验以 `implemented_tiers` 为准；两者不一致 → **配置错误告警，不静默取其一**。（对应评审 A2/S1）

```jsonc
{
  "word_office": {
    "schema_version": "1",
    "implemented_capabilities": ["readonly_extract", "readonly_structured_summary", "readonly_proofread_suggest"],  // ← v1 只读能力（唯一事实来源）
    "implemented_tiers": [],                     // ← 写回分层 L0-L3 全部后置（v1 不做写回）
    "tiers_by_version": {                        // ← 仅作审计参考（写回分层，后置）
      "v1.10.0": [],
      "v1.11.0": ["L0", "L1"],
      "v1.12.0": ["L0", "L1", "L2"],
      "v1.13.0": ["L0", "L1", "L2", "L3"]
    },
    "writeback_unlock_conditions": {             // ← 写回解锁硬门槛（两项 spike 都达标）
      "scanner_spike_proven": "read_write_without_corruption",
      "model_span_precision": "one_shot_pass_rate_above_qp_baseline"
    },

    "enums": {
      "intent_type": ["rewrite", "format_clean", "proofread", "translate", "summarize", "extract", "outline", "review", "compare", "other"],
      "tier": ["L0", "L1", "L2", "L3"],
      "confirmation_level": ["silent", "light", "modal"],
      "operation_scope": ["selection", "single_unit", "multi_unit", "full_document"],
      "predicate_tristate": ["true", "false", "unknown"],
      "predicate_scope": ["target_only", "target_plus_adjacent", "target_plus_direct_influence"],
      "binding_status": ["unbound", "bound", "failed"],
      "fidelity_constraint": ["revision", "comment", "content_control", "field", "formula", "merged_cell", "hyperlink", "layer", "signature", "embedded_object", "external_link", "toc", "bookmark", "section_break", "nested_table", "other"],
      "fail_locus": ["binding", "anchor", "predicate", "structure", "constraint", "postcheck", "replay", "other"],
      "plan_state": ["generated", "confirmed", "executing", "validating", "done", "rolled_back", "partially_completed", "plan_invalidated", "failed_execution", "failed_postcheck", "compensation_failed", "rollback_failed"],
      "op_type": ["replace", "insert", "delete", "move", "format", "tracked_change"],
      "claim_type": ["modify_count", "preserve_assert", "structure_assert", "content_accounting", "span_anchoring"]
    },

    "standard_predicates": [
      {"name": "no_revision_in_target", "scope": "target_only"},
      {"name": "no_content_control", "scope": "target_only"},
      {"name": "no_section_break_in_range", "scope": "target_plus_adjacent"},
      {"name": "no_nested_table", "scope": "target_plus_direct_influence"},
      {"name": "no_bookmark_in_range", "scope": "target_plus_direct_influence"},
      {"name": "no_field_in_range", "scope": "target_plus_direct_influence"},
      {"name": "undo_available", "scope": "target_only"}
    ],

    "tool_registry": {
      "format_clean_basic": {
        "match_pattern": {"kind": "structural", "hint": "基础格式清理"},
        "predicates": ["no_revision_in_target", "no_content_control", "no_section_break_in_range", "undo_available"],
        "supported_fidelity": [],
        "rejected_fidelity": ["revision", "comment", "content_control", "field", "nested_table"],
        "post_check": "paragraph_count_unchanged",
        "undo_policy": "bind_word_undo_or_snapshot",
        "supported_formats": ["docx"]
      }
    },

    "decision_table": {
      "version": "dt-1",
      "rules": [
        {"rule_id": "R_L1_PLAIN_TEXT_REPLACE", "tier": "L1",
         "intent_type": ["rewrite", "proofread", "translate"],
         "fidelity_constraints": [], "complexity_class": ["simple", "medium"],
         "binding_confidence_min": 0.7, "operation_scope": ["selection", "single_unit"],
         "reason_code": "RSN_L1_WHITELIST_TEXT_REPLACE", "requires_confirmation": "light"},
        {"rule_id": "R_L2_CROSS_STRUCTURE", "tier": "L2", "implemented": false,
         "since_version": "v1.11.0", "intent_type": ["outline", "compare"],
         "reason_code": "RSN_L2_CROSS_STRUCTURE", "requires_confirmation": "light"},
        {"rule_id": "R_L3_HIGH_FIDELITY", "tier": "L3", "implemented": false,
         "since_version": "v1.12.0",
         "fidelity_constraints": ["revision", "comment", "field", "content_control"],
         "reason_code": "RSN_L3_HIGH_FIDELITY", "requires_confirmation": "modal"}
      ]
    },

    "confirmation_overlay": {
      "version": "ov-1",
      "strict_order": ["silent", "light", "modal"],
      "rules": [
        {"rule_id": "OV_FULL_DOC", "condition": "operation_scope == full_document", "target_level": "modal"},
        {"rule_id": "OV_LOW_CONFIDENCE", "condition": "binding_confidence < 0.6", "target_level": "modal"},
        {"rule_id": "OV_DELIVERY_DOC", "condition": "complexity_class == delivery", "target_level": "modal"}
      ]
    },

    "capability_manifest": {
      "version": "cm-1",
      "base": {"L1": {"op_types": ["replace", "suggest_only"],
                     "write_whitelist": ["plain_text_replace", "suggestion_only", "readonly_mark"],
                     "rejected": ["comment", "highlight", "tracked_change", "content_control", "bookmark"]}},
      "deployment_overrides": {
        "local_sandbox": {"extra_support": [], "extra_reject": ["signed_doc_write"]},
        "local_service": {"extra_support": [], "extra_reject": []},
        "backend": {"extra_support": [], "extra_reject": []}
      }
    },

    "budget": {
      "preflight_per_op_ms": 200, "preflight_plan_ms": 1000,
      "version_check_per_op_ms": 50, "version_check_plan_ms": 500, "plan_card_ms": 500
    },

    "authority_scan": { /* 见 §七 */ },
    "deprecated_enum_values": {}
  }
}
```

**启动强校验（boot.py 扩展）**：

```python
wo = cv.data.get("word_office")
if not isinstance(wo, dict):
    raise ContractLibraryError("word_office partition missing")
caps = set(wo.get("implemented_capabilities") or [])
if not caps:
    raise ContractLibraryError("word_office.implemented_capabilities empty")
implemented = set(wo.get("implemented_tiers") or [])  # v1 只读时可为空（写回后置）
# 只校验已实现档位的规则完整；未实现档位（tier ∉ implemented）不报错
for rule in wo.get("decision_table", {}).get("rules", []):
    if rule.get("tier") in implemented:
        for k in ("rule_id", "tier", "reason_code", "requires_confirmation"):
            if not rule.get(k):
                raise ContractLibraryError("word_office rule incomplete: %s" % rule.get("rule_id"))
    else:
        # 冗余标注一致性：未实现档位若写 implemented=true → 配置错误告警（不静默）
        if rule.get("implemented") is True:
            raise ContractLibraryError("word_office rule %s declared implemented but tier not in implemented_tiers" % rule.get("rule_id"))
```

### 4.2 契约 JSON 膨胀的应对（分两步，对应评审 Q1）

**第一步（v1 就做）：单文件 + 分区懒加载 + 引用完整性校验。**

- `load_contract()` 拆两段：`load_contract_skeleton()` 只加载 `schema_version`、CURRENT、各分区顶层键和 `enums`（高频基础层，几十毫秒）；`load_partition(name)` 按需加载决策表、Overlay、Manifest 等重分区。
- 分区间引用**显式声明**，启动时做引用完整性校验：决策表规则引用 `enums.tier`、`enums.fidelity_constraint`；Overlay 引用 `enums.operation_scope`；CapabilityManifest 引用 `enums.op_type`。缺引用 → `ContractLibraryError`。
- 单文件仍是唯一事实来源，CURRENT 不变，启动强校验覆盖全文件，只是运行时按需解析。

**第二步（破 6000 行 / 多人并行改契约时再做）：版本目录 + 多文件 + 目录级 hash。**

- `contracts/versions/vX.Y.Z/` 变目录（版本号随目录化时点定）：`_meta.json`（版本/hash/分区清单）+ `enums.json` + `cross_table.json` + `word_office.json`。
- CURRENT 仍指向版本号；hash 校验覆盖整个目录（目录内每文件 hash 汇总成根 hash）。
- 启动强校验遍历目录，缺任何分区文件 → `ContractLibraryError`。
- 「单一事实来源」语义从「一个 JSON 文件」升级为「一个版本目录 + 一个 CURRENT 指针 + 一次启动强校验」。

**明确不做**：不用 JSON `$ref`/include 做单文件内引用（JSON 原生不支持，需预处理，预处理会成为新故障点）；不为了「拆而拆」拆成十几个小文件——**分区以「变更频率 + 责任方」为界，不以「对象类型」为界。**

---

## 五、Word 内部流程 FileMate 同构版（10 个通用点标注）

> 左列是 1.1 版原流程，右列标注「照搬哪个 FileMate 机制 + 补在哪一步」。
> 列名「1.1 版有对应机制？」区分「有机制但没对标 FileMate」和「真缺」两种情况。

```
Word 1.1 总流程                     FileMate 同构锚点           归类
─────────────────────────────────────────────────────────────────────────────
① 粗识别（不读内容）          = intent_router.route 不读文件内容     🟢 复用
   补：intent_type 白名单 + 纯结构兜底（_infer_tool_from_actions 式） [点2]
   补：记忆召回 hybrid_recall 显式步骤（召回历史「术语↔锚点」）      [点10]
     ↓
② 只读结构探测               = build_minimal_portrait（轻量画像）    🟢 复用
   补：disclose-only（只披露不展开，仿 _layout_facts_from_path）     [点4]
     ↓
③ 精绑定（target_hints→ref）  = 无直接对应（FileMate 表格无此步）    🔴 重设计
   补：原话锚定 utterance_span 必须是真实子串（仿 qr.span_covered）  [点5]
     ↓
④ L0 独立前置路由            = dispatcher._wants_table_synthesize 短路 🟢 复用
   补：谓词三态 unknown 保守（仿 classify_empty 的 unclassified 拒绝）[点3]
   补：命中即短路，绝不绕回 AI（仿 want_synth_early 纪律）          [点1]
     ↓
⑤ 完整状态扫描（DocumentState）= 完整表加载 load_flat_xlsx           🔴 重设计
   补：文件 sha256 + 结构指纹 双层缓存（仿 cache_db）               [点10]
     ↓
⑥ 方案构造（决策表）         = 契约 gate_plan + validate             🟢 复用
   补：权威扫描防止代码 if-else 判档位（仿 authority_scan）          [点6]
   补：规则带兼容窗口（仿 deprecated accept_until_version）         [点7]
     ↓
⑦ ConfirmationOverlay        = review.confirm 的「只升不降」        🟡 保留优化
   （FileMate review 是复核非确认级别；「只升不降」纪律同源）
     ↓
⑧ capability_preflight       = gate_step_budget 代价前置 + I-slice 分片 🟢 复用
   补：estimate_cost 超阈值拒绝（仿 gate_step_budget）             [点4]
   补：route_before_scan 分片（仿 route_before_qp）               [点10]
     ↓
⑨ 方案卡片 → 用户确认        = needs_user 返回结构                  🟢 复用
   （方案卡片是新消息类型，返回结构复用 table_query 的 needs_user 模式）
     ↓
⑩ 逐 op 执行 + 写入后校验     = execute_query_plan + qr 复核        🔴 执行重设计
   补：失败分类 retryable（仿 task_runner 的 retryable 标志）       [点8]
   补：效果重放统一成 verifiable_basis（见 §六）                  [点5]
   补：trace 落盘（仿 QueryTraceSink + diagnostics/trace）         [点9]
   补：质量门禁重规划 ≤2 轮（仿 _association_quality_feedback）     [点10]
```

**10 个通用点的状态清单**（「有对应机制？」= 1.1 版是否已有该机制，而非是否对标 FileMate）：

| # | 通用点 | 1.1 版有对应机制？ | 补在哪一步 | 归类 |
|---|---|---|---|---|
| 1 | 快通道短路（want_synth_early） | 有（L0 前置路由） | ④ 强化短路纪律 | 🟢 复用 |
| 2 | 白名单 + 结构化兜底 | **缺** | ① 粗识别 | 🟢 复用 |
| 3 | 谓词三态≈空值五分类 | 有（谓词三态） | ④ 强化「未分类即拒绝」 | 🟢 复用 |
| 4 | verifiable_basis 四要素对账 | 有（效果重放，未统一） | ⑧/⑩ 统一 | 🟢 复用 |
| 5 | 内容记账带 scope | 有（内容记账） | ⑩ 补 scope 分区 | 🟢 复用 |
| 6 | 决策表权威扫描 | **缺** | ⑥ 方案构造 | 🟢 复用 |
| 7 | 契约兼容窗口 | **缺** | ⑥ 决策表 | 🟢 复用 |
| 8 | retryable 失败分类 | **缺** | ⑩ 执行 | 🟢 复用 |
| 9 | trace 落盘 | **缺** | ⑩ 全程 | 🟢 复用 |
| 10 | 记忆召回显式步骤 | 有（文档画像，未显式） | ① 粗识别前 | 🟢 复用 |

---

## 六、语义对账四要素协议（仿 validate_verifiable_basis）

> FileMate `validate_verifiable_basis` 把「AI 声明」规范成 `source_type / source_ref / value / compare_field / compare_op` 五要素机械验证。
> Word 把「效果重放 + 内容记账 + 原话锚定」统一成 **`WordVerifiableClaim` 一种对象 + `validate_word_claim` 一个入口**。

### 6.1 协议对象

```python
@dataclass
class WordVerifiableClaim:
    claim_id: str        # 声明编号（对应 op_id）
    claim_type: str      # 声明类型（契约 word_office.enums.claim_type）
    claim_ref: dict      # 声明对象引用（anchor_version / utterance_span / op_id）
    expected: Any        # AI 声明的期望值
    actual_source: dict  # 本地测量源（"什么字段从哪里数"）
    compare_op: str      # eq / le / ge（照 FileMate compare_op 枚举）
```

### 6.2 五种 claim_type 统一成一张表

| claim_type | 对应 1.1 机制 | expected | actual_source | FileMate 对照 |
|---|---|---|---|---|
| `modify_count` | 效果重放 | "改了 N 处" | 本地 diff 计数 | `calc.replay_calc_spec` |
| `preserve_assert` | 效果重放 | "保留交叉引用/编号" | 前后数量比对 | `validate_verifiable_basis` |
| `structure_assert` | 效果重放 | "标题调成 3 级" | 本地读大纲级别 | `validate_verifiable_basis` |
| `content_accounting` | 内容记账 | "原文全覆盖" | 三集合两两不相交 | `validate_row_accounting` |
| `span_anchoring` | 原话锚定 | "intent 对应 span" | span 是原话真实子串 | `qr.span_covered_by_utterance` |

### 6.3 校验规则（照 FileMate「声明与实测不一致即拒绝」）

```python
def validate_word_claim(claim, doc_before, doc_after) -> ValidationResult:
    if claim.claim_type not in CONTRACT_CLAIM_TYPES:      # 契约枚举
        return fail("claim_type_unknown")
    if claim.claim_type == "span_anchoring":
        if not span_covered_by_utterance(claim.claim_ref["utterance_span"], user_utterance):
            return fail("fabricated_span")                # ← anti self-cert
    if claim.claim_type in ("modify_count", "preserve_assert", "structure_assert"):
        actual = measure(claim.actual_source, doc_before, doc_after)
        if not mech_compare(actual, claim.expected, claim.compare_op):
            return fail("claim_replay_mismatch", actual=actual, expected=claim.expected)
    if claim.claim_type == "content_accounting":
        r = validate_content_accounting(claim.claim_ref["scope"], doc_before, doc_after)
        if not r.ok:
            return r
    return ValidationResult(ok=True)
```

**要点**：把 1.1 版分散在 4.2 的「结构校验 / 内容记账 / 效果重放」三件事，统一成一种对象 + 一个入口。AI 的每个 claim 都必须是可机械验证的，否则拒绝。

---

## 七、权威扫描规则清单（仿 forbidden_defs / forbidden_substrings）

> FileMate `authority_scan` 块有 `forbidden_defs / forbidden_substrings / forbidden_root_scripts`。
> Word 决策表的「禁止代码 if-else 判档位」需要同样的机械扫描。

```jsonc
{
  "word_office": {
    "authority_scan": {
      "rules_version": "wo-as-1",
      "forbidden_defs": [
        "tier_decision", "_decide_tier", "_is_l1", "_is_l2", "_is_l3",
        "hardcode_tier", "should_be_modal", "_requires_confirmation", "_pick_confirmation"
      ],
      "forbidden_substrings": [
        "if tier ==", "tier == \"L", "if operation_scope ==", "return \"L2\"",
        "confirm_level = \"modal\"", "rule_id == \"", "reason_code = \""
      ],
      "forbidden_root_scripts": [
        "_scan_word_tier.py", "_word_tier_hardcode_scan.py", "_purge_word_rules.py"
      ],
      "allowed_sources": {
        "tier": "word_office.decision_table.rules[].tier",
        "requires_confirmation": "word_office.decision_table.rules[].requires_confirmation",
        "reason_code": "word_office.decision_table.rules[].reason_code",
        "overlay_target_level": "word_office.confirmation_overlay.rules[].target_level"
      }
    }
  }
}
```

**判定逻辑**：扫描 `word_office/` 下所有 `.py`，grep `forbidden_substrings`；命中即「档位/确认级别硬编码」，CI 变红。档位/确认级别/原因码的唯一合法来源是 `allowed_sources`。例外：`authority_scan` 规则本身 + `tests/` 下 fixture 豁免。

---

## 八、第 0 步：两项 spike（扫描器 + 模型能力）

> v0.5 把「模型 span 精确输出能力」提升为与「扫描器」并列的第 0 步 spike。**两者都达标，写回（L0–L3）才解锁。** 扫描器是地基（L0 谓词全依赖它）；模型是瓶颈（有硬数据，见 8.3）。

### 8.1 扫描器 spike 范围表（建议 1 周内完成）

| 验证项 | 方案 A：python-docx | 方案 B：OpenXML SDK (pythonnet) | 方案 C：COM (win32com) |
|---|---|---|---|
| 读段落/样式/大纲 | ✅ | ✅ | ✅ |
| 读修订痕迹 | ❌ | ✅ | ✅ |
| 读批注 | ❌ | ✅ | ✅ |
| 读内容控件 | ❌ | ✅ | ✅ |
| 读交叉引用 | 部分 | ✅ | ✅ |
| 读字段 | 部分 | ✅ | ✅ |
| 写回 + 保留修订 | ❌ | ✅ | ✅ |
| 读 100 页耗时 | 快 | 中 | 慢（依赖 Word 进程） |
| 部署复杂度 | 低 | 中（.NET runtime） | 高（需装 Word） |
| PyQt6 + Py3.13 兼容 | ✅ | 需验证 | 需验证 |

### 8.2 扫描器 spike 结论（写死）

- **方案 B 可行且性能达标** → 写回有基础；可落 L2 受控写回、L3 事务层。
- **只有方案 A 可行** → 写回缩水为「只支持无修订/无控件/无交叉引用的简单文档」；遇到保真约束直接拒绝（`scan_capability: unsupported`）。
- **三种都不达标** → 写回彻底放弃，只做只读。

**但注意（v0.5 修正）：无论扫描器 spike 结果如何，v1 都只做「只读」，不做写回。** 写回解锁要同时满足 8.3 的模型能力门槛。

### 8.3 模型能力 spike（span 精确 JSON 一次通过率，对应第四轮评审）

- **基线（硬数据）**：P2 冻结期 QP 调用存档 ok 约 9.2%、reject 44%、retry 24%、needs_user 19%（诊断存档有幸存者偏差，不当精确通过率，但「一次给对是少数」是确定的）。
- **Word 要求比 QP 更苛刻**：原话锚定（span 必须是原话真实子串）、内容记账（三集合两两不相交、并集=原文）、效果重放（AI 声明改 N 处，本地必须数出 N 处）。这些要求 **span 级精确、可机械验证** 的 JSON。
- **spike 内容**：拿一批真实 Word 指令，测模型**一次产出「可机械验证的 span 级 JSON」的比例**。
- **验收指标（写死）**：一次通过率要**显著高于 9% 基线**，否则内容记账/效果重放是空中楼阁。

**两项 spike 都达标，写回（L0–L3）才解锁。**

---

## 九、file_parser 边界声明（对应评审 Q5）

> 在 1.1「三、Word 模块内部架构」开头，加一条**写死的边界**：

**Word 模块不经过 `file_parser` / stage1-4。**

- `file_parser` 把 docx 拍平成 `{text}` 或 `{table}`，丢失结构；Word 模块需要完整 OOXML 结构树（paragraph / run / style / revision / content_control / field / hyperlink / section / bookmark）。
- `DocumentState` 扫描器是**独立新组件**，不走 stage1-4。
- 文档格式识别在接入层已完成（`intent_router` 按 `.docx` 后缀分流），不需 `file_parser` 再识别。
- **唯一复用点**：文件 sha256 指纹计算，作为纯工具函数复用，不经过 `file_parser` 的解析流程。

**能力重叠的避让（写死）**：当前 docx 已有一条可用的「文档摘要」链路（`cap_parse` 拍平成 text → `cap_analyze` 全文摘要），已被验收。Word 模块 **v1 只做 file_parser 完全不覆盖的新能力**（写回/编辑/排版/审阅），**避开与已有「文档摘要」正面抢**。「总结文档」是否迁移到 Word L1，是后续独立的替换决策，**不在 v1 范围**；stage1-4 下线单独立项，与 Word 接入解耦。

意义：避免「同一份 docx，file_parser 拍一遍给 stage1-4，Word 模块又读一遍结构」的双重解析，也避免 stage1-4 的文本结果被 Word 模块误用的隐性耦合。

---

## 十、落地清单与成本（v1 范围内，按依赖排序）

> **顺序调整（第二轮评审 B6）**：spike 结论冻结后再定 v1 范围 → 版本号对齐 → 契约分区 + 强校验 → intent_router 5 选 1 → 句柄/信号表/同步模型骨架。三处重设计里，「句柄确定」和「信号表」先于执行器（它们是接入层与 Word 模块的契约边界）。

### 10.1 落地清单（重排后）

| 步骤 | 内容 | 归类 |
|---|---|---|
| **0** | **两项 spike**（§八）：扫描器（读 OOXML）+ 模型能力（span 精确 JSON 一次通过率）——**双达标后才解锁写回** | 🔴 重设计 |
| **1** | **版本号对齐**：CURRENT v1.9.0 → v1.10.0，确认走 v1.x 延续（word_office 是新增分区，非 breaking change） | 🟢 复用改造 |
| 2 | `contracts/versions/v1.10.0.json` 加 `word_office` 分区（implemented_capabilities=只读，implemented_tiers=[]）+ `boot` 扩强校验（§四.1） | 🟢 复用结构 🔴 新分区 |
| 3 | `intent_router.VALID_TOOLS` 加 `word_office` + `_normalize` 适配 + `_PROMPT_TEMPLATE` 扩裁决（§二） | 🟢 复用结构 🔴 扩裁决 |
| 4 | `dispatcher.dispatch` 加 word_office 分支（tool 短路，同 table_query）+ 句柄校验（§三） | 🔴 重设计 |
| 5 | `WordModule` 只读链路（提取内嵌对象 / 结构摘要 / 校对建议）——不涉及写回/事务/逐 op 快照 | 🔴 重设计 |
| 6 | 语义对账（只读轻量版：提取完整性、建议锚定）（§六） | 🟢 复用 |
| 7 | 权威扫描 `word_office.authority_scan` + CI 脚本（§七） | 🟢 复用 |
| 8 | 契约分区懒加载 `load_contract_skeleton`/`load_partition`（§四.2） | 🟢 复用改造 |
| 9 | file_parser 边界声明落地（§九）；「结构感知摘要」是新能力，「拍平全文摘要」继续走 cap_analyze 不迁移；stage1-4 下线单独立项 | 🟢 复用改造 |

> **写回（L0–L3）另起一个迭代**，解锁条件 = 两项 spike 双达标（§八.3），不在本 v1 清单内。

### 10.2 三处真·重设计的成本单列（其余「复用」项是小改）

| 重设计项 | 工作量 | 说明 |
|---|---|---|
| 当前文档句柄确定 | 中（牵动 UI 层） | 唯一 UI 级改动：UI 要能传 .docx 句柄 + 选区/光标元数据 |
| Word 只读链路（提取/结构摘要/校对建议） | 中 | v1 核心新增，不涉及写回/事务 |
| Word 独立执行链 + 审计关联 | 中 | 复用 channel_id 关联审计，需动 dispatcher（短路分支）；不涉及 task_runner |
| op 级事务回滚（快照+补偿） | 高 | **写回才需要，v1 后置**：每 op 独立快照 + 补偿 + 失败子状态 |

> 这三处是真实工期；其余「复用」项（改 VALID_TOOLS、加分区、扩 prompt、扩 boot 校验）是小时级小改。

### 10.3 落地提醒（不阻塞 v1，但需单独拍板）

1. **v1 只读不需要 handle() 阻塞确认**：只读链路（提取/摘要/校对建议）是「一次性出结果」，不涉及方案卡片等用户确认。§三.3 的「同步阻塞 + 可中断回调」是**写回**才需要的机制，随写回一起后置。
2. **v1 只读用单模型即可**：当前仅一个 GLM 客户端（`ai/client.py`，默认 glm-4-flash）。只读提取/摘要对模型能力要求低于写回，单模型够用；「L1 小模型 / L2 中档 / L3 强模型」的档位配置是写回才需要的新基建。
3. **模型能力是第 0 步风险**：QP 一次通过率约 9%（见 §八.3），只读场景的「提取完整性」「摘要锚定」虽比写回宽松，仍需在 spike 里验证模型能否稳定产出可校验 JSON。

---

## 十一、一句话收口

> v0.5 用硬数据（QP 一次通过率约 9%）修正了范围判断：**模型 span 精确输出能力与扫描器并列为第 0 步两项 spike，v1 从「L0+L1 写回」砍到「只读 Word 理解」（提取/结构摘要/校对建议），写回后置、双 spike 达标才解锁**。架构层（tool 短路、不经过 task_runner）已在前几轮确认成立，不改。最终路径：先只读切片验证用户是否真要「在文档里改」，再决定是否投入 L0–L3 事务层——不反着来先把迷你编辑引擎架好，再发现小模型吐不出可机械验证的 span。
