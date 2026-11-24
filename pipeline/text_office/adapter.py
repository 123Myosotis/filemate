# -*- coding: utf-8 -*-
"""文本格式适配器：编码探测、原子写、指令→op、执行与校验。

支持 .txt / .md / .json / .log / .ini / .yaml。
JSON 文件写回前必须能重新解析，避免把坏 JSON 写进磁盘。
"""
from __future__ import annotations

import codecs
import json
import os
import re
import shutil
from pathlib import Path

from pipeline.office_core.result import fail

from .ops import OpError, apply_ops, json_error_text

TOOL = "text_office"
EXTS = {".txt", ".md", ".markdown", ".json", ".log", ".ini", ".yaml", ".yml"}

READONLY_OPS = frozenset({"inspect", "json_validate"})
DANGEROUS_OPS = frozenset({"delete_line"})
ALLOWED_OPS = frozenset({
    "inspect", "json_validate", "find_replace", "delete_line",
    "insert_line", "json_format", "json_minify",
})

# ---- 指令 → op 的规则（顺序敏感：具体规则在前） ----
_JSON_VALIDATE = re.compile(
    r"(?:校验|验证|检查|看看).{0,8}(?:json|JSON)|"
    r"(?:json|JSON).{0,10}(?:合法|格式对不对|有没有错|语法)"
)
_JSON_MINIFY = re.compile(r"(?:压缩|最小化|去掉空白).{0,8}(?:json|JSON)")
_JSON_FORMAT = re.compile(
    r"(?:格式化|美化|排版).{0,8}(?:json|JSON)|"
    r"(?:json|JSON).{0,6}(?:格式化|美化)"
)
_INSERT_LINE = re.compile(
    r"在\s*第\s*(\d+)\s*行\s*(?:之后|后面|后)\s*(?:插入|添加|加上)\s*(.+?)(?:[。，,；;]|$)"
)
_DELETE_LINE = re.compile(
    r"(?:删除|删掉|移除)\s*(?:包含|含有|带)\s*(.+?)\s*(?:的)?(?:行|那行)"
)
_REPLACE_QUOTED = re.compile(
    r"(?:把|将)?\s*[「\"“'](.+?)[」\"“']\s*(?:全部)?\s*"
    r"(?:替换成|替换为|改成|改为|换成)\s*[「\"“'](.+?)[」\"“']"
)
_REPLACE = re.compile(
    r"(?:把|将)\s*(.+?)\s*(?:替换成|替换为|改成|改为|换成)\s*(.+?)(?:[。，,；;]|$)"
)
_INSPECT = re.compile(
    r"(?:检查|看看|查看|统计).{0,6}(?:结构|编码|行数|内容|文件)|"
    r"(?:什么|哪种)编码|多少行"
)


def _clean_term(raw: str) -> str:
    """去掉模型/用户口语里常见的包裹符号。"""
    text = str(raw or "").strip()
    text = text.strip("「」\"'“”‘’")
    return text.strip(" \t\r\n，。,.；;：:")


def read_text(path) -> tuple[str, str]:
    """先认 BOM，再按 utf-8 → gb18030 依次尝试；返回 (文本, 实际编码)。"""
    raw = Path(path).read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        return raw[len(codecs.BOM_UTF8):].decode("utf-8", errors="replace"), "utf-8-sig"
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return raw.decode("utf-16", errors="replace"), "utf-16"
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


def write_text(path, text: str, encoding: str) -> None:
    """先写同目录临时文件再原子替换；编码不支持的新字符会抛 UnicodeEncodeError。"""
    path = Path(path)
    data = text.encode(encoding)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        try:
            shutil.copymode(path, tmp)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _json_type_name(obj) -> str:
    if isinstance(obj, dict):
        return "对象"
    if isinstance(obj, list):
        return "数组"
    if isinstance(obj, str):
        return "字符串"
    if isinstance(obj, bool):
        return "布尔值"
    if isinstance(obj, (int, float)):
        return "数字"
    return "空值"


