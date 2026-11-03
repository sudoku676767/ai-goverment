"""Measure the proofreader on real documents.

    python3 -m govdoc.evaluate --docs 公文样本/            # rules + DeepSeek
    python3 -m govdoc.evaluate --docs 公文样本/ --no-llm   # rules only

For each clean document it seeds one known error at a time (see inject.py),
runs the rule engine and the model, and checks whether the seeded error was
caught. It also runs the untouched documents to count how many prompts a
clean document produces. Writes report.md and results.json to --out.

Documents: .txt / .md, or .docx (read with the standard library). Save PDFs
as .docx or .txt first. The API key is read from DEEPSEEK_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import mean, median

from . import llm, rules
from .inject import Injected, variants


def read_doc(path: Path) -> str:
    if path.suffix.lower() == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        paras = []
        for p in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
            t = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, flags=re.S))
            t = t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
            paras.append(t)
        text = "\n".join(paras)
    else:
        text = path.read_text(encoding="utf-8-sig")
    text = re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip() + "\n"
    return text


def genre_of(text: str) -> str:
    head = "\n".join(text.split("\n")[:5])
    for g in ("请示", "报告", "通知", "函", "纪要", "意见", "决定", "方案"):
        if g in head:
            return g
    return "公文"


def overlaps(issue: rules.Issue, inj: Injected) -> bool:
    if inj.start == inj.end:  # missing element, e.g. date
        return "成文日期" in issue.reason or "成文日期" in issue.suggestion
    return issue.start < inj.end and inj.start < issue.end


def run_one(text: str, genre: str, args) -> dict:
    t0 = time.perf_counter()
    r_issues = rules.check(text)
    rule_ms = (time.perf_counter() - t0) * 1000
    m_issues, stats = [], {}
    if not args.no_llm:
        m_issues, stats = llm.check(text, genre, api_key=args.api_key, base_url=args.base_url, model=args.model)
    return {"rule": r_issues, "model": m_issues, "rule_ms": rule_ms, "llm": stats}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="govdoc.evaluate")
    ap.add_argument("--docs", required=True, help="folder of clean documents")
    ap.add_argument("--out", default="eval-out")
    ap.add_argument("--no-llm", action="store_true", help="rule engine only")
    ap.add_argument("--base-url", default=llm.DEFAULT_BASE_URL, help="API base, or the blackbox proxy http://127.0.0.1:8787")
    ap.add_argument("--model", default=llm.DEFAULT_MODEL)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--price-in", type=float, default=1.0, help="元 / 百万输入 token（官方闲时价）")
    ap.add_argument("--price-out", type=float, default=4.0, help="元 / 百万输出 token")
    ap.add_argument("--price-cached", type=float, default=0.02, help="元 / 百万缓存命中 token")
    args = ap.parse_args(argv)
    args.api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not args.no_llm and not args.api_key:
        print("缺少 DEEPSEEK_API_KEY 环境变量；只测规则引擎请加 --no-llm", file=sys.stderr)
        return 2

    paths = sorted(p for p in Path(args.docs).iterdir() if p.suffix.lower() in {".txt", ".md", ".docx"})
    if not paths:
        print(f"{args.docs} 里没有 .txt / .md / .docx 文件", file=sys.stderr)
        return 2
    docs = [(p.name, read_doc(p)) for p in paths]

    jobs: list[tuple[str, str, str, Injected | None]] = []
    for name, text in docs:
        g = genre_of(text)
        jobs.append((name, g, text, None))
        for inj in variants(text):
            jobs.append((name, g, inj.text, inj))
    print(f"{len(docs)} 份原文，{len(jobs) - len(docs)} 个埋错样本，共 {len(jobs)} 次校核", file=sys.stderr)

    def work(job):
        name, g, text, inj = job
        try:
            return job, run_one(text, g, args), None
        except Exception as exc:  # keep going; report errors
            return job, None, str(exc)

    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=1 if args.no_llm else args.workers) as pool:
        for job, res, err in pool.map(work, jobs):
            done += 1
            print(f"\r{done}/{len(jobs)}", end="", file=sys.stderr)
            results.append((job, res, err))
    print(file=sys.stderr)

    report = summarise(results, args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "report.md").write_text(render(report), encoding="utf-8")
    print(render(report))
    print(f"\n已写入 {out / 'report.md'} 和 {out / 'results.json'}", file=sys.stderr)
    return 0


def summarise(results, args) -> dict:
    per_type: dict[str, dict] = {}
    clean = {"docs": 0, "rule_flags": [], "model_flags": []}
    extra_flags = []
    calls = []
    errors = []
    details = []
    rule_ms = []
    for (name, g, text, inj), res, err in results:
        if err:
            errors.append({"doc": name, "variant": inj.name if inj else "原文", "error": err})
            continue
        rule_ms.append(res["rule_ms"])
        if res["llm"]:
            calls.append({**res["llm"], "chars": len(text), "rule_ms": res["rule_ms"]})
        if inj is None:
            clean["docs"] += 1
            clean["rule_flags"].append(len(res["rule"]))
            clean["model_flags"].append(len(res["model"]))
            details.append({"doc": name, "variant": "原文", "rule": [i.to_dict() for i in res["rule"]], "model": [i.to_dict() for i in res["model"]]})
            continue
        t = per_type.setdefault(inj.label, {"expected": inj.expected, "n": 0, "rule": 0, "model": 0, "either": 0})
        hit_r = any(overlaps(i, inj) for i in res["rule"])
        hit_m = any(overlaps(i, inj) for i in res["model"])
        t["n"] += 1
        t["rule"] += hit_r
        t["model"] += hit_m
        t["either"] += hit_r or hit_m
        extra_flags.append(sum(not overlaps(i, inj) for i in res["rule"] + res["model"]))
        details.append({"doc": name, "variant": inj.label, "wrong": inj.wrong, "right": inj.right, "caught_by_rule": hit_r, "caught_by_model": hit_m})

    n = sum(t["n"] for t in per_type.values())
    either = sum(t["either"] for t in per_type.values())
    cost = None
    if calls:
        tin = sum(c["prompt_tokens"] for c in calls)
        tc = sum(c["cached_tokens"] for c in calls)
        tout = sum(c["completion_tokens"] for c in calls)
        yuan = ((tin - tc) * args.price_in + tc * args.price_cached + tout * args.price_out) / 1e6
        cost = {
            "calls": len(calls),
            "prompt_tokens": tin,
            "cached_tokens": tc,
            "completion_tokens": tout,
            "yuan_total": round(yuan, 4),
            "yuan_per_doc": round(yuan / len(calls), 5),
            "seconds_mean": round(mean(c["seconds"] for c in calls), 2),
            "seconds_median": round(median(c["seconds"] for c in calls), 2),
            "dropped_unquoted": sum(c["dropped_unquoted"] for c in calls),
            "chars_mean": round(mean(c["chars"] for c in calls)),
        }
    rule_ms = rule_ms or [0]
    return {
        "model": None if args.no_llm else args.model,
        "documents": clean["docs"],
        "seeded": n,
        "caught": either,
        "recall": round(either / n, 3) if n else None,
        "per_type": per_type,
        "clean_rule_flags_mean": round(mean(clean["rule_flags"]), 2) if clean["rule_flags"] else None,
        "clean_model_flags_mean": round(mean(clean["model_flags"]), 2) if clean["model_flags"] and not args.no_llm else None,
        "extra_flags_mean": round(mean(extra_flags), 2) if extra_flags else None,
        "rule_ms_mean": round(mean(rule_ms), 2),
        "llm": cost,
        "errors": errors,
        "details": details,
    }


def _pct(a: int, b: int) -> str:
    return f"{a}/{b}（{a / b:.0%}）" if b else "—"


def render(r: dict) -> str:
    out = ["# 公文校核测评结果", ""]
    out.append(f"原文 {r['documents']} 份，埋入已知错误 {r['seeded']} 处（每个样本只埋一处），模型：{r['model'] or '未启用（仅规则引擎）'}。")
    out.append("")
    out.append(f"**总体检出率：{_pct(r['caught'], r['seeded'])}**")
    out.append("")
    out.append("| 错误类型 | 预期通道 | 样本数 | 规则引擎检出 | 大模型检出 | 合计检出 |")
    out.append("| --- | --- | --- | --- | --- | --- |")
    for label, t in r["per_type"].items():
        out.append(f"| {label} | {'规则' if t['expected'] == 'rule' else '模型'} | {t['n']} | {_pct(t['rule'], t['n'])} | {_pct(t['model'], t['n']) if r['model'] else '—'} | {_pct(t['either'], t['n'])} |")
    out.append("")
    out.append("**误报参考**")
    out.append("")
    out.append(f"- 未改动的原文，平均每份规则引擎提示 {r['clean_rule_flags_mean']} 条" + (f"，大模型提示 {r['clean_model_flags_mean']} 条" if r["clean_model_flags_mean"] is not None else "") + "。原文本身可能就有问题，这些提示需要人工判断是否为误报。")
    out.append(f"- 埋错样本中，除埋入的错误外平均还有 {r['extra_flags_mean']} 条其他提示。")
    out.append("")
    if r["llm"]:
        c = r["llm"]
        out.append("**耗时与成本**")
        out.append("")
        out.append(f"- 模型调用 {c['calls']} 次，平均每篇 {c['chars_mean']} 字，单篇耗时平均 {c['seconds_mean']} 秒（中位数 {c['seconds_median']} 秒）。规则引擎单篇 {r['rule_ms_mean']} 毫秒。")
        out.append(f"- token：输入 {c['prompt_tokens']:,}（其中缓存命中 {c['cached_tokens']:,}），输出 {c['completion_tokens']:,}。")
        out.append(f"- 成本合计 ¥{c['yuan_total']}，单篇约 ¥{c['yuan_per_doc']}（按输入 ¥1、输出 ¥4、缓存 ¥0.02 每百万 token 计）。")
        out.append(f"- 模型引用了原文中不存在的片段 {c['dropped_unquoted']} 次，均已自动丢弃、不展示。")
    else:
        out.append(f"**耗时**：规则引擎单篇平均 {r['rule_ms_mean']} 毫秒。")
    if r["errors"]:
        out.append("")
        out.append(f"**失败 {len(r['errors'])} 次**：{r['errors'][0]['error']}")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
