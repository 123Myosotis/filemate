# -*- coding: utf-8 -*-
"""
P2-B Query UI FastAPI app.

Listen 127.0.0.1 only. Auth: X-Memory-Token (file-backed). No API keys in responses.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from web.memory_correction.auth import (
    FAIL_LOCUS_AUTH,
    TOKEN_HEADER,
    ensure_token,
    validate_token,
)
from web.query_ui.export_store import cleanup_expired, resolve_export_file
from web.query_ui.port_settings import resolve_port, startup_command
from web.query_ui.service import build_ask_history_continuation, run_query

ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))


def _upload_root() -> Path:
    env = os.environ.get("QUERY_UI_UPLOAD_DIR")
    if env and str(env).strip():
        p = Path(str(env).strip())
    else:
        p = Path(tempfile.gettempdir()) / "filemate_query_ui_uploads"
    p.mkdir(parents=True, exist_ok=True)
    return p


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_token(ROOT)
    cleanup_expired()
    yield


app = FastAPI(title="query_ui", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


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


class RunBody(BaseModel):
    utterance: str = Field(default="")
    file_paths: list[str] = Field(default_factory=list)
    ask_history: list[dict[str, Any]] | None = None
    # Convenience fields for five-part continue (assembled server-side)
    user_reply: str | None = None
    prior_five_part: dict[str, Any] | None = None
    prior_fail_locus: str | None = None


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "port_hint": resolve_port(),
        "startup": startup_command(),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    token = ensure_token(ROOT)
    # Starlette>=0.37: TemplateResponse(request, name, context)
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "token": token,
            "token_header": TOKEN_HEADER,
        },
    )


@app.post("/api/query/upload")
async def upload_files(
    files: list[UploadFile] = File(...),
    _: None = Depends(require_token),
) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="files_required")
    session = uuid.uuid4().hex
    dest_dir = _upload_root() / session
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, str]] = []
    for uf in files:
        name = Path(uf.filename or "upload.xlsx").name
        # only allow spreadsheet-like names by extension from client filename
        suffix = Path(name).suffix.lower()
        allowed = {
            s.strip().lower()
            for s in (
                os.environ.get("QUERY_UI_UPLOAD_EXTS") or ".xlsx,.xlsm,.xls"
            ).split(",")
            if s.strip()
        }
        if suffix not in allowed:
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise HTTPException(
                status_code=400,
                detail={"error": "unsupported_extension", "suffix": suffix},
            )
        target = dest_dir / name
        data = await uf.read()
        max_b = int(os.environ.get("QUERY_UI_UPLOAD_MAX_BYTES") or (80 * 1024 * 1024))
        if len(data) > max_b:
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise HTTPException(status_code=400, detail="file_too_large")
        target.write_bytes(data)
        saved.append({"name": name, "path": str(target)})
    return {
        "session_id": session,
        "files": saved,
        "file_paths": [x["path"] for x in saved],
    }


@app.post("/api/query/run")
def query_run(
    body: RunBody,
    _: None = Depends(require_token),
) -> dict[str, Any]:
    history = list(body.ask_history or [])
    reply = (body.user_reply or "").strip()
    if reply:
        history = build_ask_history_continuation(
            prior_five_part=body.prior_five_part,
            prior_fail_locus=body.prior_fail_locus,
            user_reply=reply,
            prior_ask_history=history,
        )
    result = run_query(
        utterance=body.utterance,
        file_paths=list(body.file_paths or []),
        ask_history=history or None,
    )
    # Never echo API keys / raw prompts
    if isinstance(result, dict):
        result.pop("api_key", None)
        result.pop("raw_ai", None)
        result["ask_history"] = history
    return result


@app.get("/api/query/download/{export_id}")
def download_export(
    export_id: str,
    _: None = Depends(require_token),
) -> FileResponse:
    path = resolve_export_file(export_id)
    if path is None:
        raise HTTPException(status_code=404, detail="export_expired_or_missing")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    # Let FastAPI's own HTTPException handler deal with HTTPException.
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return JSONResponse(
        status_code=500,
        content={"status": "failed", "error": type(exc).__name__},
    )
