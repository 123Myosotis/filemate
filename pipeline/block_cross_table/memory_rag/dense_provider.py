# -*- coding: utf-8 -*-
"""
Dense provider v2 (XOR): off | local | external + FTS/jargon always.

Runtime MUST NOT read auto_local_embed / external_embedding.enabled.
Migration maps old keys once into dense_provider (+ audit fields).
No cross-provider fallback. hybrid_parallel is historical read-only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import config

# --- enums (writable) ---
DenseProvider = Literal["off", "local", "external"]
DENSE_OFF: DenseProvider = "off"
DENSE_LOCAL: DenseProvider = "local"
DENSE_EXTERNAL: DenseProvider = "external"
DENSE_PROVIDERS: frozenset[str] = frozenset({DENSE_OFF, DENSE_LOCAL, DENSE_EXTERNAL})
DEFAULT_DENSE_PROVIDER: DenseProvider = DENSE_OFF

# config keys (params.*)
PARAM_DENSE_PROVIDER = "dense_provider"
PARAM_MIGRATED_FROM = "dense_provider_migrated_from"
PARAM_MIGRATION_CONFLICT = "migration_conflict"
PARAM_MIGRATION_ACK = "migration_conflict_acknowledged"

# legacy keys — migration ONLY (never in new runtime decisions)
LEGACY_AUTO_LOCAL_EMBED = "auto_local_embed"
LEGACY_EXTERNAL_ENABLED = "external_embedding.enabled"
LEGACY_EXTERNAL_ENABLED_ALT = "memory_rag.external_recall_enabled"

# retrieval modes
MODE_SPARSE_ONLY = "sparse_only"
MODE_DENSE_LOCAL_PLUS_SPARSE = "dense_local_plus_sparse"
MODE_DENSE_EXTERNAL_PLUS_SPARSE = "dense_external_plus_sparse"
MODE_HYBRID_PARALLEL_HISTORICAL = "hybrid_parallel"  # readable, never newly written

RETRIEVAL_MODES_WRITABLE: frozenset[str] = frozenset(
    {
        MODE_SPARSE_ONLY,
        MODE_DENSE_LOCAL_PLUS_SPARSE,
        MODE_DENSE_EXTERNAL_PLUS_SPARSE,
    }
)
RETRIEVAL_MODES_READABLE: frozenset[str] = RETRIEVAL_MODES_WRITABLE | {
    MODE_HYBRID_PARALLEL_HISTORICAL
}

# Fallback state machine (constants — not scattered if/else for mode pick)
FALLBACK_STATE_TO_MODE: dict[str, str] = {
    "off": MODE_SPARSE_ONLY,
    "local_success": MODE_DENSE_LOCAL_PLUS_SPARSE,
    "local_fail": MODE_SPARSE_ONLY,
    "external_success": MODE_DENSE_EXTERNAL_PLUS_SPARSE,
    "external_fail": MODE_SPARSE_ONLY,
    "external_weight_zero": MODE_SPARSE_ONLY,
}

DENSE_CHANNEL_NAMES: frozenset[str] = frozenset({"local_dense", "external_dense"})

# Migration table: (auto_local_embed, external_enabled) -> (provider, conflict_label|None)
# Missing / falsy treated as False.
MIGRATION_TABLE: dict[tuple[bool, bool], tuple[DenseProvider, str | None]] = {
    (True, False): (DENSE_LOCAL, None),
    (False, True): (DENSE_EXTERNAL, None),
    (True, True): (DENSE_LOCAL, "both_enabled→local"),
    (False, False): (DENSE_OFF, None),
}

MIGRATED_FROM_LABEL = "auto_local_embed+external.enabled"

CHANNEL_LOCAL = "local_dense"
CHANNEL_EXTERNAL = "external_dense"
CHANNEL_SPARSE = "sparse"


def normalize_dense_provider(raw: Any) -> DenseProvider | None:
    text = str(raw or "").strip().lower()
    if text in DENSE_PROVIDERS:
        return text  # type: ignore[return-value]
    return None


def retrieval_mode_for_state(state: str) -> str:
    """Map fallback state → writable retrieval_mode. Unknown → sparse_only."""
    mode = FALLBACK_STATE_TO_MODE.get(str(state or "").strip(), MODE_SPARSE_ONLY)
    if mode not in RETRIEVAL_MODES_WRITABLE:
        return MODE_SPARSE_ONLY
    return mode


def assert_writable_retrieval_mode(mode: str) -> str:
    m = str(mode or "").strip()
    if m == MODE_HYBRID_PARALLEL_HISTORICAL:
        raise ValueError("hybrid_parallel is historical read-only; new writes forbidden")
    if m not in RETRIEVAL_MODES_WRITABLE:
        raise ValueError("unknown retrieval_mode for write: %r" % m)
    return m


def count_dense_channels(channel_list: list[str] | tuple[str, ...] | None) -> int:
    return sum(1 for c in (channel_list or []) if c in DENSE_CHANNEL_NAMES)


def assert_at_most_one_dense(channel_list: list[str] | tuple[str, ...] | None) -> None:
    n = count_dense_channels(channel_list)
    if n > 1:
        raise AssertionError(
            "dense XOR violated: channel_list has %d dense channels: %r"
            % (n, list(channel_list or []))
        )


def mask_api_key_tail8(key: str) -> str:
    """key_masked = '*' * max(0, len-8) + key[-8:]. Empty → empty."""
    text = str(key or "")
    if not text:
        return ""
    n = len(text)
    if n <= 8:
        return text
    return ("*" * (n - 8)) + text[-8:]


def _as_bool(raw: Any) -> bool:
    if raw is None:
        return False
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw)


def _read_legacy_auto_local() -> bool:
    # Prefer explicit auto_local_embed; absent → False (conservative).
    return _as_bool(config.get_param(LEGACY_AUTO_LOCAL_EMBED, None))


def _read_legacy_external_enabled() -> bool:
    raw = config.get_param(LEGACY_EXTERNAL_ENABLED, None)
    if raw is None:
        raw = config.get_param(LEGACY_EXTERNAL_ENABLED_ALT, None)
    return _as_bool(raw)


@dataclass(frozen=True)
class DenseProviderResolution:
    dense_provider: DenseProvider
    migrated: bool
    dense_provider_migrated_from: str | None
    migration_conflict: str | None
    migration_conflict_acknowledged: bool
    show_migration_banner: bool


def migrate_legacy_dense_provider(*, persist: bool = True) -> DenseProviderResolution:
    """
    If dense_provider already set → return it (no re-migration).
    Else map legacy keys via MIGRATION_TABLE and optionally persist.
    """
    existing = normalize_dense_provider(config.get_param(PARAM_DENSE_PROVIDER, None))
    ack = _as_bool(config.get_param(PARAM_MIGRATION_ACK, False))
    conflict_stored = str(config.get_param(PARAM_MIGRATION_CONFLICT, "") or "").strip() or None

    if existing is not None:
        show = bool(conflict_stored) and not ack
        return DenseProviderResolution(
            dense_provider=existing,
            migrated=False,
            dense_provider_migrated_from=str(
                config.get_param(PARAM_MIGRATED_FROM, "") or ""
            ).strip()
            or None,
            migration_conflict=conflict_stored,
            migration_conflict_acknowledged=ack,
            show_migration_banner=show,
        )

    auto_local = _read_legacy_auto_local()
    ext_on = _read_legacy_external_enabled()
    provider, conflict = MIGRATION_TABLE[(auto_local, ext_on)]
    if persist:
        config.set_param(PARAM_DENSE_PROVIDER, provider)
        config.set_param(PARAM_MIGRATED_FROM, MIGRATED_FROM_LABEL)
        if conflict:
            config.set_param(PARAM_MIGRATION_CONFLICT, conflict)
        # do not set ack
    show = bool(conflict) and not ack
    return DenseProviderResolution(
        dense_provider=provider,
        migrated=True,
        dense_provider_migrated_from=MIGRATED_FROM_LABEL,
        migration_conflict=conflict,
        migration_conflict_acknowledged=ack,
        show_migration_banner=show,
    )


def resolve_dense_provider(*, persist_migration: bool = True) -> DenseProviderResolution:
    """Runtime entry: only dense_provider (after optional one-shot migration)."""
    return migrate_legacy_dense_provider(persist=persist_migration)


def set_dense_provider(provider: str) -> DenseProvider:
    """Persist user choice. Does not wipe other-side configs."""
    p = normalize_dense_provider(provider)
    if p is None:
        raise ValueError("dense_provider must be off|local|external, got %r" % provider)
    config.set_param(PARAM_DENSE_PROVIDER, p)
    return p


def acknowledge_migration_conflict(
    *,
    choice: str | None = None,
) -> DenseProviderResolution:
    """
    Banner actions: confirm local / switch external / switch off.
    Always sets migration_conflict_acknowledged=true.
    """
    if choice is not None:
        set_dense_provider(choice)
    config.set_param(PARAM_MIGRATION_ACK, True)
    return resolve_dense_provider(persist_migration=False)


def effective_external_attempt_allowed(
    *,
    dense_provider: DenseProvider,
    external_weight: float | None,
) -> bool:
    """
    external path may live-call only when provider=external AND weight>0.
    weight 0/empty → treat as off (no call).
    """
    if dense_provider != DENSE_EXTERNAL:
        return False
    try:
        w = float(external_weight) if external_weight is not None else 0.0
    except (TypeError, ValueError):
        w = 0.0
    return w > 0.0


def pick_fallback_state(
    *,
    dense_provider: DenseProvider,
    external_weight: float | None,
    local_ok: bool | None,
    external_ok: bool | None,
) -> str:
    """
    Conservative state pick for FALLBACK_STATE_TO_MODE.
    local_ok/external_ok: True success, False fail, None not attempted.
    """
    if dense_provider == DENSE_OFF:
        return "off"
    if dense_provider == DENSE_LOCAL:
        if local_ok is True:
            return "local_success"
        return "local_fail"
    # external
    if not effective_external_attempt_allowed(
        dense_provider=dense_provider, external_weight=external_weight
    ):
        return "external_weight_zero"
    if external_ok is True:
        return "external_success"
    return "external_fail"


def public_dense_provider_view() -> dict[str, Any]:
    """Settings payload fragment (no secrets)."""
    res = resolve_dense_provider(persist_migration=True)
    return {
        "dense_provider": res.dense_provider,
        "dense_provider_migrated_from": res.dense_provider_migrated_from,
        "migration_conflict": res.migration_conflict,
        "migration_conflict_acknowledged": res.migration_conflict_acknowledged,
        "show_migration_banner": res.show_migration_banner,
        "migration_banner_text": (
            "检测到旧配置同时开启本地与外部向量，已默认切换为本地（更保守、不出网）。请确认当前选择。"
            if res.show_migration_banner
            else None
        ),
    }
