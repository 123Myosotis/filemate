# -*- coding: utf-8 -*-
"""
Memory correction FastAPI app (M4–M4.6 + M5-CPU-Merge + M5.2/M5.3 + M6).

Listen only on 127.0.0.1. Auth: X-Memory-Token. No Cookie / Set-Cookie.
M6: dialogue-review learning (no dir scan); egress privacy audit.
"""
from __future__ import annotations

import json
import os
import threading
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pipeline.block_cross_table.memory_rag.bootstrap import open_memory_db
from pipeline.block_cross_table.memory_rag.dialogue_review import (
    append_dialogue_turn,
    end_dialogue_session,
    list_dialogue_sessions,
    start_dialogue_session,
)
from pipeline.block_cross_table.memory_rag.egress_audit import (
    list_egress_audits,
    register_audit_sink,
)
from pipeline.block_cross_table.memory_rag.jargon_dedup import (
    list_open_conflicts,
    resolve_conflict,
)
from pipeline.block_cross_table.memory_rag.jargon_miner import (
    import_txt_and_mine,
    list_mining_jobs,
    mining_status_summary,
)
from pipeline.block_cross_table.memory_rag.jargon_store import list_filemate_jargons
from pipeline.block_cross_table.memory_rag.merge_settings import (
    load_merge_runtime_settings,
    load_model_settings_public,
    save_merge_settings_patch,
)
from pipeline.block_cross_table.memory_rag.settings_kv import (
    get_maibot_data_dir,
    set_maibot_data_dir,
)
from web.memory_correction.directory_picker import (
    pick_directory,
    validate_maibot_data_dir,
)
from web.memory_correction.auth import (
    FAIL_LOCUS_AUTH,
    TOKEN_HEADER,
    ensure_token,
    validate_token,
)
from web.memory_correction.service import (
    confirm_memory_edit,
    discard_proposal,
    list_bindings,
    list_proposals,
    list_traces,
    propose_memory_edit,
    purge_local_test_data,
    replay_memory_edit,
    revoke_memory_edit,
)
from web.memory_correction.system_context import (
    build_general_binding_payload,
    fingerprint_for_term,
)

ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))
TEMPLATES.env.filters["tojson_zh"] = lambda v: json.dumps(
    v, ensure_ascii=False, indent=2, default=str
)

_DB_PATH = os.environ.get("MEMORY_CORRECTION_DB")
_CONN = None
# Single shared sqlite3 Connection is not thread-safe. FastAPI runs sync
# routes in a threadpool; concurrent UI refresh + mutate caused
# InterfaceError / dirty rows → browser "Failed to fetch".
# Use Lock (not RLock): yield-deps may acquire/release on different worker
# threads; Lock may be released by any thread, RLock cannot.
_DB_LOCK = threading.Lock()


def _embedding_dim() -> int:
    env = os.environ.get("MEMORY_CORRECTION_EMBEDDING_DIM")
    if env is not None and str(env).strip() != "":
        return int(str(env).strip())
    fix = (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "fixtures"
        / "memory_rag"
        / "contract_slice_fixture.json"
    )
    data = json.loads(fix.read_text(encoding="utf-8"))
    return int(data["test_only_numeric_overrides"]["embedding_dim"])


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return int(default)
    return int(str(raw).strip())


def _audit_conn():
    """Return shared conn under lock for egress sink (same process)."""
    with _DB_LOCK:
        return _CONN


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _CONN
    ensure_token(ROOT)
    dim = _embedding_dim()
    with _DB_LOCK:
        _CONN = open_memory_db(
            _DB_PATH if _DB_PATH else None,
            embedding_dim=dim,
            enable_vec=True,
        )
    register_audit_sink(_audit_conn)
    yield
    register_audit_sink(None)
    with _DB_LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None


app = FastAPI(title="memory_correction", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


def get_conn():
    """
    Yield the process-global SQLite connection while holding _DB_LOCK
    for the whole request (serialize mutate + parallel refreshAll).
    """
    if _CONN is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    _DB_LOCK.acquire()
    try:
        yield _CONN
    finally:
        _DB_LOCK.release()


def require_token(
    x_memory_token: str | None = Header(default=None, alias=TOKEN_HEADER),
) -> None:
    if not validate_token(x_memory_token, ROOT):
        raise HTTPException(
            status_code=401,
            detail={
                "fail_locus": FAIL_LOCUS_AUTH,
                "subreason": "missing_or_bad_token",
            },
        )


@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception):
    """Always return JSON 500 — never drop the TCP connection silently."""
    # HTTPException has its own handler; this catches the rest.
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={
            "fail_locus": "memory_web_unhandled",
            "subreason": type(exc).__name__,
            "message": str(exc)[:400],
            "path": str(request.url.path),
        },
    )


