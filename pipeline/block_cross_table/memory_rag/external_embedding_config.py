# -*- coding: utf-8 -*-
"""
Optional external embedding enhancement config (D7=A).

Priority (written — product path):
  local config.json params.external_embedding.api_key
  > optional env (dev/debug only; not required for users)
  > none (missing_external_api_key; skip external, never block recall).

Call path reads plaintext Key from config/env; masking is display/evidence/log only.
GUI is not a separate data source: GUI save writes the same config file.
Does not mutate chat api.model / api.base_url / zhipu_api_key.
Model names are never hardcoded in resolve — only read from env/config.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

import config
from pipeline.block_cross_table.memory_rag.external_settings import (
    ExternalEmbeddingSettings,
)

# Config params (under config.json params.*)
PARAM_ENABLED = "external_embedding.enabled"
PARAM_BASE_URL = "external_embedding.base_url"
PARAM_MODEL = "external_embedding.model"
PARAM_API_KEY = "external_embedding.api_key"
PARAM_WEIGHT = "external_embedding.weight"

# Env overrides (generic + optional vendor key alias — not a model hardcode)
ENV_ENABLED = "MEMORY_RAG_EXTERNAL_RECALL_ENABLED"
ENV_WEIGHT = "MEMORY_RAG_FUSION_EXTERNAL_WEIGHT"
ENV_BASE_URL = "MEMORY_RAG_EXTERNAL_EMBEDDING_BASE_URL"
ENV_MODEL = "MEMORY_RAG_EXTERNAL_EMBEDDING_MODEL"
ENV_API_KEY = "MEMORY_RAG_EXTERNAL_EMBEDDING_API_KEY"
ENV_API_KEY_VENDOR_ALIAS = "SILICONFLOW_API_KEY"

ApiKeySource = Literal["env", "config", "none"]

SOURCE_ENV: ApiKeySource = "env"
SOURCE_CONFIG: ApiKeySource = "config"
SOURCE_NONE: ApiKeySource = "none"


def _env(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    return text if text else None


def _env_bool(name: str) -> bool | None:
    raw = _env(name)
    if raw is None:
        return None
    return raw.lower() in ("1", "true", "yes", "on")


def _env_float(name: str) -> float | None:
    raw = _env(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def mask_api_key(key: str) -> str:
    """GUI / evidence / log: last 8 chars visible; rest '*'. Never use for HTTP."""
    from pipeline.block_cross_table.memory_rag.dense_provider import (
        mask_api_key_tail8,
    )

    return mask_api_key_tail8(str(key or ""))


def _is_masked_or_placeholder_key(raw: str) -> bool:
    """True when value is empty/masked placeholder — not a real secret."""
    key = str(raw or "").strip()
    if not key:
        return True
    if key == "sk-****":
        return True
    if key.startswith("***"):
        return True
    if "****" in key and len(key) <= 12:
        return True
    return False


def resolve_external_api_key() -> tuple[str, ApiKeySource]:
    """
    Priority: local config.json (plaintext) > optional env (dev) > none.

    Returns the real Key for HTTP calls — never a masked placeholder.
    """
    cfg = str(config.get_param(PARAM_API_KEY, "") or "").strip()
    if cfg and not _is_masked_or_placeholder_key(cfg):
        return cfg, SOURCE_CONFIG
    # Optional env fallback for developers only (not a product requirement).
    for name in (ENV_API_KEY, ENV_API_KEY_VENDOR_ALIAS):
        env_key = _env(name)
        if env_key and not _is_masked_or_placeholder_key(env_key):
            return env_key, SOURCE_ENV
    return "", SOURCE_NONE


@dataclass(frozen=True)
class ExternalEmbeddingPublicView:
    enabled: bool
    base_url: str
    model: str
    weight: float
    api_key_configured: bool
    api_key_masked: str
    api_key_source: ApiKeySource


@dataclass(frozen=True)
class ResolvedExternalEmbedding:
    enabled: bool
    base_url: str
    model: str
    weight: float
    api_key: str
    api_key_source: ApiKeySource
    api_key_present: bool

    def evidence_auth(self) -> dict[str, Any]:
        return {
            "api_key_present": bool(self.api_key_present),
            "api_key_source": self.api_key_source,
        }

    def as_external_settings(
        self,
        *,
        embedding_dim: int = 0,
        embedding_schema_version: str = "external-embedding-v1",
        call_count_cap: int = 5000,
        timeout_ms: int = 45000,
        max_retries: int = 0,
        top_k: int = 10,
    ) -> ExternalEmbeddingSettings | None:
        """Build call settings when key+url+model present; else None."""
        if not self.api_key_present:
            return None
        if not (self.base_url or "").strip() or not (self.model or "").strip():
            return None
        return ExternalEmbeddingSettings(
            base_url=str(self.base_url).strip(),
            api_key=self.api_key,
            model_id=str(self.model).strip(),
            embedding_dim=int(embedding_dim),
            embedding_schema_version=embedding_schema_version,
            call_count_cap=int(call_count_cap),
            timeout_ms=int(timeout_ms),
            max_retries=int(max_retries),
            top_k=int(top_k),
            recall_max_distance=None,
            allowlist=(),
        )


def resolve_external_embedding() -> ResolvedExternalEmbedding:
    """Resolve enabled/url/model/weight/key: config.json first, optional env second."""
    # Legacy bridge params (pre-GUI productization)
    _LEGACY_ENABLED = "memory_rag.external_recall_enabled"
    _LEGACY_WEIGHT = "memory_rag.fusion.external_weight"

    new_raw = config.get_param(PARAM_ENABLED, None)
    if new_raw is not None:
        enabled = bool(new_raw) if not isinstance(new_raw, str) else (
            str(new_raw).strip().lower() in ("1", "true", "yes", "on")
        )
    else:
        legacy = config.get_param(_LEGACY_ENABLED, None)
        if legacy is not None:
            if isinstance(legacy, str):
                enabled = legacy.strip().lower() in ("1", "true", "yes", "on")
            else:
                enabled = bool(legacy)
        else:
            env_enabled = _env_bool(ENV_ENABLED)
            enabled = bool(env_enabled) if env_enabled is not None else False

    new_w = config.get_param(PARAM_WEIGHT, None)
    if new_w is not None:
        try:
            weight = max(0.0, float(new_w or 0))
        except (TypeError, ValueError):
            weight = 0.0
    else:
        legacy_w = config.get_param(_LEGACY_WEIGHT, None)
        if legacy_w is not None:
            try:
                weight = max(0.0, float(legacy_w or 0))
            except (TypeError, ValueError):
                weight = 0.0
        else:
            env_weight = _env_float(ENV_WEIGHT)
            weight = max(0.0, float(env_weight)) if env_weight is not None else 0.0

    base_url = str(config.get_param(PARAM_BASE_URL, "") or "").strip() or (
        _env(ENV_BASE_URL) or ""
    )
    model = str(config.get_param(PARAM_MODEL, "") or "").strip() or (
        _env(ENV_MODEL) or ""
    )

    api_key, source = resolve_external_api_key()
    return ResolvedExternalEmbedding(
        enabled=bool(enabled),
        base_url=base_url,
        model=model,
        weight=float(weight),
        api_key=api_key,
        api_key_source=source,
        api_key_present=bool(api_key),
    )


def load_external_embedding_public() -> ExternalEmbeddingPublicView:
    """GUI-safe view: masked key only (never used for HTTP)."""
    resolved = resolve_external_embedding()
    return ExternalEmbeddingPublicView(
        enabled=resolved.enabled,
        base_url=resolved.base_url,
        model=resolved.model,
        weight=resolved.weight,
        api_key_configured=resolved.api_key_present,
        api_key_masked=mask_api_key(resolved.api_key) if resolved.api_key else "",
        api_key_source=resolved.api_key_source,
    )


def save_external_embedding_patch(patch: dict[str, Any] | None) -> ExternalEmbeddingPublicView:
    """
    Persist partial external_embedding settings into config.json params.

    api_key rules (hard):
      - clear=true → empty config key
      - empty / masked / sk-**** → keep existing key unchanged
      - new plaintext value → overwrite
    GUI is not a separate data source: save writes config file only.
    """
    data = dict(patch or {})
    # Accept nested or flat
    nested = data.get("external_embedding")
    if isinstance(nested, dict):
        flat = dict(nested)
    else:
        flat = {}
        for k in ("enabled", "base_url", "model", "api_key", "weight", "clear"):
            fk = "external_embedding_%s" % k
            if k in data:
                flat[k] = data[k]
            elif fk in data:
                flat[k] = data[fk]

    clear_flag = flat.get("clear")
    if isinstance(clear_flag, str):
        clear_flag = clear_flag.strip().lower() in ("1", "true", "yes", "on")
    else:
        clear_flag = bool(clear_flag) if clear_flag is not None else False
    # Also honor top-level clear on patch
    if not clear_flag and data.get("clear") is not None:
        top = data.get("clear")
        if isinstance(top, str):
            clear_flag = top.strip().lower() in ("1", "true", "yes", "on")
        else:
            clear_flag = bool(top)

    if "enabled" in flat:
        val = flat["enabled"]
        if isinstance(val, str):
            val = val.strip().lower() in ("1", "true", "yes", "on")
        else:
            val = bool(val)
        config.set_param(PARAM_ENABLED, bool(val))
        # Keep legacy key in sync for older readers
        config.set_param("memory_rag.external_recall_enabled", bool(val))
    if "base_url" in flat:
        config.set_param(PARAM_BASE_URL, str(flat.get("base_url") or "").strip())
    if "model" in flat:
        config.set_param(PARAM_MODEL, str(flat.get("model") or "").strip())
    if "weight" in flat:
        try:
            w = max(0.0, float(flat.get("weight") or 0))
        except (TypeError, ValueError):
            w = 0.0
        config.set_param(PARAM_WEIGHT, w)
        config.set_param("memory_rag.fusion.external_weight", w)

    if clear_flag:
        config.set_param(PARAM_API_KEY, "")
    elif "api_key" in flat and flat["api_key"] is not None:
        key = str(flat["api_key"]).strip()
        if _is_masked_or_placeholder_key(key):
            pass  # keep existing
        else:
            config.set_param(PARAM_API_KEY, key)

    return load_external_embedding_public()
