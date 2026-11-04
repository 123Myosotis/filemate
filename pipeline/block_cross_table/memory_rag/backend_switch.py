# -*- coding: utf-8 -*-
"""EmbeddingBackendSwitch + code-level egress gate (not prompt-based)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from pipeline.block_cross_table.memory_rag.constants import (
    BACKEND_CHOICES,
    BACKEND_EMBEDDED,
    BACKEND_EXTERNAL,
    TABLE_BACKEND_SWITCH,
    TRACE_BACKEND_SWITCH,
    TRACE_EXTERNAL_CALL,
)
from pipeline.block_cross_table.memory_rag.errors import (
    LOCUS_BACKEND_GATE,
    LOCUS_EMBEDDED_UNAVAILABLE,
    MemoryRagError,
)


@dataclass(frozen=True)
class BackendSwitchState:
    backend: str
    chosen_at: str
    chosen_source_trace_id: str


def set_backend_switch(
    conn: sqlite3.Connection,
    *,
    backend: str,
    chosen_at: str,
    chosen_source_trace_id: str,
) -> list[dict[str, Any]]:
    """Persist switch (singleton). Returns local trace events."""
    if backend not in BACKEND_CHOICES:
        raise MemoryRagError(
            "unknown embedding backend",
            fail_locus=LOCUS_BACKEND_GATE,
            subreason="unknown_backend",
            detail={"backend": backend},
        )
    conn.execute(
        f"""
        INSERT INTO {TABLE_BACKEND_SWITCH}
            (singleton, backend, chosen_at, chosen_source_trace_id)
        VALUES (1, ?, ?, ?)
        ON CONFLICT(singleton) DO UPDATE SET
            backend = excluded.backend,
            chosen_at = excluded.chosen_at,
            chosen_source_trace_id = excluded.chosen_source_trace_id
        """,
        (backend, chosen_at, chosen_source_trace_id),
    )
    conn.commit()
    return [
        {
            "kind": TRACE_BACKEND_SWITCH,
            "backend": backend,
            "chosen_at": chosen_at,
            "chosen_source_trace_id": chosen_source_trace_id,
        }
    ]


def get_backend_switch(conn: sqlite3.Connection) -> BackendSwitchState | None:
    row = conn.execute(
        f"""
        SELECT backend, chosen_at, chosen_source_trace_id
        FROM {TABLE_BACKEND_SWITCH}
        WHERE singleton = 1
        """
    ).fetchone()
    if row is None:
        return None
    return BackendSwitchState(
        backend=row[0],
        chosen_at=row[1],
        chosen_source_trace_id=row[2],
    )


def require_backend_chosen(conn: sqlite3.Connection) -> BackendSwitchState:
    state = get_backend_switch(conn)
    if state is None:
        raise MemoryRagError(
            "embedding backend not chosen",
            fail_locus=LOCUS_BACKEND_GATE,
            subreason="backend_unset",
        )
    return state


def assert_external_call_allowed(conn: sqlite3.Connection) -> BackendSwitchState:
    """
    Code-level egress gate: external embeddings path only when switch is external.
    When switch is embedded_model → hard reject (fail_locus).
    """
    state = require_backend_chosen(conn)
    if state.backend == BACKEND_EMBEDDED:
        raise MemoryRagError(
            "external embeddings path rejected by backend gate",
            fail_locus=LOCUS_BACKEND_GATE,
            subreason="external_blocked_when_embedded",
            detail={"backend": state.backend},
        )
    if state.backend != BACKEND_EXTERNAL:
        raise MemoryRagError(
            "external embeddings path rejected by backend gate",
            fail_locus=LOCUS_BACKEND_GATE,
            subreason="backend_not_external",
            detail={"backend": state.backend},
        )
    return state


def assert_embedded_path_allowed(conn: sqlite3.Connection) -> BackendSwitchState:
    """
    M2: embedded path is batch-exempt / unimplemented → always hard-fail.
    Gate kept so UI disabled is not the only control.
    """
    state = require_backend_chosen(conn)
    if state.backend != BACKEND_EMBEDDED:
        raise MemoryRagError(
            "embedded path rejected: backend is not embedded_model",
            fail_locus=LOCUS_BACKEND_GATE,
            subreason="backend_not_embedded",
            detail={"backend": state.backend},
        )
    raise MemoryRagError(
        "embedded model path unavailable in this batch",
        fail_locus=LOCUS_EMBEDDED_UNAVAILABLE,
        subreason="embedded_batch_exempt",
        detail={"backend": state.backend},
    )


def invoke_external_embeddings_hook(
    conn: sqlite3.Connection,
    *,
    hook: Callable[..., Any],
    hook_kwargs: dict[str, Any] | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """
    Injection point for external OpenAI-compatible embeddings (M3 wires real HTTP).
    M2: gate only; hook is caller-supplied (tests inject a stub; no real network).
    """
    state = assert_external_call_allowed(conn)
    kwargs = dict(hook_kwargs or {})
    result = hook(**kwargs)
    traces = [
        {
            "kind": TRACE_EXTERNAL_CALL,
            "backend": state.backend,
            "chosen_source_trace_id": state.chosen_source_trace_id,
        }
    ]
    return result, traces
