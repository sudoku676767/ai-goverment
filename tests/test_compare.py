"""compare: baseline runs vs runs with a plugin that adds tools and a turn."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from blackbox.cli import main
from blackbox.compare import compare_groups
from blackbox.recorder import Recorder

BASE_TOOLS = ["glob", "read", "write", "pwsh"]
PLUGIN_TOOLS = BASE_TOOLS + ["todo_write", "reflect"]


def make_run(path: Path, tools: list[str], inputs: list[int], calls: list[str | None], system: str) -> None:
    rec = Recorder(path, session=path.stem, fsync=False)
    defs = [{"type": "function", "function": {"name": n, "description": "d" * 300}} for n in tools]
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": "make hello.py"}]
    for i, (inp, call) in enumerate(zip(inputs, calls)):
        rid = f"r{i}"
        rec.write("request", req_id=rid, method="POST", path="/chat/completions",
                  body={"json": {"model": "m", "messages": msgs, "tools": defs}})
        rec.write("response_end", req_id=rid, status=200, duration_ms=1000.0, analysis={
            "usage": {"input_tokens": inp, "output_tokens": 50, "cached_input_tokens": int(inp * 0.9) if i else 0,
                      "reasoning_tokens": 10},
            "finish_reasons": ["tool_calls" if call else "stop"],
            "tool_calls": [{"name": call, "arguments": "{}"}] if call else [],
            "text_chars": 0 if call else 4, "reasoning_chars": 5,
        })
    rec.close()


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.base, self.plug = [], []
        for i, jitter in enumerate((0, 40, -20)):
            p = d / f"base-{i + 1}.jsonl"
            make_run(p, BASE_TOOLS, [7600 + jitter, 7900, 8000, 8100], ["glob", "write", "pwsh", None], "S" * 6000)
            self.base.append(p)
            q = d / f"plug-{i + 1}.jsonl"
            make_run(q, PLUGIN_TOOLS, [9400 + jitter, 9700, 9800, 9900, 10000],
                     ["todo_write", "glob", "write", "pwsh", None], "S" * 7000)
            self.plug.append(q)
        self.c = compare_groups(self.base, self.plug)
        self.by = {m["key"]: m for m in self.c["metrics"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_tools_added(self):
        self.assertEqual(self.c["tools_added"], ["reflect", "todo_write"])
        self.assertEqual(self.c["tools_removed"], [])

    def test_turns_and_notes(self):
        t = self.by["agent_turns"]
        self.assertEqual((t["a"]["mean"], t["b"]["mean"], t["delta"]), (4, 5, 1))
        self.assertEqual(t["note"], "separate")
        self.assertEqual(self.by["n_tools"]["delta"], 2)
        self.assertEqual(self.by["system_chars"]["delta"], 1000)
        self.assertEqual(self.by["repeated_calls"]["note"], "same")

    def test_overlap_marked(self):
        a = Path(self.tmp.name) / "x1.jsonl"
        b = Path(self.tmp.name) / "x2.jsonl"
        make_run(a, BASE_TOOLS, [100, 200], ["glob", None], "S")
        make_run(b, BASE_TOOLS, [150, 160], ["glob", None], "S")
        c = compare_groups([a, self.base[0]], [b, self.base[1]])
        turn1 = {m["key"]: m for m in c["metrics"]}["first_turn_input"]
        self.assertEqual(turn1["note"], "overlap")

    def test_cli_with_session_names(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["compare", "base-1", "base-2", "base-3", "--vs", "plug-1", "plug-2", "plug-3",
                         "--log-dir", self.tmp.name, "--labels", "base,clever"])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("base (n=3)", out)
        self.assertIn("tools offered only in clever: reflect, todo_write", out)
        self.assertIn("todo_write", out)


if __name__ == "__main__":
    unittest.main()
