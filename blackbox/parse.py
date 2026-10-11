"""Extract the behaviour channel from API responses.

Supports, without any SDK:
* OpenAI-compatible Chat Completions (DeepSeek, most routers), plain and SSE.
* OpenAI Responses API, plain and SSE.
* Anthropic Messages usage fields (for later Claude support).

Output is normalised so different providers can be compared side by side.
"""

from __future__ import annotations

import json
from typing import Any


def normalise_usage(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None

    def first(*keys: str) -> Any:
        for key in keys:
            node: Any = usage
            for part in key.split("."):
                if not isinstance(node, dict) or part not in node:
                    node = None
                    break
                node = node[part]
            if node is not None:
                return node
        return None

    return {
        "input_tokens": first("prompt_tokens", "input_tokens"),
        "output_tokens": first("completion_tokens", "output_tokens"),
        "cached_input_tokens": first(
            "prompt_cache_hit_tokens",  # DeepSeek
            "prompt_tokens_details.cached_tokens",  # OpenAI chat
            "input_tokens_details.cached_tokens",  # OpenAI responses
            "cache_read_input_tokens",  # Anthropic
        ),
        "reasoning_tokens": first(
            "completion_tokens_details.reasoning_tokens",
            "output_tokens_details.reasoning_tokens",
        ),
        "raw": usage,
    }


class ResponseAnalysis:
    """Accumulates usage, tool calls, finish reasons and text size."""

    def __init__(self) -> None:
        self.usage: dict[str, Any] | None = None
        self.finish_reasons: list[str] = []
        self.tool_calls: dict[str, dict[str, Any]] = {}
        self.text_chars = 0
        self.reasoning_chars = 0
        self.model: str | None = None

    # ---- shared helpers -------------------------------------------------
    def _tool(self, key: str, name: str | None = None, args: str | None = None) -> None:
        call = self.tool_calls.setdefault(key, {"name": None, "arguments": ""})
        if name:
            call["name"] = name
        if args:
            call["arguments"] += args

    def _finish(self, reason: Any) -> None:
        if reason:
            self.finish_reasons.append(str(reason))

    # ---- chat completions -----------------------------------------------
    def _chat_message(self, choice: dict[str, Any], idx: int) -> None:
        self._finish(choice.get("finish_reason"))
        msg = choice.get("message") or {}
        self.text_chars += len(msg.get("content") or "")
        self.reasoning_chars += len(msg.get("reasoning_content") or "")
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            self._tool(f"{idx}:{i}", fn.get("name"), fn.get("arguments"))

    def _chat_delta(self, choice: dict[str, Any]) -> None:
        self._finish(choice.get("finish_reason"))
        idx = choice.get("index", 0)
        delta = choice.get("delta") or {}
        self.text_chars += len(delta.get("content") or "")
        self.reasoning_chars += len(delta.get("reasoning_content") or "")
        for tc in delta.get("tool_calls") or []:
            fn = tc.get("function") or {}
            self._tool(f"{idx}:{tc.get('index', 0)}", fn.get("name"), fn.get("arguments"))

    # ---- responses api ----------------------------------------------------
    def _responses_output(self, items: list[Any]) -> None:
        for i, item in enumerate(items or []):
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "function_call":
                key = str(item.get("call_id") or item.get("id") or f"r:{i}")
                self.tool_calls[key] = {
                    "name": item.get("name"),
                    "arguments": item.get("arguments") or "",
                }
            elif kind == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict):
                        self.text_chars += len(part.get("text") or "")

    # ---- entry points -------------------------------------------------------
    def feed_json(self, obj: Any, streamed: bool) -> None:
        if not isinstance(obj, dict):
            return
        self.model = obj.get("model") or self.model
        if obj.get("usage"):
            self.usage = normalise_usage(obj["usage"])

        if "choices" in obj:
            for idx, choice in enumerate(obj.get("choices") or []):
                if not isinstance(choice, dict):
                    continue
                if streamed:
                    self._chat_delta(choice)
                else:
                    self._chat_message(choice, idx)
            return

        # Responses API, non-streamed
        if obj.get("object") == "response":
            self._responses_output(obj.get("output") or [])
            self._finish(obj.get("status"))
            return

        # Responses API, streamed events
        kind = obj.get("type", "")
        if kind in ("response.completed", "response.incomplete", "response.failed"):
            resp = obj.get("response") or {}
            self.model = resp.get("model") or self.model
            if resp.get("usage"):
                self.usage = normalise_usage(resp["usage"])
            self.tool_calls = {}
            self.text_chars = 0
            self._responses_output(resp.get("output") or [])
            self._finish(resp.get("status"))

    def feed_sse(self, raw: str) -> None:
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                self.feed_json(json.loads(data), streamed=True)
            except json.JSONDecodeError:
                continue

    def summary(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "usage": self.usage,
            "finish_reasons": self.finish_reasons,
            "tool_calls": list(self.tool_calls.values()),
            "text_chars": self.text_chars,
            "reasoning_chars": self.reasoning_chars,
        }
