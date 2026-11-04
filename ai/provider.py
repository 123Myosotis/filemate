# -*- coding: utf-8 -*-
"""按 API Key 自动识别服务商（provider）：「用户给什么 Key，就用什么地址」。

做法：拿 Key 逐个探已知服务商的最小 chat 请求，第一个返回 200 的即该 Key 所属服务商；
全失败返回 None。识别结果可写回 config.json 的 api.base_url / api.model。
"""
from __future__ import annotations

import requests

# 已知服务商：id / 聊天接口根地址 / 默认模型
PROVIDERS = [
    {"id": "deepseek", "base_url": "https://api.deepseek.com/v1",
     "model": "deepseek-chat"},
    {"id": "siliconflow", "base_url": "https://api.siliconflow.cn/v1",
     "model": "deepseek-ai/DeepSeek-V3.1-Terminus"},
    {"id": "zhipu", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "model": "glm-4-flash"},
    {"id": "moonshot", "base_url": "https://api.moonshot.cn/v1",
     "model": "moonshot-v1-8k"},
]


def _chat_url(base_url: str) -> str:
    base = str(base_url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def detect_provider(api_key: str, *, timeout: int = 8, post=None) -> dict | None:
    """逐个探已知服务商；返回第一个能正常 200 的 provider dict，全失败返回 None。

    post: 可选注入（测试用），签名 post(url, headers, json, timeout) -> resp。
    """
    if not api_key:
        return None
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    }
    for prov in PROVIDERS:
        body = {
            "model": prov["model"],
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "temperature": 0.0,
        }
        try:
            if post is not None:
                resp = post(_chat_url(prov["base_url"]), headers=headers,
                            json=body, timeout=timeout)
            else:
                resp = requests.post(_chat_url(prov["base_url"]), headers=headers,
                                     json=body, timeout=timeout)
            if getattr(resp, "status_code", 0) == 200:
                return dict(prov)
        except Exception:
            continue
    return None


def persist_provider(prov: dict) -> bool:
    """把识别到的 provider 写回 config.json 的 api.base_url / api.model。"""
    if not isinstance(prov, dict) or not prov.get("base_url"):
        return False
    try:
        import config
        config.set_param("api.base_url", prov["base_url"])
        config.set_param("api.model", prov.get("model"))
        return True
    except Exception:
        return False
