# agent_core/intent_router.py
"""
AI 意图解析器（对话链路的唯一决策入口，AI 全权代理）：
用户指令 → AI 结构化解析 → mode(执行/咨询/闲聊) + actions[]（可同时含文件动作与内容动作）

                ┌─ execute 执行模式：按 actions 直接操作，不输出方案
用户指令 → 解析 ─┤  · file 动作：AI 直接点名"哪个文件 → 哪个文件夹"（也支持按扩展名/关键词/时间参数表达）
                │  · content 动作：AI 点名要读取整理哪些文件的内容
                ├─ consult 咨询模式：只给方案和真实预览，不动任何文件
                └─ chat 闲聊模式：普通对话

一条指令可以包含多个动作（如"整理这些文档，并整理文档内容"= file 动作 + content 动作）。
AI 只输出严格 JSON；本地只做白名单消毒与执行，不做任何意图决策。
无 API Key、网络失败或 JSON 连续非法时返回 None，由调用方明确提示（不再偷偷走规则）。
"""
import json
import re
import time
from pathlib import Path

import requests

import config

# 合法动作白名（防止模型自由发挥出未实现的动作）
FILE_OPS = {"move", "organize_by_ext", "move_keyword", "move_recent", "move_date"}
CONTENT_OPS = {"extract", "summary", "stats", "join", "report"}
VALID_MODES = {"execute", "consult", "chat"}
VALID_TOOLS = {"file_organize", "table_query", "table_synthesize", "chat"}

_PROMPT_TEMPLATE = """你是 FileMate 的唯一意图决策器（Agent Router）。本地程序只按你的 JSON 调度工具。只输出一个 JSON 对象，禁止输出 JSON 以外的任何文字、不要 markdown 代码块。

【用户指令】
{user_input}

【根目录】{target_dir}
【用户当前选中的文件】（若非空，跨表查询优先用这些路径）
{selected_files}
【根目录直接包含的文件】
{file_list}
【各子文件夹及其内部文件】（用户指定子文件夹时，动作里的文件名只能从对应清单照抄）
{subdir_detail}

【第零步：选择 tool（最重要，互斥优先）】
tool 只能是以下之一：
- table_query：从 Excel/CSV **单元格数据**里查/找/提取/筛选/联查/标注派生列/按时间窗对齐，尤其是多表、复合条件、生成结果表。
  强信号（命中任一条就选 table_query，禁止选 file_organize）：
  · 「选中的几个 Excel / 这些表里 / 跨表 / 联查 / 找出任何实体、记录或字段 / 生成新表 / 输出对应表格」
  · 含多条件逻辑（并且/或者/只要…都标出/按时间对齐到另一张表）
  · 要从表内字段做过滤、派生列、多表关联取属性或责任人等
- table_synthesize：简单按规则把已有列重排出一张新表，但**没有**复杂谓词树/多表 join/时间窗对齐这类跨表查询语义时可用。
- file_organize：只移动/归类/按扩展名或文件名归档**文件本身**（不读单元格业务逻辑）。
- chat：问候闲聊、与文件/表格无关。
冲突裁决：只要目标是「表里的数据」而不是「文件放哪个文件夹」，即使口语带「整理」，也选 table_query（或 table_synthesize），绝不能 file_organize。
必须填写 tool 字段；本地不会根据用户原文猜 tool。
例句：「帮我把现在选中的几个 Excel 表里的…找出来，生成一个新表」→ tool=table_query, mode=execute。

【第一步：确定工作文件夹 subdir】
- 用户明确说了某个子文件夹（如"图书馆文件夹里""项目资料文件夹"），且它确实出现在上面的子文件夹清单中，subdir 填该文件夹名，动作里的文件名从该子文件夹的文件清单照抄；否则填空字符串 ""，文件名从根目录清单照抄。
- 用户说「根目录/项目下的某某」「某某里面有N个文件」「只要某某里的文件」时：只要某某能对应到上面的子文件夹名，subdir 必须填该名，禁止留空后去扫整个根目录。
- 严禁填写 ../、盘符、多层路径。

【第二步：判断 mode】
- execute：祈使语气，要求现在就把事办了。如：整理/移动/归档/分类/备份/提取/汇总/统计/联查/生成报告/总结内容/读一下/看看里面有什么/分析一下，并整理。
- consult：询问做法、建议、可行性，或明确说"先给方案、先别动、能不能、怎么、如何"。
- chat：问候、闲聊、与文件无关的问题。tool 也必须是 chat。

【第三步：生成 actions 数组】（可含多个动作，按用户语义排列；内容整理可处理的扩展名：{supported_exts}）
1) 文件动作（仅 tool=file_organize 时需要；移动文件本身，5 种 op）：
   {{"domain":"file","op":"move","moves":[{{"file":"精确文件名.pdf","folder":"文档"}}]}}
   —— 最推荐：直接点名每个文件去哪个文件夹。folder 必须是单层文件夹名（程序会自动创建）。
   {{"domain":"file","op":"organize_by_ext","ext_map":{{".pdf":"文档",".jpg":"图片"}}}}
   {{"domain":"file","op":"move_keyword","keyword":"合同","folder":"合同类"}}
   {{"domain":"file","op":"move_recent","days":7,"folder":"最近7天备份"}}
   {{"domain":"file","op":"move_date"}}  （按修改时间归档到 年/月 文件夹）
2) 内容动作（tool=table_synthesize 或旧式内容整理；不动文件位置）：
   {{"domain":"content","op":"summary|extract|stats|join|report","target_files":["精确文件名.txt"]}}
   —— 处理全部文件时用 "all":true 且不填 target_files；只有内容类扩展名的文件才允许进入 target_files。
3) tool=table_query 时：actions 可空数组 []（系统用选中/目录下的 xlsx 调用跨表查询 Tool）；mode 必须为 execute。
- 用户说"整理文档并整理内容"这类双诉求时，必须同时给一个 file 动作和一个 content 动作，tool=file_organize 或 table_synthesize 按主诉求选。
- mode=chat 时 actions 留空数组，tool=chat。
- mode=consult 时也要把 actions 尽量填好（供系统生成真实预览），并在 plan 里用中文说明方案。
- 语义模糊无法确定文件时，在 plan 里说明需要用户补充什么，actions 中不要放猜测的文件。

只输出如下 JSON：
{{"tool":"file_organize|table_query|table_synthesize|chat","mode":"execute|consult|chat","subdir":"","actions":[...],"plan":""}}"""

