"""§4 / §8 安全边界。比较系数与轴名是几何原语，不是业务档位。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LayoutBounds:
    """§4.0 结构密度比较系数。调用方可不传，用这些安全边界。"""

    sparse_row_density: float = 0.5
    field_header_min_density: float = 0.5
    dense_header_density: float = 0.85
    label_vs_data_len_ratio: float = 1.25
    max_top_sparse_band: int = 1
    merge_top_ratio_exclusive_max: float = 0.5


@dataclass(frozen=True)
class SerializeSpec:
    """§3.2 / §3.4：单元格之间、行之间的序列化分隔。与 estimate_tokens 同一 blob。"""

    cell_sep: str = "\t"
    row_sep: str = "\n"


# 五种切轴，封闭几何原语（§8）
AXIS_NONE = "none"
AXIS_ROW = "row"
AXIS_COLUMN = "column"
AXIS_REGION = "region"
AXIS_MIXED = "mixed"

AXIS_ORDER = {
    AXIS_NONE: 0,
    AXIS_ROW: 1,
    AXIS_COLUMN: 2,
    AXIS_REGION: 3,
    AXIS_MIXED: 4,
}

LAYOUT_ORDERED = "ordered"
LAYOUT_MATRIX = "matrix"

ERR_BUDGET_NON_POSITIVE = "budget_non_positive"
ERR_HEADER_EXCEEDS = "header_exceeds_budget"
ERR_ROW_WITH_HEADER = "row_with_header_exceeds_budget"
ERR_COLUMN_EXCEEDS = "column_exceeds_budget"
