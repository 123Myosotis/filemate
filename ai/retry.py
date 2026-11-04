"""统一 API 重试：429 分类退避、空正文识别、进程级冷却、总时长上限。"""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable, Optional


ERR_RETRY_TIMEOUT = "analysis_retry_timeout"
ERR_OUTPUT_EMPTY = "analysis_output_empty"

# 1302 速率限制；1305 访问量过大；其它按 1305 保守
_BACKOFF_1302 = (5, 15, 45, 90, 120)
_BACKOFF_1305 = (30, 90, 270, 600)

_lock = threading.Lock()
_rate_limit_cooldown_until = 0.0
_last_retry_log: list = []


class ChatRetryError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def get_last_retry_log():
    return list(_last_retry_log)


def reset_last_retry_log():
    global _last_retry_log
    _last_retry_log = []


def clear_cooldown_for_tests():
    """仅测试用：清掉进程冷却。"""
    global _rate_limit_cooldown_until
    with _lock:
        _rate_limit_cooldown_until = 0.0


def _now():
    return time.time()


def _sleep(sec, sleeper=None):
    fn = sleeper or time.sleep
    if sec and sec > 0:
        fn(sec)


def _wait_cooldown(sleeper=None, clock=None):
    clock = clock or _now
    with _lock:
        until = _rate_limit_cooldown_until
    now = clock()
    if until > now:
        _sleep(until - now, sleeper)


def _set_cooldown(duration_sec, clock=None):
    global _rate_limit_cooldown_until
    clock = clock or _now
    with _lock:
        _rate_limit_cooldown_until = max(
            _rate_limit_cooldown_until, clock() + max(0.0, float(duration_sec))
        )


def parse_error_code(resp) -> Optional[str]:
    if resp is None:
        return None
    try:
        body = resp.json()
    except Exception:
        return None
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict) and err.get("code") is not None:
        return str(err.get("code"))
    if isinstance(body, dict) and body.get("code") is not None:
        return str(body.get("code"))
    return None


def parse_retry_after(resp) -> Optional[float]:
    if resp is None:
        return None
    headers = getattr(resp, "headers", None) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def backoff_seconds(error_code, attempt_index, retry_after=None, rng=None):
    """attempt_index 从 0 起。Retry-After 优先。"""
    if retry_after is not None:
        base = float(retry_after)
    else:
        code = str(error_code or "1305")
        schedule = _BACKOFF_1302 if code == "1302" else _BACKOFF_1305
        idx = min(int(attempt_index), len(schedule) - 1)
        base = float(schedule[idx])
    # ±20% 抖动
    r = rng.uniform(0.8, 1.2) if rng is not None else random.uniform(0.8, 1.2)
    return max(0.0, base * r)


def extract_content(data) -> str:
    """Prefer message.content; fall back to reasoning_content (thinking-style models)."""
    choice = (data.get("choices") or [{}])[0] or {}
    msg = choice.get("message") or {}
    content = msg.get("content")
    if content is not None and str(content).strip() != "":
        return str(content)
    reasoning = msg.get("reasoning_content")
    if reasoning is not None and str(reasoning).strip() != "":
        return str(reasoning)
    return "" if content is None else str(content)


def is_truncated_empty(data) -> bool:
    """True only when usable text is empty AND finish looks truncated."""
    if not isinstance(data, dict):
        return False
    # Avoid hang: thinking models may fill reasoning_content while content is blank.
    if extract_content(data).strip():
        return False
    choice = (data.get("choices") or [{}])[0] or {}
    finish = str(choice.get("finish_reason") or "").lower()
    return finish in ("length", "max_tokens")