_CORRECTION_HINT = """

【系统反馈】你上一次的决策无法执行：{feedback}。请修正 JSON 后重新输出，确保要执行的文件都来自清单且动作参数完整。"""


def route(user_input: str, target_dir, api_key: str = None, feedback: str = "",
          selected_files: list | None = None):
    """
    调 AI 解析意图。返回规范化 dict：{tool, mode, subdir, actions, plan}；
    feedback 非空时把"上次为什么不能执行"反馈给 AI 做自我修正。
    无 Key/网络失败/JSON 非法（重试后）返回 None。
    """
    api_key = api_key if api_key is not None else config.get_api_key()
    if not api_key:
        return None

    work_dir, subdirs, file_names, subdir_files = _scan(Path(target_dir))
    file_list = ("（空文件夹）" if not file_names
                 else "\n".join(f"{i}. {n}" for i, n in enumerate(file_names, 1)))
    if subdir_files:
        subdir_detail = "\n".join(
            f"▸ {name}/\n" + ("\n".join(f"  {i}. {f}" for i, f in enumerate(files, 1))
                             if files else "  （空）")
            for name, files in subdir_files.items())
    else:
        subdir_detail = "（无子文件夹）"
    sel = [str(p) for p in (selected_files or []) if str(p).strip()]
    selected_block = (
        "（无选中）" if not sel
        else "\n".join(f"{i}. {n}" for i, n in enumerate(sel, 1))
    )
    from pipeline.stage1_parser import file_parser
    supported = " ".join(sorted(file_parser.TEXT_EXTS | file_parser.TABLE_EXTS))

    prompt = _PROMPT_TEMPLATE.format(
        user_input=user_input, target_dir=str(Path(target_dir)),
        selected_files=selected_block,
        subdir_detail=subdir_detail, file_list=file_list, supported_exts=supported,
    )
    if feedback:
        prompt += _CORRECTION_HINT.format(feedback=feedback[:200])

    max_retries = int(config.get_param("router.max_retries"))
    waits = list(config.get_param("router.retry_waits"))
    for attempt in range(max_retries):
        try:
            from ai.client import chat_completion
            data = chat_completion(
                [{"role": "user", "content": prompt}],
                api_key,
                max_tokens=int(config.get_param("router.max_tokens")),
                temperature=config.get_param("router.temperature"),
                response_format={"type": "json_object"},
                timeout=int(config.get_param("router.timeout")),
            )
            raw = data["choices"][0]["message"]["content"]
            return _normalize(json.loads(_strip_fence(raw)), user_input=user_input)
        except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError):
            if attempt < max_retries - 1:
                time.sleep(waits[min(attempt, len(waits) - 1)])
    return None


