# -*- coding: utf-8 -*-
"""本轮撤回：接入层机械还原，不猜意图。

- 移动：dst → src（与日志撤回同一语义）
- Word：用本轮写前备份盖回目标路径
- 查表导出 / 报告：可选删除本轮产物
不碰记忆、不调模型。
"""
from __future__ import annotations

import shutil
from pathlib import Path


def empty_round() -> dict:
    return {
        "ai_msg_ids": [],
        "moves": [],          # {src, dst, log_id?}
        "word_restores": [],  # {path, backup}
        "extra_files": [],    # 本轮生成的导出/报告，撤回时可删
        "undone": False,
    }


def collect_from_result(result: dict | None, *, log_ids: list | None = None) -> dict:
    """从 dispatch 结果抽出可撤回项（只认明确字段）。"""
    round_rec = empty_round()
    if not isinstance(result, dict):
        return round_rec

    for i, op in enumerate(result.get("operations") or []):
        if not isinstance(op, dict):
            continue
        # A 级整理：src/dst；带 type=move 或仅有路径对
        src, dst = op.get("src"), op.get("dst")
        if src and dst:
            item = {"src": str(src), "dst": str(dst)}
            if log_ids and i < len(log_ids) and log_ids[i] is not None:
                item["log_id"] = log_ids[i]
            round_rec["moves"].append(item)

    ps = result.get("patch_set") if isinstance(result.get("patch_set"), dict) else {}
    bak = str(ps.get("backup") or "").strip()
    path = str(ps.get("path") or "").strip()
    # 已提交的写：备份是改前原件；失败已自动还原时再盖一次也无害
    if bak and path:
        round_rec["word_restores"].append({"path": path, "backup": bak})

    txn = result.get("txn") if isinstance(result.get("txn"), dict) else {}
    arch = str(txn.get("archive") or "").strip()
    if arch and path and not any(w["backup"] == arch for w in round_rec["word_restores"]):
        round_rec["word_restores"].append({"path": path, "backup": arch})

    export = result.get("export") if isinstance(result.get("export"), dict) else {}
    ep = str((export or {}).get("path") or "").strip()
    if ep:
        round_rec["extra_files"].append(ep)

    for key in ("report_path",):
        rp = str(result.get(key) or "").strip()
        if rp:
            round_rec["extra_files"].append(rp)
    for rp in result.get("report_paths") or []:
        s = str(rp or "").strip()
        if s:
            round_rec["extra_files"].append(s)

    return round_rec


def merge_rounds(base: dict, extra: dict) -> dict:
    out = empty_round()
    out["ai_msg_ids"] = list(base.get("ai_msg_ids") or []) + list(extra.get("ai_msg_ids") or [])
    out["moves"] = list(base.get("moves") or []) + list(extra.get("moves") or [])
    out["word_restores"] = list(base.get("word_restores") or []) + list(
        extra.get("word_restores") or [])
    out["extra_files"] = list(base.get("extra_files") or []) + list(
        extra.get("extra_files") or [])
    return out


def has_undoable(round_rec: dict | None) -> bool:
    if not round_rec or round_rec.get("undone"):
        return False
    return bool(
        round_rec.get("ai_msg_ids")
        or round_rec.get("moves")
        or round_rec.get("word_restores")
        or round_rec.get("extra_files")
    )


def apply_file_undos(round_rec: dict) -> tuple[list[str], list[str]]:
    """执行文件侧撤回。返回 (成功说明, 错误)。"""
    oks: list[str] = []
    errs: list[str] = []
    # Word 先还原（再挪文件，避免路径搅在一起）
    for w in list(round_rec.get("word_restores") or []):
        path, bak = w.get("path"), w.get("backup")
        try:
            if not bak or not Path(bak).is_file():
                errs.append("Word 备份缺失：%s" % Path(path or "").name)
                continue
            if not path:
                errs.append("Word 目标路径缺失")
                continue
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bak, path)
            oks.append("已还原 Word：%s" % Path(path).name)
        except OSError as exc:
            errs.append("还原 Word 失败 %s：%s" % (Path(path or "").name, exc))

    # 移动：把 dst 挪回 src（与日志撤回一致）
    for m in reversed(list(round_rec.get("moves") or [])):
        src, dst = Path(m.get("src") or ""), Path(m.get("dst") or "")
        try:
            if not dst.exists():
                errs.append("找不到已移动文件：%s" % dst.name)
                continue
            src.parent.mkdir(parents=True, exist_ok=True)
            if src.exists():
                errs.append("原位置已有文件，未覆盖：%s" % src.name)
                continue
            shutil.move(str(dst), str(src))
            oks.append("已撤回移动：%s" % src.name)
        except OSError as exc:
            errs.append("撤回移动失败 %s：%s" % (dst.name, exc))

    for p in list(round_rec.get("extra_files") or []):
        try:
            fp = Path(p)
            if fp.is_file():
                fp.unlink()
                oks.append("已删除本轮产物：%s" % fp.name)
        except OSError as exc:
            errs.append("删除产物失败 %s：%s" % (Path(p).name, exc))

    return oks, errs
