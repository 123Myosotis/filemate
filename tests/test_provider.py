# -*- coding: utf-8 -*-
"""provider 自动识别（按 Key 选地址）契约回归。"""
from __future__ import annotations

from ai.provider import PROVIDERS, detect_provider, persist_provider


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


def test_detect_provider_finds_deepseek():
    def fake_post(url, headers=None, json=None, timeout=None):
        return _Resp(200 if "api.deepseek.com" in url else 401)

    prov = detect_provider("sk-test", post=fake_post)
    assert prov is not None
    assert prov["id"] == "deepseek"


def test_detect_provider_finds_siliconflow():
    def fake_post(url, headers=None, json=None, timeout=None):
        return _Resp(200 if "api.siliconflow.cn" in url else 401)

    prov = detect_provider("sk-test", post=fake_post)
    assert prov is not None
    assert prov["id"] == "siliconflow"


def test_detect_provider_none_when_all_fail():
    def fake_post(url, headers=None, json=None, timeout=None):
        return _Resp(401)

    assert detect_provider("sk-test", post=fake_post) is None


def test_detect_provider_no_key():
    assert detect_provider("", post=lambda *a, **k: _Resp(200)) is None


def test_providers_have_required_fields():
    for p in PROVIDERS:
        assert p.get("id") and p.get("base_url") and p.get("model")


def test_persist_provider_writes_config(monkeypatch):
    calls = {}
    monkeypatch.setattr("config.set_param", lambda k, v: calls.__setitem__(k, v))
    assert persist_provider({"base_url": "https://x", "model": "m"}) is True
    assert calls["api.base_url"] == "https://x"
    assert calls["api.model"] == "m"


def test_persist_provider_rejects_empty():
    assert persist_provider({}) is False