def _scan(target_dir: Path):
    """
    扫描根目录：返回 (当前工作目录, 子文件夹名列表, 根目录文件名列表, {子文件夹: [文件名]})。
    子文件夹内文件一并列出（仅单层），否则 AI 看不到子文件夹内容、无法对子文件夹指令生成动作。
    """
    try:
        subdirs = sorted(p.name for p in target_dir.iterdir()
                         if p.is_dir() and not p.name.startswith(".")
                         and p.name not in ("FileMate报告",))
        names = sorted(p.name for p in target_dir.iterdir()
                       if p.is_file() and not p.name.startswith("."))
    except OSError:
        return target_dir, [], [], {}
    limit = int(config.get_param("router.max_list_files"))
    skip = set(config.get_param("router.skip_dirs") or [])
    subdir_files = {}
    for sd in subdirs[:int(config.get_param("router.max_list_subdirs"))]:
        try:
            # 递归列出子文件夹内所有文件（含子目录），用相对路径表示，
            # 避免 AI 因看不到深层文件导致动作返回空结果
            # 跳过项目垃圾目录（.git/.venv 等）
            fs = sorted(str(p.relative_to(target_dir / sd))
                        for p in (target_dir / sd).rglob("*")
                        if p.is_file() and not p.name.startswith(".")
                        and not any(part in skip for part in p.relative_to(target_dir / sd).parts))
        except OSError:
            fs = []
        subdir_files[sd] = fs[:limit]
    return target_dir, subdirs, names[:limit], subdir_files


def infer_work_dir_from_path(user_input: str, fallback_root: Path = None):
    """
    从用户原文抽出绝对/相对路径，取最长存在的目录；
    路径尾巴粘了「目录下的全部材料」等口语时向上回退，并在父目录下按子名动态命中。
    """
    text = str(user_input or "")
    if not text:
        return None
    # Windows 盘符路径：段内允许中文目录名，遇逗号/句号等截断
    for m in re.finditer(
            r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n,，。；;]+\\)*[^\\/:*?\"<>|\r\n,，。；;]+",
            text):
        raw = m.group(0).strip().rstrip("，。；;、）)】]")
        # 口语粘连：…\测试目录下的全部材料 → 先剥常见尾巴
        for glue in ("目录下的全部材料", "目录下的", "目录下", "下的全部材料",
                     "里面的全部材料", "里面", "下的材料", "下"):
            if raw.endswith(glue) and len(raw) > len(glue) + 3:
                raw = raw[: -len(glue)]
                break
        cur = raw
        found = None
        while cur:
            p = Path(cur)
            try:
                ok = p.is_dir()
            except OSError:
                ok = False
            if ok:
                found = p
                break
            nxt = re.sub(r"[^\\/]+$", "", cur).rstrip("\\/")
            if nxt == cur:
                break
            cur = nxt
        if found is not None:
            # 若命中的是父目录，再看路径尾巴/后文能否点名其子目录
            rest = text[m.end():] if m.end() <= len(text) else ""
            tail = raw[len(str(found)):].lstrip("\\/") if str(found) in raw else ""
            blob = f"{tail} {rest} {text}"
            try:
                kids = [c for c in found.iterdir()
                        if c.is_dir() and not c.name.startswith(".")]
            except OSError:
                kids = []
            best = None
            best_s = 0
            for kid in kids:
                sc = 0
                if kid.name and kid.name in blob:
                    sc += 5
                if kid.name and f"{kid.name}目录" in blob:
                    sc += 3
                if sc > best_s:
                    best, best_s = kid, sc
            if best and best_s >= 3:
                return best
            return found
        # 父存在时：用剩余片段命中其子目录名
        parent = Path(raw)
        while parent.parent != parent and not parent.is_dir():
            parent = parent.parent
        if parent.is_dir():
            tail = raw[len(str(parent)):].lstrip("\\/")
            try:
                kids = [c for c in parent.iterdir() if c.is_dir()]
            except OSError:
                kids = []
            for kid in kids:
                if kid.name and kid.name in (tail or text):
                    return kid
    if fallback_root and Path(fallback_root).is_dir():
        sub = infer_subdir_from_user(fallback_root, text)
        if sub:
            return Path(fallback_root) / sub
    return None


