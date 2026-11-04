# -*- coding: utf-8 -*-
"""Memory RAG (M2–M5 + M5-CPU-Merge hybrid/jargon)."""
from __future__ import annotations

from pipeline.block_cross_table.memory_rag.allowlist import (
    evaluate_endpoint_allowlist,
    require_endpoint_allowlisted,
)
from pipeline.block_cross_table.memory_rag.backend_switch import (
    assert_embedded_path_allowed,
    assert_external_call_allowed,
    get_backend_switch,
    invoke_external_embeddings_hook,
    set_backend_switch,
)
from pipeline.block_cross_table.memory_rag.bootstrap import open_memory_db
from pipeline.block_cross_table.memory_rag.c14_inject import (
    inject_c14_all_targets,
    inject_c14_reference,
)
from pipeline.block_cross_table.memory_rag.conflict import (
    build_memory_conflict_package,
    detect_multi_binding_conflict,
)
from pipeline.block_cross_table.memory_rag.errors import MemoryRagError
from pipeline.block_cross_table.memory_rag.external_call import (
    build_egress_body,
    embed_binding_external,
    get_call_count,
)
from pipeline.block_cross_table.memory_rag.external_settings import (
    ExternalEmbeddingSettings,
    load_external_embedding_settings,
    require_embedding_capable_model,
)
from pipeline.block_cross_table.memory_rag.fts_index import (
    backfill_bindings_fts,
    ensure_bindings_fts,
    sparse_search_bindings,
)
from pipeline.block_cross_table.memory_rag.hybrid_recall import hybrid_recall, merge_jargon_corpora
from pipeline.block_cross_table.memory_rag.jargon_dedup import (
    apply_dedup_after_inference,
    list_open_conflicts,
    resolve_conflict,
)
from pipeline.block_cross_table.memory_rag.jargon_inference import infer_jargon_meaning
from pipeline.block_cross_table.memory_rag.jargon_miner import (
    import_txt_and_mine,
    list_mining_jobs,
    mining_status_summary,
    should_infer_meaning,
)
from pipeline.block_cross_table.memory_rag.jargon_store import (
    confirm_jargon_with_binding,
    list_filemate_jargons,
    upsert_filemate_jargon,
)
from pipeline.block_cross_table.memory_rag.maibot_ro import (
    discover_maibot_db_path,
    fetch_jargon_by_content,
    open_maibot_ro,
)
from pipeline.block_cross_table.memory_rag.merge_settings import (
    load_merge_runtime_settings,
    resolve_maibot_data_dir,
    save_maibot_data_dir,
)
from pipeline.block_cross_table.memory_rag.meta import (
    EmbeddingVectorMeta,
    metas_mechanically_compatible,
    require_compatible_metas,
)
from pipeline.block_cross_table.memory_rag.production_slots import (
    load_memory_numeric_slots_from_current,
    require_production_embedding_dim,
    require_production_recall_top_k,
)
from pipeline.block_cross_table.memory_rag.qp_memory_inject import (
    normalize_recalled_memories,
    recall_memories_for_qp,
)
from pipeline.block_cross_table.memory_rag.qp_slot import (
    assemble_qp_input_with_memory_candidates,
)
from pipeline.block_cross_table.memory_rag.query_embed import embed_query_text
from pipeline.block_cross_table.memory_rag.recall import recall_bindings, recall_by_knn
from pipeline.block_cross_table.memory_rag.store import (
    complete_reembed_queue_item,
    enqueue_reembed,
    insert_binding_confirmed,
    insert_embedding_meta,
    transition_lifecycle,
    try_reactivate_binding,
)
from pipeline.block_cross_table.memory_rag.vec_index import (
    drop_vec0_for_rebuild,
    ensure_vec0,
    insert_vector_row,
    load_sqlite_vec,
    rebuild_vec0_to_dim,
    stored_embedding_dim,
)
from pipeline.block_cross_table.memory_rag.weighted_rrf import weighted_rrf
from pipeline.block_cross_table.memory_rag.write_with_embed import (
    write_binding_with_external_embed,
)

__all__ = [
    "EmbeddingVectorMeta",
    "ExternalEmbeddingSettings",
    "MemoryRagError",
    "apply_dedup_after_inference",
    "assemble_qp_input_with_memory_candidates",
    "normalize_recalled_memories",
    "recall_memories_for_qp",
    "assert_embedded_path_allowed",
    "assert_external_call_allowed",
    "backfill_bindings_fts",
    "build_egress_body",
    "build_memory_conflict_package",
    "complete_reembed_queue_item",
    "confirm_jargon_with_binding",
    "detect_multi_binding_conflict",
    "discover_maibot_db_path",
    "drop_vec0_for_rebuild",
    "embed_binding_external",
    "embed_query_text",
    "enqueue_reembed",
    "ensure_bindings_fts",
    "ensure_vec0",
    "evaluate_endpoint_allowlist",
    "fetch_jargon_by_content",
    "get_backend_switch",
    "get_call_count",
    "hybrid_recall",
    "import_txt_and_mine",
    "infer_jargon_meaning",
    "inject_c14_all_targets",
    "inject_c14_reference",
    "insert_binding_confirmed",
    "insert_embedding_meta",
    "insert_vector_row",
    "invoke_external_embeddings_hook",
    "list_filemate_jargons",
    "list_mining_jobs",
    "list_open_conflicts",
    "load_external_embedding_settings",
    "load_memory_numeric_slots_from_current",
    "load_merge_runtime_settings",
    "load_sqlite_vec",
    "merge_jargon_corpora",
    "metas_mechanically_compatible",
    "mining_status_summary",
    "open_maibot_ro",
    "open_memory_db",
    "recall_bindings",
    "recall_by_knn",
    "rebuild_vec0_to_dim",
    "require_compatible_metas",
    "require_embedding_capable_model",
    "require_endpoint_allowlisted",
    "require_production_embedding_dim",
    "require_production_recall_top_k",
    "resolve_conflict",
    "resolve_maibot_data_dir",
    "save_maibot_data_dir",
    "set_backend_switch",
    "should_infer_meaning",
    "sparse_search_bindings",
    "stored_embedding_dim",
    "transition_lifecycle",
    "try_reactivate_binding",
    "upsert_filemate_jargon",
    "weighted_rrf",
    "write_binding_with_external_embed",
]
