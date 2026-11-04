# -*- coding: utf-8 -*-
"""Memory-rag fail envelopes (fail_locus for local mechanical failures)."""
from __future__ import annotations

from typing import Any


class MemoryRagError(ValueError):
    """Local mechanical failure with an explicit fail locus."""

    def __init__(
        self,
        message: str,
        *,
        fail_locus: str,
        subreason: str | None = None,
        detail: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.fail_locus = fail_locus
        self.subreason = subreason
        self.detail = detail or {}

    def as_report(self) -> dict[str, Any]:
        return {
            "status": "failed",
            "fail_locus": self.fail_locus,
            "subreason": self.subreason,
            "message": str(self),
            "detail": self.detail,
        }


# Fail locus tokens (code identifiers; not business words).
LOCUS_WRITE_UNCONFIRMED = "memory_write_unconfirmed"
LOCUS_LIFECYCLE_ILLEGAL = "memory_lifecycle_illegal"
LOCUS_META_INCOMPATIBLE = "memory_meta_incompatible"
LOCUS_BACKEND_GATE = "memory_backend_gate"
LOCUS_EMBEDDED_UNAVAILABLE = "memory_embedded_unavailable"
LOCUS_DIM_MISMATCH = "memory_embedding_dim_mismatch"
LOCUS_PRODUCTION_NUMERIC_NULL = "memory_production_numeric_null"
LOCUS_VEC_SETUP = "memory_vec_setup"
LOCUS_EXTERNAL_CALL_FAILED = "memory_external_call_failed"
LOCUS_MAIBOT_RO = "memory_maibot_ro"
LOCUS_JARGON_INFERENCE = "memory_jargon_inference"
LOCUS_HYBRID_RECALL = "memory_hybrid_recall"
LOCUS_TXT_IMPORT = "memory_txt_import"
