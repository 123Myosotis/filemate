# pipeline/stage3_processor/text_splitter.py
"""
Stage3 分块器：按文档类型选策略，把全文切成带元信息的块。
每块字段：chunk_id / section_path / start_offset / end_offset / content / status
- 结构标记（章节/条款/步骤）独立成单元并维护 section_path
- 代码块（``` 围栏）整体作为一个单元，绝不从中间截断
- 超长单元按句子/行硬切，保证块上限，不丢内容
- 块间 overlap：收口时把尾部 ≤overlap tokens 的单元带入下一块，保证上下文连续
token 估算：中日韩字符 ≈1 token/字，其余按 4 字符 ≈1 token（够分块用，不必精确）
单块上限默认 = 模型窗口 × 填充比 − 输出预留 − 模板开销；只有 config.json 显式写了
chunk.max_tokens.<类型> 才改用更小值。装不满这一额度不切。
"""
import re

import config  # 分块参数可从 config.json 的 params 段覆盖（严禁写死）
from ai.context_budget import estimate_tokens, input_token_budget

# 文档类型 → (策略名, 默认 overlap tokens, 默认单块上限 tokens)
# 实际取值按 chunk.overlap.<类型> / chunk.max_tokens.<类型> 从参数读取
DEFAULT_CHUNK_CONFIG = {
    "technical": ("章节+代码块", 200, 4000),
    "novel":     ("章节+段落",   300, 3000),
    "guide":     ("步骤",        150, 3500),
    "project":   ("任务",        200, 3500),
    "contract":  ("条款",        100, 4000),
    "academic":  ("章节",        200, 4000),
}

