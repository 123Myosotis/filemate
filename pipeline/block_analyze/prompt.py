"""拼装 analyze_blocks 提示词。材料顺序固定；无行业词、无疑问范文。

α 边界（写死，勿误用）：
- ordered：表头行在【块】blob 内（整段含行 0，或 repeat_header 重贴），α 有效。
- matrix：合并标签在 blob 里只是普通单元格字；【context】仅坐标。
  matrix 列名合理性靠 5a 复核的源列头对照抓，不靠本节【生成指导】α。
"""

from ai.context_budget import estimate_tokens
from pipeline.block_plan.policy import SerializeSpec
from pipeline.block_plan.tokens import serialize_rows

# 生成指导（α）：生成语气，不是复核「检查是否」语气。
# ordered 有效；matrix 帮助有限，列名质量主要靠 5a。
_GUIDANCE = """【生成指导】
以下是生成 mappings 时的指导（由你根据看见的块材料判断；不是固定判定规则；不要套字数或词表）：

1. target.name 应描述「这一列装的是什么数据」（数据字段名），不是「用户要求做什么」（任务描述）。任务描述属于用户原话，不应直接当作输出列名。
2. 写 target.name 时，对照本包【块】里已经给出的单元格文本（含表头行或块内可见的列题/标签）；使列名与这些源侧题名/数据所表达的字段一致。这不是要求与某一格逐字相同。
3. 块内有多个不同数据列需要输出时，应各输出一条 mapping；一条 mapping 对应一个输出列。不要把整次任务收成单一条映射来概括。
4. sources[].locator 应能对准你声称要输出的那一列在块内的位置（列题或可区分的列定位）；不要用与该输出列无关的定位充数。
"""

# 输出形状说明：只列字段名，不写业务例子（安全边界）
_SHAPE = """只输出一个 JSON 对象，键名固定：
{"mappings":[{"sources":[{"block_id":<已给出的id>,"locator":<模型自写>}],
"target":{"name":<模型自写>},"rule":<任意对象可省略>}],
"questions":[{"hypothesis":"","evidence":"","if_holds":"","ask_user":"",
"block_ids":[<已给出的block_id>],"scope":"block|mapping|value_domain",
"mapping_indexes":[<该块本次mappings下标，scope需要时>],
"value_domain":<仅scope=value_domain时>}],
"alignments":[{"align_id":<可选>,
"slots":[{"block_id":<已给出的id>,"locator":<必须能唯一落到列，如列标题；不要用单格坐标当对齐键>}]}]}
没有疑问时 questions 必须是空数组，不要省略该键，不要把提问写进 mappings 的 rule。
没有对齐时 alignments 必须是空数组，不要省略该键；不要把对齐写进 mappings.sources。
四要素用 hypothesis/evidence/if_holds/ask_user；正文由你写。
mappings、questions、alignments 都可以是空数组；不要求等长。
mappings 的条数由块内实际需要输出的列决定；不要求凑数；但块内有多个不同数据列时，应各输出一条。
对齐槽的 locator 表示列键（供跨块按列分组），不是单格。
"""

_MATRIX_GUIDANCE = """【matrix 字段约束】
本包含 layout=matrix 的块。字段清单由本地几何草案给出，你只为每个 field_id 写 target.name。
硬约束：
1. 每个草案 field_id 恰好一条 mapping；sources 必须含 field_id，locator 写成 field:<field_id>。
2. 禁止把单元格原文（库名、店名、人名、段标题、组标签）直接当作 target.name。
3. 同一 subtable_id 内 target.name 必须互不相同。角色在多个组里重复时，用组标签作前缀区分（例如 group_label + 角色），不要复用同一个名字。
4. 禁止发明草案外的 field_id；禁止改 segments/col_range。
5. 草案里每一个 field_id 都要出现，一条都不能少。
6. questions/alignments 仍可为数组；有疑问再问。
"""

_MATRIX_SHAPE = """matrix 时只输出一个 JSON：
{"mappings":[{"sources":[{"block_id":<id>,"field_id":"f0","locator":"field:f0"}],
"target":{"name":<语义字段名>}}],
"questions":[],"alignments":[],
"subtable_0_dimensions":["f3"],
"subtable_1_dimensions":["f4"]}
条数必须等于草案字段数；每个 field_id 出现一次。
维度键 subtable_<id>_dimensions：从该子表 dimension_candidates 选 ≥1 个。
只有一个 list 时必须包含该 field_id，且总数 ≤2。
有多个 list 时每个 list 的 field_id 都要放进维度（可以超过 2），不要把 group/attribute/column 全放进去。
"""

