# -*- coding: utf-8 -*-
"""老格式 .doc：可选 Word COM 转临时 docx 取文本；失败则明确跳过。

默认不依赖 pywin32；检测到才走转换。不执行宏、不改原件。
COM：独立进程、只读、强制禁用自动化宏、无提示；finally 先 Close 再 Quit。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

# msoAutomationSecurityForceDisable — 禁止 AutoOpen 等自动化宏
_MSO_AUTOMATION_SECURITY_FORCE_DISABLE = 3


def word_com_available() -> bool:
    try:
        import win32com.client  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _classify_com_error(exc: BaseException, *, stage: str) -> str:
    """把 COM/策略/打开失败拆成可读 reason，避免一律 corrupt。"""
    msg = str(exc).lower()
    raw = str(exc)
    if "password" in msg or "encrypt" in msg or "受密码" in raw or "密码" in raw:
        return "encrypted"
    if stage == "dispatch":
        return "com_unavailable"
    if stage == "open":
        return "open_failed"
    if any(
        k in msg
        for k in (
            "blocked", "policy", "disabled", "automation",
            "拒绝", "策略", "禁用", "权限",
        )
    ):
        return "open_failed"
    if stage in ("save", "parse"):
        return "corrupt"
    return "open_failed"


def extract_doc_text(src: Path) -> dict:
    """
    从 .doc 取纯文本。

    返回 {ok, text, reason, converter}:
      ok=False 时 reason 为
        legacy_doc_no_converter / com_unavailable / open_failed /
        encrypted / corrupt
    """
    path = Path(src)
    if not path.is_file():
        return {"ok": False, "text": "", "reason": "corrupt", "converter": ""}
    if not word_com_available():
        return {
            "ok": False,
            "text": "",
            "reason": "legacy_doc_no_converter",
            "converter": "",
        }
    tmp_dir = Path(tempfile.mkdtemp(prefix="filemate_doc_"))
    tmp_docx = tmp_dir / (path.stem + ".docx")
    app = None
    doc = None
    try:
        import win32com.client as win32

        try:
            app = win32.DispatchEx("Word.Application")
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "text": "",
                "reason": _classify_com_error(exc, stage="dispatch"),
                "converter": "word_com",
                "detail": str(exc)[:200],
            }

        app.Visible = False
        try:
            app.DisplayAlerts = 0
        except Exception:  # noqa: BLE001
            pass
        # 关闭自动化宏：只读仍可能按本机设置跑 AutoOpen
        try:
            app.AutomationSecurity = _MSO_AUTOMATION_SECURITY_FORCE_DISABLE
        except Exception:  # noqa: BLE001
            pass

        try:
            doc = app.Documents.Open(
                str(path.resolve()),
                ReadOnly=True,
                AddToRecentFiles=False,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "text": "",
                "reason": _classify_com_error(exc, stage="open"),
                "converter": "word_com",
                "detail": str(exc)[:200],
            }

        try:
            # 16 = wdFormatXMLDocument
            doc.SaveAs2(str(tmp_docx), FileFormat=16)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "text": "",
                "reason": _classify_com_error(exc, stage="save"),
                "converter": "word_com",
                "detail": str(exc)[:200],
            }

        try:
            doc.Close(False)
        except Exception:  # noqa: BLE001
            pass
        doc = None

        from pipeline.stage1_parser.file_parser import parse_file

        parsed = parse_file(tmp_docx)
        text = str(parsed.get("text") or "")
        if not text.strip():
            return {
                "ok": False,
                "text": "",
                "reason": "corrupt",
                "converter": "word_com",
            }
        return {
            "ok": True,
            "text": text,
            "reason": "",
            "converter": "word_com",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "text": "",
            "reason": _classify_com_error(exc, stage="open"),
            "converter": "word_com",
            "detail": str(exc)[:200],
        }
    finally:
        if doc is not None:
            try:
                doc.Close(False)
            except Exception:  # noqa: BLE001
                pass
        if app is not None:
            try:
                app.Quit()
            except Exception:  # noqa: BLE001
                pass
            try:
                del app
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(tmp_dir, ignore_errors=True)
