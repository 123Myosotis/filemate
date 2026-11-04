# agent_core/validator.py
"""
独立外部验证器（硬门禁）——不依赖模型自评。

原则：
1. 用可执行规则判定成功/失败（输出格式、列约束、非空、行完整性、文件存在性）
2. 验证失败 → 原子回滚已写出的报告文件，并返回结构化错误供重跑
3. 模型复核意见只作辅助说明，不能单独放行 hard_gate
"""
import hashlib
import re
from pathlib import Path


def snapshot_delivery(ctx: dict) -> dict:
    """导出前拍快照，供回滚恢复。"""
    data = ctx.get("data") or {}
    return {
        "table_result": _clone_table(data.get("table_result")),
        "report_paths": list(data.get("report_paths") or []),
        "staged_paths": list(data.get("staged_paths") or []),
        "synth_plan": data.get("synth_plan"),
        "filter_notes": list(data.get("filter_notes") or []),
        "degrade_notes": list(data.get("degrade_notes") or []),
    }


def _clone_table(table):
    if not isinstance(table, dict):
        return None
    return {
        "name": table.get("name"),
        "columns": list(table.get("columns") or []),
        "rows": [dict(r) for r in (table.get("rows") or [])],
        "plan_notes": list(table.get("plan_notes") or []),
        "row_fingerprint": table.get("row_fingerprint"),
        "source_fingerprint": table.get("source_fingerprint"),
    }


def rollback_delivery(ctx: dict, snap: dict = None, delete_files: bool = True) -> list:
    """原子回滚：删除本轮写出的文件，恢复 data 快照中的交付字段。"""
    data = ctx.setdefault("data", {})
    deleted = []
    paths = set(data.get("staged_paths") or []) | set(data.get("report_paths") or [])
    if snap:
        old = set(snap.get("report_paths") or []) | set(snap.get("staged_paths") or [])
        paths = paths - old
    if delete_files:
        for p in list(paths):
            try:
                path = Path(p)
                if path.is_file():
                    path.unlink()
                    deleted.append(str(path))
            except OSError:
                pass
    if snap is not None:
        data["table_result"] = _clone_table(snap.get("table_result"))
        data["report_paths"] = list(snap.get("report_paths") or [])
        data["staged_paths"] = list(snap.get("staged_paths") or [])
        data["synth_plan"] = snap.get("synth_plan")
        data["filter_notes"] = list(snap.get("filter_notes") or [])
        data["degrade_notes"] = list(snap.get("degrade_notes") or [])
    else:
        data["table_result"] = None
        data["report_paths"] = []
        data["staged_paths"] = []
        data["synth_plan"] = None
    data["rollback_deleted"] = deleted
    return deleted


