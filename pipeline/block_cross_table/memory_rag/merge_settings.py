# -*- coding: utf-8 -*-
"""
M5.2/M6 runtime settings (dual engine + dialogue review + egress policy).

Sources (high → low): explicit arg → env → config.json params → defaults.
No CURRENT merge. Directory-scan auto-learning removed in M6.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Sequence

import config
from pipeline.block_cross_table.memory_rag.constants import (
    EGRESS_ALLOW_EXTERNAL,
    EGRESS_LOCAL_ONLY,
    EGRESS_POLICY_CHOICES,
    ENGINE_CHAT,
    ENGINE_CHOICES,
    ENGINE_VECTOR,
    MEANING_EQ_AI,
    MEANING_EQ_CHOICES,
    MEANING_EQ_LOCAL,
)

ENV_MAIBOT_DATA_DIR = "MEMORY_RAG_MAIBOT_DATA_DIR"
ENV_MEANING_SIM_THRESHOLD = "MEMORY_RAG_MEANING_SIMILARITY_THRESHOLD"
ENV_MEANING_FALLBACK_THRESHOLD = "MEMORY_RAG_MEANING_FALLBACK_THRESHOLD"
ENV_JARGON_THRESHOLDS = "MEMORY_RAG_JARGON_INFERENCE_THRESHOLDS"
ENV_STOPWORDS = "MEMORY_RAG_JARGON_STOPWORDS"
ENV_RRF_K = "MEMORY_RAG_RRF_K"
ENV_VECTOR_WEIGHT = "MEMORY_RAG_FUSION_VECTOR_WEIGHT"
ENV_BM25_WEIGHT = "MEMORY_RAG_FUSION_BM25_WEIGHT"
ENV_SPARSE_TOP_K = "MEMORY_RAG_SPARSE_TOP_K"
ENV_DENSE_TOP_K = "MEMORY_RAG_DENSE_TOP_K"
ENV_HYBRID_FINAL_K = "MEMORY_RAG_HYBRID_FINAL_TOP_K"
ENV_SELF_LEARNING_ENGINE = "MEMORY_RAG_SELF_LEARNING_ENGINE"
ENV_MEANING_EQ_MODE = "MEMORY_RAG_MEANING_EQUIVALENCE_MODE"
ENV_EGRESS_POLICY = "MEMORY_RAG_EGRESS_POLICY"
ENV_DIALOGUE_REVIEW = "MEMORY_RAG_DIALOGUE_REVIEW_ENABLED"
ENV_EMBEDDED_MODEL_PATH = "MEMORY_RAG_EMBEDDED_MODEL_PATH"
ENV_EXTERNAL_RECALL_ENABLED = "MEMORY_RAG_EXTERNAL_RECALL_ENABLED"
ENV_EXTERNAL_WEIGHT = "MEMORY_RAG_FUSION_EXTERNAL_WEIGHT"

PARAM_MAIBOT_DATA_DIR = "memory_rag.maibot_data_dir"
PARAM_MEANING_SIM = "memory_rag.meaning_similarity_threshold"
PARAM_MEANING_FALLBACK = "memory_rag.meaning_fallback_threshold"
PARAM_JARGON_THRESHOLDS = "memory_rag.jargon_inference_thresholds"
PARAM_STOPWORDS = "memory_rag.jargon_stopwords"
PARAM_RRF_K = "memory_rag.fusion.rrf_k"
PARAM_VECTOR_WEIGHT = "memory_rag.fusion.vector_weight"
PARAM_BM25_WEIGHT = "memory_rag.fusion.bm25_weight"
PARAM_SPARSE_TOP_K = "memory_rag.retrieval.sparse_top_k"
PARAM_DENSE_TOP_K = "memory_rag.retrieval.dense_top_k"
PARAM_HYBRID_FINAL_K = "memory_rag.retrieval.hybrid_final_top_k"
PARAM_SELF_LEARNING_ENGINE = "memory_rag.self_learning_engine"
PARAM_MEANING_EQ_MODE = "memory_rag.meaning_equivalence_mode"
PARAM_EGRESS_POLICY = "memory_rag.egress_policy"
PARAM_DIALOGUE_REVIEW = "memory_rag.dialogue_review_enabled"
PARAM_EMBEDDED_MODEL_PATH = "memory_rag.embedded_model_path"
PARAM_EXTERNAL_RECALL_ENABLED = "memory_rag.external_recall_enabled"
PARAM_EXTERNAL_WEIGHT = "memory_rag.fusion.external_weight"

DEFAULT_MEANING_SIM = 0.92
DEFAULT_MEANING_FALLBACK = 0.6
DEFAULT_JARGON_THRESHOLDS: tuple[int, ...] = (4, 8, 25, 100)
DEFAULT_STOPWORDS: tuple[str, ...] = (
    "的",
    "了",
    "是",
    "在",
    "和",
    "与",
    "或",
    "及",
    "等",
    "也",
    "都",
    "就",
    "而",
    "被",
    "把",
    "对",
    "从",
    "到",
    "为",
    "以",
    "其",
    "这",
    "那",
    "一个",
    "没有",
    "可以",
    "因为",
    "所以",
    "如果",
    "但是",
)
DEFAULT_RRF_K = 60
DEFAULT_VECTOR_WEIGHT = 0.7
DEFAULT_BM25_WEIGHT = 0.3
DEFAULT_SPARSE_TOP_K = 40
DEFAULT_DENSE_TOP_K = 40
DEFAULT_HYBRID_FINAL_K = 10
DEFAULT_SELF_LEARNING_ENGINE = ENGINE_CHAT
DEFAULT_MEANING_EQ_MODE = MEANING_EQ_AI
DEFAULT_EGRESS_POLICY = EGRESS_ALLOW_EXTERNAL
DEFAULT_DIALOGUE_REVIEW = True
DEFAULT_EMBEDDED_MODEL_PATH = ""
# Optional external branch off by default (enable via env/config for P1 / tests).
DEFAULT_EXTERNAL_RECALL_ENABLED = False
DEFAULT_EXTERNAL_WEIGHT = 0.0


def _env(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    return text if text else None


def _parse_int_list(raw: object, fallback: Sequence[int]) -> tuple[int, ...]:
    if raw is None:
        return tuple(int(x) for x in fallback)
    if isinstance(raw, (list, tuple)):
        out = [int(x) for x in raw]
        return tuple(out) if out else tuple(int(x) for x in fallback)
    text = str(raw).strip()
    if not text:
        return tuple(int(x) for x in fallback)
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list) and data:
                return tuple(int(x) for x in data)
        except Exception:
            pass
    parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    if not parts:
        return tuple(int(x) for x in fallback)
    return tuple(int(p) for p in parts)


def _float_param(env_name: str, param_key: str, default: float) -> float:
    env = _env(env_name)
    if env is not None:
        return float(env)
    val = config.get_param(param_key, default)
    if val is None:
        return float(default)
    return float(val)


def _int_param(env_name: str, param_key: str, default: int) -> int:
    env = _env(env_name)
    if env is not None:
        return int(env)
    val = config.get_param(param_key, default)
    if val is None:
        return int(default)
    return int(val)


def _bool_param(env_name: str, param_key: str, default: bool) -> bool:
    env = _env(env_name)
    if env is not None:
        return env.lower() in ("1", "true", "yes", "on")
    val = config.get_param(param_key, default)
    if isinstance(val, bool):
        return val
    if val is None:
        return bool(default)
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _str_param(env_name: str, param_key: str, default: str) -> str:
    env = _env(env_name)
    if env is not None:
        return env
    val = config.get_param(param_key, default)
    return str(val if val is not None else default).strip()


def _parse_str_list(raw: object, fallback: Sequence[str]) -> tuple[str, ...]:
    if raw is None:
        return tuple(str(x) for x in fallback)
    if isinstance(raw, (list, tuple)):
        out = [str(x).strip() for x in raw if str(x).strip()]
        return tuple(out) if out else tuple(str(x) for x in fallback)
    text = str(raw).strip()
    if not text:
        return tuple(str(x) for x in fallback)
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list) and data:
                return tuple(str(x).strip() for x in data if str(x).strip())
        except Exception:
            pass
    parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    return tuple(parts) if parts else tuple(str(x) for x in fallback)


def _normalize_engine(raw: str) -> str:
    text = str(raw or "").strip().lower()
    if text in ENGINE_CHOICES:
        return text
    return DEFAULT_SELF_LEARNING_ENGINE


def _normalize_eq_mode(raw: str) -> str:
    text = str(raw or "").strip().lower()
    if text in MEANING_EQ_CHOICES:
        return text
    return DEFAULT_MEANING_EQ_MODE


def _normalize_egress(raw: str) -> str:
    text = str(raw or "").strip().lower()
    if text in EGRESS_POLICY_CHOICES:
        return text
    # Aliases
    if text in ("local", "offline", "no_external"):
        return EGRESS_LOCAL_ONLY
    return DEFAULT_EGRESS_POLICY


@dataclass(frozen=True)
class MergeRuntimeSettings:
    maibot_data_dir: str
    meaning_similarity_threshold: float
    meaning_fallback_threshold: float
    jargon_inference_thresholds: tuple[int, ...]
    jargon_stopwords: tuple[str, ...]
    rrf_k: int
    vector_weight: float
    bm25_weight: float
    sparse_top_k: int
    dense_top_k: int
    hybrid_final_top_k: int
    self_learning_engine: str = DEFAULT_SELF_LEARNING_ENGINE
    meaning_equivalence_mode: str = DEFAULT_MEANING_EQ_MODE
    egress_policy: str = DEFAULT_EGRESS_POLICY
    dialogue_review_enabled: bool = DEFAULT_DIALOGUE_REVIEW
    embedded_model_path: str = DEFAULT_EMBEDDED_MODEL_PATH
    external_recall_enabled: bool = DEFAULT_EXTERNAL_RECALL_ENABLED
    external_weight: float = DEFAULT_EXTERNAL_WEIGHT
    # v2 XOR dense source; runtime decisions use this, not external_recall_enabled.
    dense_provider: str = "off"

    def prefer_ai_for_equivalence(self) -> bool:
        """chat engine always AI-first; vector uses meaning_equivalence_mode."""
        if self.self_learning_engine == ENGINE_CHAT:
            return True
        return self.meaning_equivalence_mode == MEANING_EQ_AI

    def as_public_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["jargon_inference_thresholds"] = list(self.jargon_inference_thresholds)
        d["jargon_stopwords"] = list(self.jargon_stopwords)
        return d


def resolve_maibot_data_dir(explicit: str | None = None) -> str:
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    env = _env(ENV_MAIBOT_DATA_DIR)
    if env:
        return env
    val = config.get_param(PARAM_MAIBOT_DATA_DIR, "")
    return str(val or "").strip()


def load_merge_runtime_settings(
    *,
    maibot_data_dir: str | None = None,
) -> MergeRuntimeSettings:
    thresholds_env = _env(ENV_JARGON_THRESHOLDS)
    thresholds_param = config.get_param(
        PARAM_JARGON_THRESHOLDS, list(DEFAULT_JARGON_THRESHOLDS)
    )
    thresholds = _parse_int_list(
        thresholds_env if thresholds_env is not None else thresholds_param,
        DEFAULT_JARGON_THRESHOLDS,
    )
    stop_env = _env(ENV_STOPWORDS)
    stop_param = config.get_param(PARAM_STOPWORDS, list(DEFAULT_STOPWORDS))
    stopwords = _parse_str_list(
        stop_env if stop_env is not None else stop_param,
        DEFAULT_STOPWORDS,
    )
    vw = _float_param(ENV_VECTOR_WEIGHT, PARAM_VECTOR_WEIGHT, DEFAULT_VECTOR_WEIGHT)
    bw = _float_param(ENV_BM25_WEIGHT, PARAM_BM25_WEIGHT, DEFAULT_BM25_WEIGHT)
    total = vw + bw
    if total <= 0:
        vw, bw = DEFAULT_VECTOR_WEIGHT, DEFAULT_BM25_WEIGHT
    else:
        vw, bw = vw / total, bw / total
    engine = _normalize_engine(
        _str_param(
            ENV_SELF_LEARNING_ENGINE,
            PARAM_SELF_LEARNING_ENGINE,
            DEFAULT_SELF_LEARNING_ENGINE,
        )
    )
    eq_mode = _normalize_eq_mode(
        _str_param(
            ENV_MEANING_EQ_MODE,
            PARAM_MEANING_EQ_MODE,
            DEFAULT_MEANING_EQ_MODE,
        )
    )
    return MergeRuntimeSettings(
        maibot_data_dir=resolve_maibot_data_dir(maibot_data_dir),
        meaning_similarity_threshold=_float_param(
            ENV_MEANING_SIM_THRESHOLD, PARAM_MEANING_SIM, DEFAULT_MEANING_SIM
        ),
        meaning_fallback_threshold=_float_param(
            ENV_MEANING_FALLBACK_THRESHOLD,
            PARAM_MEANING_FALLBACK,
            DEFAULT_MEANING_FALLBACK,
        ),
        jargon_inference_thresholds=thresholds,
        jargon_stopwords=stopwords,
        rrf_k=max(1, _int_param(ENV_RRF_K, PARAM_RRF_K, DEFAULT_RRF_K)),
        vector_weight=vw,
        bm25_weight=bw,
        sparse_top_k=max(1, _int_param(ENV_SPARSE_TOP_K, PARAM_SPARSE_TOP_K, DEFAULT_SPARSE_TOP_K)),
        dense_top_k=max(1, _int_param(ENV_DENSE_TOP_K, PARAM_DENSE_TOP_K, DEFAULT_DENSE_TOP_K)),
        hybrid_final_top_k=max(
            1, _int_param(ENV_HYBRID_FINAL_K, PARAM_HYBRID_FINAL_K, DEFAULT_HYBRID_FINAL_K)
        ),
        self_learning_engine=engine,
        meaning_equivalence_mode=eq_mode,
        egress_policy=_normalize_egress(
            _str_param(ENV_EGRESS_POLICY, PARAM_EGRESS_POLICY, DEFAULT_EGRESS_POLICY)
        ),
        dialogue_review_enabled=_bool_param(
            ENV_DIALOGUE_REVIEW, PARAM_DIALOGUE_REVIEW, DEFAULT_DIALOGUE_REVIEW
        ),
        embedded_model_path=_str_param(
            ENV_EMBEDDED_MODEL_PATH,
            PARAM_EMBEDDED_MODEL_PATH,
            DEFAULT_EMBEDDED_MODEL_PATH,
        ),
        external_recall_enabled=_load_external_enabled(),
        external_weight=_load_external_weight(),
        dense_provider=_load_dense_provider(),
    )


def _load_dense_provider() -> str:
    from pipeline.block_cross_table.memory_rag.dense_provider import (
        resolve_dense_provider,
    )

    return str(resolve_dense_provider(persist_migration=True).dense_provider)


def _load_external_enabled() -> bool:
    from pipeline.block_cross_table.memory_rag.external_embedding_config import (
        resolve_external_embedding,
    )

    return bool(resolve_external_embedding().enabled)


def _load_external_weight() -> float:
    from pipeline.block_cross_table.memory_rag.external_embedding_config import (
        resolve_external_embedding,
    )

    return max(0.0, float(resolve_external_embedding().weight))


def save_maibot_data_dir(path: str) -> str:
    """Persist GUI/web setting into config.json params."""
    cleaned = str(path or "").strip()
    config.set_param(PARAM_MAIBOT_DATA_DIR, cleaned)
    return cleaned


def save_merge_settings_patch(patch: dict[str, Any]) -> MergeRuntimeSettings:
    """
    Persist a partial settings dict into config params.
    Unknown keys ignored. Model keys (api.*) also accepted.
    """
    mapping = {
        "maibot_data_dir": PARAM_MAIBOT_DATA_DIR,
        "meaning_similarity_threshold": PARAM_MEANING_SIM,
        "meaning_fallback_threshold": PARAM_MEANING_FALLBACK,
        "jargon_inference_thresholds": PARAM_JARGON_THRESHOLDS,
        "jargon_stopwords": PARAM_STOPWORDS,
        "rrf_k": PARAM_RRF_K,
        "vector_weight": PARAM_VECTOR_WEIGHT,
        "bm25_weight": PARAM_BM25_WEIGHT,
        "sparse_top_k": PARAM_SPARSE_TOP_K,
        "dense_top_k": PARAM_DENSE_TOP_K,
        "hybrid_final_top_k": PARAM_HYBRID_FINAL_K,
        "self_learning_engine": PARAM_SELF_LEARNING_ENGINE,
        "meaning_equivalence_mode": PARAM_MEANING_EQ_MODE,
        "egress_policy": PARAM_EGRESS_POLICY,
        "dialogue_review_enabled": PARAM_DIALOGUE_REVIEW,
        "embedded_model_path": PARAM_EMBEDDED_MODEL_PATH,
        "external_recall_enabled": PARAM_EXTERNAL_RECALL_ENABLED,
        "external_weight": PARAM_EXTERNAL_WEIGHT,
        "dense_provider": "dense_provider",
    }
    model_keys = {
        "api_base_url": "api.base_url",
        "api_model": "api.model",
        "api_key": None,  # special
    }
    data = dict(patch or {})
    for key, param in mapping.items():
        if key not in data:
            continue
        val = data[key]
        if key == "self_learning_engine":
            val = _normalize_engine(str(val))
        elif key == "meaning_equivalence_mode":
            val = _normalize_eq_mode(str(val))
        elif key == "egress_policy":
            val = _normalize_egress(str(val))
        elif key == "dialogue_review_enabled":
            if isinstance(val, str):
                val = val.strip().lower() in ("1", "true", "yes", "on")
            else:
                val = bool(val)
        elif key == "jargon_inference_thresholds":
            val = list(_parse_int_list(val, DEFAULT_JARGON_THRESHOLDS))
        elif key == "jargon_stopwords":
            val = list(_parse_str_list(val, DEFAULT_STOPWORDS))
        elif key == "dense_provider":
            from pipeline.block_cross_table.memory_rag.dense_provider import (
                normalize_dense_provider,
                set_dense_provider,
            )

            p = normalize_dense_provider(val)
            if p is None:
                continue
            set_dense_provider(p)
            continue
        config.set_param(param, val)

    if "api_base_url" in data:
        config.set_param("api.base_url", str(data["api_base_url"] or "").strip())
    if "api_model" in data:
        config.set_param("api.model", str(data["api_model"] or "").strip())
    if "api_key" in data and data["api_key"] is not None:
        key = str(data["api_key"]).strip()
        # Empty string clears; sentinel "***" / masked means leave unchanged
        if key and not key.startswith("***"):
            config.set_api_key(key)
        elif key == "":
            config.set_api_key("")

    # fusion nested form support
    fusion = data.get("fusion")
    if isinstance(fusion, dict):
        if "rrf_k" in fusion:
            config.set_param(PARAM_RRF_K, int(fusion["rrf_k"]))
        if "vector_weight" in fusion:
            config.set_param(PARAM_VECTOR_WEIGHT, float(fusion["vector_weight"]))
        if "bm25_weight" in fusion:
            config.set_param(PARAM_BM25_WEIGHT, float(fusion["bm25_weight"]))
        if "external_weight" in fusion:
            from pipeline.block_cross_table.memory_rag.external_embedding_config import (
                PARAM_WEIGHT as EXT_WEIGHT,
            )

            config.set_param(EXT_WEIGHT, max(0.0, float(fusion["external_weight"])))
            config.set_param(
                PARAM_EXTERNAL_WEIGHT, max(0.0, float(fusion["external_weight"]))
            )

    # Nested / flat external_embedding (optional enhancement)
    from pipeline.block_cross_table.memory_rag.external_embedding_config import (
        save_external_embedding_patch,
    )

    if "external_embedding" in data or any(
        str(k).startswith("external_embedding_") for k in data
    ):
        save_external_embedding_patch(data)
    elif "external_recall_enabled" in data or "external_weight" in data:
        # Bridge legacy flat keys into new param namespace
        bridge: dict[str, Any] = {}
        if "external_recall_enabled" in data:
            bridge["enabled"] = data["external_recall_enabled"]
        if "external_weight" in data:
            bridge["weight"] = data["external_weight"]
        save_external_embedding_patch({"external_embedding": bridge})

    return load_merge_runtime_settings()


def load_model_settings_public() -> dict[str, Any]:
    """Chat + local vector model settings for Web (API key masked)."""
    key = config.get_api_key() or ""
    masked = ""
    if key:
        if len(key) <= 8:
            masked = "***"
        else:
            masked = key[:4] + "***" + key[-4:]
    return {
        "api_base_url": str(config.get_param("api.base_url") or ""),
        "api_model": str(config.get_param("api.model") or ""),
        "api_key_masked": masked,
        "api_key_configured": bool(key),
        "embedded_model_path": str(
            config.get_param(PARAM_EMBEDDED_MODEL_PATH, "") or ""
        ),
    }
