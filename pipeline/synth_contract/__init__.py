# -*- coding: utf-8 -*-
"""AI-plan / local-execute contract: versioned enums + mechanical validation only."""
from .registry import (
    ContractVersion,
    load_contract,
    resolve_contract_dir,
    verify_contract_hash,
)
from .scope import Scope, parse_scope, scope_covers, scopes_disjoint
from .executor import execute_mechanical
from .gate import (
    attach_contract_meta,
    gate_plan,
    reject_payload,
)
from .boot import (
    ContractLibraryError,
    assert_contract_library_ready,
    ensure_contract_library,
)
from .normalize import (
    apply_ops,
    compare_with_null_policy,
    normalize_key,
    validate_normalize_spec,
)
from .validate import (
    ValidationIssue,
    ValidationResult,
    pick_state,
    validate_binding,
    validate_dsl_continuity,
    validate_enum,
    validate_global_judgment,
    validate_join_spec,
    validate_plan_envelope,
    validate_region_cover,
    validate_row_cover,
    validate_verifiable_basis,
)
from .accuracy import enforce_declared_accuracy
from .calc import enforce_calc_and_order, replay_calc_spec
from .xref import enforce_xref_and_caliber
from .budget import enforce_budgets
from .dag import enforce_dag_and_review

__all__ = [
    "ContractLibraryError",
    "ContractVersion",
    "Scope",
    "ValidationIssue",
    "ValidationResult",
    "apply_ops",
    "assert_contract_library_ready",
    "attach_contract_meta",
    "compare_with_null_policy",
    "enforce_budgets",
    "enforce_calc_and_order",
    "enforce_dag_and_review",
    "enforce_declared_accuracy",
    "enforce_xref_and_caliber",
    "ensure_contract_library",
    "execute_mechanical",
    "gate_plan",
    "load_contract",
    "normalize_key",
    "parse_scope",
    "pick_state",
    "reject_payload",
    "replay_calc_spec",
    "resolve_contract_dir",
    "scope_covers",
    "scopes_disjoint",
    "validate_binding",
    "validate_dsl_continuity",
    "validate_enum",
    "validate_global_judgment",
    "validate_join_spec",
    "validate_normalize_spec",
    "validate_plan_envelope",
    "validate_region_cover",
    "validate_row_cover",
    "validate_verifiable_basis",
    "verify_contract_hash",
]