@app.middleware("http")
async def no_set_cookie_and_loopback_host(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    if host and host not in ("127.0.0.1", "localhost", "testserver"):
        return JSONResponse(
            status_code=403,
            content={"fail_locus": "memory_web_host", "subreason": "non_loopback"},
        )
    response = await call_next(request)
    if "set-cookie" in response.headers:
        del response.headers["set-cookie"]
    return response


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, conn=Depends(get_conn)):
    token = ensure_token(ROOT)
    trace_limit = _env_int("MEMORY_CORRECTION_TRACE_LIST_LIMIT", 50)
    try:
        bindings = list_bindings(conn) or []
    except Exception:
        bindings = []
    try:
        proposals = list_proposals(conn, status="proposed") or []
    except Exception:
        proposals = []
    try:
        traces = list_traces(conn, limit=trace_limit) or []
    except Exception:
        traces = []
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "token": token,
            "token_header": TOKEN_HEADER,
            "bindings": bindings,
            "proposals": proposals,
            "traces": traces,
            "trace_limit": trace_limit,
        },
    )


@app.get("/api/bindings")
def api_bindings(
    status: str | None = Query(default=None),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    try:
        items = list_bindings(conn, status=status) or []
    except Exception:
        items = []
    return {"bindings": items}


@app.get("/api/proposals")
def api_proposals(
    status: str = Query(default="proposed"),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    try:
        items = list_proposals(conn, status=status) or []
    except Exception:
        items = []
    return {"proposals": items}


@app.get("/api/traces")
def api_traces(
    limit: int | None = Query(default=None),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    lim = int(limit) if limit is not None else _env_int(
        "MEMORY_CORRECTION_TRACE_LIST_LIMIT", 50
    )
    try:
        items = list_traces(conn, limit=lim) or []
    except Exception:
        items = []
    return {"traces": items}


@app.post("/api/propose_wizard")
def api_propose_wizard(
    term: str = Form(""),
    description: str = Form(""),
    scope_text: str = Form(""),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    """Generalized knowledge propose. Confirm-before-write."""
    try:
        payload = build_general_binding_payload(
            term=term,
            description=description,
            scope=scope_text or "",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"fail_locus": "memory_web_wizard", "subreason": str(exc)},
        ) from exc
    from pipeline.block_cross_table.memory_rag.constants import SOURCE_WIZARD

    fp = fingerprint_for_term(term)
    scene = str(scope_text or "").strip()
    return propose_memory_edit(
        conn,
        utterance_span_fingerprint=fp,
        binding_payload=payload,
        scope={
            "source": "wizard_m45_general",
            "scene": scene,
        },
        source_type=SOURCE_WIZARD,
    )


@app.post("/api/propose_memory_edit")
def api_propose(
    utterance_span_fingerprint: str = Form(...),
    binding_payload_text: str = Form(...),
    scope_text: str = Form("{}"),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    try:
        payload = json.loads(binding_payload_text)
    except Exception:
        payload = binding_payload_text
    try:
        scope = json.loads(scope_text) if scope_text else {}
    except Exception:
        scope = {}
    return propose_memory_edit(
        conn,
        utterance_span_fingerprint=utterance_span_fingerprint,
        binding_payload=payload,
        scope=scope,
    )


@app.post("/api/confirm_memory_edit")
def api_confirm(
    proposal_id: str = Form(...),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    try:
        return confirm_memory_edit(conn, proposal_id=proposal_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"fail_locus": "memory_web_confirm", "subreason": str(exc)},
        ) from exc


@app.post("/api/discard_proposal")
def api_discard(
    proposal_id: str = Form(...),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    try:
        return discard_proposal(conn, proposal_id=proposal_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"fail_locus": "memory_web_discard", "subreason": str(exc)},
        ) from exc


@app.post("/api/revoke_memory_edit")
def api_revoke(
    binding_id: str = Form(...),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    try:
        return revoke_memory_edit(conn, binding_id=binding_id)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={"fail_locus": "memory_web_revoke", "message": str(exc)},
        ) from exc


@app.post("/api/replay_memory_edit")
def api_replay(
    trace_id: str = Form(...),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    try:
        return replay_memory_edit(conn, trace_id=trace_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail={"fail_locus": "memory_web_replay", "subreason": str(exc)},
        ) from exc


def _settings_payload(conn) -> dict:
    settings = load_merge_runtime_settings(
        maibot_data_dir=get_maibot_data_dir(conn) or None
    )
    model = load_model_settings_public()
    from pipeline.block_cross_table.memory_rag.dense_provider import (
        public_dense_provider_view,
    )
    from pipeline.block_cross_table.memory_rag.external_embedding_config import (
        load_external_embedding_public,
    )

    ext = load_external_embedding_public()
    dense = public_dense_provider_view()
    provider = str(dense.get("dense_provider") or "off")
    return {
        "maibot_data_dir": settings.maibot_data_dir,
        "self_learning_engine": settings.self_learning_engine,
        "meaning_equivalence_mode": settings.meaning_equivalence_mode,
        "meaning_similarity_threshold": settings.meaning_similarity_threshold,
        "meaning_fallback_threshold": settings.meaning_fallback_threshold,
        "jargon_inference_thresholds": list(settings.jargon_inference_thresholds),
        "egress_policy": settings.egress_policy,
        "dialogue_review_enabled": settings.dialogue_review_enabled,
        "embedded_model_path": settings.embedded_model_path,
        "dense_provider": provider,
        "dense_provider_migrated_from": dense.get("dense_provider_migrated_from"),
        "migration_conflict": dense.get("migration_conflict"),
        "migration_conflict_acknowledged": bool(
            dense.get("migration_conflict_acknowledged")
        ),
        "show_migration_banner": bool(dense.get("show_migration_banner")),
        "migration_banner_text": dense.get("migration_banner_text"),
        "api_key_masked": ext.api_key_masked,
        "ui": {
            "show_local_path": provider == "local",
            "show_external_fields": provider == "external",
            "show_off_hint": provider == "off",
            "off_hint": "当前不启用向量，仅 FTS+黑话检索",
        },
        "fusion": {
            "rrf_k": settings.rrf_k,
            "vector_weight": settings.vector_weight,
            "bm25_weight": settings.bm25_weight,
            "external_weight": settings.external_weight,
        },
        "model": model,
        "external_embedding": {
            "enabled": provider == "external",
            "base_url": ext.base_url,
            "model": ext.model,
            "weight": ext.weight,
            "api_key_configured": ext.api_key_configured,
            "api_key_masked": ext.api_key_masked,
            "api_key_source": ext.api_key_source,
            "status_hint": (
                "外部 API 向量已配置"
                if provider == "external" and ext.api_key_configured and ext.weight > 0
                else (
                    "外部 API 已填（当前未选用或权重为 0）"
                    if ext.api_key_configured
                    else "外部 API Key 未配置"
                )
            ),
        },
    }


@app.get("/api/settings")
def api_get_settings(
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    return _settings_payload(conn)


@app.post("/api/settings")
async def api_post_settings(
    request: Request,
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    """Unified settings save (JSON body preferred; form also accepted)."""
    patch: dict = {}
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            body = await request.json()
            if isinstance(body, dict):
                patch = body
        except Exception:
            patch = {}
    else:
        form = await request.form()
        for k in form.keys():
            patch[str(k)] = form.get(k)

    # Nested fusion from flat form fields
    if "vector_weight" in patch or "bm25_weight" in patch or "rrf_k" in patch:
        fusion = dict(patch.get("fusion") or {})
        if "vector_weight" in patch:
            fusion["vector_weight"] = patch.pop("vector_weight")
        if "bm25_weight" in patch:
            fusion["bm25_weight"] = patch.pop("bm25_weight")
        if "rrf_k" in patch:
            fusion["rrf_k"] = patch.pop("rrf_k")
        patch["fusion"] = fusion

    if "maibot_data_dir" in patch:
        set_maibot_data_dir(conn, str(patch.get("maibot_data_dir") or ""))

    # Migration conflict banner: user picks local / external / off then ack.
    if patch.get("migration_conflict_acknowledged") or patch.get(
        "migration_conflict_choice"
    ):
        from pipeline.block_cross_table.memory_rag.dense_provider import (
            acknowledge_migration_conflict,
        )

        choice = patch.pop("migration_conflict_choice", None)
        patch.pop("migration_conflict_acknowledged", None)
        if choice is not None:
            acknowledge_migration_conflict(choice=str(choice))
        else:
            acknowledge_migration_conflict(choice=None)

    save_merge_settings_patch(patch)
    return {"status": "ok", "settings": _settings_payload(conn)}


@app.post("/api/settings/maibot_data_dir")
def api_set_maibot_dir(
    maibot_data_dir: str = Form(""),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    path = set_maibot_data_dir(conn, maibot_data_dir)
    check = validate_maibot_data_dir(path)
    return {"maibot_data_dir": path, "status": "ok", "validation": check}


# Injectable for unit tests (mock OS folder dialog).
_PICK_DIRECTORY_FN = None


def set_pick_directory_fn_for_tests(fn) -> None:
    global _PICK_DIRECTORY_FN
    _PICK_DIRECTORY_FN = fn


@app.get("/api/pick_directory")
def api_pick_directory(
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    """
    M5.3: pop system folder dialog (tkinter); echo path + auto-save maibot_data_dir.
    Tests inject via set_pick_directory_fn_for_tests.
    """
    result = pick_directory(ask_directory_fn=_PICK_DIRECTORY_FN)
    if result.get("status") != "ok" or not result.get("path"):
        return result
    path = set_maibot_data_dir(conn, str(result["path"]))
    check = validate_maibot_data_dir(path)
    return {
        "status": "ok",
        "cancelled": False,
        "path": path,
        "maibot_data_dir": path,
        "validation": check,
        "auto_saved": True,
    }


@app.get("/api/settings/maibot_data_dir/validate")
def api_validate_maibot_dir(
    path: str | None = Query(default=None),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    target = path if path is not None else get_maibot_data_dir(conn)
    return validate_maibot_data_dir(str(target or ""))


@app.get("/api/dashboard")
def api_dashboard(
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    settings = load_merge_runtime_settings(
        maibot_data_dir=get_maibot_data_dir(conn) or None
    )
    mining = mining_status_summary(conn)
    jobs = list_mining_jobs(conn, limit=10)
    proposals = list_proposals(conn, status="proposed") or []
    conflicts = list_open_conflicts(conn) or []
    sessions = list_dialogue_sessions(conn, limit=10)
    egress = list_egress_audits(conn, limit=30)
    success_n = sum(1 for j in jobs if j.get("status") == "done")
    fail_n = sum(1 for j in jobs if j.get("status") not in ("done", "running"))
    # Merge pending + conflicts for unified board (source labels retained).
    inbox = []
    for p in proposals:
        inbox.append({"kind": "pending", **p})
    for c in conflicts:
        inbox.append({"kind": "conflict", **c})
    has_local_embed = bool((settings.embedded_model_path or "").strip())
    engine = settings.self_learning_engine
    if has_local_embed:
        retrieval_hint = "检索：FTS + 本地向量可并行（已配置本地模型路径）"
    elif engine == "chat":
        retrieval_hint = "检索：无向量也能出结果（FTS + 黑话文本；chat 引擎默认）"
    else:
        retrieval_hint = "检索：向量引擎；未配本地模型时仍走 FTS/黑话兜底"
    return {
        "self_learning_engine": settings.self_learning_engine,
        "meaning_equivalence_mode": settings.meaning_equivalence_mode,
        "egress_policy": settings.egress_policy,
        "dialogue_review_enabled": settings.dialogue_review_enabled,
        "embedded_model_path_set": has_local_embed,
        "retrieval_hint": retrieval_hint,
        "mining": mining,
        "jobs": jobs,
        "job_stats": {
            "total": len(jobs),
            "success": success_n,
            "failed": fail_n,
            "running": sum(1 for j in jobs if j.get("status") == "running"),
        },
        "pending_proposals": proposals,
        "conflicts": conflicts,
        "inbox": inbox,
        "dialogue_sessions": sessions,
        "egress_audits": egress,
    }


@app.post("/api/admin/purge_local_memory")
def api_purge_local_memory(
    confirm: str = Form(...),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    """
    One-click clear FileMate local memory test/learning data.
    Does not touch MaiBot. Requires confirm=PURGE.
    """
    if str(confirm or "").strip() != "PURGE":
        raise HTTPException(
            status_code=400,
            detail={
                "fail_locus": "memory_purge_local",
                "subreason": "confirm_required",
                "hint": "confirm must be PURGE",
            },
        )
    return purge_local_test_data(conn)


@app.post("/api/dialogue/sessions")
def api_dialogue_start(
    title: str = Form(""),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    return start_dialogue_session(conn, title=title)


@app.post("/api/dialogue/turns")
def api_dialogue_turn(
    session_id: str = Form(...),
    role: str = Form(...),
    content: str = Form(...),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    try:
        return append_dialogue_turn(
            conn, session_id=session_id, role=role, content=content
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"fail_locus": "memory_dialogue", "subreason": "not_found"},
        ) from exc
    except (ValueError, Exception) as exc:
        from pipeline.block_cross_table.memory_rag.errors import MemoryRagError

        if isinstance(exc, MemoryRagError):
            raise HTTPException(
                status_code=400,
                detail=exc.as_report(),
            ) from exc
        raise HTTPException(
            status_code=400,
            detail={"fail_locus": "memory_dialogue", "subreason": str(exc)},
        ) from exc


@app.post("/api/dialogue/end")
def api_dialogue_end(
    session_id: str = Form(...),
    async_review: str = Form("0"),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    settings = load_merge_runtime_settings(
        maibot_data_dir=get_maibot_data_dir(conn) or None
    )
    if not settings.dialogue_review_enabled:
        raise HTTPException(
            status_code=400,
            detail={
                "fail_locus": "memory_dialogue",
                "subreason": "dialogue_review_disabled",
            },
        )
    async_flag = str(async_review or "").strip().lower() in ("1", "true", "yes", "on")
    try:
        return end_dialogue_session(
            conn,
            session_id=session_id,
            settings=settings,
            async_review=async_flag,
        )
    except Exception as exc:
        from pipeline.block_cross_table.memory_rag.errors import MemoryRagError

        if isinstance(exc, MemoryRagError):
            raise HTTPException(status_code=400, detail=exc.as_report()) from exc
        raise HTTPException(
            status_code=400,
            detail={"fail_locus": "memory_dialogue", "subreason": str(exc)},
        ) from exc


@app.get("/api/dialogue/sessions")
def api_dialogue_list(
    limit: int = Query(default=20),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    return {"sessions": list_dialogue_sessions(conn, limit=limit) or []}


@app.get("/api/egress/audits")
def api_egress_audits(
    limit: int = Query(default=50),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    return {"audits": list_egress_audits(conn, limit=limit) or []}


@app.get("/api/mining/status")
def api_mining_status(
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    try:
        return mining_status_summary(conn)
    except Exception:
        return {
            "latest_job": None,
            "open_conflict_count": 0,
            "pending_proposal_count": 0,
            "filemate_jargon_count": 0,
        }


@app.get("/api/mining/jobs")
def api_mining_jobs(
    limit: int = Query(default=20),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    try:
        return {"jobs": list_mining_jobs(conn, limit=limit) or []}
    except Exception:
        return {"jobs": []}


@app.post("/api/mining/import_txt")
async def api_mining_import_txt(
    file: UploadFile = File(...),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    settings = load_merge_runtime_settings(
        maibot_data_dir=get_maibot_data_dir(conn) or None
    )
    # Injectable chat is not available over HTTP; live chat_json used.
    # Tests call import_txt_and_mine directly with chat_json_fn.
    try:
        result = import_txt_and_mine(
            conn,
            text=text,
            source_name=file.filename or "upload.txt",
            settings=settings,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "fail_locus": "memory_txt_import",
                "subreason": str(exc),
            },
        ) from exc
    return result


@app.get("/api/conflicts")
def api_conflicts(
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    try:
        return {"conflicts": list_open_conflicts(conn) or []}
    except Exception:
        return {"conflicts": []}


@app.post("/api/conflicts/resolve")
def api_resolve_conflict(
    conflict_id: str = Form(...),
    resolution: str = Form(...),
    note: str = Form(""),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    try:
        return resolve_conflict(
            conn,
            conflict_id=conflict_id,
            resolution=resolution,
            note=note,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"fail_locus": "memory_jargon_conflict", "subreason": "not_found"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"fail_locus": "memory_jargon_conflict", "subreason": str(exc)},
        ) from exc


@app.get("/api/jargons")
def api_jargons(
    limit: int = Query(default=100),
    _auth: None = Depends(require_token),
    conn=Depends(get_conn),
):
    try:
        return {"jargons": list_filemate_jargons(conn, limit=limit) or []}
    except Exception:
        return {"jargons": []}
