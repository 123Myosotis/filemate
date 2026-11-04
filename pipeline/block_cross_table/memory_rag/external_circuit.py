# -*- coding: utf-8 -*-
"""
In-process case-lifetime sidecar for optional external query embed (D7=A).

Single-process memory only: restart / new sidecar clears circuit.
Not persisted to DB. Cross-case isolation by case_id.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CaseExternalEmbedState:
    attempted: bool = False
    used: bool = False
    skipped_reason: str | None = None
    circuit_open: bool = False
    embedding: list[float] | None = None
    model_id: str | None = None
    embedding_dim: int | None = None
    elapsed_ms: float = 0.0
    external_calls: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    def audit_dict(self) -> dict[str, Any]:
        return {
            "external_attempted": bool(self.attempted),
            "external_used": bool(self.used),
            "skipped_reason": self.skipped_reason,
            "circuit_open": bool(self.circuit_open),
            "elapsed_ms": float(self.elapsed_ms),
            "model_id": self.model_id,
            "embedding_dim": self.embedding_dim,
            "external_calls": int(self.external_calls),
        }


class CaseExternalEmbedSidecar:
    """Process-local registry: one CaseExternalEmbedState per case_id."""

    def __init__(self) -> None:
        self._by_case: dict[str, CaseExternalEmbedState] = {}

    def state_for(self, case_id: str) -> CaseExternalEmbedState:
        key = str(case_id or "").strip() or "_anonymous"
        if key not in self._by_case:
            self._by_case[key] = CaseExternalEmbedState()
        return self._by_case[key]

    def reset(self) -> None:
        """Clear all case states (test / process-restart simulation)."""
        self._by_case.clear()

    def reset_case(self, case_id: str) -> None:
        key = str(case_id or "").strip() or "_anonymous"
        self._by_case.pop(key, None)


_default_sidecar: CaseExternalEmbedSidecar | None = None


def get_default_sidecar() -> CaseExternalEmbedSidecar:
    global _default_sidecar
    if _default_sidecar is None:
        _default_sidecar = CaseExternalEmbedSidecar()
    return _default_sidecar


def reset_default_sidecar() -> CaseExternalEmbedSidecar:
    """Replace process-default sidecar (simulates process restart)."""
    global _default_sidecar
    _default_sidecar = CaseExternalEmbedSidecar()
    return _default_sidecar
