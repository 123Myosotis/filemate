# -*- coding: utf-8 -*-
"""EmbeddingVectorMeta: mechanical three-field equality; no AI."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pipeline.block_cross_table.memory_rag.errors import (
    LOCUS_META_INCOMPATIBLE,
    MemoryRagError,
)


@dataclass(frozen=True)
class EmbeddingVectorMeta:
    model_id: str
    endpoint_fingerprint: str
    embedding_schema_version: str

    def as_dict(self) -> dict[str, str]:
        return {
            "model_id": self.model_id,
            "endpoint_fingerprint": self.endpoint_fingerprint,
            "embedding_schema_version": self.embedding_schema_version,
        }

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "EmbeddingVectorMeta":
        return cls(
            model_id=str(data["model_id"]),
            endpoint_fingerprint=str(data["endpoint_fingerprint"]),
            embedding_schema_version=str(data["embedding_schema_version"]),
        )


def metas_mechanically_compatible(
    left: EmbeddingVectorMeta,
    right: EmbeddingVectorMeta,
) -> bool:
    """Local mechanical equality on three fields; never semantic/AI."""
    return (
        left.model_id == right.model_id
        and left.endpoint_fingerprint == right.endpoint_fingerprint
        and left.embedding_schema_version == right.embedding_schema_version
    )


def require_compatible_metas(
    query_meta: EmbeddingVectorMeta,
    candidate_meta: EmbeddingVectorMeta,
) -> None:
    """Reject mix; fail_locus = memory_meta_incompatible."""
    if not metas_mechanically_compatible(query_meta, candidate_meta):
        raise MemoryRagError(
            "embedding meta incompatible; refuse mix",
            fail_locus=LOCUS_META_INCOMPATIBLE,
            subreason="incompatible_meta",
            detail={
                "query": query_meta.as_dict(),
                "candidate": candidate_meta.as_dict(),
            },
        )
