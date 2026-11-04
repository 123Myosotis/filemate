# -*- coding: utf-8 -*-
"""Query-chain trace kinds; reuses write_trace when a writer is supplied."""
from __future__ import annotations

from typing import Any, Callable, Mapping

from .errors import QueryEngineError

DEFAULT_QUERY_TRACE_KINDS = (
    "query_plan",
    "query_exec_step",
    "query_review",
    "query_user_reply",
    "query_budget_verdict",
)


def read_query_trace_kinds(contract_slice: Mapping[str, Any]) -> frozenset[str]:
    block = contract_slice.get("cross_table") or contract_slice
    enums = block.get("enums") or {}
    members = enums.get("query_trace_kind")
    if not isinstance(members, list) or not members:
        raise QueryEngineError(
            "query_trace_kind enum missing in contract",
            fail_locus="other",
        )
    return frozenset(str(x) for x in members)


class QueryTraceSink:
    """In-memory sink; optional writer(kind, body)->path for disk (e.g. write_trace)."""

    def __init__(
        self,
        *,
        allowed_kinds: frozenset[str],
        writer: Callable[[str, dict[str, Any]], str] | None = None,
    ):
        self.allowed_kinds = allowed_kinds
        self.writer = writer
        self.records: list[dict[str, Any]] = []
        self.paths: list[str] = []

    def write(self, kind: str, body: dict[str, Any]) -> str | None:
        k = str(kind)
        if k not in self.allowed_kinds:
            raise QueryEngineError(
                "call_kind not in query_trace_kind: %s" % k,
                fail_locus="other",
                detail={"call_kind": k},
            )
        payload = {"call_kind": k, **body}
        self.records.append(payload)
        if self.writer is None:
            return None
        path = self.writer(k, payload)
        self.paths.append(path)
        return path