def infer_subdir_from_user(target_dir: Path, user_input: str) -> str:
    """
    AI 未填/填错 subdir 时的动态兜底：用用户原文命中真实子目录名。
    不维护业务目录词表；候选只来自当前根目录的真实子文件夹。
    """
    text = str(user_input or "")
    root = Path(target_dir)
    if not text or not root.is_dir():
        return ""
    skip = set(config.get_param("router.skip_dirs") or [])
    try:
        children = [
            p for p in root.iterdir()
            if p.is_dir() and p.name not in skip and not p.name.startswith(".")
        ]
    except OSError:
        return ""
    if not children:
        return ""

    def _cn_num(s: str):
        m = {
            "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
        }
        if s.isdigit():
            return int(s)
        return m.get(s)

    want_n = None
    m = re.search(r"([一二两三四四五六七八九十\d]+)\s*个文件", text)
    if m:
        want_n = _cn_num(m.group(1))

    best, best_s = "", -1.0
    for p in children:
        name = p.name
        score = 0.0
        if name and name in text:
            score += 5.0
        # 「下的X」「X里面」「X文件夹」
        if re.search(
                rf"(?:下的|里的|内的|/|\\){re.escape(name)}|"
                rf"{re.escape(name)}(?:里面|之内|中|文件夹|目录|里)",
                text):
            score += 4.0
        # 用户说「X文件」而子目录名是 X
        if name and f"{name}文件" in text:
            score += 3.0
        if want_n:
            try:
                n_files = sum(
                    1 for f in p.iterdir()
                    if f.is_file() and not f.name.startswith("~$") and not f.name.startswith(".")
                )
            except OSError:
                n_files = -1
            if n_files == want_n:
                score += 3.5
            elif n_files > 0 and abs(n_files - want_n) <= 1:
                score += 1.5
        if score > best_s:
            best, best_s = name, score
    # 需要至少「点名」或「文件数对上」其一，避免乱锁目录
    if best_s < 3.0 or not best:
        return ""
    # 原文还点了更深层子目录（模拟/人事）时继续往下锁，不能停在父目录把各行业扫在一起
    deeper = infer_subdir_from_user(root / best, text)
    if deeper:
        return f"{best}/{deeper}"
    return best


def resolve_work_dir(target_dir: Path, subdir: str):
    """
    校验并解析 AI 指定的子文件夹。返回 Path；非法/不存在返回 None。
    支持相对多层路径（如 数据/子目录），逐段消毒，拒绝 .. 与绝对路径。
    """
    subdir = str(subdir or "").strip().replace("\\", "/")
    if not subdir:
        return Path(target_dir)
    # 用户可能说「某某文件夹」——去掉尾缀再试
    candidates = [subdir]
    for suffix in ("文件夹", "目录", "里", "内"):
        if subdir.endswith(suffix) and len(subdir) > len(suffix):
            candidates.append(subdir[:-len(suffix)].strip("/ "))
    root = Path(target_dir)
    for cand in candidates:
        parts = [p for p in cand.split("/") if p and p not in (".",)]
        if not parts or any(p == ".." for p in parts):
            continue
        safe_parts = []
        ok = True
        for part in parts:
            safe = sanitize_folder(part)
            if not safe or safe != part:
                ok = False
                break
            safe_parts.append(safe)
        if not ok:
            continue
        work = root.joinpath(*safe_parts)
        if work.is_dir():
            return work
        # 单层：在根下按名称模糊包含匹配一个子目录
        if len(safe_parts) == 1:
            key = safe_parts[0]
            try:
                for child in root.iterdir():
                    if child.is_dir() and (child.name == key or key in child.name):
                        return child
            except OSError:
                pass
    return None