def validate_execution(user_input: str, intent: dict, ctx: dict) -> dict:
    """硬门禁验证。"""
    issues = []
    data = ctx.get("data") or {}
    fmt = str((intent or {}).get("output_format") or "").lower()
    table = data.get("table_result")
    reports = list(data.get("report_paths") or [])
    notes = list(data.get("filter_notes") or []) + list(data.get("degrade_notes") or [])

    if fmt in ("excel", "xlsx"):
        ok_files = [p for p in reports if str(p).lower().endswith((".xlsx", ".xls", ".csv"))]
        if not ok_files:
            issues.append(_iss("输出格式",
                               "用户要求导出 Excel/表格文件，但未生成可用的 xlsx/csv",
                               "high", "missing_excel"))
        elif not any(str(p).lower().endswith((".xlsx", ".xls")) for p in ok_files):
            if any("openpyxl" in str(n).lower() or "csv" in str(n).lower() for n in notes):
                issues.append(_iss("输出格式",
                                   "用户要求 Excel，但环境降级为 CSV（可打开，但格式不完全符合）",
                                   "medium", "excel_degraded_csv"))
    elif fmt == "csv":
        if not any(str(p).lower().endswith(".csv") for p in reports):
            issues.append(_iss("输出格式", "用户要求 CSV，但未生成 .csv 文件",
                               "high", "missing_csv"))
    elif fmt == "word":
        if not any(str(p).lower().endswith(".docx") for p in reports):
            issues.append(_iss("输出格式", "用户要求 Word，但未生成 .docx 文件",
                               "high", "missing_word"))
    elif fmt == "ppt":
        issues.append(_iss("输出格式", "暂不支持 PPT，需要改为 word/excel/csv 之一",
                           "high", "unsupported_ppt"))

    if fmt in ("excel", "xlsx", "csv") or (table and _mentions_table(user_input)):
        if not table or not table.get("columns"):
            issues.append(_iss("需求符合度", "没有可交付的表格结果",
                               "high", "empty_table"))
        else:
            cols = table.get("columns") or []
            rows = table.get("rows") or []
            if not rows:
                issues.append(_iss("需求符合度", "表格列已生成但行数为 0，可能筛选过严或映射失败",
                                   "high", "zero_rows"))
            # 语义门禁（空列/常值/人名形态/列互含）已删除。
            # 本地只保留物理事实：有列、行数、指纹。准确性由 AI 复核裁定。
            requested = _extract_requested_columns(user_input)
            missing = [c for c in requested if not _col_present(cols, c)]
            if missing:
                issues.append(_iss("需求符合度",
                                   f"用户要求的列缺失：{'、'.join(missing)}；实际列：{'、'.join(cols)}",
                                   "medium", "missing_columns"))
            fp = table.get("integrity")
            if isinstance(fp, dict):
                if fp.get("lost_rows"):
                    issues.append(_iss("完整性校验",
                                       f"处理过程丢失 {fp['lost_rows']} 行源数据",
                                       "high", "row_loss"))
                if fp.get("checksum_mismatch"):
                    issues.append(_iss("完整性校验",
                                       "分块重组后校验和不匹配，数据可能失真",
                                       "high", "checksum_mismatch"))

    structure_issues = data.get("structure_issues") or []
    for si in structure_issues:
        if not isinstance(si, dict):
            continue
        detail = "；".join(str(x) for x in (si.get("issues") or []))
        if detail:
            issues.append(_iss("完整性校验",
                               "表「%s」结构自洽校验可疑：%s" % (si.get("table"), detail),
                               "medium", "structure_inconsistent"))

    wash_rejected = data.get("wash_rejected") or []
    for w in wash_rejected:
        if not isinstance(w, dict):
            continue
        issues.append(_iss("完整性校验",
                           "表「%s」清洗后变空，已保留原表（疑似清洗失败，未采用）"
                           % w.get("table"),
                           "medium", "wash_rejected"))

    for p in reports:
        if not Path(p).is_file():
            issues.append(_iss("格式完整性", f"报告路径不存在或不是文件：{p}",
                               "high", "file_missing"))
        elif Path(p).stat().st_size <= 0:
            issues.append(_iss("格式完整性", f"报告文件为空：{p}",
                               "high", "file_empty"))

    moved = len(ctx.get("operations") or [])
    if (not reports and not table and not moved
            and not (data.get("unified") or data.get("answer_text") or data.get("analysis"))):
        issues.append(_iss("需求符合度", "本次没有任何有效产出",
                           "high", "no_output"))

    high = [i for i in issues if i["severity"] == "high"]
    medium = [i for i in issues if i["severity"] == "medium"]
    passed = len(high) == 0 and len(medium) == 0
    if high:
        action = "retry"
    elif medium:
        action = "degrade"
    else:
        action = "pass"

    import config
    if config.get_param("review.hard_gate") and high:
        action = "retry"
        passed = False

    dims = list(dict.fromkeys(i["dimension"] for i in issues))
    return {
        "passed": passed and action == "pass",
        "failed_dimensions": dims,
        "issues": issues,
        "action": action,
        "evidence": {
            "output_format": fmt,
            "report_paths": reports,
            "table_columns": (table or {}).get("columns"),
            "table_rows": len((table or {}).get("rows") or []),
        },
    }


def table_fingerprint(table: dict) -> str:
    h = hashlib.sha256()
    cols = table.get("columns") or []
    h.update("|".join(map(str, cols)).encode("utf-8", errors="replace"))
    for r in table.get("rows") or []:
        line = "\t".join(str(r.get(c, "")) for c in cols)
        h.update(line.encode("utf-8", errors="replace"))
        h.update(b"\n")
    return h.hexdigest()


