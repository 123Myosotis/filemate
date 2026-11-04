"""跨表数据结构。"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class FieldRef(TypedDict):
    field_id: str
    block_id: str | int
    parent_table_id: str
    subtable_id: NotRequired[int | None]
    role: str
    target_name: str
    source_labels: list[str]
    source_label: str
    in_dimensions: bool
    value_samples: list[str]


class ExactMatchGroup(TypedDict):
    candidate_id: str
    member_refs: list[FieldRef]
    matched_on: Literal["target_name", "source_label"]
    decision_status: Literal["proposed", "confirmed", "rejected", "deferred"]


class SynonymCandidate(TypedDict):
    candidate_id: str
    member_refs: list[FieldRef]
    proposal_status: Literal["ok", "ambiguous", "invalid"]
    ai_raw: dict[str, Any]
    decision_status: Literal["proposed", "confirmed", "rejected", "deferred"]


class CrossTableQuestion(TypedDict):
    question_id: str
    interrupt_kind: Literal[
        "concept_bind",
        "synonym_merge",
        "exact_merge",
        "alias_link",
        "align_key_pick",
        "unmatched_policy",
        "output_grain",
    ]
    hypothesis: str
    evidence: str
    if_holds: str
    ask_user: str
    candidate_id: str
    field_refs: list[FieldRef]
    allowed_replies: list[Literal["confirm", "reject", "defer"]]
    recommended_reply: NotRequired[Literal["confirm", "reject", "defer"] | None]


class UserReplyRecord(TypedDict):
    question_id: str
    reply: Literal["confirm", "reject", "defer"]
    reply_text: str
    selected_field_id: str | None
    answered_at: str


class RowLink(TypedDict):
    link_id: str
    hypothesis_id: str
    left_field_id: str
    right_field_id: str
    left_value: str
    right_value: str
    link_kind: Literal["exact", "alias"]
    decision_status: Literal["proposed", "confirmed", "rejected", "deferred"]
    proposal_status: NotRequired[Literal["ok", "ambiguous", "invalid"] | None]


class DecisionLog(TypedDict):
    fingerprint: str
    contract_version: str
    field_refs: list[FieldRef]
    exact_groups: list[ExactMatchGroup]
    synonym_candidates: list[SynonymCandidate]
    questions: list[CrossTableQuestion]
    replies: list[UserReplyRecord]
    align_keys: list[dict[str, Any]]
    row_links: list[RowLink]
    ai_raws: list[dict[str, Any]]
    needs_retry_history: NotRequired[list[dict[str, Any]]]
