# -*- coding: utf-8 -*-
"""A' / 画像写 上限读取（契约第一批）。缺键或非法 → 契约无效类；不降级默认值。

本模块不编排识别业务；供后续 A' 收尾与 I 接线调用。
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from pipeline.block_cross_table.errors import CrossTableError

ASK_MAX_ROUNDS_KEY = "layer_recognize_ask_max_rounds"
ENTRY_RETRY_MAX_KEY = "layer_recognize_entry_retry_max"
PORTRAIT_WRITE_RETRY_MAX_KEY = "portrait_write_retry_max"
PROPOSE_INPUT_MAX_CHARS_KEY = "layer_recognize_propose_input_max_chars"

_LIMIT_KEYS = (
    ASK_MAX_ROUNDS_KEY,
    ENTRY_RETRY_MAX_KEY,
    PORTRAIT_WRITE_RETRY_MAX_KEY,
)


def _cross_table_block(contract_data: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
    if contract_data is not None:
        block = contract_data.get("cross_table")
    else:
        from pipeline.synth_contract.registry import load_contract

        block = (load_contract().data or {}).get("cross_table")
    if not isinstance(block, Mapping):
        raise CrossTableError(
            "cross_table_contract_invalid",
            "cross_table block missing",
        )
    return block


def require_positive_int_limit(
    cross_table: Mapping[str, Any],
    key: str,
) -> int:
    """读单个上限：缺键 / 非 int / <1 → cross_table_contract_invalid。不返回默认值。"""
    if key not in cross_table:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "%s missing" % key,
        )
    value = cross_table[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "%s invalid" % key,
        )
    return int(value)


def read_layer_recognize_ask_max_rounds(
    contract_data: Optional[Mapping[str, Any]] = None,
) -> int:
    return require_positive_int_limit(_cross_table_block(contract_data), ASK_MAX_ROUNDS_KEY)


def read_layer_recognize_entry_retry_max(
    contract_data: Optional[Mapping[str, Any]] = None,
) -> int:
    return require_positive_int_limit(
        _cross_table_block(contract_data), ENTRY_RETRY_MAX_KEY
    )


def read_portrait_write_retry_max(
    contract_data: Optional[Mapping[str, Any]] = None,
) -> int:
    return require_positive_int_limit(
        _cross_table_block(contract_data), PORTRAIT_WRITE_RETRY_MAX_KEY
    )


def read_all_batch1_limits(
    contract_data: Optional[Mapping[str, Any]] = None,
) -> dict[str, int]:
    block = _cross_table_block(contract_data)
    return {key: require_positive_int_limit(block, key) for key in _LIMIT_KEYS}


def read_layer_recognize_propose_input_max_chars(
    contract_data: Optional[Mapping[str, Any]] = None,
) -> int:
    return require_positive_int_limit(
        _cross_table_block(contract_data), PROPOSE_INPUT_MAX_CHARS_KEY
    )
