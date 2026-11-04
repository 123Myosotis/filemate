# -*- coding: utf-8 -*-
"""Port resolution for query UI (env > config > default)."""
from __future__ import annotations

import os

DEFAULT_PORT = 8770
PARAM_KEY = "query_ui.port"
ENV_KEY = "QUERY_UI_PORT"


def resolve_port() -> int:
    env = os.environ.get(ENV_KEY)
    if env is not None and str(env).strip() != "":
        return int(str(env).strip())
    try:
        import config

        val = config.get_param(PARAM_KEY)
        if val is not None and str(val).strip() != "":
            return int(val)
    except Exception:
        pass
    return int(DEFAULT_PORT)


def startup_command(port: int | None = None) -> str:
    p = int(port if port is not None else resolve_port())
    return "uvicorn web.query_ui.app:app --host 127.0.0.1 --port %s" % p
