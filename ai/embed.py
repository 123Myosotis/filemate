# -*- coding: utf-8 -*-
"""通用 embedding（SiliconFlow / BGE-M3）——供 `word_office.memory` 的向量通道用。

**与 Excel 的 `memory_rag` 无关**（不 import、不碰它的代码/表）；只是**同样的 HTTP 形状**。
Key 来源：`config` 的 `api.embedding_key`（空则不启用）。
"""
from __future__ import annotations

import json
from typing import Optional

import requests

import config

_DEFAULT_MODEL = "BAAI/bge-m3"
_DEFAULT_URL = "https://api.siliconflow.cn/v1/embeddings"


def is_available() -> bool:
    return bool(str(config.get_param("api.embedding_key") or "").strip())


def embed(text: str, *, model: str = "", timeout: int = 20) -> Optional[list]:
    """返回向量；**没配 Key / 调用失败** → `None`（调用方自行回退）。"""
    key = str(config.get_param("api.embedding_key") or "").strip()
    if not key:
        return None
    body = {"model": model or _DEFAULT_MODEL, "input": str(text or "")[:4000]}
    try:
        resp = requests.post(
            _DEFAULT_URL,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json=body, timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        vec = data["data"][0]["embedding"]
        return [float(x) for x in vec]
    except Exception:  # noqa: BLE001
        return None


def cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