class TextAdapter:
    tool = TOOL
    exts = EXTS
    readonly_ops = READONLY_OPS
    dangerous_ops = DANGEROUS_OPS
    allowed_ops = ALLOWED_OPS
    no_target_message = (
        "请先在文件管理器里选中一个文本文件（.txt/.md/.json/.log），我才能处理。"
    )

    # ---------------- 扫描 / 展示 ----------------

    def scan(self, path) -> dict:
        path = Path(path)
        text, encoding = read_text(path)
        lines = text.splitlines()
        state = {
            "path": str(path),
            "filename": path.name,
            "ext": path.suffix.lower(),
            "encoding": encoding,
            "bytes": path.stat().st_size,
            "chars": len(text),
            "lines": len(lines),
            "blank_lines": sum(1 for line in lines if not line.strip()),
            "newline": "CRLF" if "\r\n" in text else ("LF" if "\n" in text else "无"),
            "json_valid": None,
        }
        if path.suffix.lower() == ".json":
            try:
                obj = json.loads(text)
                state["json_valid"] = True
                state["json_type"] = _json_type_name(obj)
                state["json_size"] = len(obj) if isinstance(obj, (dict, list)) else 0
            except json.JSONDecodeError as exc:
                state["json_valid"] = False
                state["json_error"] = json_error_text(exc)
        return state

    def state_detail(self, state: dict) -> str:
        detail = "%s · %d 行 · %d 字符" % (
            str(state.get("encoding") or "").upper(),
            int(state.get("lines") or 0),
            int(state.get("chars") or 0),
        )
        if state.get("json_valid") is True:
            detail += " · JSON 合法（%s %d 项）" % (
                state.get("json_type") or "", int(state.get("json_size") or 0))
        elif state.get("json_valid") is False:
            detail += " · JSON 语法错误"
        return detail

    # ---------------- 计划 ----------------

    def plan(self, user_input: str, state: dict) -> list[dict]:
        text = str(user_input or "").strip()
        if _JSON_VALIDATE.search(text):
            return [{"op": "json_validate", "confidence": 0.9}]
        if _JSON_MINIFY.search(text):
            return [{"op": "json_minify", "confidence": 0.9}]
        if _JSON_FORMAT.search(text):
            return [{"op": "json_format", "confidence": 0.9}]

        m = _INSERT_LINE.search(text)
        if m:
            return [{"op": "insert_line", "after": int(m.group(1)),
                     "text": _clean_term(m.group(2)), "confidence": 0.85}]

        m = _DELETE_LINE.search(text)
        if m:
            return [{"op": "delete_line", "contains": _clean_term(m.group(1)),
                     "confidence": 0.85}]

        m = _REPLACE_QUOTED.search(text) or _REPLACE.search(text)
        if m:
            find, repl = _clean_term(m.group(1)), _clean_term(m.group(2))
            if find and find != repl:
                return [{"op": "find_replace", "find": find,
                         "replace": repl, "confidence": 0.9}]

        if _INSPECT.search(text):
            return [{"op": "inspect", "confidence": 0.9}]
        return [{"op": "inspect", "confidence": 0.3}]

    def is_low_confidence(self, user_input: str, ops: list[dict]) -> bool:
        return (len(ops) == 1
                and str(ops[0].get("op") or "") == "inspect"
                and float(ops[0].get("confidence") or 0) < 0.5)

    # ---------------- 执行 ----------------

    def execute(self, path, ops: list[dict], state: dict) -> dict:
        path = Path(path)
        text, encoding = read_text(path)
        writes = [op for op in ops if str(op.get("op") or "") not in self.readonly_ops]

        if not writes:
            return self._read_only_result(text, ops, state)

        try:
            new_text, applied = apply_ops(text, ops)
            write_text(path, new_text, encoding)
        except OpError as exc:
            return fail("not_found", str(exc))
        except json.JSONDecodeError as exc:
            return fail("json_invalid", "JSON 语法有错误，文件没有改动：%s" % json_error_text(exc))
        except UnicodeEncodeError:
            return fail(
                "encoding_failed",
                "原文件是 %s 编码，新内容含该编码无法表示的字符；"
                "请先另存为 UTF-8 再改。" % encoding.upper())

        return {
            "status": "done",
            "reason": "applied",
            "message": "已修改 %d 处。" % applied,
            "applied": applied,
            "plan_ref": None,
            "audit_ref": None,
            "user_actions": [],
        }

    def _read_only_result(self, text: str, ops: list[dict],
                          state: dict) -> dict:
        names = {str(op.get("op") or "") for op in ops}
        if "json_validate" in names:
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                return fail("json_invalid", "JSON 语法有错误：%s" % json_error_text(exc))
            return {
                "status": "done",
                "reason": "json_valid",
                "message": "JSON 语法正确（顶层是%s，共 %d 项）。" % (
                    _json_type_name(obj),
                    len(obj) if isinstance(obj, (dict, list)) else 0),
                "applied": 0,
                "plan_ref": None,
                "audit_ref": None,
                "user_actions": [],
            }

        message = "文件编码：%s；共 %d 行 / %d 字符；换行符：%s。" % (
            str(state.get("encoding") or "").upper(),
            int(state.get("lines") or 0),
            int(state.get("chars") or 0),
            state.get("newline") or "无",
        )
        if state.get("json_valid") is True:
            message += " JSON 合法（%s %d 项）。" % (
                state.get("json_type") or "", int(state.get("json_size") or 0))
        elif state.get("json_valid") is False:
            message += " JSON 语法错误：%s。" % (state.get("json_error") or "")
        return {
            "status": "done",
            "reason": "inspect",
            "message": message,
            "applied": 0,
            "plan_ref": None,
            "audit_ref": None,
            "user_actions": [],
        }

    # ---------------- 写后校验 ----------------

    def verify(self, path, ops: list[dict], state: dict) -> dict:
        try:
            text, _encoding = read_text(path)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": "回读失败：%s" % exc}

        writes = [op for op in ops if str(op.get("op") or "") not in self.readonly_ops]
        if state.get("json_valid") and writes:
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                return {"ok": False, "reason": json_error_text(exc)}
        return {"ok": True, "reason": ""}
