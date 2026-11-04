# -*- coding: utf-8 -*-
"""Query-engine fail envelopes (QueryFailLocus)."""
from __future__ import annotations

from typing import Any


class QueryEngineError(ValueError):
    """Local mechanical failure with QueryFailLocus (+ optional subreason)."""

    def __init__(
        self,
        message: str,
        *,
        fail_locus: str,
        subreason: str | None = None,
        report_to_i: bool = True,
        detail: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.fail_locus = fail_locus
        self.subreason = subreason
        self.report_to_i = report_to_i
        self.detail = detail or {}

    def as_report(self) -> dict[str, Any]:
        return {
            "status": "failed",
            "fail_locus": self.fail_locus,
            "subreason": self.subreason,
            "report_to_i": self.report_to_i,
            "message": str(self),
            "detail": self.detail,
        }