_USER_LABEL = (
    "【用户原话（只说明要完成的任务；不是输出列名的来源，也不要改写成 target.name）】"
)


def block_cell_blob(block, tables_by_id, spec: SerializeSpec = None) -> str:
    spec = spec or SerializeSpec()
    parts = []
    for span in block.get("parent_spans") or [
        {
            "table_id": (block.get("parent_table_ids") or [None])[0],
            "row_span": block.get("row_span"),
            "col_span": block.get("col_span"),
            "repeat_header": False,
        }
    ]:
        tid = span.get("table_id")
        table = tables_by_id.get(tid) or {}
        grid = table.get("grid") or table.get("rows") or []
        r0, r1 = span["row_span"]
        c0, c1 = span["col_span"]
        rows = []
        if span.get("repeat_header") and grid:
            rows.append([grid[0][c] if c < len(grid[0]) else "" for c in range(c0, c1)])
        for r in range(r0, r1):
            src = grid[r] if r < len(grid) else []
            rows.append([src[c] if c < len(src) else "" for c in range(c0, c1)])
        parts.append(serialize_rows(rows, spec.cell_sep, spec.row_sep))
    return (spec.row_sep + spec.row_sep).join(parts)


def context_blob(block) -> str:
    ctx = block.get("context") or {}
    lines = []
    for m in ctx.get("merges") or []:
        lines.append(
            "merge %s %s %s %s %s"
            % (m.get("table_id"), m.get("r0"), m.get("c0"), m.get("r1"), m.get("c1"))
        )
    for f in ctx.get("fills") or []:
        lines.append("fill %s %s %s %s" % (f.get("table_id"), f.get("r"), f.get("c"), f.get("value")))
    for c in ctx.get("comments") or []:
        lines.append(
            "comment %s %s %s %s" % (c.get("table_id"), c.get("r"), c.get("c"), c.get("text"))
        )
    return "\n".join(lines)


def build_prompt(user_input, blocks, tables_by_id, *, reply_text=None,
                 prior_mappings=None, bridge_readonly=None, open_questions=None,
                 matrix_section=None):
    """按 2b 设计材料顺序拼装。返回 (prompt_text, parts_tokens)。"""
    sections = []
    # β：标注用户原话不是 target.name 来源
    sections.append(_USER_LABEL + "\n" + (user_input or ""))
    if reply_text is not None:
        sections.append("【用户回复原文】\n" + (reply_text or ""))
        if prior_mappings is not None:
            sections.append("【上次点名范围内的映射原文】\n" + _jsonish(prior_mappings))
        if bridge_readonly is not None:
            sections.append(
                "【未点名已过桥清单（只读，不是新答案）】\n" + _jsonish(bridge_readonly)
            )
        if open_questions is not None:
            sections.append("【仍开放的疑问原文】\n" + _jsonish(open_questions))
    for block in blocks:
        blob = block_cell_blob(block, tables_by_id)
        ctx = context_blob(block)
        head = (
            "【块】block_id=%s layout=%s axis=%s row_span=%s col_span=%s"
            % (
                block.get("block_id"),
                block.get("layout"),
                block.get("axis"),
                block.get("row_span"),
                block.get("col_span"),
            )
        )
        body = head + "\n" + blob
        if ctx:
            body += "\n【context】\n" + ctx
        sections.append(body)
    if matrix_section:
        sections.append(matrix_section)
        sections.append(_MATRIX_GUIDANCE.strip())
        sections.append("【输出形状】\n" + _MATRIX_SHAPE)
        template = _MATRIX_GUIDANCE + _MATRIX_SHAPE
    else:
        # α：生成指导（块材料之后、输出形状之前）
        sections.append(_GUIDANCE.strip())
        sections.append("【输出形状】\n" + _SHAPE)
        template = _GUIDANCE + _SHAPE
    prompt = "\n\n".join(sections)
    parts = {
        "template_tokens": estimate_tokens(template),
        "user_tokens": estimate_tokens(user_input or ""),
        "reply_tokens": estimate_tokens(reply_text or "") if reply_text is not None else 0,
        "blocks_tokens": sum(
            estimate_tokens(block_cell_blob(b, tables_by_id) + "\n" + context_blob(b))
            for b in blocks
        ),
        "prompt_tokens": estimate_tokens(prompt),
    }
    return prompt, parts


def _jsonish(obj):
    import json

    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(obj)
