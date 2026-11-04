# -*- coding: utf-8 -*-
"""
M3 external OpenAI-compatible embeddings call:
timeout / retry / cache / call_count / allowlist / content boundary.
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Sequence
from urllib.parse import urljoin

from pipeline.block_cross_table.memory_rag.allowlist import require_endpoint_allowlisted
from pipeline.block_cross_table.memory_rag.backend_switch import assert_external_call_allowed
from pipeline.block_cross_table.memory_rag.constants import (
    EXTERNAL_CALL_STATUS_CACHE_HIT,
    EXTERNAL_CALL_STATUS_FAILED,
    EXTERNAL_CALL_STATUS_OK,
    TABLE_CALL_COUNTER,
    TABLE_VECTOR_CACHE,
    TRACE_EXTERNAL_CALL,
)
from pipeline.block_cross_table.memory_rag.errors import (
    LOCUS_EXTERNAL_CALL_FAILED,
    MemoryRagError,
)
from pipeline.block_cross_table.memory_rag.meta import EmbeddingVectorMeta
from pipeline.block_cross_table.memory_rag.payload_text import (
    binding_payload_to_text,
    cache_key_for,
    payload_fingerprint,
)

HttpTransport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


def endpoint_fingerprint(base_url: str) -> str:
    return str(base_url or "").strip().rstrip("/").lower()


def _embeddings_url(base_url: str) -> str:
    base = str(base_url or "").strip()
    if not base.endswith("/"):
        base = base + "/"
    return urljoin(base, "embeddings")


def default_http_transport(
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout_s: float,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        return int(exc.code), raw


def get_call_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        f"SELECT call_count FROM {TABLE_CALL_COUNTER} WHERE singleton=1"
    ).fetchone()
    return int(row[0]) if row else 0


def _bump_call_count(conn: sqlite3.Connection) -> int:
    conn.execute(
        f"""
        UPDATE {TABLE_CALL_COUNTER}
        SET call_count = call_count + 1
        WHERE singleton = 1
        """
    )
    conn.commit()
    return get_call_count(conn)


def _require_under_cap(conn: sqlite3.Connection, *, call_count_cap: int) -> None:
    current = get_call_count(conn)
    if current >= int(call_count_cap):
        raise MemoryRagError(
            "external call count cap reached",
            fail_locus=LOCUS_EXTERNAL_CALL_FAILED,
            subreason="call_count_cap",
            detail={"call_count": current, "cap": int(call_count_cap)},
        )


def _load_cache(
    conn: sqlite3.Connection,
    *,
    cache_key: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        f"""
        SELECT embedding_json, binding_id, payload_fingerprint
        FROM {TABLE_VECTOR_CACHE}
        WHERE cache_key=?
        """,
        (cache_key,),
    ).fetchone()
    if row is None:
        return None
    return {
        "embedding": json.loads(row[0]),
        "binding_id": row[1],
        "payload_fingerprint": row[2],
    }


def _store_cache(
    conn: sqlite3.Connection,
    *,
    cache_key: str,
    binding_id: str | None,
    payload_fp: str,
    meta: EmbeddingVectorMeta,
    embedding: list[float],
    created_at: str,
) -> None:
    conn.execute(
        f"""
        INSERT INTO {TABLE_VECTOR_CACHE} (
            cache_key, binding_id, payload_fingerprint,
            model_id, endpoint_fingerprint, embedding_schema_version,
            embedding_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
            embedding_json=excluded.embedding_json,
            binding_id=excluded.binding_id,
            created_at=excluded.created_at
        """,
        (
            cache_key,
            binding_id,
            payload_fp,
            meta.model_id,
            meta.endpoint_fingerprint,
            meta.embedding_schema_version,
            json.dumps(embedding),
            created_at,
        ),
    )
    conn.commit()


def build_egress_body(
    *,
    model_id: str,
    binding_payload: Any,
) -> dict[str, Any]:
    """Content boundary: only model + binding_payload text as input."""
    return {
        "model": model_id,
        "input": binding_payload_to_text(binding_payload),
    }


def embed_binding_external(
    conn: sqlite3.Connection,
    *,
    binding_id: str | None,
    binding_payload: Any,
    base_url: str,
    api_key: str,
    model_id: str,
    embedding_schema_version: str,
    allowlist: Sequence[str] | None,
    call_count_cap: int,
    timeout_ms: int,
    max_retries: int,
    created_at: str,
    http_transport: HttpTransport | None = None,
    expected_dim: int | None = None,
    use_cache: bool = True,
) -> tuple[list[float], EmbeddingVectorMeta, list[dict[str, Any]]]:
    """
    Per-binding external embeddings call (M3).
    Does not send scope / utterance / full tables — only binding_payload text.
    use_cache=False: skip DB vector cache read (soft recall / 真机验收须 live).
    """
    assert_external_call_allowed(conn)
    allowlist_mode = require_endpoint_allowlisted(base_url, allowlist)
    ep_fp = endpoint_fingerprint(base_url)
    meta = EmbeddingVectorMeta(
        model_id=str(model_id),
        endpoint_fingerprint=ep_fp,
        embedding_schema_version=str(embedding_schema_version),
    )
    ckey = cache_key_for(
        binding_payload=binding_payload,
        model_id=meta.model_id,
        endpoint_fingerprint=meta.endpoint_fingerprint,
        embedding_schema_version=meta.embedding_schema_version,
    )
    cached = _load_cache(conn, cache_key=ckey) if use_cache else None
    if cached is not None:
        emb = list(cached["embedding"])
        if expected_dim is not None and len(emb) != int(expected_dim):
            raise MemoryRagError(
                "cached embedding dim mismatch; refuse silent use",
                fail_locus=LOCUS_EXTERNAL_CALL_FAILED,
                subreason="cache_dim_mismatch",
                detail={"binding_id": binding_id, "len": len(emb)},
            )
        traces = [
            {
                "kind": TRACE_EXTERNAL_CALL,
                "status": EXTERNAL_CALL_STATUS_CACHE_HIT,
                "binding_id": binding_id,
                "model_id": meta.model_id,
                "endpoint_fingerprint": meta.endpoint_fingerprint,
                "allowlist_mode": allowlist_mode,
                "call_count": get_call_count(conn),
                "cache_hit": True,
            }
        ]
        return emb, meta, traces

    _require_under_cap(conn, call_count_cap=call_count_cap)

    body_obj = build_egress_body(model_id=model_id, binding_payload=binding_payload)
    # Harden content boundary: only these keys may leave.
    if set(body_obj.keys()) != {"model", "input"}:
        raise MemoryRagError(
            "egress body keys illegal",
            fail_locus=LOCUS_EXTERNAL_CALL_FAILED,
            subreason="egress_keys",
        )
    body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    url = _embeddings_url(base_url)
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % api_key,
    }
    # M6 egress: local_only hard block + audit (token never logged).
    from pipeline.block_cross_table.memory_rag.egress_audit import (
        assert_external_egress_allowed,
        redact_preview,
        record_egress,
    )

    assert_external_egress_allowed(channel="embed")
    timeout_s = max(float(timeout_ms), 1.0) / 1000.0
    transport = http_transport or default_http_transport
    attempts = 1 + int(max_retries)
    last_err: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            _require_under_cap(conn, call_count_cap=call_count_cap)
            status, raw = transport(url, headers, body, timeout_s)
            try:
                record_egress(
                    target_url=url,
                    model_name=str(model_id),
                    payload_chars=len(body),
                    payload_preview=redact_preview(
                        body_obj.get("input") if isinstance(body_obj, dict) else "",
                        limit=240,
                    ),
                    status="ok" if 200 <= status < 300 else "failed",
                    channel="embed",
                    detail={"http_status": status, "attempt": attempt},
                )
            except Exception:
                pass
            # Network attempt counts toward fuse (cache hits do not).
            new_count = _bump_call_count(conn)
            if status < 200 or status >= 300:
                last_err = "http_%s:%s" % (status, raw[:200].decode("utf-8", "replace"))
                # Rate-limit / transient: backoff before next attempt.
                if status in (408, 425, 429, 500, 502, 503, 504) and attempt < attempts:
                    # Exponential backoff; 429 waits longer (provider rate limit).
                    wait_s = min(2.0 ** attempt, 45.0)
                    if status == 429:
                        wait_s = max(wait_s, 8.0 * float(attempt))
                    time.sleep(wait_s)
                continue
            parsed = json.loads(raw.decode("utf-8"))
            data = parsed.get("data") or []
            if not data:
                last_err = "empty_data"
                continue
            emb = list(data[0].get("embedding") or [])
            if not emb:
                last_err = "empty_embedding"
                continue
            if expected_dim is not None and len(emb) != int(expected_dim):
                raise MemoryRagError(
                    "embedding dim mismatch; refuse store",
                    fail_locus=LOCUS_EXTERNAL_CALL_FAILED,
                    subreason="response_dim_mismatch",
                    detail={"binding_id": binding_id, "len": len(emb)},
                )
            _store_cache(
                conn,
                cache_key=ckey,
                binding_id=binding_id,
                payload_fp=payload_fingerprint(binding_payload),
                meta=meta,
                embedding=emb,
                created_at=created_at,
            )
            traces = [
                {
                    "kind": TRACE_EXTERNAL_CALL,
                    "status": EXTERNAL_CALL_STATUS_OK,
                    "binding_id": binding_id,
                    "model_id": meta.model_id,
                    "endpoint_fingerprint": meta.endpoint_fingerprint,
                    "allowlist_mode": allowlist_mode,
                    "call_count": new_count,
                    "attempt": attempt,
                    "cache_hit": False,
                }
            ]
            return emb, meta, traces
        except MemoryRagError:
            raise
        except Exception as exc:  # noqa: BLE001 — map to failed locus
            last_err = str(exc)
            try:
                _bump_call_count(conn)
            except Exception:  # noqa: BLE001
                pass

    raise MemoryRagError(
        "external embeddings call failed after retries",
        fail_locus=LOCUS_EXTERNAL_CALL_FAILED,
        subreason="retries_exhausted",
        detail={
            "binding_id": binding_id,
            "attempts": attempts,
            "error": last_err,
            "status": EXTERNAL_CALL_STATUS_FAILED,
            "allowlist_mode": allowlist_mode,
            "call_count": get_call_count(conn),
            "model_id": meta.model_id,
            "endpoint_fingerprint": meta.endpoint_fingerprint,
        },
    )
