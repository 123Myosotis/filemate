"""run_bridge：按阶段 3a 把 2b 的 mappings / alignments 解成可执行项。

对齐只读 2b 返回的 alignments（parse 包装或原文槽位），不从 sources 推断。
主流程先 resolve_alignments，再执行 mappings。
"""

import copy
import json
import re

from ai.context_budget import estimate_tokens


class BridgeError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


_RE_RC = re.compile(r"^R(\d+)C(\d+)$", re.IGNORECASE)
_RE_A1 = re.compile(r"^([A-Za-z]+)(\d+)$")
_RE_MERGE = re.compile(r"^merge:(\d+),(\d+),(\d+),(\d+)$", re.IGNORECASE)


def _spans(block):
    spans = block.get("parent_spans")
    if spans:
        return spans
    tids = block.get("parent_table_ids") or [None]
    return [
        {
            "table_id": tids[0],
            "row_span": list(block.get("row_span") or [0, 0]),
            "col_span": list(block.get("col_span") or [0, 0]),
            "repeat_header": False,
        }
    ]


def _grid(tables_by_id, table_id):
    table = (tables_by_id or {}).get(table_id) or {}
    return table.get("grid") or table.get("rows") or []


def _abs_cell(grid, r, c):
    if r < 0 or c < 0 or r >= len(grid):
        return ""
    row = grid[r] if isinstance(grid[r], (list, tuple)) else []
    if c >= len(row):
        return ""
    val = row[c]
    return "" if val is None else str(val)


def _span_contains(span, r, c):
    r0, r1 = span["row_span"]
    c0, c1 = span["col_span"]
    return r0 <= r < r1 and c0 <= c < c1


def _find_span(block, r, c):
    for span in _spans(block):
        if _span_contains(span, r, c):
            return span
    return None


def _col_letters(text):
    n = 0
    for ch in text:
        n = n * 26 + (ord(ch.upper()) - ord("A") + 1)
    return n - 1


def _header_abs_row(block, span):
    """ordered 子网格第 0 行的源表行号。matrix 不适用。"""
    if block.get("layout") != "ordered":
        return None
    if span.get("repeat_header"):
        return 0
    r0, _r1 = span["row_span"]
    return r0


def _iter_rect_cells(block, tables_by_id):
    for span in _spans(block):
        grid = _grid(tables_by_id, span.get("table_id"))
        r0, r1 = span["row_span"]
        c0, c1 = span["col_span"]
        for r in range(r0, r1):
            for c in range(c0, c1):
                yield span, r, c, _abs_cell(grid, r, c)


def _data_rows(block):
    """数据行：ordered 且表头落在块矩形内时跳过表头行。"""
    rows = []
    for span in _spans(block):
        r0, r1 = span["row_span"]
        header = _header_abs_row(block, span)
        for r in range(r0, r1):
            if header is not None and r == header:
                continue
            rows.append((span, r))
    return rows


