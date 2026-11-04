# -*- coding: utf-8 -*-
"""
Single authoritative scan for Direction-1 clearout.
Rules (including industry ban patterns) come from the CURRENT contract
version's authority_scan block — add words only via version publish.
"""
from __future__ import annotations

import ast
import importlib
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .registry import load_contract, read_current_pointer, resolve_contract_dir

ROOT = Path(__file__).resolve().parent.parent.parent

EXCLUDE_DIR_NAMES = {
    "__pycache__",
    ".git",
    "archive",
    "模拟",
    ".session",
}

EXCLUDE_PATH_PARTS = (
    "FileMate报告",
    "archive/legacy_reports",
    "archive\\legacy_reports",
)

SCAN_ROOTS = [
    ROOT / "pipeline",
    ROOT / "contracts",
    ROOT / "agent_core",
    ROOT / "tests",
    ROOT / "config.py",
]


def _excluded(path: Path) -> bool:
    parts = set(path.parts)
    if parts & EXCLUDE_DIR_NAMES:
        return True
    s = str(path)
    return any(x in s for x in EXCLUDE_PATH_PARTS)


def _iter_files() -> List[Path]:
    out: List[Path] = []
    for item in SCAN_ROOTS:
        if item.is_file():
            if not _excluded(item):
                out.append(item)
            continue
        if not item.is_dir():
            continue
        for p in item.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix not in {".py", ".json", ".md", ".txt"}:
                continue
            if _excluded(p):
                continue
            out.append(p)
    return out


def _load_rules() -> dict:
    version = read_current_pointer()
    cv = load_contract(version=version)
    block = cv.data.get("authority_scan")
    if not isinstance(block, dict):
        raise RuntimeError("contract missing authority_scan rules_version block")
    if not block.get("rules_version"):
        raise RuntimeError("authority_scan.rules_version missing")
    return block


def _text_hits(rules: dict) -> List[str]:
    """
    forbidden_substrings apply to code/tests only — not to version JSON where
    the ban list itself is published (would self-fail).
    industry patterns skip the authority_scan block lines in version files.
    """
    bad: List[str] = []
    forbids = [str(x) for x in (rules.get("forbidden_substrings") or [])]
    industry = [str(x) for x in (rules.get("industry_ban_patterns") or [])]
    for p in _iter_files():
        text = p.read_text(encoding="utf-8", errors="replace")
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        is_version_json = rel.startswith("contracts/versions/") and rel.endswith(".json")
        # scanner module may mention tokens only via loaded rules; skip self
        if rel.endswith("pipeline/synth_contract/authority_scan.py"):
            continue
        # forbidden_substrings: code/tests only — not contracts docs (handbook cites bans)
        code_surface = (
            rel.startswith("pipeline/")
            or rel.startswith("tests/")
            or rel.startswith("agent_core/")
            or rel == "config.py"
        )
        if code_surface and p.suffix in {".py", ".md", ".txt"}:
            for s in forbids:
                if s and s in text:
                    bad.append("R_SUBSTR %s :: %s" % (rel, s))
        # industry bans: skip contracts/versions (rules may cite examples)
        if is_version_json:
            continue
        if rel.startswith("contracts/"):
            continue
        for pat in industry:
            if pat and re.search(pat, text):
                bad.append("R8_INDUSTRY %s :: %s" % (rel, pat))
    return bad


