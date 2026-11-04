# -*- coding: utf-8 -*-
"""A 段：消费 I 下发的成功/无此结构包，按映射表写画像切片。不调 A'、不落 A' trace。"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.layer_recognize_limits import (
    read_portrait_write_retry_max,
)

# --- 结构标记（枚举型；非布尔、非自由串）---
STRUCTURE_STATUS_UPSTREAM_ABSENT = "upstream_absent"
STRUCTURE_STATUS_LAYERS_RECOGNIZED = "layers_recognized"
STRUCTURE_STATUS_VALUES = frozenset(
    {
        STRUCTURE_STATUS_UPSTREAM_ABSENT,
        STRUCTURE_STATUS_LAYERS_RECOGNIZED,
    }
)

STATUS_OK = "ok"
STATUS_NO_STRUCTURE = "no_structure"
_ALLOWED_PACKAGE_STATUS = frozenset({STATUS_OK, STATUS_NO_STRUCTURE})

FAIL_FIELD_MISSING = "field_missing"
FAIL_MAPPING_UNDEFINED = "mapping_undefined"
FAIL_WRITE_REJECTED = "write_rejected"
_FAILURE_CODES = frozenset(
    {FAIL_FIELD_MISSING, FAIL_MAPPING_UNDEFINED, FAIL_WRITE_REJECTED}
)

# A' 成功 8 字段 → 画像字段（本轮 1:1；证据映射表强制）
# 画像侧键名与 A' 对外键同名，禁止在映射外另起业务别名。
A_PRIME_TO_PORTRAIT_FIELD: dict[str, str] = {
    "rect_to_layer": "rect_to_layer",
    "layer_order": "layer_order",
    "layers": "layers",
    "subtable_boundaries": "subtable_boundaries",
    "nesting_facts": "nesting_facts",
    "group_label_declaration": "group_label_declaration",
    "dimension_membership": "dimension_membership",
    "header_completeness": "header_completeness",
}

# 允许空列表原样写入的画像键（空 ≠ 缺字段）
_EMPTY_LIST_OK = frozenset(
    {
        "layers",
        "subtable_boundaries",
        "nesting_facts",
        "group_label_declaration",
        "dimension_membership",
    }
)

PORTRAIT_STRUCTURE_STATUS_KEY = "structure_status"
PORTRAIT_BLOCK_ID_KEY = "block_id"


class PortraitWriteRejected(Exception):
    """存储/权限等拒绝落盘；映射为 write_rejected。"""

    def __init__(self, message: str = "portrait write rejected") -> None:
        super().__init__(message)
        self.message = message


class PortraitStore(Protocol):
    def write_slice(self, block_id: Any, slice_data: Mapping[str, Any]) -> None:
        """写入一块画像切片。拒绝时抛 PortraitWriteRejected。"""


class InMemoryPortraitStore:
    """测试与默认内存画像；B 可读 slices。"""

    def __init__(self) -> None:
        self.slices: dict[str, dict[str, Any]] = {}
        self.reject_next: bool = False
        self.reject_message: str = "storage rejected"

    def write_slice(self, block_id: Any, slice_data: Mapping[str, Any]) -> None:
        if self.reject_next:
            self.reject_next = False
            raise PortraitWriteRejected(self.reject_message)
        self.slices[str(block_id)] = dict(slice_data)

    def get(self, block_id: Any) -> Optional[dict[str, Any]]:
        return self.slices.get(str(block_id))


def _cross_table(contract_data: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
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


def read_required_layer_fields(
    contract_data: Optional[Mapping[str, Any]] = None,
) -> list[str]:
    """契约 LayerRecognizeOk.required_layer_fields；缺 → 契约无效类。"""
    ct = _cross_table(contract_data)
    ok_shape = ct.get("LayerRecognizeOk")
    if not isinstance(ok_shape, Mapping):
        raise CrossTableError(
            "cross_table_contract_invalid",
            "LayerRecognizeOk missing",
        )
    fields = ok_shape.get("required_layer_fields")
    if not isinstance(fields, list) or not fields:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "required_layer_fields missing",
        )
    return [str(x) for x in fields]


def read_portrait_write_failure_codes(
    contract_data: Optional[Mapping[str, Any]] = None,
) -> list[str]:
    ct = _cross_table(contract_data)
    enums = ct.get("enums") or {}
    codes = enums.get("portrait_write_failure_code")
    if not isinstance(codes, list) or not codes:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "portrait_write_failure_code missing",
        )
    return [str(x) for x in codes]


def read_failure_error_class(
    failure_code: str,
    contract_data: Optional[Mapping[str, Any]] = None,
) -> str:
    ct = _cross_table(contract_data)
    mapping = ct.get("portrait_write_failure_error_class")
    if not isinstance(mapping, Mapping) or failure_code not in mapping:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "portrait_write_failure_error_class missing for %s" % failure_code,
        )
    return str(mapping[failure_code])


def rewrite_allowed(failure_code: str) -> bool:
    """仅 write_rejected 可重写；field_missing / mapping_undefined 禁重写。"""
    return failure_code == FAIL_WRITE_REJECTED


def assert_mapping_covers_required(
    contract_data: Optional[Mapping[str, Any]] = None,
) -> None:
    """契约必填 8 字段均须在映射表有画像键；否则 mapping_undefined 语义（开工自检）。"""
    required = read_required_layer_fields(contract_data)
    for field in required:
        if field not in A_PRIME_TO_PORTRAIT_FIELD:
            raise CrossTableError(
                "cross_table_contract_invalid",
                "mapping undefined for required field",
                {"a_prime_field": field},
            )
        portrait_key = A_PRIME_TO_PORTRAIT_FIELD[field]
        if not isinstance(portrait_key, str) or portrait_key == "":
            raise CrossTableError(
                "cross_table_contract_invalid",
                "portrait field empty in mapping",
                {"a_prime_field": field},
            )


def mapping_table() -> dict[str, str]:
    """证据用：返回 A' → 画像字段映射副本。"""
    return dict(A_PRIME_TO_PORTRAIT_FIELD)


