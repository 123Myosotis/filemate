"""§4.0 classify_layout。判定即最终判定，运行时不纠正（§4.0.1）。"""

from pipeline.block_plan.policy import (
    LAYOUT_MATRIX,
    LAYOUT_ORDERED,
    LayoutBounds,
)


def _avg_len(row):
    nonempty = [c for c in row if c]
    if not nonempty:
        return 0.0
    return sum(len(c) for c in nonempty) / len(nonempty)


def classify_layout(grid, merges, bounds: LayoutBounds = None):
    """返回 (layout, metrics)。merges 为半开矩形 (r0, c0, r1, c1)。"""
    bounds = bounds or LayoutBounds()
    rows = grid or []
    r_count = len(rows)
    c_count = max((len(r) for r in rows), default=0)
    metrics = {
        "row0_density": 0.0,
        "top_sparse_band": 0,
        "merge_top_ratio": 0.0,
        "avg_len_row0": 0.0,
        "avg_len_row1": None,
        "looks_like_field_header": False,
        "rows": r_count,
        "cols": c_count,
    }
    if r_count == 0 or c_count == 0:
        return LAYOUT_MATRIX, metrics

    width_rows = [list(r) + [""] * (c_count - len(r)) for r in rows]

    def density(row):
        return sum(1 for c in row if c) / c_count

    row0 = width_rows[0]
    row0_density = density(row0)
    top_sparse_band = 0
    for row in width_rows:
        if density(row) < bounds.sparse_row_density:
            top_sparse_band += 1
        else:
            break

    top_end = max(1, top_sparse_band)
    merges = list(merges or [])
    touching = 0
    for rect in merges:
        r0, _c0, r1, _c1 = rect[0], rect[1], rect[2], rect[3]
        # 半开区间与 [0, top_end) 相交
        if r0 < top_end and r1 > 0:
            touching += 1
    merge_top_ratio = touching / max(1, len(merges)) if merges else 0.0

    avg0 = _avg_len(row0)
    avg1 = None
    if r_count > 1:
        avg1 = _avg_len(width_rows[1])
    looks = row0_density >= bounds.field_header_min_density and (
        r_count == 1
        or avg0 <= (avg1 or 0.0) * bounds.label_vs_data_len_ratio
        or row0_density >= bounds.dense_header_density
    )
    layout = LAYOUT_MATRIX
    if (
        looks
        and merge_top_ratio < bounds.merge_top_ratio_exclusive_max
        and top_sparse_band <= bounds.max_top_sparse_band
    ):
        layout = LAYOUT_ORDERED

    metrics.update(
        {
            "row0_density": row0_density,
            "top_sparse_band": top_sparse_band,
            "merge_top_ratio": merge_top_ratio,
            "avg_len_row0": avg0,
            "avg_len_row1": avg1,
            "looks_like_field_header": looks,
        }
    )
    return layout, metrics