def _def_hits(rules: dict) -> List[str]:
    bad: List[str] = []
    names: Set[str] = set(str(x) for x in (rules.get("forbidden_defs") or []))
    synth = ROOT / "pipeline" / "stage3_processor" / "table_synth.py"
    gate = ROOT / "pipeline" / "synth_contract" / "gate.py"
    for path in (synth, gate):
        if not path.is_file():
            bad.append("R_MISSING %s" % path)
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = {
            n.name
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        # also module-level assigns like _STRUCTURE_CACHE
        for n in tree.body:
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id in names:
                        bad.append("R2_ASSIGN %s :: %s" % (path.name, t.id))
        for name in names:
            if name in defined:
                bad.append("R2_DEF %s :: %s" % (path.name, name))
    return bad


def _root_script_hits(rules: dict) -> List[str]:
    bad: List[str] = []
    for name in rules.get("forbidden_root_scripts") or []:
        if (ROOT / str(name)).is_file():
            bad.append("R10_ROOT_SCRIPT %s" % name)
    return bad


def _structure_hits(rules: dict) -> List[str]:
    bad: List[str] = []
    root = resolve_contract_dir()
    ver_dir = root / "versions"
    if not ver_dir.is_dir() or not list(ver_dir.glob("*.json")):
        bad.append("R6_VERSIONS_MISSING")
    try:
        ver = read_current_pointer()
        if not (ver_dir / ("%s.json" % ver)).is_file():
            bad.append("R6_CURRENT_DANGLE %s" % ver)
    except (FileNotFoundError, ValueError) as e:
        bad.append("R6_CURRENT %s" % e)

    synth = (ROOT / "pipeline" / "stage3_processor" / "table_synth.py").read_text(
        encoding="utf-8"
    )
    if "execute_mechanical" not in synth:
        bad.append("R9_NO_MECHANICAL")
    # ban tokens come from versioned rules — do not hardcode them in this file
    for s in rules.get("forbidden_substrings") or []:
        token = str(s)
        if token and token in synth:
            bad.append("R9_FORBIDDEN_IN_SYNTH %s" % token)
    for name in rules.get("forbidden_defs") or []:
        # def presence also covered by AST; keep import-name string check light
        if ("def %s" % name) in synth or ("def %s(" % name) in synth:
            bad.append("R9_DEF_IN_SYNTH %s" % name)
    return bad


def _operator_dispatch_hits() -> List[str]:
    """
    AST-extract OPERATOR_DISPATCH literal keys; must equal CURRENT operator_set.
    Runtime .keys() is not authoritative.
    """
    bad: List[str] = []
    path = ROOT / "pipeline" / "synth_contract" / "ops_plan.py"
    if not path.is_file():
        return ["R_OPSET_MISSING ops_plan.py"]
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as e:
        return ["R_OPSET_PARSE %s" % e]
    keys: Set[str] = set()
    found = False
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) and not isinstance(node, ast.Assign):
            continue
        name = None
        value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            value = node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            t0 = node.targets[0]
            if isinstance(t0, ast.Name):
                name = t0.id
                value = node.value
        if name not in ("OPERATOR_DISPATCH", "DISPATCH"):
            continue
        found = True
        if not isinstance(value, ast.Dict):
            bad.append("R_OPSET_NOT_DICT %s" % name)
            continue
        for k in value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
            elif isinstance(k, ast.Str):  # py<3.8
                keys.add(k.s)
            else:
                bad.append("R_OPSET_NONLITERAL_KEY")
    if not found:
        bad.append("R_OPSET_NO_DISPATCH")
        return bad
    try:
        cv = load_contract(version=read_current_pointer())
    except Exception as e:
        bad.append("R_OPSET_CONTRACT %s" % e)
        return bad
    oset = set()
    for e in cv.data.get("operator_set") or []:
        if isinstance(e, dict) and e.get("op"):
            oset.add(str(e["op"]))
    if keys != oset:
        bad.append(
            "R_OPSET_MISMATCH dispatch=%s operator_set=%s"
            % (sorted(keys), sorted(oset))
        )
    return bad


def _module_dict_str_keys(module_path: Path, var_name: str) -> Optional[Set[str]]:
    """AST 抽取某模块顶层 dict 字面量的字符串 key；不可解析/非字面量 → None。"""
    if not module_path.is_file():
        return None
    try:
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
    except SyntaxError:
        return None
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        if not any(isinstance(t, ast.Name) and t.id == var_name for t in targets):
            continue
        value = node.value
        if not isinstance(value, ast.Dict):
            return None
        keys: Set[str] = set()
        for k in value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
            else:
                return None
        return keys
    return None


