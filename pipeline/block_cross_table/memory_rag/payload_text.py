# -*- coding: utf-8 -*-
"""binding_payload → egress text + fingerprint (mechanical; no business words)."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def binding_payload_to_text(binding_payload: Any) -> str:
    """Only this text may leave the process for embeddings input."""
    if isinstance(binding_payload, str):
        return binding_payload
    return json.dumps(binding_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_fingerprint(binding_payload: Any) -> str:
    text = binding_payload_to_text(binding_payload)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_key_for(
    *,
    binding_payload: Any,
    model_id: str,
    endpoint_fingerprint: str,
    embedding_schema_version: str,
) -> str:
    fp = payload_fingerprint(binding_payload)
    raw = "|".join(
        [fp, str(model_id), str(endpoint_fingerprint), str(embedding_schema_version)]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
