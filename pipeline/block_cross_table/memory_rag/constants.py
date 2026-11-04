# -*- coding: utf-8 -*-
"""Contract-aligned identifiers for memory_rag (M2)."""
from __future__ import annotations

# EmbeddingBackendSwitch
BACKEND_EMBEDDED = "embedded_model"
BACKEND_EXTERNAL = "external_api_openai_compatible"
BACKEND_CHOICES = frozenset({BACKEND_EMBEDDED, BACKEND_EXTERNAL})

# MemoryBindingRecord.lifecycle_status
STATUS_ACTIVE = "active"
STATUS_INVALIDATED = "invalidated"
STATUS_REVOKED = "revoked"
STATUS_CHOICES = frozenset({STATUS_ACTIVE, STATUS_INVALIDATED, STATUS_REVOKED})
STATUS_TERMINAL = frozenset({STATUS_INVALIDATED, STATUS_REVOKED})

# MemoryTraceKind (locked M1; used when emitting local traces)
TRACE_WRITE = "memory_write"
TRACE_RECALL = "memory_recall"
TRACE_CONFLICT = "memory_conflict"
TRACE_INVALIDATE = "memory_invalidate"
TRACE_REVOKE = "memory_revoke"
TRACE_REEMBED = "memory_reembed"
TRACE_BACKEND_SWITCH = "memory_backend_switch"
TRACE_EXTERNAL_CALL = "memory_external_call"
TRACE_C14_INJECT = "memory_c14_inject"

# Reembed queue status
REEMBED_QUEUED = "queued"
REEMBED_DONE = "done"
REEMBED_FAILED = "failed"

# Physical table names (schema identifiers)
TABLE_BINDING = "memory_binding_record"
TABLE_EMBEDDING_META = "memory_embedding_meta"
TABLE_REEMBED_QUEUE = "memory_reembed_queue"
TABLE_BACKEND_SWITCH = "memory_backend_switch"
TABLE_VEC = "memory_embedding_vec"

# Index names
IDX_BINDING_ACTIVE = "idx_memory_binding_active"

# MemoryRecallCandidate rejected[] reasons (M3 recall filter; locked for contract)
REJECT_LIFECYCLE_NOT_ACTIVE = "lifecycle_not_active"
REJECT_INCOMPATIBLE_META = "incompatible_meta"
REJECT_TOP_K_TRUNCATED = "top_k_truncated"

ALLOWLIST_MODE_EMPTY = "empty_exemption"
ALLOWLIST_MODE_ENFORCED = "enforced"

TABLE_CALL_COUNTER = "memory_external_call_counter"
TABLE_VECTOR_CACHE = "memory_vector_cache"

# M5-CPU-Merge: local jargon + dual-db conflict / FTS
TABLE_FILEMATE_JARGONS = "filemate_jargons"
TABLE_JARGON_CONFLICTS = "filemate_jargon_conflicts"
TABLE_JARGON_MINING_JOBS = "filemate_jargon_mining_jobs"
TABLE_JARGON_TERM_STATS = "filemate_jargon_term_stats"
TABLE_BINDINGS_FTS = "bindings_fts"
TABLE_SETTINGS_KV = "memory_rag_settings"

EXTERNAL_CALL_STATUS_OK = "ok"
EXTERNAL_CALL_STATUS_FAILED = "failed"
EXTERNAL_CALL_STATUS_CACHE_HIT = "cache_hit"

EMBEDDING_SCHEMA_VERSION_M3 = "m3.v1"

# Dedup disposition after meaning inference (not business vocabulary)
DEDUP_SILENT_SKIP = "silent_skip"
DEDUP_CONFLICT = "conflict"
DEDUP_PENDING_CONFIRM = "pending_confirm"

CONFLICT_STATUS_OPEN = "open"
CONFLICT_STATUS_KEEP_MAIBOT = "keep_maibot"
CONFLICT_STATUS_KEEP_FILEMATE = "keep_filemate"
CONFLICT_STATUS_COEXIST = "coexist"
CONFLICT_STATUS_DISMISSED = "dismissed"

SETTING_MAIBOT_DATA_DIR = "maibot_data_dir"

# Proposal source_type (UI label; not business vocabulary)
SOURCE_TXT_MINING = "TXT_MINING"
SOURCE_MAIBOT_CONFLICT = "MAIBOT_CONFLICT"
SOURCE_WIZARD = "WIZARD"
SOURCE_MANUAL = "MANUAL"
SOURCE_DIALOGUE_REVIEW = "DIALOGUE_REVIEW"
SOURCE_CHOICES = frozenset(
    {
        SOURCE_TXT_MINING,
        SOURCE_MAIBOT_CONFLICT,
        SOURCE_WIZARD,
        SOURCE_MANUAL,
        SOURCE_DIALOGUE_REVIEW,
    }
)

# M6 dialogue + egress
TABLE_DIALOGUE_SESSION = "memory_dialogue_session"
TABLE_DIALOGUE_TURN = "memory_dialogue_turn"
TABLE_EGRESS_AUDIT = "memory_egress_audit"
EGRESS_ALLOW_EXTERNAL = "allow_external"
EGRESS_LOCAL_ONLY = "local_only"
EGRESS_POLICY_CHOICES = frozenset({EGRESS_ALLOW_EXTERNAL, EGRESS_LOCAL_ONLY})
DIALOGUE_SESSION_OPEN = "open"
DIALOGUE_SESSION_REVIEWING = "reviewing"
DIALOGUE_SESSION_DONE = "done"
DIALOGUE_SESSION_FAILED = "failed"
TRACE_DIALOGUE_REVIEW = "memory_dialogue_review"
TRACE_EGRESS_BLOCKED = "memory_egress_blocked"

# M5.2 self-learning engine (vector = local screen; chat = MaiBot-style)
ENGINE_VECTOR = "vector"
ENGINE_CHAT = "chat"
ENGINE_CHOICES = frozenset({ENGINE_VECTOR, ENGINE_CHAT})

# Meaning equivalence mode (vector engine final step; chat always AI-first)
MEANING_EQ_AI = "ai"
MEANING_EQ_LOCAL = "local"
MEANING_EQ_CHOICES = frozenset({MEANING_EQ_AI, MEANING_EQ_LOCAL})

# Trace kinds for merge batch (audit only; M1 locked kinds unchanged)
TRACE_JARGON_DEDUP = "memory_jargon_dedup"
TRACE_JARGON_MINING = "memory_jargon_mining"
TRACE_HYBRID_RECALL = "memory_hybrid_recall"