# 结构标记行：markdown 标题 / 第X章(节/回/条) / 步骤N / 第N步
_HEADING_RE = re.compile(
    r"^#{1,6}\s+\S"                                  # markdown 标题
    r"|^第[一二三四五六七八九十百千\d]+[章节回条]"       # 中文章节/条款
    r"|^(步骤\s*\d+|第[一二三四五六七八九十\d]+步)"     # 步骤标记
)
def _chunk_config(doc_type: str) -> tuple:
    """(overlap, 单块上限)。上限默认顶到窗口填充比，只有 params 显式写了才用更小值。"""
    _, d_overlap, _d_max = DEFAULT_CHUNK_CONFIG.get(
        doc_type, DEFAULT_CHUNK_CONFIG["technical"])
    key = f"chunk.max_tokens.{doc_type}"
    if config.param_is_user_set(key):
        max_tokens = int(config.get_param(key))
    else:
        max_tokens = input_token_budget(
            reserved_output=int(config.get_param("extract.max_tokens")))
    overlap = int(config.get_param(f"chunk.overlap.{doc_type}", d_overlap))
    overlap = min(overlap, max(0, max_tokens // 10))
    return overlap, max(1, max_tokens)


def split(text: str, doc_type: str) -> list:
    """
    主入口：全文 → 块列表（顺序即阅读顺序）。
    每块 content 含 overlap 回看前缀；start/end_offset 指向块首/块尾单元在原文的跨度。
    """
    overlap, max_tokens = _chunk_config(doc_type)
    # 超长单元按「上限 − overlap」切开，下一块带回尾部后仍不超过窗口预算
    slice_cap = max(1, max_tokens - overlap)

    # 切单元；超上限的单单元先硬切，避免后面死循环
    units = []
    for u in _segment(text):
        if estimate_tokens(u["text"]) > slice_cap:
            units.extend(_split_oversize(u, slice_cap))
        else:
            units.append(u)

    chunks, buf, buf_tokens, buf_section = [], [], 0, None
    cur_section, chunk_idx = "（文首）", 0

    for u in units:
        if u["is_heading"]:  # 结构标记 → 更新当前 section_path
            cur_section = u["text"].strip().splitlines()[0][:40]
        t = estimate_tokens(u["text"])

        # 装不下了 → 收口成块，再按 overlap 回看携带尾部
        if buf and buf_tokens + t > max_tokens:
            chunks.append(_pack(chunk_idx, buf, buf_section, text))
            chunk_idx += 1
            buf, buf_tokens, buf_section = _overlap_carry(buf, overlap)

        if buf_section is None:  # 新块第一个单元决定 section_path
            buf_section = cur_section
        buf.append(u)
        buf_tokens += t

    if buf:  # 收尾
        chunks.append(_pack(chunk_idx, buf, buf_section, text))
    return chunks


def _overlap_carry(buf: list, overlap: int) -> tuple:
    """下一块带回上一块尾部。整单元放得下就带整单元；超长单元只带尾部切片。"""
    carry, carry_tokens = [], 0
    for uu in reversed(buf):
        if uu["is_heading"]:
            break
        tt = estimate_tokens(uu["text"])
        if carry_tokens + tt > overlap:
            if not carry and overlap > 0:
                piece = _suffix_within(uu["text"], overlap)
                if piece:
                    carry.insert(0, {
                        "start": uu["end"] - len(piece),
                        "end": uu["end"],
                        "text": piece,
                        "is_heading": False,
                    })
                    carry_tokens = estimate_tokens(piece)
            break
        carry.insert(0, uu)
        carry_tokens += tt
    return carry, carry_tokens, None


def _suffix_within(text: str, max_tokens: int) -> str:
    if not text or max_tokens <= 0:
        return ""
    lo, hi, best = 1, len(text), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if estimate_tokens(text[-mid:]) <= max_tokens:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return text[-best:] if best else ""


def _pack(idx: int, units: list, section: str, full_text: str) -> dict:
    """把一组单元打包成一个块 dict"""
    content = "\n\n".join(u["text"].strip() for u in units).strip()
    return {
        "chunk_id": f"c{idx + 1}",
        "section_path": section or "（未分节）",
        "start_offset": units[0]["start"],
        "end_offset": units[-1]["end"],
        "content": content,
        "status": "pending",
    }


def _segment(text: str) -> list:
    """
    把全文切成"单元"序列：段落 / 标题行 / 完整代码块。
    每个单元：{start, end, text, is_heading}，offset 为原文绝对偏移。
    """
    units, para, para_start, in_fence, pos = [], [], None, False, 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if in_fence:  # 代码块内部：累积到围栏闭合为止
            para.append(line)
            if stripped.startswith("```"):
                units.append({"start": para_start, "end": pos + len(line),
                              "text": "".join(para), "is_heading": False})
                para, para_start, in_fence = [], None, False
        elif stripped.startswith("```"):  # 代码块开始
            if para:
                units.append({"start": para_start, "end": pos,
                              "text": "".join(para), "is_heading": False})
                para, para_start = [], None
            para, para_start, in_fence = [line], pos, True
        elif not stripped:  # 空行 → 段落结束
            if para:
                units.append({"start": para_start, "end": pos,
                              "text": "".join(para), "is_heading": False})
                para, para_start = [], None
        else:
            if not para:
                para_start = pos
            para.append(line)
            if _HEADING_RE.match(stripped):  # 标题行独立成单元
                units.append({"start": para_start, "end": pos + len(line),
                              "text": "".join(para), "is_heading": True})
                para, para_start = [], None
        pos += len(line)
    if para:  # 收尾残留
        units.append({"start": para_start, "end": pos,
                      "text": "".join(para), "is_heading": False})
    return units


def _fit_pieces(text: str, max_tokens: int) -> list:
    """没有标点可切时，按 token 预算取最长前缀，保证单片不超过上限。"""
    if not text:
        return []
    if estimate_tokens(text) <= max_tokens:
        return [text]
    pieces, start, n = [], 0, len(text)
    while start < n:
        lo, hi, best = start + 1, n, start + 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if estimate_tokens(text[start:mid]) <= max_tokens:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        pieces.append(text[start:best])
        start = best
    return pieces


def _split_oversize(unit: dict, max_tokens: int) -> list:
    """
    超长单元硬切：优先按句子边界（。！？!?\n），代码块按行。
    单句/单行仍超上限时按 token 预算切开。切出的子单元继承偏移量。
    """
    text = unit["text"]
    pieces = []
    if text.lstrip().startswith("```"):  # 代码块：按行切
        lines = text.splitlines(keepends=True)
        cur = []
        for ln in lines:
            if estimate_tokens(ln) > max_tokens:
                if cur:
                    pieces.append("".join(cur))
                    cur = []
                pieces.extend(_fit_pieces(ln, max_tokens))
                continue
            cur.append(ln)
            if estimate_tokens("".join(cur)) >= max_tokens:
                pieces.append("".join(cur))
                cur = []
        if cur:
            pieces.append("".join(cur))
    else:  # 普通段落：按句子切，超长句再硬切
        sentences = re.split(r"(?<=[。！？!?])|\n", text)
        cur = ""
        for s in sentences:
            if not s:
                continue
            for bit in _fit_pieces(s, max_tokens):
                if cur and estimate_tokens(cur + bit) > max_tokens:
                    pieces.append(cur)
                    cur = bit
                else:
                    cur += bit
        if cur.strip():
            pieces.append(cur)

    base = unit["start"]
    subs, offset = [], base
    for p in pieces:
        end = offset + len(p)
        subs.append({"start": offset, "end": end, "text": p, "is_heading": False})
        offset = end
    return subs
