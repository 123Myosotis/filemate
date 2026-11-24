# -*- coding: utf-8 -*-
"""读目录内 Word → 规则判别类型 → 按类复制为新文档（原件不动）。

「各自新文档」= 每个源文件在「内容分类/<类型>/」下落一份新文件。
指纹去重：以输出树**现存文件**重建索引；撤回删产物后可再生成。
低置信：classify 已归 other；有 Key 时软标签升级（单轮上限）。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import config
from pipeline.stage1_parser.file_parser import parse_file
from pipeline.stage2_classifier.doc_classifier import (
    CLASSIFIER_VERSION,
    TYPE_LABELS,
    classify_decision,
    label_of,
    type_of_label,
)

from . import state as content_state
from .common import LEGACY_WORD_EXTS, list_docx, resolve_work_dir, unique_dest
from .legacy_word import extract_doc_text, word_com_available
from .soft_label import (
    SOFT_LABEL_MAX_PER_RUN,
    load_label_cache,
    save_label_cache,
    soft_label_from_model,
)


def _tags_on() -> bool:
    try:
        return bool(config.get_param("tags.enabled", True))
    except Exception:  # noqa: BLE001
        return True


def _upsert_content_tags(
    digest: str,
    dest_path: str,
    tags,
    *,
    source: str,
    confidence: float = 0.0,
) -> list[str]:
    """写标签库；失败静默。返回规范化后的中文标签列表（供清单列）。"""
    if not _tags_on():
        return []
    try:
        from infra import content_tags as ct

        src = "soft" if str(source).startswith("soft") else "rule"
        if ct.upsert_tags(
            digest, dest_path, tags, source=src, confidence=confidence,
        ):
            row = ct.get_tags(digest)
            if row and row.get("tags"):
                return list(row["tags"])
            return ct.normalize_tags(tags)
    except Exception:  # noqa: BLE001
        pass
    return []

OUT_ROOT = "内容分类"
_EXCLUDE_DIRS = (OUT_ROOT, "内容拆分", "模板成稿")
MANIFEST_STEM = "分类清单"
_MIN_CHARS = 80
_ONE_LEVEL = re.compile(r"含子目录|含一层子目录|包括子文件夹|包含子目录")
_TOP_ONLY = re.compile(r"只(?:扫|看)?顶层|不要子目录|仅顶层")


def _want_one_level(user_input: str) -> bool:
    text = str(user_input or "")
    if _TOP_ONLY.search(text):
        return False
    return bool(_ONE_LEVEL.search(text))


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _fmt_of(path: Path) -> str:
    return (path.suffix.lower().lstrip(".") or "?")


def _entry(
    rel: str,
    *,
    label: str = "",
    doc_type: str = "",
    top2: str = "-",
    stale: bool = False,
) -> dict:
    rel_s = str(rel or "").replace("\\", "/")
    lab = label or (rel_s.split("/")[0] if "/" in rel_s else "")
    dt = doc_type or type_of_label(lab)
    return {
        "rel": rel_s,
        "label": lab,
        "doc_type": dt,
        "top2": top2 or "-",
        "stale": bool(stale),
    }


def _coerce_entry(raw, *, stale: bool = False) -> dict | None:
    if isinstance(raw, str) and raw.strip():
        return _entry(raw, stale=stale)
    if isinstance(raw, dict) and raw.get("rel"):
        return _entry(
            str(raw.get("rel") or ""),
            label=str(raw.get("label") or ""),
            doc_type=str(raw.get("doc_type") or ""),
            top2=str(raw.get("top2") or "-"),
            stale=bool(raw.get("stale", stale)),
        )
    return None


def _save_hash_index(root_out: Path, index: dict) -> None:
    files = {}
    for h, ent in index.items():
        e = _coerce_entry(ent) or _entry(str(ent))
        files[h] = {
            "rel": e["rel"],
            "label": e["label"],
            "doc_type": e["doc_type"],
            "top2": e["top2"],
            "stale": False,
        }
    content_state.save_index(
        root_out,
        {
            "classifier_version": CLASSIFIER_VERSION,
            "files": files,
        },
    )


def _load_index_payload(root_out: Path) -> tuple[int | None, dict]:
    """返回 (stored_version, raw_files_map)。复制目录导致 root 不一致时为空。"""
    data = content_state.load_index(root_out)
    if not data:
        return None, {}
    if "classifier_version" in data or "files" in data:
        try:
            ver = int(data.get("classifier_version") or 0)
        except (TypeError, ValueError):
            ver = 0
        files = data.get("files") if isinstance(data.get("files"), dict) else {}
        return ver, files
    if data and all(isinstance(v, str) for v in data.values()):
        return 1, data
    return 0, {}


def _rebuild_hash_index(root_out: Path) -> dict:
    """
    以输出树现存文件重建索引；合并 JSON 元数据。
    版本不一致 → 全部标记 stale（不丢路径，下一轮覆盖/换类，不产生 _1）。
    """
    index: dict[str, dict] = {}
    if not root_out.is_dir():
        return index
    stored_ver, stored_files = _load_index_payload(root_out)
    # 无索引（删 id / 删 storage / 复制换 UUID）→ 按磁盘重建，不标 stale，避免重复写入
    # 有索引且版本不一致 → stale，下一轮覆盖/换类
    version_stale = (
        stored_ver is not None and int(stored_ver) != CLASSIFIER_VERSION
    )
    zh_dirs = set(TYPE_LABELS.values())

    for pat in ("*.docx", "*.docm", "*.doc"):
        for p in root_out.rglob(pat):
            if not p.is_file() or p.name.startswith("~$"):
                continue
            try:
                digest = _file_sha256(p)
                rel = str(p.relative_to(root_out)).replace("\\", "/")
            except OSError:
                continue
            parent = p.parent.name if p.parent != root_out else ""
            label = parent if parent in zh_dirs else ""
            prev = _coerce_entry(
                stored_files.get(digest), stale=version_stale,
            )
            if prev and prev.get("rel"):
                # 路径以磁盘为准；判别元数据尽量沿用
                ent = _entry(
                    rel,
                    label=prev.get("label") or label,
                    doc_type=prev.get("doc_type") or type_of_label(label),
                    top2=prev.get("top2") or "-",
                    stale=version_stale,
                )
            else:
                ent = _entry(
                    rel,
                    label=label,
                    doc_type=type_of_label(label),
                    top2="-",
                    stale=version_stale,
                )
            index[digest] = ent
    return index


def _extract_text_for_classify(src: Path) -> dict:
    """
    返回 {ok, text, note, skip_reason, format_note}.
    .doc → 可选 COM；.docx/.docm → parse_file。
    """
    ext = src.suffix.lower()
    if ext in LEGACY_WORD_EXTS:
        got = extract_doc_text(src)
        if not got.get("ok"):
            return {
                "ok": False,
                "text": "",
                "note": "",
                "skip_reason": got.get("reason") or "legacy_doc_no_converter",
            }
        return {
            "ok": True,
            "text": str(got.get("text") or ""),
            "note": f"legacy:{got.get('converter') or 'word_com'}",
            "skip_reason": "",
        }
    parsed = parse_file(src)
    note = str(parsed.get("note") or "")
    if parsed.get("kind") == "unsupported":
        reason = note or "无法解析"
        if note == "legacy_doc_no_converter":
            reason = "legacy_doc_no_converter"
        return {
            "ok": False,
            "text": "",
            "note": note,
            "skip_reason": reason,
        }
    return {
        "ok": True,
        "text": str(parsed.get("text") or "").strip(),
        "note": note,
        "skip_reason": "",
    }


def _index_lookup_alive(root_out: Path, index: dict, digest: str) -> dict | None:
    """命中且目标仍在 → entry；否则剔除并返回 None。"""
    raw = index.get(digest)
    if not raw:
        return None
    ent = _coerce_entry(raw)
    if not ent:
        index.pop(digest, None)
        return None
    path = root_out / Path(ent["rel"])
    if path.is_file():
        return ent
    index.pop(digest, None)
    return None


def _same_content(a: Path, b: Path) -> bool:
    """两边都存在且 sha256 相同。"""
    try:
        if not a.is_file() or not b.is_file():
            return False
        return _file_sha256(a) == _file_sha256(b)
    except OSError:
        return False


def _place_classified_copy(
    src: Path,
    root_out: Path,
    label: str,
    *,
    stale_entry: dict | None,
) -> tuple[Path, str]:
    """
    写入分类副本。
    stale 同类：只覆盖索引登记的旧路径。
    stale 换类：删旧副本；新路径若已有异 hash 同名 → unique_dest，避免误盖无关文件。
    新文件：unique_dest。
    """
    folder = root_out / label
    folder.mkdir(parents=True, exist_ok=True)
    if stale_entry:
        old_rel = str(stale_entry.get("rel") or "")
        old_path = root_out / Path(old_rel) if old_rel else None
        old_label = str(stale_entry.get("label") or "")
        if not old_label and old_rel:
            old_label = old_rel.split("/")[0]
        if old_label == label and old_path is not None:
            dest = old_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            return dest, "stale_overwrite"
        # 换类：只删索引登记过的旧副本
        if old_path is not None and old_path.is_file():
            try:
                old_path.unlink()
            except OSError:
                pass
        cand = folder / src.name
        if cand.is_file() and not _same_content(cand, src):
            # 目标已有不同内容 → 不静默覆盖
            dest = unique_dest(folder, src.name)
            shutil.copy2(src, dest)
            return dest, "stale_migrate|同名另存"
        shutil.copy2(src, cand)
        return cand, "stale_migrate"
    dest = unique_dest(folder, src.name)
    note = "同名另存" if dest.name != src.name else ""
    shutil.copy2(src, dest)
    return dest, note


def _cleanup_empty_type_dirs(root_out: Path) -> list[str]:
    """删掉「内容分类/<类目>」下已空的类目录（不删清单/索引等根文件）。"""
    removed = []
    if not root_out.is_dir():
        return removed
    zh = set(TYPE_LABELS.values())
    try:
        for p in sorted(root_out.iterdir()):
            if not p.is_dir() or p.name not in zh:
                continue
            try:
                if any(p.iterdir()):
                    continue
                p.rmdir()
                removed.append(p.name)
            except OSError:
                continue
    except OSError:
        pass
    return removed


def _top2_cell(decision: dict) -> str:
    """规则密度 top2；强制报告时明示，避免与判别类型看起来矛盾。"""
    t1 = decision.get("top1") or ""
    s1 = decision.get("top1_score")
    t2 = decision.get("top2") or ""
    s2 = decision.get("top2_score")
    a = f"{label_of(t1)} {s1}" if t1 else "-"
    b = f"{label_of(t2)} {s2}" if t2 else "-"
    cell = f"{a} / {b}"
    if decision.get("forced_report"):
        # 得分仍是真实密度；前缀说明最终类型来自结构强规则
        cell = f"〔结构强规则→报告〕规则分 {cell}"
    return cell


def _manifest_note(*, tags_col: bool) -> str:
    note = (
        "说明：原件未移动；指纹含规则版本，版本变更强制重算；"
        "低置信默认 other，有 Key 才软标签（单轮有上限）。"
        " .doc 无 pywin32/Word 时跳过并记 legacy_doc_no_converter；"
        "COM 启动失败=com_unavailable，打开失败=open_failed，"
        "加密=encrypted，损坏=corrupt；自动化宏 ForceDisable。"
        " .docm 只解析文本不执行宏。"
        "结构强规则强制为报告时，得分保留真实密度，"
        "判别类型标〔结构强规则〕。"
    )
    if tags_col:
        note += " 标签存本地库（按内容 hash），不写进 docx。"
    return note


def _write_manifest(root_out: Path, meta_lines: list, rows: list, tags_col: bool) -> list[Path]:
    """分类清单：列 schema 交给通用 Word 报告（≤500 仅 docx；超出再附 xlsx）。"""
    from export.docx_report import write_table_report

    headers = ["原文件", "状态", "格式", "判别类型"]
    if tags_col:
        headers.append("标签")
    headers += ["得分 top2", "来源", "新文档", "跳过原因"]
    table_rows = []
    for r in rows:
        vals = [
            r.get("src") or "",
            r.get("status") or "",
            r.get("format") or "-",
            r.get("label") or "-",
        ]
        if tags_col:
            vals.append(r.get("tags") or "-")
        vals += [
            r.get("top2") or "-",
            r.get("source") or "-",
            r.get("dest") or "-",
            r.get("skip_reason") or "",
        ]
        table_rows.append(vals)
    return write_table_report(
        root_out / MANIFEST_STEM,
        "内容分类清单",
        meta_lines,
        headers,
        table_rows,
        _manifest_note(tags_col=tags_col),
        max_rows=500,
    )


def run_batch_content_new_docs(user_input: str, target_dir) -> dict:
    work = resolve_work_dir(user_input, Path(target_dir))
    if not work.is_dir():
        return {
            "ok": False,
            "handler": "系统",
            "message": "⚠️ 工作目录无效，无法做内容分类。",
            "operations": [],
            "tool": "content_split",
            "reason": "bad_work_dir",
        }

    depth = 1 if _want_one_level(user_input) else 0
    sources = list_docx(work, depth=depth, exclude_dir_names=_EXCLUDE_DIRS)
    sources = [p for p in sources if OUT_ROOT not in p.parts]

    if not sources:
        scope = "顶层+一层子目录" if depth else "顶层"
        return {
            "ok": False,
            "handler": "系统",
            "message": (
                f"在「{work.resolve()}」{scope}没有找到可处理的 Word"
                "（.docx/.docm/.doc；已跳过 ~$ / 隐藏 / 成稿输出目录）。\n"
                "需要扫子文件夹时，指令里加「含子目录」。"
            ),
            "operations": [],
            "tool": "content_split",
            "reason": "no_docx",
            "work_dir": str(work.resolve()),
        }

    root_out = work / OUT_ROOT
    root_out.mkdir(parents=True, exist_ok=True)

    # 首次自动回填（按目录记 meta；关 tags 则跳过）
    if _tags_on():
        try:
            from infra import content_tags as ct

            ct.ensure_auto_backfill(root_out)
        except Exception:  # noqa: BLE001
            pass

    # 仅以现存文件建索引（不合并磁盘残留 JSON）
    index = _rebuild_hash_index(root_out)
    label_cache = load_label_cache(root_out)
    soft_model_calls = 0

    api_key = ""
    try:
        api_key = str(config.get_api_key() or "")
    except Exception:  # noqa: BLE001
        api_key = ""

    rows: list[dict] = []
    created: list[str] = []
    copied = 0
    skipped_n = 0
    legacy_doc_n = 0
    legacy_skipped_n = 0

    for src in sources:
        fmt = _fmt_of(src)
        try:
            rel_src = str(src.relative_to(work)).replace("\\", "/")
        except ValueError:
            rel_src = src.name

        if src.suffix.lower() in LEGACY_WORD_EXTS:
            legacy_doc_n += 1

        try:
            sz = src.stat().st_size
        except OSError as exc:
            rows.append({
                "src": rel_src, "status": "跳过", "label": "-",
                "doc_type": "", "top2": "-", "dest": "-",
                "source": "-", "format": fmt,
                "skip_reason": f"无法读文件：{exc}",
            })
            skipped_n += 1
            continue
        if sz <= 0:
            rows.append({
                "src": rel_src, "status": "跳过", "label": "-",
                "doc_type": "", "top2": "-", "dest": "-",
                "source": "-", "format": fmt,
                "skip_reason": "0 字节",
            })
            skipped_n += 1
            continue

        try:
            digest = _file_sha256(src)
        except OSError as exc:
            rows.append({
                "src": rel_src, "status": "跳过", "label": "-",
                "doc_type": "", "top2": "-", "dest": "-",
                "source": "-", "format": fmt,
                "skip_reason": f"哈希失败：{exc}",
            })
            skipped_n += 1
            continue

        prior = _index_lookup_alive(root_out, index, digest)
        if prior and not prior.get("stale"):
            # 指纹命中且非 stale：跳过，回填上次判别
            try:
                dest_show = str(
                    (root_out / prior["rel"]).relative_to(work)
                ).replace("\\", "/")
            except ValueError:
                dest_show = prior.get("rel") or "-"
            rows.append({
                "src": rel_src,
                "status": "跳过",
                "label": prior.get("label") or "-",
                "doc_type": prior.get("doc_type") or "",
                "top2": prior.get("top2") or "-",
                "dest": f"`{dest_show}`",
                "source": "hash_skip",
                "format": fmt,
                "tags": prior.get("label") or "-",
                "skip_reason": "内容相同已分类（指纹命中）",
                "content_hash": digest,
            })
            skipped_n += 1
            continue

        extracted = _extract_text_for_classify(src)
        if not extracted.get("ok"):
            reason = extracted.get("skip_reason") or "无法解析"
            if reason == "legacy_doc_no_converter":
                legacy_skipped_n += 1
            rows.append({
                "src": rel_src, "status": "跳过", "label": "-",
                "doc_type": "", "top2": "-", "dest": "-",
                "source": "-", "format": fmt,
                "skip_reason": reason,
            })
            skipped_n += 1
            continue

        text = str(extracted.get("text") or "").strip()
        extract_note = str(extracted.get("note") or "")
        if len(text) < _MIN_CHARS:
            rows.append({
                "src": rel_src, "status": "跳过", "label": "-",
                "doc_type": "", "top2": "-", "dest": "-",
                "source": "-", "format": fmt,
                "skip_reason": f"有效正文不足 {_MIN_CHARS} 字（{len(text)}）",
            })
            skipped_n += 1
            continue

        decision = classify_decision(text, filename=src.name)
        doc_type = decision["doc_type"]
        tag_keys = [doc_type]
        tag_conf = float(decision.get("top1_score") or 0)
        source = "rule"
        if decision.get("forced_report"):
            source = "rule:force_report"
        if extract_note.startswith("legacy:"):
            source = f"{source}|{extract_note}"
        if extract_note == "macro_docm":
            source = f"{source}|macro_docm"
        if decision["low_confidence"]:
            source = "rule_low→other"
            tag_conf = 0.0
            if api_key:
                if soft_model_calls < SOFT_LABEL_MAX_PER_RUN:
                    soft = soft_label_from_model(
                        text, digest, api_key=api_key, cache=label_cache,
                    )
                    doc_type = soft["doc_type"]
                    tag_keys = list(soft.get("tags") or [doc_type])
                    tag_conf = float(soft.get("confidence") or 0)
                    src_tag = soft.get("source") or "fallback"
                    source = f"soft:{src_tag}"
                    if src_tag != "cache":
                        soft_model_calls += 1
                else:
                    doc_type = "other"
                    tag_keys = ["other"]
                    source = "soft:quota→other"

        label = label_of(doc_type)
        # 清单展示可带结构标注；落盘目录名仍用纯 label
        label_show = (
            f"{label}〔结构强规则〕"
            if decision.get("forced_report") and doc_type == "report"
            else label
        )
        top2 = _top2_cell(decision)
        try:
            dest, place_note = _place_classified_copy(
                src, root_out, label,
                stale_entry=prior if (prior and prior.get("stale")) else None,
            )
        except OSError as exc:
            rows.append({
                "src": rel_src, "status": "跳过", "label": label_show,
                "doc_type": doc_type, "top2": top2,
                "dest": "-", "source": source, "tags": "", "format": fmt,
                "skip_reason": f"复制失败：{exc}",
            })
            skipped_n += 1
            continue

        try:
            dest_rel = str(dest.relative_to(work)).replace("\\", "/")
            index_rel = str(dest.relative_to(root_out)).replace("\\", "/")
        except ValueError:
            dest_rel = str(dest)
            index_rel = dest.name

        if place_note:
            source = f"{source}|{place_note}"

        index[digest] = _entry(
            index_rel,
            label=label,
            doc_type=doc_type,
            top2=top2,
            stale=False,
        )
        created.append(str(dest.resolve()))
        copied += 1
        zh_tags = _upsert_content_tags(
            digest, str(dest.resolve()), tag_keys,
            source=source, confidence=tag_conf,
        )
        tags_cell = "、".join(zh_tags) if zh_tags else label
        # stale_overwrite / stale_migrate / stale_migrate|同名另存 → 已重算
        status = "已重算" if str(place_note).startswith("stale_") else "已分类"
        rows.append({
            "src": rel_src,
            "status": status,
            "label": label_show,
            "doc_type": doc_type,
            "top2": top2,
            "dest": f"`{dest_rel}`",
            "source": source,
            "tags": tags_cell,
            "format": fmt,
            "skip_reason": "",
            "low_confidence": decision["low_confidence"],
            "content_hash": digest,
        })

    _save_hash_index(root_out, index)
    save_label_cache(root_out, label_cache)
    empty_removed = _cleanup_empty_type_dirs(root_out)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tags_col = _tags_on()
    meta_lines = [
        f"工作目录：{work}",
        f"指令：{str(user_input or '').strip()}",
        f"时间：{stamp}",
        f"扫描：{'顶层+一层子目录' if depth else '仅顶层'}",
        f"本轮：已分类 {copied} · 跳过 {skipped_n} · 软标签模型调用 {soft_model_calls}/{SOFT_LABEL_MAX_PER_RUN}"
        f" · 规则版本 v{CLASSIFIER_VERSION}",
    ]
    if empty_removed:
        meta_lines.append(f"已清理空类目录：{'、'.join(empty_removed)}")
    manifest_paths: list[Path] = []
    try:
        manifest_paths = _write_manifest(root_out, meta_lines, rows, tags_col)
        for mp in manifest_paths:
            created.append(str(mp.resolve()))
    except OSError:
        pass
    legacy_md = root_out / "_分类清单.md"
    if legacy_md.is_file():
        try:
            head = legacy_md.read_text(encoding="utf-8", errors="ignore")[:800]
        except OSError:
            head = ""
        if "内容分类" in head or "得分 top2" in head or "本轮：" in head:
            try:
                legacy_md.unlink()
            except OSError:
                pass

    out_abs = str(root_out.resolve())
    expected = {f"{MANIFEST_STEM}.docx", f"{MANIFEST_STEM}.xlsx"}
    manifest_hint = (
        "、".join(p.name for p in manifest_paths)
        if manifest_paths
        else f"{MANIFEST_STEM}.docx"
    )
    locked_tip = ""
    if any(p.name not in expected for p in manifest_paths):
        locked_tip = f"\n清单目标被占用，已另存为：{manifest_hint}。"
    legacy_tip = ""
    if legacy_skipped_n > 0:
        tip_extra = (
            "（未安装 pywin32：请执行 `pip install pywin32` 后重试，"
            "或把这两份另存为 .docx）"
            if not word_com_available()
            else "（转换失败，见清单跳过原因）"
        )
        legacy_tip = (
            f"\n发现 {legacy_doc_n} 个 .doc，其中 {legacy_skipped_n} 个已跳过并列入清单"
            f"{tip_extra}。"
        )
    empty_tip = (
        f"\n已清理空类目录：{'、'.join(empty_removed)}。"
        if empty_removed else ""
    )

    if copied <= 0:
        return {
            "ok": True,
            "handler": "内容分类",
            "message": (
                f"本轮无新写入（跳过 {skipped_n}；规则 v{CLASSIFIER_VERSION}）。"
                "常见原因：指纹已分类且产物仍在 / 正文过短 / .doc 无转换器。\n"
                f"清单：{OUT_ROOT}/{manifest_hint}"
                f"{locked_tip}{legacy_tip}{empty_tip}"
            ),
            "operations": [],
            "tool": "content_split",
            "reason": "all_skipped",
            "report_paths": created,
            "classifications": rows,
            "work_dir": str(work.resolve()),
            "output_dir": out_abs,
            "soft_model_calls": soft_model_calls,
            "classifier_version": CLASSIFIER_VERSION,
            "legacy_skipped": legacy_skipped_n,
            "empty_dirs_removed": empty_removed,
        }

    by_label: dict[str, int] = {}
    for r in rows:
        if r.get("status") not in ("已分类", "已重算"):
            continue
        by_label[r["label"]] = by_label.get(r["label"], 0) + 1
    summary = "、".join(f"{k} {v} 份" for k, v in sorted(by_label.items()))
    msg = (
        f"已按内容识别分类，新写入 {copied} 份（跳过 {skipped_n}；原件未动；"
        f"规则 v{CLASSIFIER_VERSION}）。\n"
        f"输出目录：{out_abs}\n"
        f"分布：{summary or '无'}。\n"
        f"软标签模型调用：{soft_model_calls}/{SOFT_LABEL_MAX_PER_RUN}\n"
        f"清单：{OUT_ROOT}/{manifest_hint}"
        f"{locked_tip}{legacy_tip}{empty_tip}"
    )
    return {
        "ok": True,
        "handler": "内容分类",
        "message": msg,
        "operations": [],
        "tool": "content_split",
        "reason": "ok",
        "report_paths": created,
        "classifications": rows,
        "work_dir": str(work.resolve()),
        "output_dir": out_abs,
        "soft_model_calls": soft_model_calls,
        "classifier_version": CLASSIFIER_VERSION,
        "legacy_skipped": legacy_skipped_n,
        "empty_dirs_removed": empty_removed,
    }
