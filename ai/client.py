# ai/client.py
"""智谱 GLM API 客户端（OpenAI 兼容协议）。

Base（可配置）: https://open.bigmodel.cn/api/paas/v4
实际请求拼到:   {base}/chat/completions

也支持把 api.base_url 直接写成带 /chat/completions 的完整地址。
模型默认 glm-4.5-air，全部参数走 config.params。
重试规则见 ai.retry（429 分类退避、空正文、进程冷却、总时长上限）。
"""
import json
import re
import threading
from typing import Optional
from urllib.parse import urljoin

import requests
from requests.exceptions import RequestException

import config
from ai.retry import (
    ChatRetryError,
    ERR_OUTPUT_EMPTY,
    ERR_RETRY_TIMEOUT,
    extract_content,
    get_last_retry_log,
    run_with_retry,
)


def resolve_chat_url(base: str = None) -> str:
    """把 paas/v4 根地址规范成 chat/completions 完整 URL。"""
    raw = (base or config.get_param("api.base_url") or "").strip().rstrip("/")
    if not raw:
        raw = "https://open.bigmodel.cn/api/paas/v4"
    if raw.endswith("/chat/completions"):
        return raw
    # 允许用户只配到 v4
    return raw + "/chat/completions"


def ask_llm(prompt: str, api_key: str, max_tokens: int = None,
            temperature: float = None, system: str = None) -> str:
    """单轮文本对话；失败返回错误描述字符串。"""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        data = chat_completion(
            messages, api_key,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        choices = data.get("choices") or []
        if choices:
            return (choices[0].get("message") or {}).get("content") or "抱歉，没有获取到有效回复"
        return "抱歉，没有获取到有效回复"
    except Exception as e:
        return f"请求失败: {e}"


ask_deepseek = ask_llm


class CallCancelled(Exception):
    """任务控制要求中止正在进行的模型请求。"""


def _chat_completion_once(messages: list, api_key: str, *,
                          max_tokens: int = None, temperature: float = None,
                          response_format: dict = None, timeout: int = None,
                          model: str = None, control=None, post=None) -> dict:
    """单次 HTTP 调用，无重试。post 可注入测试替身。"""
    if not api_key:
        raise ValueError("未配置 API Key")
    # M6: local_only hard block + egress audit (no prompt/model change).
    try:
        from pipeline.block_cross_table.memory_rag.egress_audit import (
            assert_external_egress_allowed,
            payload_preview_from_messages,
            record_egress,
        )

        assert_external_egress_allowed(channel="chat")
    except ImportError:
        assert_external_egress_allowed = None  # type: ignore
        payload_preview_from_messages = None  # type: ignore
        record_egress = None  # type: ignore

    model_name = model or config.get_param("api.model")
    body = {
        "model": model_name,
        "messages": messages,
        "max_tokens": int(max_tokens if max_tokens is not None
                          else config.get_param("api.max_tokens")),
        "temperature": (temperature if temperature is not None
                        else config.get_param("api.temperature")),
    }
    if response_format:
        body["response_format"] = response_format
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    limit = int(timeout if timeout is not None else config.get_param("api.timeout"))
    req_timeout = (min(20, limit), limit)
    url = resolve_chat_url()
    payload_chars = len(json.dumps(body, ensure_ascii=False))
    preview = (
        payload_preview_from_messages(messages)
        if payload_preview_from_messages
        else ""
    )

    def _audit(status: str, **extra):
        if record_egress is None:
            return
        try:
            record_egress(
                target_url=url,
                model_name=str(model_name or ""),
                payload_chars=payload_chars,
                payload_preview=preview,
                status=status,
                channel="chat",
                detail=extra,
            )
        except Exception:
            pass

    if post is not None:
        try:
            resp = post(url, headers=headers, json=body, timeout=req_timeout)
            resp.raise_for_status()
            data = resp.json()
            _audit("ok")
            return data
        except Exception as exc:
            _audit("failed", error=str(exc))
            raise

    session = requests.Session()
    stop = threading.Event()

    def _watch():
        while not stop.wait(0.4):
            if control is not None and getattr(control, "cancelled", lambda: False)():
                try:
                    session.close()
                except Exception:
                    pass
                return

    if control is not None:
        threading.Thread(target=_watch, daemon=True).start()
    try:
        if control is not None and getattr(control, "cancelled", lambda: False)():
            raise CallCancelled("用户结束任务")
        resp = session.post(
            url,
            headers=headers,
            json=body,
            timeout=req_timeout,
        )
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            # 401 → Key 与服务商地址不匹配：按 Key 自动识别服务商，写回配置后重试一次
            if resp.status_code == 401:
                from ai import provider as _ai_provider
                _prov = _ai_provider.detect_provider(api_key)
                if _prov:
                    _ai_provider.persist_provider(_prov)
                    url = resolve_chat_url()
                    body = dict(body, model=_prov["model"])
                    resp = session.post(
                        url,
                        headers=headers,
                        json=body,
                        timeout=req_timeout,
                    )
                    resp.raise_for_status()
                else:
                    raise
            else:
                raise
        data = resp.json()
        _audit("ok")
        return data
    except requests.exceptions.ConnectionError:
        if control is not None and getattr(control, "cancelled", lambda: False)():
            raise CallCancelled("用户结束任务")
        _audit("failed", error="connection_error")
        raise
    except Exception as exc:
        _audit("failed", error=str(exc))
        raise
    finally:
        stop.set()
        session.close()


def chat_completion(messages: list, api_key: str, *,
                    max_tokens: int = None, temperature: float = None,
                    response_format: dict = None, timeout: int = None,
                    model: str = None, control=None, post=None,
                    sleeper=None, rng=None, clock=None,
                    enable_retry: bool = True) -> dict:
    """对话补全。默认走统一重试层；enable_retry=False 时只打一次。

    可选 post/sleeper/rng/clock 供验收 mock。最近一次 retry_log 见 get_last_retry_log()。
    """
    initial_max = int(
        max_tokens if max_tokens is not None else config.get_param("api.max_tokens")
    )
    if not enable_retry:
        return _chat_completion_once(
            messages,
            api_key,
            max_tokens=initial_max,
            temperature=temperature,
            response_format=response_format,
            timeout=timeout,
            model=model,
            control=control,
            post=post,
        )

    max_retry_seconds = config.get_param("api.max_retry_seconds")
    if max_retry_seconds is None:
        max_retry_seconds = 900
    max_retry_seconds = float(max_retry_seconds)
    empty_cap = int(config.get_param("api.empty_retry_max_tokens") or 1024)

    def once(opts):
        return _chat_completion_once(
            messages,
            api_key,
            max_tokens=opts.get("max_tokens", initial_max),
            temperature=temperature,
            response_format=response_format,
            timeout=timeout,
            model=model,
            control=control,
            post=post,
        )

    return run_with_retry(
        once,
        max_tokens=initial_max,
        max_retry_seconds=max_retry_seconds,
        empty_max_tokens=empty_cap,
        sleeper=sleeper,
        rng=rng,
        clock=clock,
    )


def _chat_json_ok_seq() -> int:
    n = int(getattr(chat_json, "_ok_seq", 0) or 0) + 1
    chat_json._ok_seq = n
    return n


def _redact_chat_json_raw_for_trace(raw: str) -> str:
    """Persist response body for audit; strip Key/vector-like fields; cap length."""
    text = str(raw or "")
    try:
        obj = json.loads(text)
    except Exception:
        return text[:512].replace("\n", " ")
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in ("api_key", "authorization") or lk.endswith("_api_key"):
                out[k] = "<redacted>"
            elif lk in ("embedding", "vector", "query_embedding"):
                out[k] = "<vector_omitted len=%s>" % (
                    len(v) if isinstance(v, (list, str)) else "?"
                )
            else:
                out[k] = v
        text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    return text[:1500]


def _write_chat_json_ok_trace(
    *,
    run_id,
    system,
    prompt,
    parsed,
    raw,
):
    """Success-path summary + redacted raw body (≤4KB total)."""
    try:
        import hashlib
        from datetime import datetime, timezone
        from pathlib import Path

        base = (
            Path(__file__).resolve().parents[1]
            / "diagnostics"
            / "trace"
            / "chat_json"
            / str(run_id or "default")
        )
        base.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        seq = _chat_json_ok_seq()
        path = base / ("ok_%s_%s.txt" % (stamp, seq))
        sys_s = system or ""
        sys_hash = hashlib.sha256(sys_s.encode("utf-8", errors="replace")).hexdigest()[:16]
        utter = ""
        try:
            body = json.loads(prompt) if prompt else {}
            if isinstance(body, dict):
                utter = str(body.get("utterance") or "")[:80]
        except Exception:
            utter = (prompt or "")[:80]
        rp = parsed.get("result_projection") if isinstance(parsed, dict) else None
        col_ids = []
        if isinstance(rp, dict):
            for c in rp.get("columns") or []:
                if isinstance(c, dict) and c.get("column_id") is not None:
                    col_ids.append(str(c.get("column_id")))
                elif isinstance(c, str):
                    col_ids.append(c)
        elif isinstance(rp, list):
            col_ids = [str(x) for x in rp[:20]]
        keys = sorted(str(k) for k in (parsed or {}).keys()) if isinstance(parsed, dict) else []
        raw_redacted = _redact_chat_json_raw_for_trace(raw or "")
        lines = [
            "ts=%s" % stamp,
            "path=%s" % path.as_posix(),
            "request: system_sha256_16=%s system_len=%s user_prompt_len=%s utterance_prefix=%s"
            % (sys_hash, len(sys_s), len(prompt or ""), utter.replace("\n", " ")),
            "response: status=%s keys=%s column_ids=%s raw_len=%s"
            % (
                (parsed or {}).get("status") if isinstance(parsed, dict) else None,
                ",".join(keys[:40]),
                ",".join(col_ids[:40]),
                len(raw or ""),
            ),
            # Cap preview so ok_trace stays summary-sized (≤4KB; no long raw runs).
            "raw_redacted_len=%s raw_redacted_preview=%s"
            % (len(raw_redacted), raw_redacted[:80]),
        ]
        body = "\n".join(lines) + "\n"
        path.write_text(body[:4096], encoding="utf-8", errors="replace")
        chat_json.last_ok_trace = str(path)
        return str(path)
    except Exception:
        return None


def chat_json(prompt: str, api_key: str, *,
              max_tokens: int = None, temperature: float = None,
              timeout: int = None, system: str = None, control=None,
              trace_run_id: str = None) -> Optional[dict]:
    """要求模型输出 JSON 对象；成功返回 dict，失败返回 None。"""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    raw = ""
    chat_json.last_error = ""
    chat_json.last_raw = ""
    chat_json.last_ok_trace = ""
    try:
        data = chat_completion(
            messages, api_key,
            max_tokens=max_tokens,
            temperature=temperature if temperature is not None else 0.1,
            response_format={"type": "json_object"},
            timeout=timeout,
            control=control,
        )
        # glm thinking models may put JSON in reasoning_content (M7/M10).
        raw = extract_content(data) or ""
        chat_json.last_raw = raw
        parsed = parse_json_object(raw)
        if isinstance(parsed, dict):
            _write_chat_json_ok_trace(
                run_id=trace_run_id or "default",
                system=system,
                prompt=prompt,
                parsed=parsed,
                raw=raw,
            )
        return parsed
    except CallCancelled:
        raise
    except (RequestException, KeyError, ValueError, json.JSONDecodeError) as exc:
        chat_json.last_raw = raw
        snippet = (raw or "")[:400].replace("\n", " ")
        chat_json.last_error = f"{type(exc).__name__}: {exc}"[:240]
        if snippet:
            chat_json.last_error += " | raw=" + snippet
        # Trace for non_dict debugging (short file under diagnostics).
        try:
            from pathlib import Path
            from datetime import datetime, timezone

            trace_dir = Path(__file__).resolve().parents[1] / "diagnostics" / "trace" / "chat_json"
            if trace_run_id:
                trace_dir = trace_dir / str(trace_run_id)
            trace_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            (trace_dir / ("fail_%s.txt" % stamp)).write_text(
                "error=%s\nraw=\n%s\n" % (chat_json.last_error, raw[:8000]),
                encoding="utf-8",
                errors="replace",
            )
        except Exception:
            pass
        return None


def parse_json_object(text: str) -> dict:
    """解析模型输出。剥离 markdown 围栏；顶层数组收成 dict。"""
    text = (text or "").strip()
    # Strip ```json ... ``` / ``` ... ``` wrappers (anywhere, not only edges).
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.I)
    if fence:
        text = fence.group(1).strip()
    else:
        text = re.sub(r"^```(?:json)?", "", text, flags=re.I).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
        if not m:
            raise
        data = json.loads(m.group(1))
    if isinstance(data, list):
        return {"_list": data}
    if not isinstance(data, dict):
        raise json.JSONDecodeError("not object", text, 0)
    return data