def resolve_locator(block_id, locator, block_plan, tables_by_id, field_draft=None, source=None):
    """§4.0 固定次序。坐标 R{row}C{col} 为源表 0-based；A1 为 1-based 对外。"""
    blocks = {
        bl.get("block_id"): bl for bl in (block_plan or {}).get("blocks") or []
    }
    block = blocks.get(block_id)
    if block is None:
        return {"ok": False, "reason": "unknown_block_id"}

    text = str(locator if locator is not None else "").strip()
    fid = None
    if isinstance(source, dict) and source.get("field_id"):
        fid = str(source.get("field_id"))
    if text.startswith("field:"):
        fid = text.split(":", 1)[1].strip() or fid
    if fid or (block.get("layout") == "matrix" and isinstance(source, dict)):
        if block.get("layout") == "matrix" and not fid:
            return {"ok": False, "reason": "field_id_required"}
        if fid and block.get("layout") != "matrix":
            return {"ok": False, "reason": "column_range_invalid"}
        if fid:
            draft_fields = (field_draft or {}).get("fields") or []
            # field_draft may be {block_id: draft} or a single draft
            if isinstance(field_draft, dict) and "fields" not in field_draft:
                draft_fields = []
                for d in field_draft.values():
                    if isinstance(d, dict):
                        draft_fields.extend(d.get("fields") or [])
            found_f = None
            for f in draft_fields:
                if str(f.get("field_id")) == fid and f.get("block_id") in (None, block_id):
                    found_f = f
                    break
            if found_f is None:
                return {"ok": False, "reason": "column_range_invalid"}
            if isinstance(source, dict) and source.get("field_id") and str(source.get("field_id")) != fid:
                return {"ok": False, "reason": "column_range_invalid"}
            if isinstance(source, dict) and source.get("col_range") and source.get("col_range") != (found_f.get("segments") or [{}])[0].get("col_range"):
                return {"ok": False, "reason": "column_range_invalid"}
            seg0 = (found_f.get("segments") or [{}])[0]
            vrows = seg0.get("value_rows") or []
            return {
                "ok": True,
                "kind": "field",
                "block_id": block_id,
                "row": vrows[0] if vrows else found_f.get("row"),
                "col": seg0.get("col_range", [None])[0],
                "cell_text": (found_f.get("value_samples") or [""])[0] if found_f.get("value_samples") else "",
                "locator": "field:%s" % fid,
                "field_id": fid,
                "role": found_f.get("role"),
                "attribute_slot": found_f.get("attribute_slot"),
                "subtable_id": found_f.get("subtable_id"),
                "segments": found_f.get("segments"),
            }

    text = str(locator if locator is not None else "").strip()

    m = _RE_RC.match(text)
    if m:
        r, c = int(m.group(1)), int(m.group(2))
        span = _find_span(block, r, c)
        if span is None:
            return {"ok": False, "reason": "locator_not_on_grid"}
        grid = _grid(tables_by_id, span.get("table_id"))
        return {
            "ok": True,
            "kind": "cell",
            "block_id": block_id,
            "row": r,
            "col": c,
            "cell_text": _abs_cell(grid, r, c),
            "locator": locator,
        }

    m = _RE_A1.match(text)
    if m:
        c = _col_letters(m.group(1))
        r = int(m.group(2)) - 1
        span = _find_span(block, r, c)
        if span is None:
            return {"ok": False, "reason": "locator_not_on_grid"}
        grid = _grid(tables_by_id, span.get("table_id"))
        return {
            "ok": True,
            "kind": "cell",
            "block_id": block_id,
            "row": r,
            "col": c,
            "cell_text": _abs_cell(grid, r, c),
            "locator": locator,
        }

    m = _RE_MERGE.match(text)
    merge_box = None
    if m:
        merge_box = tuple(int(m.group(i)) for i in range(1, 5))
    else:
        ctx = block.get("context") or {}
        for item in ctx.get("merges") or []:
            if not isinstance(item, dict):
                continue
            box = (
                int(item.get("r0", -1)),
                int(item.get("c0", -1)),
                int(item.get("r1", -1)),
                int(item.get("c1", -1)),
            )
            serial = "%s,%s,%s,%s" % box
            if text == serial or text == "merge:" + serial:
                merge_box = box
                break
    if merge_box is not None or (m is not None):
        if merge_box is None:
            return {"ok": False, "reason": "locator_not_on_grid"}
        mr0, mc0, mr1, mc1 = merge_box
        for span in _spans(block):
            r0, r1 = span["row_span"]
            c0, c1 = span["col_span"]
            if r0 >= mr1 or r1 <= mr0 or c0 >= mc1 or c1 <= mc0:
                continue
            ar = max(mr0, r0)
            ac = max(mc0, c0)
            if ar >= r1 or ac >= c1:
                continue
            grid = _grid(tables_by_id, span.get("table_id"))
            return {
                "ok": True,
                "kind": "cell",
                "block_id": block_id,
                "row": ar,
                "col": ac,
                "cell_text": _abs_cell(grid, ar, ac),
                "locator": locator,
            }
        return {"ok": False, "reason": "locator_not_on_grid"}

    if block.get("layout") == "ordered":
        hits = []
        for span in _spans(block):
            header_r = _header_abs_row(block, span)
            if header_r is None:
                continue
            grid = _grid(tables_by_id, span.get("table_id"))
            c0, c1 = span["col_span"]
            for c in range(c0, c1):
                if _abs_cell(grid, header_r, c) == text:
                    hits.append((span, header_r, c))
        if len(hits) > 1:
            return {"ok": False, "reason": "locator_ambiguous"}
        if len(hits) == 1:
            span, hr, c = hits[0]
            grid = _grid(tables_by_id, span.get("table_id"))
            return {
                "ok": True,
                "kind": "column",
                "block_id": block_id,
                "row": hr,
                "col": c,
                "col_span": [c, c + 1],
                "cell_text": _abs_cell(grid, hr, c),
                "locator": locator,
            }

    hits = []
    for span, r, c, cell in _iter_rect_cells(block, tables_by_id):
        if cell == text:
            hits.append((span, r, c, cell))
    if len(hits) > 1:
        return {"ok": False, "reason": "locator_ambiguous"}
    if len(hits) == 1:
        _span, r, c, cell = hits[0]
        return {
            "ok": True,
            "kind": "cell",
            "block_id": block_id,
            "row": r,
            "col": c,
            "cell_text": cell,
            "locator": locator,
        }
    return {"ok": False, "reason": "locator_not_on_grid"}