def _strip_fence(text: str) -> str:
    """剥掉模型可能误加的 ```json ... ``` 围栏，取第一个花括号块"""
    m = re.search(r"\{.*\}", text, re.S)
    return m.group(0) if m else text


def _infer_tool_from_actions(mode: str, actions: list) -> str | None:
    """
    Structural-only fallback when model omits tool.
    Never inspect user utterance; never use business-word regex.
    Returns None when structure is insufficient (caller must retry AI).
    """
    if mode == "chat":
        return "chat"
    has_file = any(isinstance(a, dict) and a.get("domain") == "file" for a in actions)
    has_content = any(isinstance(a, dict) and a.get("domain") == "content" for a in actions)
    if has_file and not has_content:
        return "file_organize"
    if has_content:
        # content domain with all=true or target_files → synthesize/content path
        for a in actions:
            if not isinstance(a, dict) or a.get("domain") != "content":
                continue
            if a.get("all") or (a.get("target_files") or []):
                return "table_synthesize"
        return "table_synthesize"
    if has_file:
        return "file_organize"
    # execute/consult with empty actions often means table_query — but only LLM may say so
    return None


def _normalize(data, user_input: str = "") -> dict:
    """规范化与白名单校验，保证调用方拿到结构固定、动作合法的 dict"""
    if not isinstance(data, dict):
        raise ValueError("意图解析结果不是 JSON 对象")
    mode = data.get("mode") if data.get("mode") in VALID_MODES else "chat"
    subdir = str(data.get("subdir") or "").strip()
    plan = str(data.get("plan") or "")
    raw_actions = data.get("actions")
    # 兼容旧版单动作字段
    if not isinstance(raw_actions, list) and data.get("domain"):
        raw_actions = [{"domain": data.get("domain"), "action": data.get("action"),
                        **(data.get("params") or {})}]

    actions = []
    if mode != "chat":
        for a in raw_actions or []:
            if not isinstance(a, dict):
                continue
            domain = a.get("domain")
            op = a.get("op") or a.get("action") or ""
            if domain == "file" and op in FILE_OPS:
                actions.append({"domain": "file", "op": op,
                                "params": {k: v for k, v in a.items()
                                           if k not in ("domain", "op", "action")}})
            elif domain == "content" and (op in CONTENT_OPS or op == ""):
                item = {"domain": "content",
                        "op": op if op in CONTENT_OPS else "report",
                        "target_files": a.get("target_files") or [],
                        "all": bool(a.get("all"))}
                if isinstance(item["target_files"], str):
                    item["target_files"] = [item["target_files"]]
                actions.append(item)

    tool_raw = str(data.get("tool") or "").strip()
    if tool_raw not in VALID_TOOLS:
        inferred = _infer_tool_from_actions(mode, actions)
        if inferred is None:
            raise ValueError(
                "tool 字段缺失或非法，且无法从 actions 结构推断；"
                "请输出 tool=file_organize|table_query|table_synthesize|chat"
            )
        tool_raw = inferred
    if mode == "chat":
        tool_raw = "chat"
    if tool_raw == "table_query" and mode == "chat":
        mode = "execute"
    return {
        "tool": tool_raw,
        "mode": mode,
        "subdir": subdir,
        "actions": actions,
        "plan": plan,
    }


# ------------------------------------------------------------------
# 参数消毒：AI 输出不可信，落盘前必须过白名单
# ------------------------------------------------------------------

_FOLDER_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]')  # Windows 文件夹非法字符 + 路径分隔符


def sanitize_folder(name) -> str:
    """文件夹名消毒：只允许单层目录名，拒绝路径穿越（../、绝对路径、盘符、分隔符）"""
    name = str(name or "").strip().strip(".")
    if not name or name in ("~",):
        return ""
    if _FOLDER_BAD.search(name) or ".." in name:
        return ""
    return name[:64]