def test_connectivity(api_key: str = None) -> dict:
    """连通性自检。返回 {"ok": bool, "message": str, "latency_ms": int}。"""
    import time
    key = api_key if api_key is not None else config.get_api_key()
    if not key:
        return {"ok": False, "message": "未配置智谱 API Key", "latency_ms": 0}
    hint = ""
    if "." not in key and len(key) <= 40:
        hint = ("\n提示：智谱完整 Key 通常形如 id.secret（中间有点）。"
                "若只有一段十六进制，请到开放平台复制完整 Key。")
    t0 = time.time()
    url = resolve_chat_url()
    try:
        data = chat_completion(
            [{"role": "user", "content": "请只回复两个字：连通"}],
            key,
            max_tokens=16,
            temperature=0.1,
            timeout=int(config.get_param("api.timeout")),
        )
        ms = int((time.time() - t0) * 1000)
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        model = data.get("model") or config.get_param("api.model")
        return {
            "ok": True,
            "message": f"智谱连通成功\nURL: {url}\n模型: {model}\n耗时: {ms}ms\n回复: {content[:80]}",
            "latency_ms": ms,
        }
    except RequestException as e:
        ms = int((time.time() - t0) * 1000)
        detail = str(e)
        if hasattr(e, "response") and e.response is not None:
            try:
                detail = e.response.text[:300] or detail
            except Exception:
                pass
        return {
            "ok": False,
            "message": f"智谱连通失败（{ms}ms）\nURL: {url}\n模型: {config.get_param('api.model')}\n详情: {detail}{hint}",
            "latency_ms": ms,
        }
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return {
            "ok": False,
            "message": f"智谱连通失败（{ms}ms）\nURL: {url}\n详情: {e}{hint}",
            "latency_ms": ms,
        }
