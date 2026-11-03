"""Tests for the proofreading rules, error seeding and the evaluation run.

The model channel is tested against a fake OpenAI-compatible server, so no
network or API key is needed.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from govdoc import evaluate, inject, llm, rules

SAMPLES = Path(__file__).resolve().parent.parent / "eval" / "samples"
CLEAN = (SAMPLES / "01-安全检查通知.txt").read_text(encoding="utf-8")


class RuleTests(unittest.TestCase):
    def test_clean_sample_has_no_rule_issues(self) -> None:
        for p in SAMPLES.glob("*.txt"):
            self.assertEqual(rules.check(p.read_text(encoding="utf-8")), [], p.name)

    def test_each_rule_injection_is_caught(self) -> None:
        for v in inject.variants(CLEAN):
            if v.expected != "rule":
                continue
            found = rules.check(v.text)
            self.assertTrue(any(evaluate.overlaps(i, v) for i in found), v.name)

    def test_typo_is_not_a_rule_issue(self) -> None:
        v = inject.typo(CLEAN)
        self.assertIsNotNone(v)
        self.assertEqual(rules.check(v.text), [])

    def test_issue_span_points_at_text(self) -> None:
        text = CLEAN.replace("〔2026〕", "(2026)")
        (issue,) = rules.check(text)
        self.assertEqual(text[issue.start : issue.end], "(2026)")
        self.assertEqual(issue.suggestion, "〔2026〕")


class _FakeModel(BaseHTTPRequestHandler):
    reply: dict = {}

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        doc = body["messages"][-1]["content"]
        issues = []
        for right, wrong in inject.TYPO_PAIRS:
            if wrong in doc:
                issues.append({"text": wrong, "type": "错别字", "suggestion": right, "reason": "形近字"})
        if "妥否，请批示" in doc and "请示" not in doc.split("\n")[3]:
            issues.append({"text": "妥否，请批示。", "type": "文种用语", "suggestion": "删除", "reason": "通知不用请示结束语"})
        issues.append({"text": "原文里没有这句话", "type": "表述", "suggestion": "", "reason": "幻觉"})
        data = {
            "choices": [{"message": {"content": json.dumps({"issues": issues}, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "prompt_cache_hit_tokens": 400},
        }
        out = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args) -> None:
        pass


class ModelChannelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeModel)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def test_unquoted_snippets_are_dropped(self) -> None:
        v = inject.typo(CLEAN)
        issues, stats = llm.check(v.text, "通知", api_key="x", base_url=self.url)
        self.assertEqual(stats["dropped_unquoted"], 1)
        self.assertTrue(any(evaluate.overlaps(i, v) for i in issues))
        self.assertTrue(all(i.channel == "model" for i in issues))

    def test_evaluate_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            os.environ["DEEPSEEK_API_KEY"] = "x"
            try:
                code = evaluate.main(["--docs", str(SAMPLES), "--out", out, "--base-url", self.url, "--workers", "2"])
            finally:
                del os.environ["DEEPSEEK_API_KEY"]
            self.assertEqual(code, 0)
            r = json.loads((Path(out) / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(r["caught"], r["seeded"])
            self.assertGreater(r["llm"]["calls"], 0)
            self.assertIn("总体检出率", (Path(out) / "report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
