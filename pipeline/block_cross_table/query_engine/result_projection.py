# -*- coding: utf-8 -*-
"""ResultProjection schema validation (not an executable catalog op)."""
from __future__ import annotations

from typing import Any

FAIL_LOCUS_PROJECTION = "projection"

_REQUIRED_COLUMN_KEYS = (
    "column_id",
    "display_name",
    "column_name_source",
    "source_step_id",
    "provenance",
)


class ResultProjectionError(ValueError):
    def __init__(self, message: str, *, fail_locus: str = FAIL_LOCUS_PROJECTION):
        super().__init__(message)
        self.fail_locus = fail_locus


def validate_result_projection(projection: dict[str, Any]) -> None:
    """Validate ResultProjection schema; not selectable as a plan step op."""
    if not isinstance(projection, dict):
        raise ResultProjectionError("result_projection must be object")
    columns = projection.get("columns")
    if not isinstance(columns, list) or not columns:
        raise ResultProjectionError("result_projection.columns must be non-empty list")
    for i, col in enumerate(columns):
        if not isinstance(col, dict):
            raise ResultProjectionError("column[%s] must be object" % i)
        for key in _REQUIRED_COLUMN_KEYS:
            if key not in col:
                raise ResultProjectionError("column[%s] missing %s" % (i, key))
        prov = col.get("provenance")
        if not isinstance(prov, dict) or (
            not prov.get("utterance_span") and not prov.get("portrait_path")
        ):
            raise ResultProjectionError(
                "column[%s] provenance needs utterance_span and/or portrait_path" % i
            )
