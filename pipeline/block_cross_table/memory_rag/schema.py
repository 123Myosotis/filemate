# -*- coding: utf-8 -*-
"""SQLite schema for memory_rag (M2 core + M5-CPU-Merge extensions)."""
from __future__ import annotations

import sqlite3

from pipeline.block_cross_table.memory_rag.constants import (
    CONFLICT_STATUS_COEXIST,
    CONFLICT_STATUS_DISMISSED,
    CONFLICT_STATUS_KEEP_FILEMATE,
    CONFLICT_STATUS_KEEP_MAIBOT,
    CONFLICT_STATUS_OPEN,
    IDX_BINDING_ACTIVE,
    STATUS_ACTIVE,
    STATUS_INVALIDATED,
    STATUS_REVOKED,
    TABLE_BACKEND_SWITCH,
    TABLE_BINDING,
    TABLE_CALL_COUNTER,
    TABLE_DIALOGUE_SESSION,
    TABLE_DIALOGUE_TURN,
    TABLE_EGRESS_AUDIT,
    TABLE_EMBEDDING_META,
    TABLE_FILEMATE_JARGONS,
    TABLE_JARGON_CONFLICTS,
    TABLE_JARGON_MINING_JOBS,
    TABLE_JARGON_TERM_STATS,
    TABLE_REEMBED_QUEUE,
    TABLE_SETTINGS_KV,
    TABLE_VECTOR_CACHE,
)


