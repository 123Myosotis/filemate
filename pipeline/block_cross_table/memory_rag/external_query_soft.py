# -*- coding: utf-8 -*-
"""
Soft-fail wrapper for optional external query embedding (D7=A).

Never raises into hybrid_recall. Same case_id: embed once (reuse or circuit).
Budget: timeout=45s; retryable failures up to 2 retries (total attempts≤3, ≤135s); non-retryable immediate skip.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from pipeline.block_cross_table.memory_rag.errors import MemoryRagError
from pipeline.block_cross_table.memory_rag.constants import (
    EXTERNAL_CALL_STATUS_CACHE_HIT,
    TABLE_VECTOR_CACHE,
)
from pipeline.block_cross_table.memory_rag.external_call import (
    HttpTransport,
    embed_binding_external,
)
from pipeline.block_cross_table.memory_rag.external_circuit import (
    CaseExternalEmbedSidecar,
    CaseExternalEmbedState,
    get_default_sidecar,
)
from pipeline.block_cross_table.memory_rag.external_settings import (
    ExternalEmbeddingSettings,
    load_external_embedding_settings,
)
from pipeline.block_cross_table.memory_rag.meta import EmbeddingVectorMeta

# D7=A soft-path budget (not production hardcode of business; mechanical budget).
DEFAULT_SOFT_TIMEOUT_MS = 45000
DEFAULT_SOFT_MAX_RETRIES = 2  # extra retries after first → total attempts = 3 (≤135s)

# Immediate skip (no further soft retries).
_NON_RETRYABLE_HTTP = {401, 403, 429}
_NON_RETRYABLE_SUBREASONS = frozenset(
    {
        "response_dim_mismatch",
        "cache_dim_mismatch",
        "missing_api_key",
        "model_not_embedding_capable",
        "call_count_cap",
        "egress_keys",
    }
)


@dataclass(frozen=True)
class SoftExternalEmbedResult:
    embedding: list[float] | None
    meta: EmbeddingVectorMeta | None
    attempted: bool
    used: bool
    skipped_reason: str | None
    circuit_open: bool
    elapsed_ms: float
    external_calls: int
    model_id: str | None = None
    embedding_dim: int | None = None

    def audit_dict(self) -> dict[str, Any]:
        return {
            "external_attempted": self.attempted,
            "external_used": self.used,
            "skipped_reason": self.skipped_reason,
            "circuit_open": self.circuit_open,
            "elapsed_ms": self.elapsed_ms,
            "model_id": self.model_id,
            "embedding_dim": self.embedding_dim,
            "external_calls": self.external_calls,
        }


def _classify_http_body(status: int, raw: bytes) -> str | None:
    """Return skipped_reason for non-retryable body/status; else None."""
    if status in _NON_RETRYABLE_HTTP:
        return "http_%s" % status
    text = raw[:400].decode("utf-8", "replace") if raw else ""
    if "1113" in text or "余额不足" in text:
        return "balance_or_1113"
    if status < 200 or status >= 300:
        # 5xx / other → retryable (None here means soft layer may retry)
        if status in (408, 425, 500, 502, 503, 504):
            return None
        # 4xx other than listed → treat as immediate param/auth skip
        if 400 <= status < 500:
            return "http_%s" % status
    return None


def _counting_transport(
    inner: HttpTransport,
    counter: list[int],
    immediate_reason: list[str | None],
) -> HttpTransport:
    def _wrapped(
        url: str, headers: dict[str, str], body: bytes, timeout_s: float
    ) -> tuple[int, bytes]:
        counter[0] += 1
        try:
            status, raw = inner(url, headers, body, timeout_s)
        except Exception:
            raise
        reason = _classify_http_body(status, raw)
        if reason:
            immediate_reason[0] = reason
        return status, raw

    return _wrapped


def _mark_skip(state: CaseExternalEmbedState, reason: str, elapsed_ms: float) -> None:
    state.attempted = True
    state.used = False
    state.skipped_reason = reason
    state.circuit_open = True
    state.embedding = None
    state.elapsed_ms = float(elapsed_ms)


def _mark_ok(
    state: CaseExternalEmbedState,
    *,
    emb: list[float],
    meta: EmbeddingVectorMeta,
    elapsed_ms: float,
) -> None:
    state.attempted = True
    state.used = True
    state.skipped_reason = None
    state.circuit_open = False
    state.embedding = list(emb)
    state.model_id = meta.model_id
    state.embedding_dim = len(emb)
    state.elapsed_ms = float(elapsed_ms)
    state.meta = {
        "model_id": meta.model_id,
        "endpoint_fingerprint": meta.endpoint_fingerprint,
        "embedding_schema_version": meta.embedding_schema_version,
    }


def _result_from_state(state: CaseExternalEmbedState) -> SoftExternalEmbedResult:
    meta = None
    if state.used and state.meta:
        meta = EmbeddingVectorMeta(
            model_id=str(state.meta.get("model_id") or ""),
            endpoint_fingerprint=str(state.meta.get("endpoint_fingerprint") or ""),
            embedding_schema_version=str(
                state.meta.get("embedding_schema_version") or ""
            ),
        )
    return SoftExternalEmbedResult(
        embedding=list(state.embedding) if state.embedding else None,
        meta=meta,
        attempted=state.attempted,
        used=state.used,
        skipped_reason=state.skipped_reason,
        circuit_open=state.circuit_open,
        elapsed_ms=state.elapsed_ms,
        external_calls=state.external_calls,
        model_id=state.model_id,
        embedding_dim=state.embedding_dim,
    )


def clear_query_vector_cache(conn: sqlite3.Connection) -> int:
    """
    Clear DB query/binding vector cache rows (真机验收用).
    Does not touch sidecar memory. Returns deleted row count.
    """
    cur = conn.execute(f"DELETE FROM {TABLE_VECTOR_CACHE}")
    conn.commit()
    return int(cur.rowcount or 0)


def soft_embed_query_text(
    conn: sqlite3.Connection,
    *,
    text: str,
    case_id: str,
    sidecar: CaseExternalEmbedSidecar | None = None,
    settings: ExternalEmbeddingSettings | None = None,
    http_transport: HttpTransport | None = None,
    timeout_ms: int = DEFAULT_SOFT_TIMEOUT_MS,
    max_retries: int = DEFAULT_SOFT_MAX_RETRIES,
    call_counter: list[int] | None = None,
) -> SoftExternalEmbedResult:
    """
    Soft external query embed for one case_id lifecycle.

    - Success: cache embedding on sidecar; later recalls reuse (no transport).
    - Failure: open circuit; later recalls skip without transport.
    - Never raises MemoryRagError to caller.
    - DB vector cache is NOT read (use_cache=False): external_used means live
      transport success this case (or sidecar reuse of a prior live success).
      DB cache hit alone must never set external_used=true.
    """
    box = sidecar or get_default_sidecar()
    state = box.state_for(case_id)

    # Already resolved this case: reuse or circuit-skip (no new transport).
    if state.used and state.embedding is not None:
        return _result_from_state(state)
    if state.circuit_open or (state.attempted and not state.used):
        return _result_from_state(state)

    q = (text or "").strip()
    if not q:
        _mark_skip(state, "empty_query", 0.0)
        return _result_from_state(state)

    s = settings or load_external_embedding_settings()
    # Soft path: override budget for D7=A without mutating frozen settings object.
    soft_timeout = int(timeout_ms)
    soft_retries = max(0, int(max_retries))

    if not (s.api_key or "").strip():
        _mark_skip(state, "missing_external_api_key", 0.0)
        return _result_from_state(state)

    if http_transport is None:
        from pipeline.block_cross_table.memory_rag.external_call import (
            default_http_transport,
        )

        http_transport = default_http_transport

    counter = call_counter if call_counter is not None else [0]
    immediate: list[str | None] = [None]
    wrapped = _counting_transport(http_transport, counter, immediate)

    t0 = time.perf_counter()
    last_reason = "external_failed"
    # soft_retries = max extra attempts after first → total attempts = 1 + soft_retries
    attempts = 1 + soft_retries
    for attempt in range(1, attempts + 1):
        immediate[0] = None
        try:
            emb, meta, traces = embed_binding_external(
                conn,
                binding_id=None,
                binding_payload=q,
                base_url=s.base_url,
                api_key=s.api_key,
                model_id=s.model_id,
                embedding_schema_version=s.embedding_schema_version,
                allowlist=list(s.allowlist),
                call_count_cap=s.call_count_cap,
                timeout_ms=soft_timeout,
                max_retries=0,  # soft layer owns retry / immediate-skip
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                http_transport=wrapped,
                # embedding_dim<=0 → accept provider-returned length (no hardcode).
                expected_dim=(
                    int(s.embedding_dim) if int(s.embedding_dim or 0) > 0 else None
                ),
                use_cache=False,  # DB cache must not invent external_used
            )
            state.external_calls = int(counter[0])
            elapsed = (time.perf_counter() - t0) * 1000.0
            # Defense: refuse cache_hit status even if caller somehow enabled cache.
            if any(
                (t or {}).get("status") == EXTERNAL_CALL_STATUS_CACHE_HIT
                or (t or {}).get("cache_hit")
                for t in (traces or [])
            ):
                _mark_skip(state, "query_vector_cache_hit_refused", elapsed)
                return _result_from_state(state)
            if counter[0] < 1:
                # No live transport → cannot claim external_used.
                _mark_skip(state, "no_live_transport", elapsed)
                return _result_from_state(state)
            if not emb:
                _mark_skip(state, "empty_embedding", elapsed)
                return _result_from_state(state)
            _mark_ok(state, emb=emb, meta=meta, elapsed_ms=elapsed)
            return _result_from_state(state)
        except MemoryRagError as exc:
            state.external_calls = int(counter[0])
            sub = str(exc.subreason or "")
            detail = exc.detail or {}
            err = str(detail.get("error") or exc)
            if immediate[0]:
                elapsed = (time.perf_counter() - t0) * 1000.0
                _mark_skip(state, immediate[0], elapsed)
                return _result_from_state(state)
            if sub in _NON_RETRYABLE_SUBREASONS:
                elapsed = (time.perf_counter() - t0) * 1000.0
                _mark_skip(state, sub, elapsed)
                return _result_from_state(state)
            if "empty_embedding" in err or err == "empty_data":
                elapsed = (time.perf_counter() - t0) * 1000.0
                _mark_skip(state, "empty_embedding", elapsed)
                return _result_from_state(state)
            # Parse http_xxx from retries_exhausted detail
            if err.startswith("http_"):
                try:
                    code = int(err.split(":", 1)[0].split("_", 1)[1])
                except Exception:
                    code = 0
                reason = _classify_http_body(code, b"")
                if reason:
                    elapsed = (time.perf_counter() - t0) * 1000.0
                    _mark_skip(state, reason, elapsed)
                    return _result_from_state(state)
                # retryable http in detail
                last_reason = err.split(":", 1)[0]
            else:
                last_reason = sub or "external_failed"
            if attempt >= attempts:
                break
            continue
        except Exception as exc:  # noqa: BLE001 — soft path
            state.external_calls = int(counter[0])
            name = type(exc).__name__
            last_reason = "timeout" if "timeout" in name.lower() or "Timeout" in name else name
            if attempt >= attempts:
                break
            continue

    elapsed = (time.perf_counter() - t0) * 1000.0
    state.external_calls = int(counter[0])
    _mark_skip(state, last_reason if last_reason != "retries_exhausted" else "timeout_retries_exhausted", elapsed)
    return _result_from_state(state)