def resolve_align_slot(block_id, locator, block_plan, tables_by_id):
    """§4.0.1 选 A：必须是列。单格整段拒绝，不回退到所在列。"""
    found = resolve_locator(block_id, locator, block_plan, tables_by_id)
    if not found.get("ok"):
        return {"ok": False, "reason": "align_slot_unresolved"}
    if found.get("kind") == "column":
        return {
            "ok": True,
            "block_id": block_id,
            "col": found["col"],
            "col_span": list(found.get("col_span") or [found["col"], found["col"] + 1]),
            "locator": locator,
            "key_column": True,
        }
    return {"ok": False, "reason": "align_slot_not_column"}


def _alignment_items(analysis):
    """读 2b 实际返回：{raw, executable, reason} 或模型原文槽位对象。"""
    if not isinstance(analysis, dict):
        raise BridgeError("bridge_input_invalid", "analysis is not an object")
    if "alignments" not in analysis or analysis.get("alignments") is None:
        return []
    raw = analysis.get("alignments")
    if not isinstance(raw, list):
        raise BridgeError(
            "analysis_alignments_not_array",
            "alignments present but not array",
        )
    items = []
    for item in raw:
        if isinstance(item, dict) and "raw" in item and "executable" in item:
            items.append(item)
        else:
            items.append({"raw": item, "executable": True, "reason": None})
    return items


def resolve_alignments(alignments_wrapped, block_plan, tables_by_id):
    resolved = []
    rejected = []
    for i, item in enumerate(alignments_wrapped):
        raw = item.get("raw") if isinstance(item, dict) else item
        align_id = None
        if isinstance(raw, dict) and raw.get("align_id") is not None:
            align_id = raw.get("align_id")
        else:
            align_id = str(i)
        if isinstance(item, dict) and item.get("executable") is False:
            rejected.append(
                {
                    "align_id": align_id,
                    "path": "slots",
                    "reason": item.get("reason") or "align_slot_unresolved",
                }
            )
            continue
        slots = raw.get("slots") if isinstance(raw, dict) else None
        if not isinstance(slots, list) or len(slots) < 2:
            rejected.append(
                {"align_id": align_id, "path": "slots", "reason": "align_too_few_slots"}
            )
            continue
        slot_resolved = []
        failed = None
        for j, slot in enumerate(slots):
            if not isinstance(slot, dict):
                failed = {
                    "align_id": align_id,
                    "path": "slots[%s]" % j,
                    "reason": "align_slot_unresolved",
                }
                break
            got = resolve_align_slot(
                slot.get("block_id"),
                slot.get("locator"),
                block_plan,
                tables_by_id,
            )
            if not got.get("ok"):
                failed = {
                    "align_id": align_id,
                    "path": "slots[%s].locator" % j,
                    "reason": got.get("reason") or "align_slot_unresolved",
                }
                break
            slot_resolved.append(
                {
                    "block_id": got["block_id"],
                    "col": got["col"],
                    "col_span": got["col_span"],
                    "locator": got["locator"],
                    "key_column": True,
                }
            )
        if failed:
            rejected.append(failed)
        else:
            resolved.append({"align_id": align_id, "slots_resolved": slot_resolved})
    return resolved, rejected


def classify_primitive(rule):
    if rule is None or rule == {}:
        return "copy", None
    if not isinstance(rule, dict):
        return None, "action_not_in_primitives"
    has_replace = "table" in rule and isinstance(rule.get("table"), list)
    has_gate = "literals" in rule and isinstance(rule.get("literals"), list)
    op = rule.get("op") if isinstance(rule.get("op"), str) else None
    if op is None:
        if has_replace and not has_gate:
            return "replace", None
        if has_gate and not has_replace:
            return "gate", None
        if not has_replace and not has_gate:
            return "copy", None
        return None, "action_not_in_primitives"
    if op == "copy":
        if has_replace or has_gate:
            return None, "action_not_in_primitives"
        return "copy", None
    if op == "replace":
        if not has_replace:
            return None, "action_not_in_primitives"
        return "replace", None
    if op == "gate":
        if not has_gate:
            return None, "action_not_in_primitives"
        return "gate", None
    return None, "action_not_in_primitives"