def _iss(dimension, description, severity, code):
    return {
        "dimension": dimension,
        "description": description,
        "severity": severity,
        "code": code,
    }


def _mentions_table(text: str) -> bool:
    t = text or ""
    keys = ("表格", "excel", "csv", "xlsx", "列出行", "导出表", "合成表")
    return any(k.lower() in t.lower() for k in keys)


def _path_segments(user_input: str) -> set:
    """从指令里抽出文件系统路径的各段，这些绝不是输出列名。"""
    segs = set()
    raw = user_input or ""
    for m in re.finditer(r"[A-Za-z]:\\[^\s，,。；;\"']+", raw):
        for part in re.split(r"[\\/]+", m.group(0)):
            part = part.strip()
            if part:
                segs.add(part)
                # 去掉「目录下的…」粘连在路径尾上的口语
                segs.add(re.split(r"目录", part, maxsplit=1)[0] or part)
    for m in re.finditer(r"(?:^|\s)((?:/[\w.\u4e00-\u9fff-]+){2,})\S*", raw):
        for part in re.split(r"[\\/]+", m.group(1)):
            if part:
                segs.add(part)
    return {s for s in segs if s}


def _clean_column_label(c: str) -> str:
    """去掉口语前缀/尾巴，不维护行业词表。"""
    c = str(c or "").strip()
    c = re.sub(r"^(?:他的|他们的|相关的|有关的|对应的|哪个|什么)", "", c)
    c = re.sub(r"(?:我也要知道|我也要|我要知道|是谁|一下|之类)$", "", c)
    c = re.sub(r"(属于|对应)$", "", c)
    return c.strip(" ：:，,。；;的")


def _clip_list_label(p: str) -> str:
    """列清单第一段常粘着「整理出/列出」前面的原话，只留动词后面的列名。"""
    p = (p or "").strip(" ：:，,")
    m = re.search(r"(?:整理出|列出|输出|导出|包括)(.+)$", p)
    if m and len(m.group(1).strip()) >= 2:
        return m.group(1).strip()
    return p


