"""Turn a recorded session into a per-request table and totals.

The summary only lists numbers and mechanical flags. It never says whether a
run was good or bad: that call is left to the person reading it.

Request kinds
-------------
agent  a completion request that offers tools: one turn of the agent loop
aux    a completion request without tools: side jobs such as session
       titles or summaries, which a harness runs next to the main task
meta   anything that is not a completion: model lists, health checks

Flags (mechanical, no judgement)
--------------------------------
truncated       finish reason says the output hit the token limit
reasoning_only  output tokens were all spent on reasoning; no text, no tool call
http_<code>     non-2xx status
aborted         the stream broke off
error           the upstream could not be reached
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

COMPLETION_SUFFIXES = ("/chat/completions", "/completions", "/responses", "/messages")
TRUNCATION_REASONS = {"length", "max_tokens", "max_output_tokens", "incomplete"}


def load_records(path: str | Path) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---------------------------------------------------------------------------
# request side
# ---------------------------------------------------------------------------
def _text_len(content: Any) -> int:
    """Characters of text in a message content field of any provider shape."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, str):
                total += len(part)
            elif isinstance(part, dict):
                inner = part.get("text", part.get("content"))
                total += _text_len(inner)
        return total
    return len(json.dumps(content, ensure_ascii=False))


def request_facts(body: Any) -> dict[str, Any]:
    """Facts that can be read straight off a request body."""
    if not isinstance(body, dict):
        return {}
    tools = body.get("tools") or []
    messages = body.get("messages")
    if messages is None and isinstance(body.get("input"), list):  # responses api
        messages = body["input"]
    messages = messages if isinstance(messages, list) else []

    system_chars = 0
    conversation_chars = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        n = _text_len(m.get("content"))
        if m.get("tool_calls"):
            n += len(json.dumps(m["tool_calls"], ensure_ascii=False))
        if m.get("role") in ("system", "developer"):
            system_chars += n
        else:
            conversation_chars += n
    # anthropic `system`, responses `instructions`
    system_chars += _text_len(body.get("system")) + _text_len(body.get("instructions"))

    tool_names: list[str] = []
    for t in tools if isinstance(tools, list) else []:
        if isinstance(t, dict):
            fn = t.get("function") if isinstance(t.get("function"), dict) else {}
            name = fn.get("name") or t.get("name")
            if name:
                tool_names.append(str(name))

    return {
        "tool_names": tool_names,
        "model": body.get("model"),
        "stream": bool(body.get("stream")),
        "max_tokens": body.get("max_tokens")
        or body.get("max_completion_tokens")
        or body.get("max_output_tokens"),
        "n_tools": len(tools) if isinstance(tools, list) else 0,
        "n_messages": len(messages),
        "system_chars": system_chars,
        "tool_defs_chars": len(json.dumps(tools, ensure_ascii=False)) if tools else 0,
        "conversation_chars": conversation_chars,
    }