def _serialize(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _budget_fail(prim, rule, remaining):
    if prim == "replace":
        table = rule.get("table")
        if isinstance(table, list) and estimate_tokens(_serialize(table)) > int(remaining):
            return "replace_table_exceeds_budget"
    if prim == "gate":
        literals = rule.get("literals")
        if isinstance(literals, list) and estimate_tokens(_serialize(literals)) > int(remaining):
            return "gate_literals_exceeds_budget"
    return None


def _source_view(found, locator, block_id):
    return {
        "block_id": block_id,
        "locator": locator,
        "row": found.get("row"),
        "col": found.get("col"),
        "cell_text": found.get("cell_text"),
        "kind": found.get("kind"),
        "col_span": found.get("col_span"),
    }


def _eval_copy(sources):
    text = "" if not sources else str(sources[0].get("cell_text") or "")
    if text.strip() == "":
        return ""
    return text


def _eval_replace(sources, rule):
    text = "" if not sources else str(sources[0].get("cell_text") if sources[0].get("cell_text") is not None else "")
    for row in rule.get("table") or []:
        if not isinstance(row, dict) or "from" not in row or "to" not in row:
            return None, "action_not_in_primitives"
        if text == str(row.get("from")):
            return "" if row.get("to") is None else str(row.get("to")), None
    return "", None


def _literal_hit(text, literals):
    return str(text) in literals


def _eval_gate_cell(text, literals):
    if _literal_hit(text, literals):
        return str(text)
    return ""


def _eval_gate(sources, rule, alignments_resolved, block_plan, tables_by_id):
    literals = [str(x) for x in (rule.get("literals") or [])]
    if not sources:
        return ""
    primary = sources[0]
    blocks = {
        bl.get("block_id"): bl for bl in (block_plan or {}).get("blocks") or []
    }
    align = None
    for item in alignments_resolved or []:
        slots = item.get("slots_resolved") or []
        if any(s.get("block_id") == primary.get("block_id") for s in slots):
            align = item
            break
    # 无对齐，或主源不是列：只门控当前 sources[0]
    if align is None or primary.get("kind") != "column":
        return _eval_gate_cell(primary.get("cell_text") or "", literals)

    slot_by_block = {
        s.get("block_id"): s for s in (align.get("slots_resolved") or [])
    }
    pslot = slot_by_block.get(primary.get("block_id"))
    pblock = blocks.get(primary.get("block_id"))
    if pslot is None or pblock is None:
        return _eval_gate_cell(primary.get("cell_text") or "", literals)

    written = []
    for span, row in _data_rows(pblock):
        grid = _grid(tables_by_id, span.get("table_id"))
        key = _abs_cell(grid, row, pslot["col"])
        ptext = _abs_cell(grid, row, primary["col"])
        texts = [ptext]
        for other in sources[1:]:
            oblock = blocks.get(other.get("block_id"))
            oslot = slot_by_block.get(other.get("block_id"))
            if other.get("kind") == "column" and oblock is not None and oslot is not None:
                for ospan, orow in _data_rows(oblock):
                    ogrid = _grid(tables_by_id, ospan.get("table_id"))
                    if _abs_cell(ogrid, orow, oslot["col"]) == key:
                        texts.append(_abs_cell(ogrid, orow, other["col"]))
            else:
                texts.append(str(other.get("cell_text") or ""))
        if any(_literal_hit(t, literals) for t in texts):
            written.append(ptext)
    return written


def _eval(prim, sources, rule, alignments_resolved, block_plan, tables_by_id):
    if prim == "copy":
        return _eval_copy(sources), None
    if prim == "replace":
        return _eval_replace(sources, rule)
    if prim == "gate":
        return _eval_gate(sources, rule, alignments_resolved, block_plan, tables_by_id), None
    return None, "action_not_in_primitives"


def _released_indexes(mappings, released):
    if not released:
        return []
    chosen = []
    seen = set()
    local_of = {}
    per = {}
    for gi, m in enumerate(mappings):
        if not isinstance(m, dict):
            continue
        for src in m.get("sources") or []:
            if not isinstance(src, dict):
                continue
            bid = src.get("block_id")
            local = per.get(bid, 0)
            per[bid] = local + 1
            local_of[gi] = (bid, local)
            break
    for rel in released:
        if not isinstance(rel, dict):
            continue
        if rel.get("global_index") is not None:
            gi = rel.get("global_index")
            if isinstance(gi, int) and 0 <= gi < len(mappings) and gi not in seen:
                chosen.append(gi)
                seen.add(gi)
            continue
        key = (rel.get("block_id"), rel.get("mapping_index"))
        for gi, pair in local_of.items():
            if pair == key and gi not in seen:
                chosen.append(gi)
                seen.add(gi)
                break
    chosen.sort()
    return chosen


def _try_chain(locator, written_by_target):
    key = str(locator if locator is not None else "")
    if key in written_by_target:
        return written_by_target[key]
    return None


def detect_conflicts(executable):
    groups = {}
    for item in executable:
        groups.setdefault(item.get("target_name"), []).append(item)
    conflicts = []
    for name, items in groups.items():
        fingerprints = []
        for item in items:
            fp = tuple(
                (s.get("block_id"), s.get("row"), s.get("col"))
                for s in item.get("sources_resolved") or []
            )
            fingerprints.append(fp)
        if len(set(fingerprints)) >= 2:
            conflicts.append(
                {
                    "target_name": name,
                    "mapping_indexes": [it.get("mapping_index") for it in items],
                    "sources_fingerprint": [list(fp) for fp in fingerprints],
                }
            )
    return conflicts


def _ai_raw_copy(analysis):
    if not isinstance(analysis, dict):
        return []
    if "ai_raws" in analysis:
        raws = analysis.get("ai_raws")
    elif "ai_raw" in analysis:
        raws = analysis.get("ai_raw")
    else:
        raws = []
    if isinstance(raws, dict):
        raws = [raws]
    if not isinstance(raws, list):
        raws = [raws]
    return copy.deepcopy(raws)


def run_bridge(bridge_input):
    """§8：先对齐，再映射。只执行 released 点名的条。"""
    if not isinstance(bridge_input, dict):
        raise BridgeError("bridge_input_invalid", "bridge_input is not an object")
    block_plan = bridge_input.get("block_plan") or {}
    analysis = bridge_input.get("analysis") or {}
    tables_by_id = bridge_input.get("tables_by_id") or bridge_input.get("tables") or {}
    runtime = bridge_input.get("runtime") or {}
    if "remaining_tokens" not in runtime:
        raise BridgeError("bridge_input_invalid", "runtime.remaining_tokens required")
    remaining = int(runtime.get("remaining_tokens"))
    released = bridge_input.get("released")
    if released is None:
        released = []
    if not isinstance(released, list):
        raise BridgeError("bridge_input_invalid", "released is not an array")

    mappings = analysis.get("mappings") if isinstance(analysis, dict) else None
    if mappings is None:
        mappings = []
    if not isinstance(mappings, list):
        raise BridgeError("analysis_missing_mappings", "mappings is not an array")

    align_items = _alignment_items(analysis)
    alignments_resolved, rejected = resolve_alignments(
        align_items, block_plan, tables_by_id
    )

    executable = []
    written_by_target = {}
    field_draft = bridge_input.get("matrix_draft") or analysis.get("matrix_draft")
    seen_fid = {}
    for gi, m in enumerate(mappings):
        if not isinstance(m, dict):
            continue
        for src in m.get("sources") or []:
            if isinstance(src, dict) and src.get("field_id"):
                seen_fid.setdefault(str(src.get("field_id")), []).append(gi)
    dup_indexes = set()
    for fid, gis in seen_fid.items():
        if len(gis) > 1:
            dup_indexes.update(gis)
    indexes = _released_indexes(mappings, released)
    indexes = [gi for gi in indexes if gi not in dup_indexes]
    for gi in sorted(dup_indexes):
        rejected.append(
            {"mapping_index": gi, "path": "sources", "reason": "duplicate_field_id"}
        )
    seen_name_sub = set()
    for gi in indexes:
        m = mappings[gi]
        if not isinstance(m, dict):
            rejected.append(
                {
                    "mapping_index": gi,
                    "path": "mapping",
                    "reason": "missing_sources",
                }
            )
            continue
        sources = m.get("sources")
        target = m.get("target") if isinstance(m.get("target"), dict) else {}
        name = str(target.get("name") or "").strip()
        if not isinstance(sources, list) or not sources:
            rejected.append(
                {"mapping_index": gi, "path": "sources", "reason": "missing_sources"}
            )
            continue
        if not name:
            rejected.append(
                {
                    "mapping_index": gi,
                    "path": "target.name",
                    "reason": "missing_target_name",
                }
            )
            continue
        rule = m.get("rule")
        prim, why = classify_primitive(rule)
        if why:
            rejected.append(
                {"mapping_index": gi, "path": "rule", "reason": why}
            )
            continue
        budget_reason = _budget_fail(prim, rule if isinstance(rule, dict) else {}, remaining)
        if budget_reason:
            rejected.append(
                {"mapping_index": gi, "path": "rule", "reason": budget_reason}
            )
            continue
        if prim == "replace":
            bad_row = False
            for row in (rule or {}).get("table") or []:
                if not isinstance(row, dict) or "from" not in row or "to" not in row:
                    bad_row = True
                    break
            if bad_row:
                rejected.append(
                    {
                        "mapping_index": gi,
                        "path": "rule.table",
                        "reason": "action_not_in_primitives",
                    }
                )
                continue

        resolved_sources = []
        failed = None
        name_key = None
        for k, src in enumerate(sources):
            if not isinstance(src, dict) or "block_id" not in src:
                failed = {
                    "mapping_index": gi,
                    "path": "sources[%s]" % k,
                    "reason": "missing_sources",
                }
                break
            found = resolve_locator(
                src.get("block_id"),
                src.get("locator"),
                block_plan,
                tables_by_id,
                field_draft=field_draft,
                source=src,
            )
            if not found.get("ok"):
                chained = _try_chain(src.get("locator"), written_by_target)
                if chained is None:
                    failed = {
                        "mapping_index": gi,
                        "path": "sources[%s].locator" % k,
                        "reason": found.get("reason") or "locator_not_on_grid",
                    }
                    break
                resolved_sources.append(
                    {
                        "block_id": src.get("block_id"),
                        "locator": src.get("locator"),
                        "row": chained.get("row"),
                        "col": chained.get("col"),
                        "cell_text": chained.get("value"),
                        "kind": "cell",
                        "chained_from": chained.get("target_name"),
                    }
                )
                continue
            view = _source_view(found, found.get("locator") or src.get("locator"), src.get("block_id"))
            for extra in ("field_id", "role", "attribute_slot", "subtable_id", "segments"):
                if found.get(extra) is not None:
                    view[extra] = found.get(extra)
            resolved_sources.append(view)
            if found.get("kind") == "field":
                name_key = (found.get("subtable_id"), name)
        if failed:
            rejected.append(failed)
            continue
        if name_key is not None:
            if name_key in seen_name_sub:
                rejected.append(
                    {
                        "mapping_index": gi,
                        "path": "target.name",
                        "reason": "duplicate_target_name_within_subtable",
                        "target_name": name,
                    }
                )
                continue
            seen_name_sub.add(name_key)

        value, verr = _eval(
            prim,
            resolved_sources,
            rule if isinstance(rule, dict) else {},
            alignments_resolved,
            block_plan,
            tables_by_id,
        )
        if verr:
            rejected.append({"mapping_index": gi, "path": "rule", "reason": verr})
            continue
        block_ids = []
        for src in resolved_sources:
            if src.get("block_id") not in block_ids:
                block_ids.append(src.get("block_id"))
        item = {
            "mapping_index": gi,
            "block_ids": block_ids,
            "sources_resolved": [
                {
                    "block_id": s.get("block_id"),
                    "locator": s.get("locator"),
                    "row": s.get("row"),
                    "col": s.get("col"),
                    "cell_text": s.get("cell_text"),
                    "kind": s.get("kind"),
                    "field_id": s.get("field_id"),
                    "role": s.get("role"),
                    "attribute_slot": s.get("attribute_slot"),
                    "subtable_id": s.get("subtable_id"),
                    "segments": s.get("segments"),
                }
                for s in resolved_sources
            ],
            "target_name": name,
            "rule": copy.deepcopy(rule) if isinstance(rule, dict) else rule,
            "primitive": prim,
            "value": value,
        }
        executable.append(item)
        written_by_target[name] = {
            "target_name": name,
            "value": value if not isinstance(value, list) else (value[-1] if value else ""),
            "row": resolved_sources[0].get("row"),
            "col": resolved_sources[0].get("col"),
        }

    return {
        "ai_raw": _ai_raw_copy(analysis),
        "executable": executable,
        "rejected": rejected,
        "duplicate_names": [
            r.get("target_name")
            for r in rejected
            if r.get("reason") == "duplicate_target_name_within_subtable"
        ],
        "conflicts": detect_conflicts(executable),
        "alignments_resolved": alignments_resolved,
    }