def _module_str_dict(module_path: Path, var_name: str) -> Optional[Dict[str, str]]:
    """AST 抽取某模块顶层 {str:str} dict 字面量；值非 str → None。"""
    if not module_path.is_file():
        return None
    try:
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
    except SyntaxError:
        return None
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        if not any(isinstance(t, ast.Name) and t.id == var_name for t in targets):
            continue
        value = node.value
        if not isinstance(value, ast.Dict):
            return None
        out: Dict[str, str] = {}
        for k, v in zip(value.keys, value.values):
            if (isinstance(k, ast.Constant) and isinstance(k.value, str)
                    and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                out[k.value] = v.value
            else:
                return None
        return out
    return None


def _filter_ops_hits() -> List[str]:
    """filter_ops 分区：代码里的算子别名/引擎算子必须与契约一致（漂移即报错）。"""
    bad: List[str] = []
    try:
        cv = load_contract(version=read_current_pointer())
    except Exception as e:
        return ["R_FILTER_OPS_CONTRACT %s" % e]
    block = cv.data.get("filter_ops")
    if not isinstance(block, dict):
        return ["R_FILTER_OPS_MISSING"]
    aliases = block.get("aliases")
    units = block.get("unit_suffixes")
    if not isinstance(aliases, dict) or not aliases:
        bad.append("R_FILTER_OPS_ALIASES_EMPTY")
        return bad
    contract_aliases = {str(k): str(v) for k, v in aliases.items()}
    sm = _module_str_dict(
        ROOT / "pipeline" / "stage3_processor" / "semantic_match.py", "_OPS")
    if sm is None:
        bad.append("R_FILTER_OPS_SEMANTIC_NOT_LITERAL")
    elif sm != contract_aliases:
        bad.append("R_FILTER_OPS_ALIAS_DRIFT semantic_match._OPS != filter_ops.aliases")
    te = _module_dict_str_keys(
        ROOT / "pipeline" / "stage3_processor" / "table_engine.py", "_OPS")
    if te is None:
        bad.append("R_FILTER_OPS_ENGINE_NOT_LITERAL")
    elif te != {str(v) for v in aliases.values()}:
        bad.append("R_FILTER_OPS_ENGINE_DRIFT table_engine._OPS keys != aliases values")
    if not isinstance(units, list) or not units:
        bad.append("R_FILTER_OPS_UNITS_EMPTY")
    return bad


def _function_string_constants(module_path: Path, func_name: str) -> Optional[Set[str]]:
    """AST 抽取某顶层函数的全部字符串常量（用于 qe.py::_cmp_cell 这类 if op in (...) 分发）。"""
    if not module_path.is_file():
        return None
    try:
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
    except SyntaxError:
        return None
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != func_name:
            continue
        consts: Set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                consts.add(sub.value)
        return consts
    return None


def _cmp_aliases_hits() -> List[str]:
    """cmp_aliases 分区：executor._CMP_ALIASES 必须与契约一致。"""
    bad: List[str] = []
    try:
        cv = load_contract(version=read_current_pointer())
    except Exception as e:
        return ["R_CMP_ALIASES_CONTRACT %s" % e]
    block = cv.data.get("cmp_aliases")
    if not isinstance(block, dict):
        return ["R_CMP_ALIASES_MISSING"]
    contract_aliases = {str(k): str(v) for k, v in block.items()}
    ex = _module_str_dict(
        ROOT / "pipeline" / "synth_contract" / "executor.py", "_CMP_ALIASES")
    if ex is None:
        bad.append("R_CMP_ALIASES_NOT_LITERAL")
    elif ex != contract_aliases:
        bad.append("R_CMP_ALIASES_DRIFT executor._CMP_ALIASES != cmp_aliases")
    return bad


def _query_ops_hits() -> List[str]:
    """query_ops 分区：qe.py::_cmp_cell 用的算子 token 必须都在契约声明里。"""
    bad: List[str] = []
    try:
        cv = load_contract(version=read_current_pointer())
    except Exception as e:
        return ["R_QUERY_OPS_CONTRACT %s" % e]
    block = cv.data.get("query_ops")
    if not isinstance(block, dict):
        return ["R_QUERY_OPS_MISSING"]
    tokens = block.get("tokens")
    if not isinstance(tokens, list) or not tokens:
        return ["R_QUERY_OPS_TOKENS_EMPTY"]
    contract_tokens = {str(t) for t in tokens}
    used = _function_string_constants(
        ROOT / "pipeline" / "block_cross_table" / "query_engine" / "qe.py", "_cmp_cell")
    if used is None:
        bad.append("R_QUERY_OPS_NOT_FOUND")
        return bad
    extra = {c for c in used if c} - contract_tokens
    if extra:
        bad.append("R_QUERY_OPS_DRIFT _cmp_cell 用了契约未声明的算子: %s" % sorted(extra))
    return bad


def _enum_values(cv, name: str):
    """在顶层 enums 与 cross_table.enums 里查枚举值；找不到返回 None。"""
    data = getattr(cv, "data", None) or {}
    sources = [data.get("enums") or {}]
    cross = data.get("cross_table")
    if isinstance(cross, dict):
        sources.append(cross.get("enums") or {})
    for src in sources:
        if isinstance(src, dict):
            vals = src.get(name)
            if isinstance(vals, list) and vals:
                return [str(v) for v in vals]
    return None


def _prompt_enum_hits() -> List[str]:
    """prompt 里写死的枚举清单必须覆盖契约枚举（漂移即报错）。

    只做「契约枚举值是否都出现在对应源码里」的轻量文本校验（不硬解析 prompt）。
    """
    bad: List[str] = []
    try:
        cv = load_contract(version=read_current_pointer())
    except Exception as e:
        return ["R_PROMPT_ENUM_CONTRACT %s" % e]
    pairs = [
        ("pipeline/block_cross_table/query_engine/qp_ai.py", "QueryFailLocus"),
        ("agent_core/intent_router.py", "intent_tool"),
        ("pipeline/block_cross_table/layer_recognize_slice.py", "header_completeness"),
    ]
    for rel, enum_name in pairs:
        values = _enum_values(cv, enum_name)
        if not values:
            bad.append("R_PROMPT_ENUM_EMPTY %s" % enum_name)
            continue
        path = ROOT / rel
        if not path.is_file():
            bad.append("R_PROMPT_ENUM_FILE %s" % rel)
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        missing = [str(v) for v in values if str(v) not in text]
        if missing:
            bad.append("R_PROMPT_ENUM_DRIFT %s 缺 %s（契约 %s）"
                       % (rel, ",".join(missing), enum_name))
    return bad


def _dynamic_attr_hits(rules: dict) -> List[str]:
    """
    After import, forbidden symbols must not exist on table_synth / gate namespaces.
    Catches getattr leftovers grep may miss if defs were removed but aliases remain.
    """
    bad: List[str] = []
    names = [str(x) for x in (rules.get("forbidden_defs") or [])]
    modules = [
        "pipeline.stage3_processor.table_synth",
        "pipeline.synth_contract.gate",
    ]
    for mod_name in modules:
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:
            bad.append("R_DYNAMIC_IMPORT_FAIL %s :: %s" % (mod_name, e))
            continue
        for name in names:
            if hasattr(mod, name):
                bad.append("R_DYNAMIC_ATTR %s.%s" % (mod_name, name))
    # config keys from versioned forbidden_substrings that look like dotted params
    try:
        import config as cfg
        cfg_src = (ROOT / "config.py").read_text(encoding="utf-8")
        for s in rules.get("forbidden_substrings") or []:
            token = str(s)
            if "." not in token:
                continue
            if hasattr(cfg, "DEFAULTS") and isinstance(cfg.DEFAULTS, dict):
                if token in cfg.DEFAULTS:
                    bad.append("R3_CONFIG_KEY %s in DEFAULTS" % token)
            if token in cfg_src:
                bad.append("R3_CONFIG_SRC %s" % token)
    except Exception as e:
        bad.append("R3_CONFIG_FAIL %s" % e)
    return bad


def run_scan() -> Tuple[int, List[str]]:
    rules = _load_rules()
    bad: List[str] = []
    bad.extend(_text_hits(rules))
    bad.extend(_def_hits(rules))
    bad.extend(_root_script_hits(rules))
    bad.extend(_structure_hits(rules))
    bad.extend(_dynamic_attr_hits(rules))
    bad.extend(_operator_dispatch_hits())
    bad.extend(_filter_ops_hits())
    bad.extend(_cmp_aliases_hits())
    bad.extend(_query_ops_hits())
    bad.extend(_prompt_enum_hits())
    # dedupe preserve order
    seen = set()
    uniq = []
    for b in bad:
        if b not in seen:
            seen.add(b)
            uniq.append(b)
    return (1 if uniq else 0), uniq


def main() -> int:
    try:
        code, bad = run_scan()
    except Exception as e:
        print("FAIL authority_scan bootstrap: %s" % e)
        return 1
    if code:
        print("FAIL authority_scan (%d)" % len(bad))
        for b in bad:
            print(b)
        return 1
    print(
        "PASS authority_scan rules_version=%s files=%d"
        % (_load_rules().get("rules_version"), len(_iter_files()))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