def classify(path: str | None, method: str | None, facts: dict[str, Any]) -> str:
    p = (path or "").split("?")[0].rstrip("/")
    if method != "POST" or not p.endswith(COMPLETION_SUFFIXES):
        return "meta"
    return "agent" if facts.get("n_tools") else "aux"


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------
def _flags(row: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    status = row.get("status")
    if status is not None and not 200 <= int(status) < 300:
        flags.append(f"http_{status}")
    if row.get("error"):
        flags.append("error")
    if row.get("aborted"):
        flags.append("aborted")
    if set(row.get("finish_reasons") or []) & TRUNCATION_REASONS:
        flags.append("truncated")
    if (
        row["kind"] != "meta"
        and not row.get("tool_calls")
        and (row.get("text_chars") or 0) == 0
        and ((row.get("reasoning_chars") or 0) > 0 or (row.get("reasoning_tokens") or 0) > 0)
    ):
        flags.append("reasoning_only")
    return flags


def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def s(key: str) -> int:
        return sum(int(r.get(key) or 0) for r in rows)

    inp, cached = s("input_tokens"), s("cached_input_tokens")
    durations = [r["duration_ms"] for r in rows if r.get("duration_ms") is not None]
    ttfb = [r["first_byte_ms"] for r in rows if r.get("first_byte_ms") is not None]
    return {
        "requests": len(rows),
        "input_tokens": inp,
        "output_tokens": s("output_tokens"),
        "cached_input_tokens": cached,
        "cache_hit_pct": round(100 * cached / inp, 1) if inp else None,
        "reasoning_tokens": s("reasoning_tokens"),
        "tool_calls": sum(len(r.get("tool_calls") or []) for r in rows),
        "total_ms": round(sum(durations), 1) if durations else None,
        "mean_first_byte_ms": round(sum(ttfb) / len(ttfb), 1) if ttfb else None,
    }


def summarise(path: str | Path) -> dict[str, Any]:
    records = load_records(path)
    requests: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for rec in records:
        event = rec.get("event")
        rid = rec.get("req_id")
        if event == "request":
            body = (rec.get("body") or {}).get("json")
            facts = request_facts(body)
            requests[rid] = {
                "seq": rec["seq"],
                "ts": rec.get("ts"),
                "method": rec.get("method"),
                "path": rec.get("path"),
                "kind": classify(rec.get("path"), rec.get("method"), facts),
                **facts,
            }
            order.append(rid)
        elif event == "response_end" and rid in requests:
            a = rec.get("analysis") or {}
            u = a.get("usage") or {}
            requests[rid].update(
                status=rec.get("status"),
                input_tokens=u.get("input_tokens"),
                output_tokens=u.get("output_tokens"),
                cached_input_tokens=u.get("cached_input_tokens"),
                reasoning_tokens=u.get("reasoning_tokens"),
                tool_calls=a.get("tool_calls") or [],
                finish_reasons=a.get("finish_reasons") or [],
                text_chars=a.get("text_chars"),
                reasoning_chars=a.get("reasoning_chars"),
                first_byte_ms=rec.get("first_byte_ms"),
                duration_ms=rec.get("duration_ms"),
                aborted=rec.get("aborted"),
                truncated_log=rec.get("truncated"),
            )
        elif event == "error" and rid in requests:
            requests[rid]["error"] = f"[{rec.get('stage')}] {rec.get('error')}"

    rows = [requests[r] for r in order]

    # per-row derived numbers
    prev_agent_input: int | None = None
    agent_turn = 0
    for r in rows:
        inp, cached = r.get("input_tokens"), r.get("cached_input_tokens")
        r["cache_hit_pct"] = round(100 * cached / inp, 1) if inp and cached is not None else None
        r["input_delta"] = None
        if r["kind"] == "agent":
            agent_turn += 1
            r["turn"] = agent_turn
            if inp is not None:
                if prev_agent_input is not None:
                    r["input_delta"] = inp - prev_agent_input
                prev_agent_input = inp
        r["flags"] = _flags(r)

    agent = [r for r in rows if r["kind"] == "agent"]
    aux = [r for r in rows if r["kind"] == "aux"]
    meta = [r for r in rows if r["kind"] == "meta"]

    tool_names: Counter[str] = Counter()
    call_signatures: Counter[tuple[str, str]] = Counter()
    for r in agent:
        for tc in r.get("tool_calls", []):
            name = tc.get("name") or "?"
            tool_names[name] += 1
            call_signatures[(name, tc.get("arguments") or "")] += 1

    composition = None
    if agent:
        first = agent[0]
        composition = {
            "turn": 1,
            "input_tokens": first.get("input_tokens"),
            "n_tools": first.get("n_tools"),
            "tool_names": first.get("tool_names") or [],
            "system_chars": first.get("system_chars"),
            "tool_defs_chars": first.get("tool_defs_chars"),
            "conversation_chars": first.get("conversation_chars"),
        }

    agent_totals = _totals(agent)
    agent_totals["turns"] = len(agent)
    agent_totals["repeated_identical_tool_calls"] = sum(n - 1 for n in call_signatures.values() if n > 1)
    if agent and agent[0].get("input_tokens") is not None and agent[-1].get("input_tokens") is not None:
        agent_totals["input_first_turn"] = agent[0]["input_tokens"]
        agent_totals["input_last_turn"] = agent[-1]["input_tokens"]

    return {
        "requests": rows,
        "totals": {
            "all": _totals(rows),
            "agent": agent_totals,
            "aux": _totals(aux),
            "meta": {"requests": len(meta)},
        },
        "prompt_composition": composition,
        "tool_calls_by_name": dict(tool_names.most_common()),
        "flagged": [
            {"row": i, "kind": r["kind"], "flags": r["flags"], "max_tokens": r.get("max_tokens")}
            for i, r in enumerate(rows, start=1)
            if r["flags"]
        ],
        "errors": [r["error"] for r in rows if r.get("error")],
    }


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _fmt(v: Any) -> str:
    return "-" if v is None else str(v)


def _signed(v: Any) -> str:
    if v is None:
        return "-"
    return f"+{v}" if v > 0 else str(v)


def _table(header: list[str], body: list[list[str]]) -> list[str]:
    widths = [max([len(h)] + [len(row[j]) for row in body]) for j, h in enumerate(header)]
    out = ["  ".join(h.ljust(widths[j]) for j, h in enumerate(header)).rstrip()]
    out.append("  ".join("-" * w for w in widths))
    out += ["  ".join(v.ljust(widths[j]) for j, v in enumerate(row)).rstrip() for row in body]
    return out


def render_text(s: dict[str, Any]) -> str:
    header = ["#", "kind", "path", "st", "in", "cache%", "Δin", "out", "reason", "tools", "finish", "ms", "flags"]
    body = []
    for i, r in enumerate(s["requests"], start=1):
        kind = r["kind"] if r["kind"] != "agent" else f"agent{r['turn']}"
        body.append(
            [
                str(i),
                kind,
                _fmt(r.get("path")),
                _fmt(r.get("status")),
                _fmt(r.get("input_tokens")),
                _fmt(r.get("cache_hit_pct")),
                _signed(r.get("input_delta")),
                _fmt(r.get("output_tokens")),
                _fmt(r.get("reasoning_tokens")),
                ",".join(tc.get("name") or "?" for tc in r.get("tool_calls", [])) or "-",
                ",".join(r.get("finish_reasons", [])) or "-",
                _fmt(r.get("duration_ms")),
                ",".join(r.get("flags", [])) or "",
            ]
        )
    out = _table(header, body)

    t = s["totals"]
    out.append("")
    out.append("totals")
    out += _table(
        ["", "requests", "in", "cached", "cache%", "out", "reason", "tools", "ms"],
        [
            [
                name,
                _fmt(t[name].get("requests")),
                _fmt(t[name].get("input_tokens")),
                _fmt(t[name].get("cached_input_tokens")),
                _fmt(t[name].get("cache_hit_pct")),
                _fmt(t[name].get("output_tokens")),
                _fmt(t[name].get("reasoning_tokens")),
                _fmt(t[name].get("tool_calls")),
                _fmt(t[name].get("total_ms")),
            ]
            for name in ("agent", "aux", "all")
        ],
    )
    a = t["agent"]
    out.append(f"  meta requests: {t['meta']['requests']}")
    out.append(f"  agent turns: {a.get('turns', 0)}, repeated identical tool calls: {a.get('repeated_identical_tool_calls', 0)}")
    if "input_first_turn" in a:
        out.append(f"  agent input first turn -> last turn: {a['input_first_turn']} -> {a['input_last_turn']}")

    c = s.get("prompt_composition")
    if c:
        total_chars = (c["system_chars"] or 0) + (c["tool_defs_chars"] or 0) + (c["conversation_chars"] or 0)

        def share(n: int) -> str:
            return f"{round(100 * n / total_chars)}%" if total_chars else "-"

        out.append("")
        out.append(f"prompt composition, agent turn 1 ({_fmt(c['input_tokens'])} input tokens, by characters)")
        out.append(f"  system prompt     {c['system_chars']:>8} chars  {share(c['system_chars'])}")
        out.append(f"  tool definitions  {c['tool_defs_chars']:>8} chars  {share(c['tool_defs_chars'])}  ({c['n_tools']} tools)")
        out.append(f"  conversation      {c['conversation_chars']:>8} chars  {share(c['conversation_chars'])}")

    if s["tool_calls_by_name"]:
        out.append("")
        out.append("tool calls by name (agent turns)")
        for name, n in s["tool_calls_by_name"].items():
            out.append(f"  {name}: {n}")

    if s["flagged"]:
        out.append("")
        out.append("flagged")
        for f in s["flagged"]:
            extra = f", max_tokens={f['max_tokens']}" if f.get("max_tokens") else ""
            out.append(f"  #{f['row']} {f['kind']}: {', '.join(f['flags'])}{extra}")

    if s["errors"]:
        out.append("")
        out.append("errors")
        for e in s["errors"]:
            out.append(f"  {e}")
    return "\n".join(out)


def render_json(s: dict[str, Any]) -> str:
    return json.dumps(s, ensure_ascii=False, indent=2)
