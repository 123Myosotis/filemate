# -*- coding: utf-8 -*-
"""Port resolution: MEMORY_CORRECTION_PORT > GUI/config param > default 8765."""
from __future__ import annotations

import os

DEFAULT_PORT = 8765
PARAM_KEY = "memory_correction.port"
ENV_KEY = "MEMORY_CORRECTION_PORT"


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


def save_gui_port(port: int) -> None:
    import config

    config.set_param(PARAM_KEY, int(port))


def startup_command(port: int | None = None) -> str:
    p = int(port if port is not None else resolve_port())
    return (
        "uvicorn memory_correction.app:app --host 127.0.0.1 --port %s" % p
    )