def run_with_retry(
    once_fn: Callable[[dict], Any],
    *,
    max_tokens: int,
    max_retry_seconds: float,
    empty_max_tokens: int = 1024,
    sleeper=None,
    rng=None,
    clock=None,
) -> Any:
    """once_fn(opts) -> API dict 或抛带 .response 的 HTTPError。

    opts: {max_tokens}
    """
    global _last_retry_log
    clock = clock or _now
    sleeper = sleeper or time.sleep
    rng = rng or random.Random()
    t0 = clock()
    retry_log = []
    _last_retry_log = retry_log
    rate_attempt = 0
    did_empty_token_retry = False
    current_max_tokens = int(max_tokens)

    while True:
        elapsed = clock() - t0
        if elapsed >= float(max_retry_seconds):
            raise ChatRetryError(
                ERR_RETRY_TIMEOUT,
                "API retry wall time exceeded",
                {
                    "max_retry_seconds": max_retry_seconds,
                    "elapsed": elapsed,
                    "retry_log": list(retry_log),
                },
            )

        _wait_cooldown(sleeper, clock=clock)

        try:
            data = once_fn({"max_tokens": current_max_tokens})
        except Exception as exc:
            resp = getattr(exc, "response", None)
            status = getattr(resp, "status_code", None) if resp is not None else None
            # requests.HTTPError
            if status == 429 or (
                status is None
                and "429" in str(exc)
            ):
                code = parse_error_code(resp) or "1305"
                # 仅 1302 用短退避；其它 code / 无 code 按 1305 保守
                schedule_code = "1302" if code == "1302" else "1305"
                ra = parse_retry_after(resp)
                sleep_sec = backoff_seconds(schedule_code, rate_attempt, retry_after=ra, rng=rng)
                # 剩余预算不够再睡一整段 → 超时
                if clock() - t0 + sleep_sec > float(max_retry_seconds):
                    retry_log.append(
                        {
                            "attempt": len(retry_log),
                            "error_type": "rate_limit",
                            "http_code": 429,
                            "error_code": code,
                            "sleep_sec": sleep_sec,
                            "success": False,
                        }
                    )
                    raise ChatRetryError(
                        ERR_RETRY_TIMEOUT,
                        "API retry wall time exceeded before next backoff",
                        {
                            "max_retry_seconds": max_retry_seconds,
                            "elapsed": clock() - t0,
                            "retry_log": list(retry_log),
                        },
                    )
                retry_log.append(
                    {
                        "attempt": len(retry_log),
                        "error_type": "rate_limit",
                        "http_code": 429,
                        "error_code": code,
                        "sleep_sec": sleep_sec,
                        "success": False,
                    }
                )
                _set_cooldown(sleep_sec, clock=clock)
                _sleep(sleep_sec, sleeper)
                rate_attempt += 1
                continue
            # 网络类：按 1305 保守退避
            from requests.exceptions import ConnectionError as ReqConn
            from requests.exceptions import Timeout as ReqTimeout
            from requests.exceptions import RequestException

            if isinstance(exc, (ReqTimeout, ReqConn)) or (
                isinstance(exc, RequestException) and status in (408, 500, 502, 503, 504)
            ):
                sleep_sec = backoff_seconds("1305", rate_attempt, rng=rng)
                if clock() - t0 + sleep_sec > float(max_retry_seconds):
                    retry_log.append(
                        {
                            "attempt": len(retry_log),
                            "error_type": "network",
                            "http_code": status,
                            "error_code": None,
                            "sleep_sec": sleep_sec,
                            "success": False,
                        }
                    )
                    raise ChatRetryError(
                        ERR_RETRY_TIMEOUT,
                        "API retry wall time exceeded before next backoff",
                        {
                            "max_retry_seconds": max_retry_seconds,
                            "elapsed": clock() - t0,
                            "retry_log": list(retry_log),
                        },
                    )
                retry_log.append(
                    {
                        "attempt": len(retry_log),
                        "error_type": "network",
                        "http_code": status,
                        "error_code": None,
                        "sleep_sec": sleep_sec,
                        "success": False,
                    }
                )
                _set_cooldown(sleep_sec, clock=clock)
                _sleep(sleep_sec, sleeper)
                rate_attempt += 1
                continue
            raise

        if is_truncated_empty(data):
            if not did_empty_token_retry and current_max_tokens > int(empty_max_tokens):
                did_empty_token_retry = True
                retry_log.append(
                    {
                        "attempt": len(retry_log),
                        "error_type": "output_truncated",
                        "http_code": 200,
                        "error_code": None,
                        "sleep_sec": 0,
                        "success": False,
                        "max_tokens_from": current_max_tokens,
                        "max_tokens_to": int(empty_max_tokens),
                    }
                )
                current_max_tokens = int(empty_max_tokens)
                continue
            retry_log.append(
                {
                    "attempt": len(retry_log),
                    "error_type": "output_empty",
                    "http_code": 200,
                    "error_code": None,
                    "sleep_sec": 0,
                    "success": False,
                }
            )
            raise ChatRetryError(
                ERR_OUTPUT_EMPTY,
                "model returned empty content after truncation handling",
                {
                    "max_tokens": current_max_tokens,
                    "retry_log": list(retry_log),
                },
            )

        retry_log.append(
            {
                "attempt": len(retry_log),
                "error_type": None,
                "http_code": 200,
                "error_code": None,
                "sleep_sec": 0,
                "success": True,
            }
        )
        return data