def _fail(
    failure_code: str,
    detail: str,
    *,
    block_id: Any = None,
    contract_data: Optional[Mapping[str, Any]] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if failure_code not in _FAILURE_CODES:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "unknown portrait write failure_code",
            {"failure_code": failure_code},
        )
    # 契约枚举必须含本码
    allowed = set(read_portrait_write_failure_codes(contract_data))
    if failure_code not in allowed:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "failure_code not in contract enum",
            {"failure_code": failure_code},
        )
    out: dict[str, Any] = {
        "ok": False,
        "failure_code": failure_code,
        "rewrite_allowed": rewrite_allowed(failure_code),
        "error_class": read_failure_error_class(failure_code, contract_data),
        "detail": detail,
        "block_id": block_id,
    }
    if extra:
        out["extra"] = extra
    return out


def _success(block_id: Any, slice_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "block_id": block_id,
        "portrait_slice": slice_data,
        "failure_code": None,
        "rewrite_allowed": False,
    }


def _field_present(layer_result: Mapping[str, Any], a_prime_field: str) -> bool:
    if a_prime_field not in layer_result:
        return False
    value = layer_result.get(a_prime_field)
    if value is None:
        return False
    if a_prime_field in _EMPTY_LIST_OK and isinstance(value, list):
        return True  # 空列表合法
    return True


