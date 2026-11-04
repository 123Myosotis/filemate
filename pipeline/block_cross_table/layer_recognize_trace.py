# -*- coding: utf-8 -*-
"""A' 轨迹落点：五元组 meta 校验 + 既有 write_trace 写入。不另造目录机制。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Optional

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.join_hypothesis import write_trace

KIND_PROPOSE = "layer_propose"
KIND_ASK_SUBSET = "layer_ask_subset"
KIND_FORMAL_REVIEW = "layer_formal_review"
KIND_PROPOSE_SLICE = "layer_propose_slice"
KIND_MERGE_LAYERS = "layer_merge_layers"

TRACE_RAW_MAX_CHARS_KEY = "layer_recognize_trace_raw_max_chars"
TRACE_TRUNCATE_MARKER = "\n/* truncated */"

LAYER_RECOGNIZE_TRACE_BODY_REQUIRED = (
    "ask_usage_after",
    "prompt_hash",
    "prompt_chars",
    "prompt_bytes",
    "prompt_shape",
    "prompt_full",
    "prompt_truncated",
    "raw_full",
    "raw_chars",
    "raw_truncated",
    "validate_reasons",
)

_REQUIRED_META = (
    "run_id",
    "block_id",
    "entry_seq",
    "call_kind",
    "kind_seq",
    "counts_toward_ask",
)

# run_id -> {normalized_block_id: raw}
_BLOCK_ID_REGISTRY: dict[str, dict[str, Any]] = {}


def reset_block_id_registry(run_id: Optional[str] = None) -> None:
    if run_id is None:
        _BLOCK_ID_REGISTRY.clear()
    else:
        _BLOCK_ID_REGISTRY.pop(str(run_id), None)


def _cross_table(contract_data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if contract_data is not None:
        block = contract_data.get("cross_table")
    else:
        from pipeline.synth_contract.registry import load_contract

        block = (load_contract().data or {}).get("cross_table")
    if not isinstance(block, dict):
        raise CrossTableError(
            "cross_table_contract_invalid",
            "cross_table block missing",
        )
    return block


def read_layer_recognize_kinds(
    contract_data: Optional[dict[str, Any]] = None,
) -> list[str]:
    ct = _cross_table(contract_data)
    kinds = ct.get("layer_recognize_kinds")
    routing = set((ct.get("enums") or {}).get("routing_trace_kind") or [])
    if not isinstance(kinds, list) or not kinds:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "layer_recognize_kinds missing",
        )
    out = [str(x) for x in kinds]
    if not set(out).issubset(routing):
        raise CrossTableError(
            "cross_table_contract_invalid",
            "layer_recognize_kinds not subset of routing_trace_kind",
        )
    return out


def read_layer_recognize_trace_raw_max_chars(
    contract_data: Optional[dict[str, Any]] = None,
) -> int:
    """缺键 / 非 int / <1 → cross_table_contract_invalid。"""
    ct = _cross_table(contract_data)
    key = TRACE_RAW_MAX_CHARS_KEY
    if key not in ct:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "%s missing" % key,
        )
    value = ct[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "%s invalid" % key,
        )
    return int(value)


def prompt_shape_from_propose_payload(payload: dict[str, Any]) -> dict[str, int]:
    cell_texts = payload.get("cell_texts") or {}
    dims = payload.get("dimensions_by_subtable") or {}
    dim_count = 0
    if isinstance(dims, dict):
        for values in dims.values():
            if isinstance(values, list):
                dim_count += len(values)
    return {
        "rectangles": len(payload.get("rectangles") or []),
        "containment_facts": len(payload.get("containment_facts") or []),
        "cell_texts_total_chars": len(
            json.dumps(cell_texts, ensure_ascii=False, default=str)
        ),
        "subtable_boundaries": len(payload.get("subtable_boundaries") or []),
        "group_field_ids": len(payload.get("group_field_ids") or []),
        "dimensions": dim_count,
    }


def prompt_shape_from_review_payload(payload: dict[str, Any]) -> dict[str, int]:
    """复核 / 要子集：从 review 载荷尽量对齐同名字段量级。"""
    upstream = payload.get("upstream") if isinstance(payload.get("upstream"), dict) else {}
    layer = (
        payload.get("layer_result")
        if isinstance(payload.get("layer_result"), dict)
        else {}
    )
    cell_texts = payload.get("cell_texts") or {}
    dims_src = layer.get("dimension_membership") or []
    group_src = (
        layer.get("group_label_decls")
        or layer.get("group_label_declaration")
        or []
    )
    return {
        "rectangles": len(upstream.get("rectangles") or []),
        "containment_facts": len(upstream.get("containment_facts") or []),
        "cell_texts_total_chars": len(
            json.dumps(cell_texts, ensure_ascii=False, default=str)
        ),
        "subtable_boundaries": len(
            upstream.get("subtable_boundaries")
            or layer.get("subtable_boundaries")
            or []
        ),
        "group_field_ids": len(group_src) if isinstance(group_src, list) else 0,
        "dimensions": len(dims_src) if isinstance(dims_src, list) else 0,
    }


def _truncate_trace_text(text: str, max_chars: int) -> tuple[str, int, bool]:
    chars = len(text)
    if chars <= max_chars:
        return text, chars, False
    marker = TRACE_TRUNCATE_MARKER
    keep = max_chars - len(marker)
    if keep < 0:
        keep = 0
    out = text[:keep] + marker
    if len(out) > max_chars:
        out = out[:max_chars]
    elif len(out) < max_chars:
        out = out + (" " * (max_chars - len(out)))
    return out, chars, True


def pack_raw_for_trace(
    raw_value: Any,
    *,
    max_chars: int,
) -> tuple[Any, int, bool]:
    """返回 (raw_full, raw_chars, raw_truncated)。未截断且为 dict 时保留 object。"""
    if isinstance(raw_value, str):
        s = raw_value
    else:
        s = json.dumps(raw_value, ensure_ascii=False, default=str)
    text, chars, truncated = _truncate_trace_text(s, max_chars)
    if truncated:
        return text, chars, True
    if isinstance(raw_value, dict):
        return raw_value, chars, False
    if isinstance(raw_value, str):
        return raw_value, chars, False
    return text, chars, False


def build_layer_recognize_trace_body(
    *,
    prompt: str,
    prompt_shape: dict[str, int],
    ask_usage_after: dict[str, int],
    validate_reasons: list[str],
    prompt_out: dict[str, Any],
    exception_for_raw: Optional[BaseException],
    exception_prefix: str,
    contract_data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """组装 LayerRecognizeTraceBody（11 必填）。"""
    max_chars = read_layer_recognize_trace_raw_max_chars(contract_data)
    prompt_text = str(prompt or "")
    prompt_full, prompt_chars, prompt_truncated = _truncate_trace_text(
        prompt_text, max_chars
    )
    prompt_bytes = len(prompt_text.encode("utf-8"))
    prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:16]

    if prompt_out.get("raw_received"):
        raw_full, raw_chars, raw_truncated = pack_raw_for_trace(
            prompt_out.get("raw"),
            max_chars=max_chars,
        )
    else:
        exc = exception_for_raw
        if exc is None:
            s = "%s_exception:Unknown: missing raw" % exception_prefix
        else:
            s = "%s_exception:%s: %s" % (
                exception_prefix,
                type(exc).__name__,
                str(exc),
            )
        raw_full, raw_chars, raw_truncated = pack_raw_for_trace(
            s, max_chars=max_chars
        )

    body = {
        "ask_usage_after": {
            "proposal": int(ask_usage_after.get("proposal", 0)),
            "subset": int(ask_usage_after.get("subset", 0)),
            "review": int(ask_usage_after.get("review", 0)),
        },
        "prompt_hash": prompt_hash,
        "prompt_chars": int(prompt_chars),
        "prompt_bytes": int(prompt_bytes),
        "prompt_shape": {
            "rectangles": int(prompt_shape.get("rectangles", 0)),
            "containment_facts": int(prompt_shape.get("containment_facts", 0)),
            "cell_texts_total_chars": int(
                prompt_shape.get("cell_texts_total_chars", 0)
            ),
            "subtable_boundaries": int(prompt_shape.get("subtable_boundaries", 0)),
            "group_field_ids": int(prompt_shape.get("group_field_ids", 0)),
            "dimensions": int(prompt_shape.get("dimensions", 0)),
        },
        "prompt_full": prompt_full,
        "prompt_truncated": bool(prompt_truncated),
        "raw_full": raw_full,
        "raw_chars": int(raw_chars),
        "raw_truncated": bool(raw_truncated),
        "validate_reasons": [str(x) for x in validate_reasons],
    }
    for key in LAYER_RECOGNIZE_TRACE_BODY_REQUIRED:
        if key not in body:
            raise CrossTableError(
                "cross_table_audit_structure_invalid",
                "LayerRecognizeTraceBody field missing",
                {"field": key},
            )
    # optional slice/merge audit fields (v1.8.0+)
    for opt in (
        "slice_index",
        "slice_count",
        "slice_rect_ids",
        "facts_filter",
        "merge_phase",
        "merge_input_trim_level",
    ):
        if opt in prompt_out and prompt_out[opt] is not None:
            body[opt] = prompt_out[opt]
    return body


def normalize_block_id(raw: Any) -> str:
    if isinstance(raw, bool) or raw is None:
        raise CrossTableError(
            "cross_table_audit_structure_invalid",
            "block_id invalid type",
            {"raw": raw},
        )
    text = str(raw)
    if text == "":
        raise CrossTableError(
            "cross_table_audit_structure_invalid",
            "block_id empty",
        )
    return text


def check_block_id_collision(run_id: str, raw: Any) -> str:
    """同一 run 内不同原始值不得归一到同一字符串。"""
    rid = str(run_id)
    if rid == "":
        raise CrossTableError(
            "cross_table_audit_structure_invalid",
            "run_id empty",
        )
    normalized = normalize_block_id(raw)
    bucket = _BLOCK_ID_REGISTRY.setdefault(rid, {})
    prev = bucket.get(normalized)
    if prev is not None and prev != raw:
        raise CrossTableError(
            "cross_table_audit_structure_invalid",
            "block_id normalize collision",
            {
                "run_id": rid,
                "normalized": normalized,
                "raw_a": prev,
                "raw_b": raw,
            },
        )
    bucket[normalized] = raw
    return normalized


def build_trace_meta(
    *,
    run_id: Any,
    block_id: Any,
    entry_seq: Any,
    call_kind: Any,
    kind_seq: Any,
    counts_toward_ask: bool = True,
    contract_data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """组装并校验五元组；失败抛 cross_table_audit_structure_invalid，不写盘。"""
    if run_id is None or (isinstance(run_id, str) and run_id.strip() == ""):
        raise CrossTableError(
            "cross_table_audit_structure_invalid",
            "run_id missing",
        )
    rid = str(run_id)
    if call_kind is None or (isinstance(call_kind, str) and call_kind == ""):
        raise CrossTableError(
            "cross_table_audit_structure_invalid",
            "call_kind missing",
        )
    kind = str(call_kind)
    allowed = read_layer_recognize_kinds(contract_data)
    if kind not in allowed:
        raise CrossTableError(
            "cross_table_audit_structure_invalid",
            "call_kind not in layer_recognize_kinds",
            {"call_kind": kind, "allowed": allowed},
        )
    if not isinstance(entry_seq, int) or isinstance(entry_seq, bool) or entry_seq < 1:
        raise CrossTableError(
            "cross_table_audit_structure_invalid",
            "entry_seq invalid",
            {"entry_seq": entry_seq},
        )
    if not isinstance(kind_seq, int) or isinstance(kind_seq, bool) or kind_seq < 1:
        raise CrossTableError(
            "cross_table_audit_structure_invalid",
            "kind_seq invalid",
            {"kind_seq": kind_seq},
        )
    if not isinstance(counts_toward_ask, bool):
        raise CrossTableError(
            "cross_table_audit_structure_invalid",
            "counts_toward_ask invalid",
        )
    bid = check_block_id_collision(rid, block_id)
    meta = {
        "run_id": rid,
        "block_id": bid,
        "block_id_raw": block_id,
        "entry_seq": int(entry_seq),
        "call_kind": kind,
        "kind_seq": int(kind_seq),
        "counts_toward_ask": bool(counts_toward_ask),
    }
    for key in _REQUIRED_META:
        if key not in meta or meta[key] is None:
            raise CrossTableError(
                "cross_table_audit_structure_invalid",
                "meta field missing",
                {"field": key},
            )
    return meta


def write_layer_recognize_trace(
    *,
    call_kind: str,
    run_id: str,
    block_id: Any,
    entry_seq: int,
    kind_seq: int,
    clock: Callable[[], str],
    body: Optional[dict[str, Any]] = None,
    counts_toward_ask: bool = True,
    contract_data: Optional[dict[str, Any]] = None,
) -> str:
    """校验 meta 后经既有 write_trace 落盘。body 合并进 JSON（含 meta）。"""
    meta = build_trace_meta(
        run_id=run_id,
        block_id=block_id,
        entry_seq=entry_seq,
        call_kind=call_kind,
        kind_seq=kind_seq,
        counts_toward_ask=counts_toward_ask,
        contract_data=contract_data,
    )
    payload: dict[str, Any] = {"meta": meta}
    if body:
        payload.update(body)
    return write_trace(str(call_kind), payload, clock)


class LayerRecognizeTraceSession:
    """单块×单入口内的种类序号计数；新 entry_seq 应新建 session。"""

    def __init__(
        self,
        run_id: str,
        block_id: Any,
        entry_seq: int,
        clock: Callable[[], str],
        contract_data: Optional[dict[str, Any]] = None,
    ) -> None:
        self.run_id = str(run_id)
        self.block_id = block_id
        self.entry_seq = int(entry_seq)
        self.clock = clock
        self.contract_data = contract_data
        self._kind_seq: dict[str, int] = {}
        self.paths: list[str] = []

    def next_kind_seq(self, call_kind: str) -> int:
        cur = self._kind_seq.get(call_kind, 0) + 1
        self._kind_seq[call_kind] = cur
        return cur

    def write(self, call_kind: str, body: Optional[dict[str, Any]] = None) -> str:
        kind_seq = self.next_kind_seq(call_kind)
        path = write_layer_recognize_trace(
            call_kind=call_kind,
            run_id=self.run_id,
            block_id=self.block_id,
            entry_seq=self.entry_seq,
            kind_seq=kind_seq,
            clock=self.clock,
            body=body,
            counts_toward_ask=True,
            contract_data=self.contract_data,
        )
        self.paths.append(path)
        return path


def _trace_root() -> Path:
    import config

    directory = config.get_param("cross_table.trace_dir")
    if not isinstance(directory, str) or directory == "":
        raise CrossTableError("trace_io_failed", "trace settings missing")
    root = Path(directory)
    if not root.is_absolute():
        root = Path(__file__).resolve().parents[2] / directory
    return root


def load_trace_file(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[2] / p
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise CrossTableError(
            "cross_table_audit_structure_invalid",
            "trace body not object",
        )
    return data


def rebuild_meta_from_trace_file(path: str | Path) -> dict[str, Any]:
    """五元组从 JSON meta 重建，不从文件名推断。"""
    data = load_trace_file(path)
    meta = data.get("meta")
    if not isinstance(meta, dict):
        raise CrossTableError(
            "cross_table_audit_structure_invalid",
            "trace meta missing",
        )
    for key in _REQUIRED_META:
        if key not in meta:
            raise CrossTableError(
                "cross_table_audit_structure_invalid",
                "trace meta incomplete",
                {"field": key},
            )
    return dict(meta)


def list_traces_for_block(
    run_id: str,
    block_id: Any,
    *,
    trace_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """按 (run_id, block_id) 聚合；返回含 path + meta 的列表。"""
    root = trace_dir if trace_dir is not None else _trace_root()
    if not root.is_dir():
        return []
    want_bid = normalize_block_id(block_id)
    rid = str(run_id)
    out: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        meta = data.get("meta")
        if not isinstance(meta, dict):
            continue
        if str(meta.get("run_id")) != rid:
            continue
        if str(meta.get("block_id")) != want_bid:
            continue
        out.append({"path": str(path), "meta": dict(meta), "body": data})
    out.sort(
        key=lambda item: (
            int(item["meta"].get("entry_seq") or 0),
            str(item["meta"].get("call_kind") or ""),
            int(item["meta"].get("kind_seq") or 0),
        )
    )
    return out
