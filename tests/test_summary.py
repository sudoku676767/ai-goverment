"""Summary tests on a log shaped like a real DeepSeek Harness run.

The fixture reproduces the structure of the first real session
(2026-10-12, dsh desktop on Windows, DeepSeek-V4-Flash via an
OpenAI-compatible relay): two model-list calls, a title request with
max_tokens=64 that spent its whole budget on reasoning, and four agent
turns glob -> write -> pwsh -> stop.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from blackbox.recorder import Recorder
from blackbox.summary import render_text, summarise
from blackbox.verify import verify_log

TOOLS = [
    {"type": "function", "function": {"name": n, "description": "x" * 400, "parameters": {"type": "object"}}}
    for n in ("glob", "read", "write", "edit", "pwsh")
]
SYSTEM = {"role": "system", "content": "You are a coding agent. " * 200}


def _turn(rec: Recorder, rid: str, *, body, usage, tool=None, finish="stop", text="", reasoning="r"):
    rec.write("request", req_id=rid, method="POST", path="/chat/completions", body={"json": body})
    rec.write(
        "response_end",
        req_id=rid,
        status=200,
        streamed=True,
        first_byte_ms=1000.0,
        duration_ms=5000.0,
        aborted=None,
        truncated=False,
        analysis={
            "usage": usage,
            "finish_reasons": [finish],
            "tool_calls": [{"name": tool, "arguments": "{}"}] if tool else [],
            "text_chars": len(text),
            "reasoning_chars": len(reasoning),
        },
    )


def build_fixture(path: Path) -> None:
    rec = Recorder(path, session="fixture", fsync=False)
    for i in range(2):
        rec.write("request", req_id=f"m{i}", method="GET", path="/models", body=None)
        rec.write("response_end", req_id=f"m{i}", status=200, streamed=False, duration_ms=240.0, analysis={})

    user = {"role": "user", "content": "在工作区创建一个hello.py,打印hello，然后运行它"}
    convo = [SYSTEM, user]
    _turn(
        rec, "a1",
        body={"model": "deepseek-v4-flash-0731", "stream": True, "messages": convo, "tools": TOOLS},
        usage={"input_tokens": 7624, "output_tokens": 241, "cached_input_tokens": 0, "reasoning_tokens": 197},
        tool="glob", finish="tool_calls",
    )
    _turn(
        rec, "t1",
        body={"model": "deepseek-v4-flash-0731", "max_tokens": 64,
              "messages": [{"role": "system", "content": "Create a concise title"}, user]},
        usage={"input_tokens": 199, "output_tokens": 64, "cached_input_tokens": 0, "reasoning_tokens": 64},
        finish="length",
    )
    for rid, inp, out, tool, finish in (
        ("a2", 7880, 80, "write", "tool_calls"),
        ("a3", 8014, 65, "pwsh", "tool_calls"),
        ("a4", 8094, 61, None, "stop"),
    ):
        convo = convo + [{"role": "assistant", "content": "ok"}, {"role": "tool", "content": "result"}]
        _turn(
            rec, rid,
            body={"model": "deepseek-v4-flash-0731", "stream": True, "messages": convo, "tools": TOOLS},
            usage={"input_tokens": inp, "output_tokens": out, "cached_input_tokens": 7168, "reasoning_tokens": 0},
            tool=tool, finish=finish, text="done" if not tool else "",
        )
    rec.close()


class SummaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.log = Path(cls.tmp.name) / "fixture.jsonl"
        build_fixture(cls.log)
        cls.s = summarise(cls.log)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_fixture_is_valid_log(self):
        self.assertTrue(verify_log(self.log)["ok"])

    def test_kinds(self):
        kinds = [r["kind"] for r in self.s["requests"]]
        self.assertEqual(kinds, ["meta", "meta", "agent", "aux", "agent", "agent", "agent"])

    def test_totals_match_real_run(self):
        t = self.s["totals"]
        self.assertEqual(t["all"]["input_tokens"], 31811)
        self.assertEqual(t["all"]["output_tokens"], 511)
        self.assertEqual(t["agent"]["turns"], 4)
        self.assertEqual(t["agent"]["input_tokens"], 31612)
        self.assertEqual(t["aux"]["input_tokens"], 199)
        self.assertEqual(t["agent"]["cache_hit_pct"], round(100 * 21504 / 31612, 1))

    def test_input_delta_skips_aux(self):
        deltas = [r["input_delta"] for r in self.s["requests"] if r["kind"] == "agent"]
        self.assertEqual(deltas, [None, 256, 134, 80])

    def test_title_request_flagged(self):
        self.assertEqual(
            self.s["flagged"],
            [{"row": 4, "kind": "aux", "flags": ["truncated", "reasoning_only"], "max_tokens": 64}],
        )

    def test_prompt_composition(self):
        c = self.s["prompt_composition"]
        self.assertEqual(c["n_tools"], 5)
        self.assertEqual(c["system_chars"], len(SYSTEM["content"]))
        self.assertGreater(c["tool_defs_chars"], 2000)
        self.assertGreater(c["system_chars"] + c["tool_defs_chars"], 10 * c["conversation_chars"])

    def test_render(self):
        text = render_text(self.s)
        for needle in ("agent1", "aux", "meta", "prompt composition", "#4 aux: truncated, reasoning_only, max_tokens=64"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
