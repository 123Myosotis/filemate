# -*- coding: utf-8 -*-
"""
Mid-representation types. raw / filled / normalized stay separate.
No semantic inference — fields are carriers for AI output + mechanical checks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .scope import Scope


@dataclass
class CellIR:
    scope: Scope
    raw_value: Any = None
    filled_value: Any = None
    fill_source_coord: str = ""
    normalized_value: Any = None
    normalize_spec_id: str = ""
    color_raw: str = ""
    color_theme: str = ""
    legend_text: str = ""
    comment: str = ""
    type_declared: str = ""
    is_subtotal: bool = False
    is_blank: bool = False
    catalog_id: str = ""
    ai_binding_id: str = ""
    confidence: Optional[float] = None
    ambiguous: bool = False
    header_path: List[str] = field(default_factory=list)
    orig_col_name: str = ""
    orig_row_name: str = ""
    merged_range: str = ""

    def display_value(self) -> Any:
        """Prefer filled then raw. Never invent."""
        if self.filled_value is not None and str(self.filled_value) != "":
            return self.filled_value
        return self.raw_value


@dataclass
class CatalogEntry:
    catalog_id: str
    scope: Scope
    field_name: str
    orig_col_name: str = ""
    header_path: List[str] = field(default_factory=list)
    physical_coords: List[str] = field(default_factory=list)


@dataclass
class Binding:
    scope: Scope
    source_file: str
    source_sheet: str
    header_path: List[str]
    orig_col_name: str
    cell_coord_or_range: str
    target_field_id: str
    target_field_name: str
    confidence: float
    ambiguous_reason: str = ""
    alternatives: List[Any] = field(default_factory=list)
    ai_binding_id: str = ""


@dataclass
class JoinSpec:
    scope: Scope
    left_scope: Scope
    right_scope: Scope
    left_catalog_id: str
    right_catalog_id: str
    join_type: str
    expected_cardinality: str
    grain: str
    match: str = "exact"


@dataclass
class DslStep:
    step_id: str
    op: str
    in_scope: Scope
    out_scope: Scope
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GlobalJudgment:
    judgment_id: str
    global_judgment_type: str
    based_on: str
    confidence: float
    on_low_confidence: str
    declared_scope: Scope
    anchor: Dict[str, Any]
    evidence_coords: List[str] = field(default_factory=list)
    summary_hash: str = ""
    sample_manifest_id: str = ""


@dataclass
class RowCoverRecord:
    """P0 A6: every source row is processed | excluded | discarded."""
    scope: Scope
    source_row_id: str
    disposition: str
    reason_id: str = ""
    output_row_id: str = ""


@dataclass
class NormalizeSpec:
    """P0 A4: mechanical normalize steps, versioned."""
    spec_id: str
    scope: Scope
    ops: List[str]
    null_policy: str
    null_tokens: List[str] = field(default_factory=list)
    version: str = ""


@dataclass
class TraceLink:
    """P0 A1: output cell → input coords + binding + mechanical op."""
    scope: Scope
    output_coord: str
    ai_binding_id: str
    input_coords: List[str]
    op_id: str
    catalog_id: str = ""