def _extract_requested_columns(user_input: str) -> list:
    """
    从用户语义提取显式列名。
    优先匹配：列出行A\\B\\C  /  列：A、B、C；路径段一律丢弃。
    括号里的「只要…(A\\B\\C)」是筛选类别，不是输出列。
    """
    raw = user_input or ""
    ban = _path_segments(raw)
    text = re.sub(r"[A-Za-z]:\\[^\s，,。；;\"']+", " ", raw)
    text = re.sub(r"(?:^|\s)(?:/[\w.\u4e00-\u9fff-]+){2,}\S*", " ", text)
    # 去掉括号内的类别枚举，避免把管理类别误当成输出列
    text = re.sub(r"[（(][^）)]{0,200}[）)]", " ", text)
    cols = []

    # 1) 优先：整理出/列出/列出行 后的清单
    m = re.search(
        r"(?:准确)?列出行\s*([^\n。；;]{4,120})"
        r"|(?:请)?(?:整理出|列出|输出|导出)\s*([^\n。；;]{4,120})",
        text)
    if m:
        chunk = (m.group(1) or m.group(2) or "").strip().lstrip("：:")
        chunk = re.split(r"[，,]", chunk, maxsplit=1)[0].strip()
        if "\\" in chunk or "/" in chunk or "、" in chunk:
            for p in re.split(r"[\\/、|]+", chunk):
                p = _clip_list_label(p)
                if 1 < len(p) <= 16 and not re.search(
                        r"表格|文件|excel|csv|xlsx|本地|导出|只要|^统计$", p, re.I):
                    cols.append(p)

    # 2) 反斜杠列清单（已去掉括号类别）
    if not cols:
        m2 = re.search(
            r"([\u4e00-\u9fffA-Za-z0-9]{2,16}(?:\\[\u4e00-\u9fffA-Za-z0-9]{2,16}){2,})",
            text)
        if m2:
            for p in m2.group(1).split("\\"):
                p = _clip_list_label(p)
                if 1 < len(p) <= 16:
                    cols.append(p)

    # 3) 「字段/列：A、B、C」
    if not cols:
        m3 = re.search(r"(?:字段|列)\s*[:：]\s*([^\n。；;]+)", text)
        if m3:
            for p in re.split(r"[、，,/|]+", m3.group(1)):
                p = p.strip()
                if 1 < len(p) <= 16:
                    cols.append(p)

    # 4) 自然语言句式（结构词，非行业词表）
    if not cols:
        for m in re.finditer(
                r"对应的([\u4e00-\u9fffA-Za-z0-9]{2,12}?)(?=属于|、|，|,|是|以及|和|$)",
                text):
            cols.append(m.group(1))
        for m in re.finditer(r"属于什么([\u4e00-\u9fffA-Za-z0-9]{2,16})", text):
            cols.append(m.group(1))
        for m in re.finditer(r"是否有(?:相关的)?([\u4e00-\u9fffA-Za-z0-9]{2,24})", text):
            cols.append("是否有" + m.group(1))
        for m in re.finditer(r"哪个([\u4e00-\u9fffA-Za-z0-9]{2,16})", text):
            cols.append(m.group(1))
        for m in re.finditer(
                r"(?:他的|他们的)?([\u4e00-\u9fff]{2,8})是谁", text):
            cols.append(m.group(1))
        for m in re.finditer(
                r"哪些([\u4e00-\u9fffA-Za-z0-9]{2,6})我也要", text):
            cols.append(m.group(1))
        for m in re.finditer(
                r"(?:的|要|知|看|查)([\u4e00-\u9fffA-Za-z0-9]{2,8}原因)", text):
            cols.append(m.group(1))

    out, seen = [], set()
    for c in cols:
        c = _clean_column_label(c)
        if not c or c in seen or len(c) > 24:
            continue
        if c in ban or any(c == b or (len(c) >= 4 and c in b) for b in ban):
            continue
        # 纯 ASCII 短标识（路径残留）不要
        if re.fullmatch(r"[A-Za-z0-9_.-]+", c) and not re.search(r"[\u4e00-\u9fff]", c):
            continue
        seen.add(c)
        out.append(c)
    # 用户用 \、、 显式点名的列全部保留（区域 与 区域经理 可同时存在）
    # 仅对自然语言抽到的粘连名做互含子串折叠
    explicit = bool(re.search(
        r"(?:整理出|列出|输出|导出|列出行).{0,80}[\\/、|]",
        text) or re.search(
        r"[\u4e00-\u9fffA-Za-z0-9]{2,16}(?:\\[\u4e00-\u9fffA-Za-z0-9]{2,16}){2,}",
        text))
    if explicit:
        return out[:12]
    cleaned = []
    for c in out:
        if any(c != o and c in o for o in out):
            continue
        cleaned.append(c)
    return cleaned[:12]


def _name_overlap(a: str, b: str) -> bool:
    """两列名/点名片段是否语义重叠（互含/去虚词/子序列），无业务词表。"""
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    if a in b or b in a:
        return True

    def _core(s):
        s = re.sub(r"^(?:是否有|是否|有无|有|没有|未|无|非)", "", s)
        s = re.sub(r"^(?:相关的|对应的|他的|他们的)", "", s)
        s = re.sub(r"(?:记录|情况|状态)$", "", s)
        return s

    def _subseq(short, long):
        if not short or not long or len(short) > len(long):
            return False
        it = iter(long)
        return all(ch in it for ch in short)

    ca, cb = _core(a), _core(b)
    if not ca or not cb:
        return False
    if ca in cb or cb in ca:
        return True
    if len(ca) <= len(cb) and _subseq(ca, cb):
        return True
    if len(cb) <= len(ca) and _subseq(cb, ca):
        return True
    parts_a = set(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{2,}", ca))
    parts_b = set(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{2,}", cb))
    return bool(parts_a & parts_b)


def _col_present(columns: list, name: str) -> bool:
    name = (name or "").strip()
    if not name:
        return True
    if name in columns:
        return True
    lower = {c.lower(): c for c in columns}
    if name.lower() in lower:
        return True
    # 归一化（NFKC 宽度折叠 + casefold）后再比对，避免全半角/大小写差异误判缺失
    from pipeline.stage3_processor.semantic_match import normalize_text
    n = normalize_text(name)
    if n and any(normalize_text(c) == n for c in columns):
        return True
    return any(name in c or c in name or _name_overlap(name, c) for c in columns)