def apply_core_schema(conn: sqlite3.Connection) -> None:
    """Create core tables, indexes, and lifecycle triggers (idempotent)."""
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE_EMBEDDING_META} (
            meta_id TEXT PRIMARY KEY,
            model_id TEXT NOT NULL,
            endpoint_fingerprint TEXT NOT NULL,
            embedding_schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS {TABLE_BINDING} (
            binding_id TEXT PRIMARY KEY,
            utterance_span_fingerprint TEXT NOT NULL,
            binding_payload TEXT NOT NULL,
            source_trace_id TEXT NOT NULL,
            confirmed_at TEXT NOT NULL,
            scope TEXT NOT NULL,
            lifecycle_status TEXT NOT NULL
                CHECK (lifecycle_status IN (
                    '{STATUS_ACTIVE}',
                    '{STATUS_INVALIDATED}',
                    '{STATUS_REVOKED}'
                )),
            embedding_meta_ref TEXT,
            FOREIGN KEY (embedding_meta_ref)
                REFERENCES {TABLE_EMBEDDING_META}(meta_id)
        );

        CREATE INDEX IF NOT EXISTS {IDX_BINDING_ACTIVE}
            ON {TABLE_BINDING}(lifecycle_status)
            WHERE lifecycle_status = '{STATUS_ACTIVE}';

        CREATE TABLE IF NOT EXISTS {TABLE_REEMBED_QUEUE} (
            queue_id TEXT PRIMARY KEY,
            binding_id TEXT NOT NULL,
            old_meta_id TEXT,
            reason TEXT NOT NULL,
            enqueued_at TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ('queued', 'done', 'failed')),
            FOREIGN KEY (binding_id) REFERENCES {TABLE_BINDING}(binding_id),
            FOREIGN KEY (old_meta_id) REFERENCES {TABLE_EMBEDDING_META}(meta_id)
        );

        CREATE TABLE IF NOT EXISTS {TABLE_BACKEND_SWITCH} (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            backend TEXT NOT NULL,
            chosen_at TEXT NOT NULL,
            chosen_source_trace_id TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS {TABLE_CALL_COUNTER} (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            call_count INTEGER NOT NULL DEFAULT 0
        );

        INSERT OR IGNORE INTO {TABLE_CALL_COUNTER}(singleton, call_count)
        VALUES (1, 0);

        CREATE TABLE IF NOT EXISTS {TABLE_VECTOR_CACHE} (
            cache_key TEXT PRIMARY KEY,
            binding_id TEXT,
            payload_fingerprint TEXT NOT NULL,
            model_id TEXT NOT NULL,
            endpoint_fingerprint TEXT NOT NULL,
            embedding_schema_version TEXT NOT NULL,
            embedding_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS memory_edit_proposal (
            proposal_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ('proposed', 'confirmed', 'revoked')),
            source_type TEXT NOT NULL DEFAULT 'WIZARD'
        );

        CREATE TABLE IF NOT EXISTS memory_edit_trace (
            trace_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            body_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS memory_vec_row_map (
            rowid INTEGER PRIMARY KEY,
            binding_id TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS {TABLE_SETTINGS_KV} (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS {TABLE_FILEMATE_JARGONS} (
            jargon_id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            meaning TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            scope TEXT NOT NULL DEFAULT '',
            is_global INTEGER NOT NULL DEFAULT 0,
            binding_id TEXT,
            source TEXT NOT NULL DEFAULT 'confirmed',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(content, scope)
        );

        CREATE INDEX IF NOT EXISTS idx_filemate_jargons_content
            ON {TABLE_FILEMATE_JARGONS}(content);

        CREATE TABLE IF NOT EXISTS {TABLE_JARGON_CONFLICTS} (
            conflict_id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            maibot_meaning TEXT NOT NULL,
            filemate_meaning TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN (
                    '{CONFLICT_STATUS_OPEN}',
                    '{CONFLICT_STATUS_KEEP_MAIBOT}',
                    '{CONFLICT_STATUS_KEEP_FILEMATE}',
                    '{CONFLICT_STATUS_COEXIST}',
                    '{CONFLICT_STATUS_DISMISSED}'
                )),
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolution_note TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_jargon_conflicts_open
            ON {TABLE_JARGON_CONFLICTS}(status, created_at DESC);

        CREATE TABLE IF NOT EXISTS {TABLE_JARGON_MINING_JOBS} (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            source_name TEXT NOT NULL,
            term_count INTEGER NOT NULL DEFAULT 0,
            inferred_count INTEGER NOT NULL DEFAULT 0,
            silent_skip_count INTEGER NOT NULL DEFAULT 0,
            conflict_count INTEGER NOT NULL DEFAULT 0,
            pending_count INTEGER NOT NULL DEFAULT 0,
            detail_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS {TABLE_JARGON_TERM_STATS} (
            content TEXT PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 0,
            last_inference_count INTEGER NOT NULL DEFAULT 0,
            is_complete INTEGER NOT NULL DEFAULT 0,
            last_context_snippet TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS {TABLE_DIALOGUE_SESSION} (
            session_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            ended_at TEXT,
            detail_json TEXT NOT NULL DEFAULT '{{}}'
        );

        CREATE TABLE IF NOT EXISTS {TABLE_DIALOGUE_TURN} (
            turn_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES {TABLE_DIALOGUE_SESSION}(session_id)
        );

        CREATE INDEX IF NOT EXISTS idx_dialogue_turn_session
            ON {TABLE_DIALOGUE_TURN}(session_id, created_at);

        CREATE TABLE IF NOT EXISTS {TABLE_EGRESS_AUDIT} (
            audit_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            target_url TEXT NOT NULL,
            model_name TEXT NOT NULL,
            payload_chars INTEGER NOT NULL DEFAULT 0,
            payload_preview TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT '',
            detail_json TEXT NOT NULL DEFAULT '{{}}'
        );

        CREATE INDEX IF NOT EXISTS idx_egress_audit_created
            ON {TABLE_EGRESS_AUDIT}(created_at DESC);

        -- Forbid terminal -> active (same binding_id cannot be restored).
        CREATE TRIGGER IF NOT EXISTS memory_binding_no_reactivate
        BEFORE UPDATE OF lifecycle_status ON {TABLE_BINDING}
        FOR EACH ROW
        WHEN OLD.lifecycle_status IN ('{STATUS_INVALIDATED}', '{STATUS_REVOKED}')
         AND NEW.lifecycle_status = '{STATUS_ACTIVE}'
        BEGIN
            SELECT RAISE(
                ABORT,
                'lifecycle_status cannot return to active on same binding_id'
            );
        END;
        """
    )
    conn.commit()
    _ensure_proposal_source_type(conn)
    from pipeline.block_cross_table.memory_rag.fts_index import ensure_bindings_fts

    ensure_bindings_fts(conn)


def _ensure_proposal_source_type(conn: sqlite3.Connection) -> None:
    """M5.1: add source_type to existing DBs (idempotent)."""
    cols = {
        str(r[1])
        for r in conn.execute("PRAGMA table_info(memory_edit_proposal)").fetchall()
    }
    if "source_type" not in cols:
        conn.execute(
            """
            ALTER TABLE memory_edit_proposal
            ADD COLUMN source_type TEXT NOT NULL DEFAULT 'WIZARD'
            """
        )
        conn.commit()
