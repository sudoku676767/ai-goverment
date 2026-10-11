"""Turn a recorded session into a per-request table and totals.

The summary only lists numbers. It never says whether a run was good or bad:
that call is left to the person reading it.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def load_records(path: str | Path) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def summarise(path: str | Path) -> dict[str, Any]:
    records = load_records(path)
    requests: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    errors: list[dict[str, Any]] = []

    for rec in records:
        event = rec.get("event")
        rid = rec.get("req_id")
        if event == "request":
            requests[rid] = {"seq": rec["seq"], "method": rec.get("method"), "path": rec.get("path")}
            order.append(rid)
        elif event == "response_end" and rid in requests:
            a = rec.get("analysis") or {}
            u = a.get("usage") or {}
            requests[rid].update(
                status=rec.get("status"),
                streamed=rec.get("streamed"),
                input_tokens=u.get("input_tokens"),
                output_tokens=u.get("output_tokens"),
                cached_input_tokens=u.get("cached_input_tokens"),
                reasoning_tokens=u.get("reasoning_tokens"),
                tool_calls=a.get("tool_calls") or [],
                finish_reasons=a.get("finish_reasons") or [],
                first_byte_ms=rec.get("first_byte_ms"),
                duration_ms=rec.get("duration_ms"),
                aborted=rec.get("aborted"),
                truncated=rec.get("truncated"),
            )
        elif event == "error":
            errors.append({"req_id": rid, "stage": rec.get("stage"), "error": rec.get("error")})

    rows = [requests[r] for r in order]

    def total(key: str) -> int:
        return sum(int(r.get(key) or 0) for r in rows)

    tool_names: Counter[str] = Counter()
    call_signatures: Counter[tuple[str, str]] = Counter()
    for r in rows:
        for tc in r.get("tool_calls", []):
            name = tc.get("name") or "?"
            tool_names[name] += 1
            call_signatures[(name, tc.get("arguments") or "")] += 1

    repeated = sum(n - 1 for n in call_signatures.values() if n > 1)
    finish = Counter(fr for r in rows for fr in r.get("finish_reasons", []))
    ttfb = [r["first_byte_ms"] for r in rows if r.get("first_byte_ms") is not None]

    return {
        "requests": rows,
        "totals": {
            "requests": len(rows),
            "input_tokens": total("input_tokens"),
            "output_tokens": total("output_tokens"),
            "cached_input_tokens": total("cached_input_tokens"),
            "reasoning_tokens": total("reasoning_tokens"),
            "tool_calls": sum(tool_names.values()),
            "repeated_identical_tool_calls": repeated,
            "errors": len(errors),
            "aborted": sum(1 for r in rows if r.get("aborted")),
            "truncated_by_max_tokens": finish.get("length", 0) + finish.get("max_tokens", 0),
            "mean_first_byte_ms": round(sum(ttfb) / len(ttfb), 1) if ttfb else None,
        },
        "tool_calls_by_name": dict(tool_names.most_common()),
        "finish_reasons": dict(finish),
        "errors": errors,
    }


def _fmt(v: Any) -> str:
    return "-" if v is None else str(v)


def render_text(s: dict[str, Any]) -> str:
    cols = ["#", "path", "status", "in", "out", "cached", "reason", "tools", "finish", "ms"]
    lines = []
    for i, r in enumerate(s["requests"], start=1):
        lines.append(
            [
                str(i),
                _fmt(r.get("path")),
                _fmt(r.get("status")),
                _fmt(r.get("input_tokens")),
                _fmt(r.get("output_tokens")),
                _fmt(r.get("cached_input_tokens")),
                _fmt(r.get("reasoning_tokens")),
                ",".join(tc.get("name") or "?" for tc in r.get("tool_calls", [])) or "-",
                ",".join(r.get("finish_reasons", [])) or "-",
                _fmt(r.get("duration_ms")),
            ]
        )
    widths = [max(len(c), *(len(row[j]) for row in lines)) if lines else len(c) for j, c in enumerate(cols)]
    out = ["  ".join(c.ljust(widths[j]) for j, c in enumerate(cols))]
    out.append("  ".join("-" * w for w in widths))
    out += ["  ".join(v.ljust(widths[j]) for j, v in enumerate(row)) for row in lines]

    t = s["totals"]
    out.append("")
    out.append("totals")
    for key, value in t.items():
        out.append(f"  {key}: {_fmt(value)}")
    if s["tool_calls_by_name"]:
        out.append("tool calls by name")
        for name, n in s["tool_calls_by_name"].items():
            out.append(f"  {name}: {n}")
    if s["errors"]:
        out.append("errors")
        for e in s["errors"]:
            out.append(f"  [{e['stage']}] {e['error']}")
    return "\n".join(out)


def render_json(s: dict[str, Any]) -> str:
    return json.dumps(s, ensure_ascii=False, indent=2)
