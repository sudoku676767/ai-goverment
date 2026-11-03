"""Model channel: ask an OpenAI-compatible chat API (DeepSeek by default) for
wording issues, and keep only issues whose quoted text really occurs in the
document. Standard library only.

Point --base-url at a running blackbox proxy (http://127.0.0.1:8787) to have
every call recorded in the audit log.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .rules import Issue

DEFAULT_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")

SYSTEM_PROMPT = """你是党政机关公文校核员。请找出公文中的以下问题：
1. 错别字（含形近字、同音字、易混词，如“截止/截至”“必须/必需”）；
2. 语病；
3. 文种用语不当（如通知中使用请示的结束语“妥否，请批示”）；
4. 表述不规范。
不要报告标点、发文字号、成文日期等格式问题，这些由规则引擎负责。
没有把握的不要报。每条问题的 text 必须是原文中逐字出现的片段，尽量短（2 到 12 个字）。
只输出 JSON：{"issues":[{"text":"原文片段","type":"错别字|语病|文种用语|表述","suggestion":"建议改为","reason":"理由"}]}。
没有问题时输出 {"issues":[]}。"""


class LLMError(RuntimeError):
    pass


RETRY_DELAYS = (5, 10, 20, 40)  # seconds; DeepSeek returns 503 / empty bodies under load


def _post_once(base_url: str, api_key: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        err = LLMError(f"HTTP {exc.code}: {body}")
        err.retryable = exc.code == 429 or exc.code >= 500
        raise err from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        err = LLMError(f"network: {exc}")
        err.retryable = True
        raise err from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        err = LLMError(f"bad response body: {raw[:100]!r}")
        err.retryable = True
        raise err
    return data


def _post(base_url: str, api_key: str, payload: dict, timeout: float, delays=RETRY_DELAYS) -> dict:
    for delay in (*delays, None):
        try:
            return _post_once(base_url, api_key, payload, timeout)
        except LLMError as exc:
            if delay is None or not getattr(exc, "retryable", False):
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")


def check(
    text: str,
    genre: str,
    *,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    timeout: float = 300.0,
) -> tuple[list[Issue], dict]:
    """Return (issues, stats). stats has tokens, seconds and dropped count."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"文种：{genre}\n\n{text}"},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    t0 = time.monotonic()
    try:
        data = _post(base_url, api_key, payload, timeout)
    except LLMError as exc:
        if "response_format" not in str(exc):
            raise
        payload.pop("response_format")
        data = _post(base_url, api_key, payload, timeout)
    seconds = time.monotonic() - t0

    usage = data.get("usage") or {}
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    raw = _parse(content)

    issues: list[Issue] = []
    dropped = 0
    used: set[int] = set()
    for item in raw:
        snippet = str(item.get("text") or "").strip()
        pos = text.find(snippet) if snippet else -1
        while pos in used and pos >= 0:
            pos = text.find(snippet, pos + 1)
        if pos < 0:
            dropped += 1  # quoted text not in the document: never shown
            continue
        used.add(pos)
        issues.append(
            Issue(
                pos,
                pos + len(snippet),
                snippet,
                str(item.get("type") or "表述"),
                str(item.get("suggestion") or ""),
                str(item.get("reason") or ""),
                channel="model",
            )
        )
    stats = {
        "seconds": round(seconds, 2),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "cached_tokens": usage.get("prompt_cache_hit_tokens", 0),
        "dropped_unquoted": dropped,
    }
    return issues, stats


def _parse(content: str) -> list[dict]:
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content[content.find("{") :]
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < 0:
        return []
    try:
        obj = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return []
    items = obj.get("issues") if isinstance(obj, dict) else None
    return [i for i in items or [] if isinstance(i, dict)]