def build_ok_portrait_slice(
    block_id: Any,
    layer_result: Mapping[str, Any],
    *,
    contract_data: Optional[Mapping[str, Any]] = None,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """按映射表组装成功切片。返回 (slice, None) 或 (None, fail_result)。"""
    required = read_required_layer_fields(contract_data)
    missing: list[str] = []
    for field in required:
        if field not in A_PRIME_TO_PORTRAIT_FIELD:
            return None, _fail(
                FAIL_MAPPING_UNDEFINED,
                "a_prime field has no portrait mapping",
                block_id=block_id,
                contract_data=contract_data,
                extra={"a_prime_field": field},
            )
        portrait_key = A_PRIME_TO_PORTRAIT_FIELD[field]
        if not isinstance(portrait_key, str) or portrait_key == "":
            return None, _fail(
                FAIL_MAPPING_UNDEFINED,
                "portrait field empty in mapping",
                block_id=block_id,
                contract_data=contract_data,
                extra={"a_prime_field": field},
            )
        if not _field_present(layer_result, field):
            missing.append(field)
    if missing:
        return None, _fail(
            FAIL_FIELD_MISSING,
            "success package missing mapped fields",
            block_id=block_id,
            contract_data=contract_data,
            extra={"missing": missing},
        )

    slice_data: dict[str, Any] = {
        PORTRAIT_BLOCK_ID_KEY: block_id,
        PORTRAIT_STRUCTURE_STATUS_KEY: STRUCTURE_STATUS_LAYERS_RECOGNIZED,
    }
    for a_field in required:
        p_field = A_PRIME_TO_PORTRAIT_FIELD[a_field]
        # 原样拷贝；空列表保持 []
        slice_data[p_field] = layer_result[a_field]
    return slice_data, None


def build_no_structure_portrait_slice(block_id: Any) -> dict[str, Any]:
    """无此结构：仅枚举结构标记；不写空层列表冒充。"""
    return {
        PORTRAIT_BLOCK_ID_KEY: block_id,
        PORTRAIT_STRUCTURE_STATUS_KEY: STRUCTURE_STATUS_UPSTREAM_ABSENT,
    }


def b_should_read_layer_objects(portrait_slice: Mapping[str, Any]) -> bool:
    """图 2-甲：upstream_absent → B 不读层对象/空层列表当已识别。"""
    status = portrait_slice.get(PORTRAIT_STRUCTURE_STATUS_KEY)
    if status not in STRUCTURE_STATUS_VALUES:
        return False
    return status == STRUCTURE_STATUS_LAYERS_RECOGNIZED


def write_block_portrait(
    package: Mapping[str, Any],
    store: PortraitStore,
    *,
    contract_data: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """I→A 单次写：入参成功包/无此结构包 → 出参写成功或写失败三码。

    A 不调 A'。review_failed / conflict 包不得进入本函数（由 I 分流）。
    """
    if not isinstance(package, Mapping):
        raise CrossTableError(
            "cross_table_input_invalid",
            "portrait package not object",
        )
    status = package.get("status")
    block_id = package.get("block_id")
    if status not in _ALLOWED_PACKAGE_STATUS:
        raise CrossTableError(
            "cross_table_input_invalid",
            "portrait package status not writable by A",
            {"status": status},
        )

    if status == STATUS_NO_STRUCTURE:
        slice_data = build_no_structure_portrait_slice(block_id)
        try:
            store.write_slice(block_id, slice_data)
        except PortraitWriteRejected as exc:
            return _fail(
                FAIL_WRITE_REJECTED,
                str(exc.message),
                block_id=block_id,
                contract_data=contract_data,
            )
        return _success(block_id, slice_data)

    # status == ok
    layer_result = package.get("layer_result")
    if not isinstance(layer_result, Mapping):
        return _fail(
            FAIL_FIELD_MISSING,
            "ok package missing layer_result object",
            block_id=block_id,
            contract_data=contract_data,
        )
    # 后患 1 防漏网：成功包不得带非 complete 表头
    header = layer_result.get("header_completeness")
    if header is not None and header != "complete":
        raise CrossTableError(
            "cross_table_input_invalid",
            "ok package must not carry non-complete header_completeness",
            {"header_completeness": header},
        )

    slice_data, fail = build_ok_portrait_slice(
        block_id, layer_result, contract_data=contract_data
    )
    if fail is not None:
        return fail
    assert slice_data is not None
    try:
        store.write_slice(block_id, slice_data)
    except PortraitWriteRejected as exc:
        return _fail(
            FAIL_WRITE_REJECTED,
            str(exc.message),
            block_id=block_id,
            contract_data=contract_data,
        )
    return _success(block_id, slice_data)


class PortraitWriteRetryGate:
    """I 侧：仅 write_rejected 可经用户确认重写；受 portrait_write_retry_max 约束。

    每次用户确认消耗一次计数；达上限后即使再确认也不写，由 I 整表停→E。
    """

    def __init__(
        self,
        contract_data: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._max = read_portrait_write_retry_max(contract_data)
        self._used = 0
        self._contract_data = contract_data

    @property
    def max_retries(self) -> int:
        return self._max

    @property
    def used(self) -> int:
        return self._used

    def remaining(self) -> int:
        return max(0, self._max - self._used)

    def can_accept_user_confirm(self, prior_failure_code: str) -> bool:
        if not rewrite_allowed(prior_failure_code):
            return False
        return self._used < self._max

    def user_confirm_rewrite(
        self,
        package: Mapping[str, Any],
        store: PortraitStore,
        *,
        prior_failure_code: str,
    ) -> dict[str, Any]:
        """用户确认一次 → 写一次。禁码或不允许 / 达上限 → 不写盘并返回结构化拒绝。"""
        if prior_failure_code in (FAIL_FIELD_MISSING, FAIL_MAPPING_UNDEFINED):
            return {
                "ok": False,
                "failure_code": prior_failure_code,
                "rewrite_allowed": False,
                "error_class": read_failure_error_class(
                    prior_failure_code, self._contract_data
                ),
                "detail": "rewrite forbidden for this failure_code",
                "retry_exhausted": False,
                "retries_used": self._used,
            }
        if not rewrite_allowed(prior_failure_code):
            return {
                "ok": False,
                "failure_code": prior_failure_code,
                "rewrite_allowed": False,
                "error_class": read_failure_error_class(
                    prior_failure_code, self._contract_data
                )
                if prior_failure_code in _FAILURE_CODES
                else "cross_table_contract_invalid",
                "detail": "rewrite not allowed",
                "retry_exhausted": False,
                "retries_used": self._used,
            }
        if self._used >= self._max:
            return {
                "ok": False,
                "failure_code": FAIL_WRITE_REJECTED,
                "rewrite_allowed": False,
                "error_class": read_failure_error_class(
                    FAIL_WRITE_REJECTED, self._contract_data
                ),
                "detail": "portrait_write_retry_max reached",
                "retry_exhausted": True,
                "retries_used": self._used,
                "retry_max": self._max,
            }
        self._used += 1
        result = write_block_portrait(
            package, store, contract_data=self._contract_data
        )
        result = dict(result)
        result["retries_used"] = self._used
        result["retry_max"] = self._max
        if not result.get("ok") and result.get("failure_code") == FAIL_WRITE_REJECTED:
            # 仍拒绝：若已达上限，标记 exhausted
            result["retry_exhausted"] = self._used >= self._max
            result["rewrite_allowed"] = self._used < self._max
        return result
