# -*- coding: utf-8 -*-
"""文本类 op 的纯函数实现：只处理字符串，不碰文件系统。"""
from __future__ import annotations

import json

# 需要先解析再序列化的 op
JSON_OPS = {"json_validate", "json_format", "json_minify"}


class OpError(Exception):
    """可预期的业务失败（找不到内容/行号越界/JSON 语法错），消息直接给用户看。"""


_JSON_HINTS = {
    "Expecting value": "缺少值",
    "Expecting property name enclosed in double quotes": "键名必须用双引号",
    "Expecting ',' delimiter": "缺少逗号",
    "Expecting ':' delimiter": "缺少冒号",
    "Extra data": "JSON 结束后还有多余内容",
    "Unterminated string starting at": "字符串没有闭合",
}


def json_error_text(exc: json.JSONDecodeError) -> str:
    """把 Python 的英文 JSON 报错转成一句中文位置提示。"""
    hint = exc.msg
    for key, cn in _JSON_HINTS.items():
        if str(exc.msg).startswith(key):
            hint = cn
            break
    return "第 %d 行第 %d 列：%s" % (exc.lineno, exc.colno, hint)


def _op_replace(text: str, op: dict) -> tuple[str, int]:
    find = str(op.get("find") or "")
    repl = str(op.get("replace") or "")
    if not find:
        raise OpError("要替换的内容是空的。")
    n = text.count(find)
    if n == 0:
        raise OpError("没找到「%s」，文件没有改动。" % find)
    return text.replace(find, repl), n


def _op_delete_line(text: str, op: dict) -> tuple[str, int]:
    needle = str(op.get("contains") or "")
    if not needle:
        raise OpError("要删除哪一行？请说明行里包含的文字。")
    lines = text.splitlines(keepends=True)
    kept = [line for line in lines if needle not in line]
    n = len(lines) - len(kept)
    if n == 0:
        raise OpError("没有找到包含「%s」的行，文件没有改动。" % needle)
    return "".join(kept), n


def _op_insert_line(text: str, op: dict) -> tuple[str, int]:
    try:
        after = int(op.get("after") or 0)
    except (TypeError, ValueError):
        raise OpError("行号必须是数字。")
    payload = str(op.get("text") or "")
    if not payload:
        raise OpError("要插入的内容是空的。")
    lines = text.splitlines(keepends=True)
    if after < 0 or after > len(lines):
        raise OpError("行号 %d 超出范围（共 %d 行）。" % (after, len(lines)))
    eol = "\r\n" if "\r\n" in text else "\n"
    if after > 0 and not lines[after - 1].endswith(("\n", "\r")):
        lines[after - 1] += eol
    lines.insert(after, payload + eol)
    return "".join(lines), 1


def _op_json_format(text: str, op: dict) -> tuple[str, int]:
    try:
        indent = int(op.get("indent") or 2)
    except (TypeError, ValueError):
        indent = 2
    data = json.loads(text)  # 语法错直接抛 JSONDecodeError，由上层转成中文
    out = json.dumps(data, ensure_ascii=False, indent=indent)
    return out + ("\n" if text.endswith("\n") else ""), 1


def _op_json_minify(text: str, op: dict) -> tuple[str, int]:
    data = json.loads(text)
    out = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return out + ("\n" if text.endswith("\n") else ""), 1


def apply_ops(text: str, ops: list[dict]) -> tuple[str, int]:
    """按顺序执行写 op；返回 (新文本, 生效处数)。只读 op 在这里跳过。"""
    applied = 0
    for op in ops:
        name = str(op.get("op") or "")
        if name in ("inspect", "json_validate"):
            continue
        if name == "find_replace":
            text, n = _op_replace(text, op)
        elif name == "delete_line":
            text, n = _op_delete_line(text, op)
        elif name == "insert_line":
            text, n = _op_insert_line(text, op)
        elif name == "json_format":
            text, n = _op_json_format(text, op)
        elif name == "json_minify":
            text, n = _op_json_minify(text, op)
        else:
            raise OpError("暂不支持这个操作：%s" % name)
        applied += n
    return text, applied
