# -*- coding: utf-8 -*-
"""A 级文件整理：契约规则表，0 token。

接入层可以规则判定「只动文件位置」的指令；禁止用本模块猜测
table_query / word_office / 内容整理。命中 veto → 交给模型（或无 Key 时明示）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class LocalMatch:
    """status: hit=可本地执行；veto=像别的工具，禁止整理；miss=不像整理。"""
    status: str
    reason: str = ""
    rule_id: str = ""
    kind: str = ""
    intent: dict | None = None
    actions: list = field(default_factory=list)


def _section() -> dict:
    from pipeline.synth_contract.registry import load_contract
    data = load_contract().data.get("file_organize") or {}
    return data if isinstance(data, dict) else {}


def default_ext_map() -> dict:
    raw = _section().get("default_ext_map") or {}
    out = {}
    for ext, folder in raw.items():
        e = str(ext).lower()
        if not e.startswith("."):
            e = "." + e
        out[e] = str(folder)
    return out


def keyword_map() -> dict:
    raw = _section().get("keyword_map") or {}
    return {str(k): str(v) for k, v in raw.items() if str(k).strip() and str(v).strip()}


def date_format() -> str:
    return str(_section().get("date_format") or "%Y/%m")


def _compile_rules(items) -> list:
    out = []
    for r in items or []:
        if not isinstance(r, dict):
            continue
        pat = str(r.get("pattern") or "")
        if not pat:
            continue
        try:
            cre = re.compile(pat)
        except re.error:
            continue
        out.append((int(r.get("priority") or 0), r, cre))
    out.sort(key=lambda x: -x[0])
    return out


def _actions_for_op(op: str, rule: dict, match: re.Match, section: dict) -> list | None:
    params = dict(rule.get("params") or {})
    if op == "organize_by_ext":
        ext_map = dict(params.get("ext_map") or default_ext_map())
        if not ext_map:
            return None
        return [{"domain": "file", "op": "organize_by_ext", "params": {"ext_map": ext_map}}]
    if op == "move_keyword":
        kw = str(params.get("keyword") or "").strip()
        folder = str(params.get("folder") or "").strip()
        if not kw:
            return None
        if not folder:
            folder = keyword_map().get(kw) or "归档"
        return [{"domain": "file", "op": "move_keyword",
                 "params": {"keyword": kw, "folder": folder}}]
    if op == "move_date":
        return [{"domain": "file", "op": "move_date", "params": {}}]
    if op == "move_recent":
        days = params.get("days")
        if match.lastindex:
            try:
                days = int(match.group(1))
            except (TypeError, ValueError):
                days = days
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 7
        if days <= 0:
            return None
        folder = str(params.get("folder") or f"最近{days}天")
        return [{"domain": "file", "op": "move_recent",
                 "params": {"days": days, "folder": folder}}]
    if op == "default_pack":
        return _default_pack_actions(section)
    return None


def _default_pack_actions(section: dict) -> list:
    """与文件树「整理」按钮同一套：扩展名 → 关键词 → 日期兜底。"""
    actions = []
    ext_map = default_ext_map()
    if ext_map:
        actions.append({"domain": "file", "op": "organize_by_ext",
                        "params": {"ext_map": ext_map}})
    for kw, folder in keyword_map().items():
        actions.append({"domain": "file", "op": "move_keyword",
                        "params": {"keyword": kw, "folder": folder}})
    actions.append({"domain": "file", "op": "move_date", "params": {}})
    return actions


# 「读内容 → 分类 → 写成多份新文档」：不是 A 级搬家，也不是单文件 WordOffice
_BATCH_CONTENT_NEW_DOCS = re.compile(
    r"(?:内容识别|识别内容|按内容(?:分类|整理)"
    r"|识别(?:后|一下).{0,40}(?:分类|整理)"
    r"|(?:分类|整理)进(?:各自)?新文档|各自新文档)"
)

# P2A：按标题/章节结构拆成多份新文档（优先于 P1 六类整文件）
_STRUCTURE_SPLIT = re.compile(
    r"(?:按(?:标题|章节|大纲)拆|拆成多份新文档|按章节拆成新文档|"
    r"按标题拆成新文档|结构拆分|拆段成(?:新)?文档)"
)

# P2B：模板填槽另存（非润色、非仿写）
_TEMPLATE_FILL = re.compile(
    r"(?:按模板(?:套写|填|成稿)|模板套写|填槽(?:成稿)?|套写(?:成稿)?|"
    r"用模板(?:生成|填写|成稿)|模板填槽)"
)

# 内容标签：只读查找 / 纠正 / 回填（不搬文件）
_FIND_BY_TAG = re.compile(
    r"(?:按标签找|按标签(?:查找|搜索|检索)|找出标签(?:是|为)|"
    r"查找标签|搜索标签|标签(?:是|为).{0,12}(?:的|文件|文档)|"
    r"有哪些.{0,10}标签|找一下标签)"
)
_TAG_CORRECT = re.compile(
    r"(?:这篇|这个|该(?:文件|文档)|这份).{0,20}不是.{0,20}(?:是|改成)|"
    r"(?:纠正|修改)标签|把标签改成|标签改成|改标签为"
)
_TAG_BACKFILL = re.compile(
    r"(?:回填标签|重建标签(?:索引)?|同步标签(?:库|索引)?)"
)

# 清理内容分类孤儿索引（storage/content_index）
# 预览 vs 确认：聊天路径必须「确认…」才真删，与设置菜单二次确认对齐
_CLEANUP_CONTENT_CACHE = re.compile(
    r"(?:清理(?:一下)?(?:分类|内容分类)?(?:缓存|索引|孤儿)"
    r"|清理分类缓存|清(?:除|掉)分类(?:缓存|索引)"
    r"|删除(?:孤儿)?分类(?:缓存|索引))"
)
_CONFIRM_CLEANUP_CONTENT_CACHE = re.compile(
    r"确认清理(?:一下)?(?:分类|内容分类)?(?:缓存|索引|孤儿)|确认清理分类缓存"
)


def wants_batch_content_new_docs(user_input: str) -> bool:
    """机械判定：批量内容识别后再产出新文档（非改某一个已选中的 .docx）。"""
    text = str(user_input or "")
    # 更具体的 P2 触发优先，避免被 P1 吞掉
    if wants_template_fill(text) or wants_structure_split(text):
        return False
    return bool(_BATCH_CONTENT_NEW_DOCS.search(text))


def wants_structure_split(user_input: str) -> bool:
    """P2A：按已有标题/章节拆段另存。"""
    text = str(user_input or "")
    if wants_template_fill(text):
        return False
    return bool(_STRUCTURE_SPLIT.search(text))


def wants_template_fill(user_input: str) -> bool:
    """P2B：模板填槽另存（不做长文仿写）。"""
    return bool(_TEMPLATE_FILL.search(str(user_input or "")))


def wants_find_by_tag(user_input: str) -> bool:
    """按标签只读检索路径列表（不分类、不搬家）。"""
    text = str(user_input or "")
    if wants_tag_correct(text) or wants_tag_backfill(text):
        return False
    return bool(_FIND_BY_TAG.search(text))


def wants_tag_correct(user_input: str) -> bool:
    """用户纠正标签：只写库，不搬文件。"""
    return bool(_TAG_CORRECT.search(str(user_input or "")))


def wants_tag_backfill(user_input: str) -> bool:
    """对「内容分类/」现存产物回填标签（幂等）。"""
    return bool(_TAG_BACKFILL.search(str(user_input or "")))


def wants_confirm_cleanup_content_cache(user_input: str) -> bool:
    """确认执行清理（真删）；与设置菜单二次确认同强度。"""
    return bool(_CONFIRM_CLEANUP_CONTENT_CACHE.search(str(user_input or "")))


def wants_cleanup_content_cache(user_input: str) -> bool:
    """预览清理（含「确认…」句，由 dispatcher 区分 dry_run）。"""
    text = str(user_input or "")
    return bool(
        _CONFIRM_CLEANUP_CONTENT_CACHE.search(text)
        or _CLEANUP_CONTENT_CACHE.search(text)
    )


def extract_tag_query(user_input: str) -> str:
    """从「按标签找合同」等指令抽出标签词。"""
    text = str(user_input or "").strip()
    if not text:
        return ""
    m = re.search(
        r"(?:按标签找|按标签(?:查找|搜索|检索)|找出标签(?:是|为)|"
        r"查找标签|搜索标签|找一下标签)[:：\s]*([^\s，。,.！!？?]+)",
        text,
    )
    if m:
        return str(m.group(1) or "").strip()
    m2 = re.search(
        r"标签(?:是|为)\s*([^\s，。,.的文件文档]+)",
        text,
    )
    if m2:
        return str(m2.group(1) or "").strip()
    return ""


def extract_tag_correction(user_input: str) -> list[str]:
    """「这篇不是合同，是项目」→ ['项目']；「把标签改成合同、项目」→ 多标签。"""
    text = str(user_input or "").strip()
    if not text:
        return []
    m = re.search(
        r"(?:不是.{0,12}(?:，|,)?\s*)?(?:是|改成|改为)\s*(.+)$",
        text,
    )
    if not m:
        m = re.search(r"(?:把标签改成|标签改成|改标签为)\s*(.+)$", text)
    if not m:
        return []
    raw = str(m.group(1) or "").strip()
    raw = re.split(r"[。.!！?？]", raw)[0]
    parts = re.split(r"[、,，/|]|和", raw)
    return [p.strip() for p in parts if p and p.strip()]


def match(user_input: str) -> LocalMatch:
    """只根据契约判定。不读目录、不调模型。"""
    text = str(user_input or "").strip()
    if not text:
        return LocalMatch("miss", "空指令")
    section = _section()
    if not section:
        return LocalMatch("miss", "契约缺少 file_organize 分区")

    for _prio, rule, cre in _compile_rules(section.get("veto_patterns")):
        if cre.search(text):
            return LocalMatch(
                "veto",
                "命中否决规则，禁止本地整理（避免当成 file_organize）",
                rule_id=str(rule.get("id") or ""),
                kind=str(rule.get("kind") or ""),
            )

    for _prio, rule, cre in _compile_rules(section.get("intent_rules")):
        m = cre.search(text)
        if not m:
            continue
        op = str(rule.get("op") or "")
        actions = _actions_for_op(op, rule, m, section)
        if not actions:
            continue
        intent = {
            "tool": "file_organize",
            "mode": "execute",
            "subdir": "",
            "actions": actions,
            "plan": "A 级本地规则（0 token）",
            "local_a_file_organize": True,
        }
        return LocalMatch(
            "hit",
            "契约规则命中",
            rule_id=str(rule.get("id") or ""),
            intent=intent,
            actions=actions,
        )
    return LocalMatch("miss", "未命中 A 级整理规则")
