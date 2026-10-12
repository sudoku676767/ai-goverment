"""Compare two groups of recorded runs, e.g. clean baseline vs one plugin.

    blackbox compare base-1 base-2 base-3 --vs clever-1 clever-2 clever-3

For every metric it lists the mean and the min..max range of each group and
the difference of the means. With a handful of runs this is not a
significance test, so it stays modest: when the two ranges overlap, the
difference is marked "overlap" and should be read as "not clearly
different". Nothing here says which group is better.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .summary import summarise

# (key, label) in display order
METRICS: list[tuple[str, str]] = [
    ("agent_turns", "agent turns"),
    ("agent_input", "agent input tokens"),
    ("agent_output", "agent output tokens"),
    ("agent_reasoning", "agent reasoning tokens"),
    ("agent_cache_pct", "agent cache hit %"),
    ("first_turn_input", "turn-1 input tokens"),
    ("n_tools", "tools offered"),
    ("tool_defs_chars", "tool definition chars"),
    ("system_chars", "system prompt chars"),
    ("tool_calls", "tool calls"),
    ("repeated_calls", "repeated identical calls"),
    ("aux_requests", "aux requests"),
    ("aux_tokens", "aux tokens (in+out)"),
    ("flags", "flagged requests"),
    ("agent_ms", "agent time ms"),
]


def resolve_log(arg: str, log_dir: str) -> Path:
    """Accept a path or a bare session name like `base-1`."""
    p = Path(os.path.expanduser(arg))
    if p.exists():
        return p
    if os.sep not in arg and not arg.endswith(".jsonl"):
        candidate = Path(os.path.expanduser(log_dir)) / f"{arg}.jsonl"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"log not found: {arg}")


def run_metrics(path: Path) -> dict[str, Any]:
    s = summarise(path)
    a, x = s["totals"]["agent"], s["totals"]["aux"]
    comp = s.get("prompt_composition") or {}
    tool_counts: dict[str, int] = s.get("tool_calls_by_name") or {}
    return {
        "log": str(path),
        "agent_turns": a.get("turns", 0),
        "agent_input": a.get("input_tokens", 0),
        "agent_output": a.get("output_tokens", 0),
        "agent_reasoning": a.get("reasoning_tokens", 0),
        "agent_cache_pct": a.get("cache_hit_pct"),
        "first_turn_input": comp.get("input_tokens"),
        "n_tools": comp.get("n_tools"),
        "tool_defs_chars": comp.get("tool_defs_chars"),
        "system_chars": comp.get("system_chars"),
        "tool_calls": a.get("tool_calls", 0),
        "repeated_calls": a.get("repeated_identical_tool_calls", 0),
        "aux_requests": x.get("requests", 0),
        "aux_tokens": (x.get("input_tokens") or 0) + (x.get("output_tokens") or 0),
        "flags": len(s.get("flagged") or []),
        "agent_ms": a.get("total_ms"),
        "_tool_names": set(comp.get("tool_names") or []),
        "_tool_calls_by_name": tool_counts,
    }


def _stats(values: list[Any]) -> dict[str, Any] | None:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return {"n": len(vals), "mean": sum(vals) / len(vals), "min": min(vals), "max": max(vals)}


def compare_groups(a_logs: list[Path], b_logs: list[Path]) -> dict[str, Any]:
    a_runs = [run_metrics(p) for p in a_logs]
    b_runs = [run_metrics(p) for p in b_logs]

    rows = []
    for key, label in METRICS:
        sa = _stats([r[key] for r in a_runs])
        sb = _stats([r[key] for r in b_runs])
        row: dict[str, Any] = {"key": key, "label": label, "a": sa, "b": sb, "delta": None, "delta_pct": None, "note": ""}
        if sa and sb:
            row["delta"] = sb["mean"] - sa["mean"]
            if sa["mean"]:
                row["delta_pct"] = 100 * row["delta"] / sa["mean"]
            if sa["min"] == sa["max"] == sb["min"] == sb["max"]:
                row["note"] = "same"
            elif sb["min"] > sa["max"] or sb["max"] < sa["min"]:
                row["note"] = "separate"
            else:
                row["note"] = "overlap"
        rows.append(row)

    a_tools = set().union(*(r["_tool_names"] for r in a_runs)) if a_runs else set()
    b_tools = set().union(*(r["_tool_names"] for r in b_runs)) if b_runs else set()

    def call_totals(runs: list[dict[str, Any]]) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in runs:
            for name, n in r["_tool_calls_by_name"].items():
                out[name] = out.get(name, 0) + n
        return {k: v / len(runs) for k, v in out.items()} if runs else {}

    return {
        "a_logs": [str(p) for p in a_logs],
        "b_logs": [str(p) for p in b_logs],
        "metrics": rows,
        "tools_added": sorted(b_tools - a_tools),
        "tools_removed": sorted(a_tools - b_tools),
        "mean_calls_by_tool": {"a": call_totals(a_runs), "b": call_totals(b_runs)},
        "runs": {
            "a": [{k: v for k, v in r.items() if not k.startswith("_")} for r in a_runs],
            "b": [{k: v for k, v in r.items() if not k.startswith("_")} for r in b_runs],
        },
    }


# ---------------------------------------------------------------------------
def _num(v: float | None) -> str:
    if v is None:
        return "-"
    return str(int(round(v))) if abs(v - round(v)) < 1e-9 or abs(v) >= 100 else f"{v:.1f}"


def _cell(st: dict[str, Any] | None) -> str:
    if not st:
        return "-"
    if st["min"] == st["max"]:
        return _num(st["mean"])
    return f"{_num(st['mean'])} [{_num(st['min'])}..{_num(st['max'])}]"


def render_text(c: dict[str, Any], label_a: str = "A", label_b: str = "B") -> str:
    na, nb = len(c["a_logs"]), len(c["b_logs"])
    header = ["metric", f"{label_a} (n={na})", f"{label_b} (n={nb})", "Δ", "Δ%", ""]
    body = []
    for r in c["metrics"]:
        d = r["delta"]
        body.append(
            [
                r["label"],
                _cell(r["a"]),
                _cell(r["b"]),
                "-" if d is None else ("+" if d > 0 else "") + _num(d),
                "-" if r["delta_pct"] is None else f"{r['delta_pct']:+.0f}%",
                r["note"],
            ]
        )
    widths = [max(len(h), *(len(row[j]) for row in body)) for j, h in enumerate(header)]
    out = ["  ".join(h.ljust(widths[j]) for j, h in enumerate(header)).rstrip()]
    out.append("  ".join("-" * w for w in widths))
    out += ["  ".join(v.ljust(widths[j]) for j, v in enumerate(row)).rstrip() for row in body]
    out.append("")
    out.append("values: mean [min..max]. overlap = ranges overlap, not clearly different; separate = no overlap.")

    out.append("")
    out.append(f"tools offered only in {label_b}: " + (", ".join(c["tools_added"]) or "none"))
    out.append(f"tools offered only in {label_a}: " + (", ".join(c["tools_removed"]) or "none"))

    ma, mb = c["mean_calls_by_tool"]["a"], c["mean_calls_by_tool"]["b"]
    names = sorted(set(ma) | set(mb))
    if names:
        out.append("")
        out.append("mean tool calls per run")
        w = max(len(n) for n in names)
        for n in names:
            out.append(f"  {n.ljust(w)}  {label_a} {_num(ma.get(n, 0))}  {label_b} {_num(mb.get(n, 0))}")
    return "\n".join(out)


def render_json(c: dict[str, Any]) -> str:
    return json.dumps(c, ensure_ascii=False, indent=2)
