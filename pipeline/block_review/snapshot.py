"""块级快照与恢复。恢复只靠 executable_confirmed + alignments.json + conflicts.json。"""

import json
from pathlib import Path

from pipeline.block_stitch.stitch import stitch


def snapshots_dir(session_dir):
    path = Path(session_dir) / "snapshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def block_snapshot_path(session_dir, block_id):
    return snapshots_dir(session_dir) / ("block_%s_confirmed.json" % block_id)


def alignments_path(session_dir):
    return snapshots_dir(session_dir) / "alignments.json"


def conflicts_path(session_dir):
    return snapshots_dir(session_dir) / "conflicts.json"


def write_json(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def persist_bridge_sidecars(session_dir, alignments_resolved, conflicts):
    """首次桥成功写入；打回重桥若变化则覆盖。与 alignments 同时机。"""
    write_json(alignments_path(session_dir), alignments_resolved if alignments_resolved is not None else [])
    write_json(conflicts_path(session_dir), conflicts if conflicts is not None else [])


def empty_snapshot(block_id, fingerprint):
    return {
        "block_id": block_id,
        "status": "partial",
        "source_fingerprint": fingerprint,
        "confirmed_mappings": [],
        "rejected_mappings": [],
        "executable_confirmed": [],
        "columns": [],
        "rows_slice": [],
        "cell_origin_slice": [],
        "measured_at": None,
    }


def load_snapshot(session_dir, block_id, fingerprint):
    path = block_snapshot_path(session_dir, block_id)
    if not path.exists():
        return empty_snapshot(block_id, fingerprint)
    return read_json(path)


def save_snapshot(session_dir, snap, measured_at):
    snap = dict(snap)
    snap["measured_at"] = measured_at
    write_json(block_snapshot_path(session_dir, snap["block_id"]), snap)
    return snap


def confirm_mapping(snap, executable_item, columns, rows_slice, origins):
    mid = executable_item.get("mapping_index")
    confirmed = [m for m in snap.get("confirmed_mappings") or [] if m != mid]
    confirmed.append(mid)
    rejected = [m for m in snap.get("rejected_mappings") or [] if m != mid]
    items = [
        e for e in snap.get("executable_confirmed") or [] if e.get("mapping_index") != mid
    ]
    items.append(
        {
            "mapping_index": mid,
            "sources_resolved": executable_item.get("sources_resolved") or [],
            "target_name": executable_item.get("target_name"),
            "rule": executable_item.get("rule"),
            "primitive": executable_item.get("primitive"),
        }
    )
    snap["confirmed_mappings"] = confirmed
    snap["rejected_mappings"] = rejected
    snap["executable_confirmed"] = items
    snap["columns"] = list(columns or [])
    snap["rows_slice"] = rows_slice
    snap["cell_origin_slice"] = origins
    snap["status"] = "confirmed" if not rejected else "partial"
    return snap


def reject_mapping(snap, mapping_index):
    snap["confirmed_mappings"] = [
        m for m in snap.get("confirmed_mappings") or [] if m != mapping_index
    ]
    snap["executable_confirmed"] = [
        e
        for e in snap.get("executable_confirmed") or []
        if e.get("mapping_index") != mapping_index
    ]
    rejected = list(snap.get("rejected_mappings") or [])
    if mapping_index not in rejected:
        rejected.append(mapping_index)
    snap["rejected_mappings"] = rejected
    snap["status"] = "partial" if snap["confirmed_mappings"] else "rejected"
    return snap


def reject_block(snap):
    snap["status"] = "rejected"
    snap["confirmed_mappings"] = []
    snap["executable_confirmed"] = []
    return snap


def alignments_match(alignments, block_plan):
    if not isinstance(alignments, list):
        return False
    known = {b.get("block_id") for b in (block_plan or {}).get("blocks") or []}
    for item in alignments:
        if not isinstance(item, dict):
            return False
        slots = item.get("slots_resolved") or []
        if len(slots) < 2:
            return False
        for slot in slots:
            if not isinstance(slot, dict) or slot.get("block_id") not in known:
                return False
    return True


def restore_from_snapshots(session_dir, current_fingerprint, block_plan, tables_by_id):
    """选 A：executable_confirmed + alignments.json + conflicts.json → 全量 stitch。"""
    folder = snapshots_dir(session_dir)
    files = sorted(folder.glob("block_*_confirmed.json"))
    executable = []
    for path in files:
        snap = read_json(path)
        fp = snap.get("source_fingerprint") or {}
        if fp.get("grid_hash") != current_fingerprint.get("grid_hash") or fp.get(
            "plan_hash"
        ) != current_fingerprint.get("plan_hash"):
            raise SnapshotError("fingerprint_mismatch", "源指纹与快照不一致，拒绝恢复")
        if snap.get("status") == "rejected":
            continue
        executable.extend(snap.get("executable_confirmed") or [])

    align_file = alignments_path(session_dir)
    if not align_file.exists():
        raise SnapshotError("alignments_missing_or_mismatch", "缺少 alignments.json，拒绝恢复")
    alignments = read_json(align_file)
    if not alignments_match(alignments, block_plan):
        raise SnapshotError("alignments_missing_or_mismatch", "alignments 与块计划不匹配，拒绝恢复")

    conflict_file = conflicts_path(session_dir)
    if not conflict_file.exists():
        raise SnapshotError("conflicts_missing", "缺少 conflicts.json，拒绝恢复")
    conflicts = read_json(conflict_file)
    if not isinstance(conflicts, list):
        raise SnapshotError("conflicts_missing", "conflicts.json 不是数组，拒绝恢复")

    return stitch(
        {
            "executable": executable,
            "conflicts": conflicts,
            "alignments_resolved": alignments,
            "block_plan": block_plan,
            "tables_by_id": tables_by_id,
        }
    )


class SnapshotError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
