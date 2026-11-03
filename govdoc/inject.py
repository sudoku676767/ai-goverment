"""Seed one known error into a clean document, so detection can be measured.

Each injector returns an Injected record with the modified text and the span
of the error in it, or None when the document has nothing to inject into.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

CJK = r"一-鿿"


@dataclass
class Injected:
    name: str  # injector id
    label: str  # human-readable error type
    expected: str  # channel expected to catch it: rule | model
    text: str
    start: int
    end: int
    wrong: str
    right: str


def _replace_at(text: str, start: int, end: int, new: str) -> str:
    return text[:start] + new + text[end:]


def docno_bracket(text: str) -> Injected | None:
    m = re.search(r"〔(\d{4})〕", text)
    if not m:
        return None
    wrong = f"({m.group(1)})"
    return Injected("docno_bracket", "发文字号括号", "rule", _replace_at(text, m.start(), m.end(), wrong), m.start(), m.start() + len(wrong), wrong, m.group(0))


def title_period(text: str) -> Injected | None:
    pos = 0
    for line in text.split("\n")[:4]:
        s = line.rstrip()
        if s.strip().startswith("关于") and not re.search(r"[。，；：！？.,;:!?]$", s):
            at = pos + len(s)
            return Injected("title_period", "标题末尾标点", "rule", _replace_at(text, at, at, "。"), at, at + 1, "。", "")
        pos += len(line) + 1
    return None


def colon_halfwidth(text: str) -> Injected | None:
    m = re.search(r"(?m)^[^\n]{2,40}：$", text)
    if not m:
        return None
    at = m.end() - 1
    return Injected("colon_halfwidth", "主送机关半角冒号", "rule", _replace_at(text, at, at + 1, ":"), at, at + 1, ":", "：")


def comma_halfwidth(text: str) -> Injected | None:
    hits = list(re.finditer(r"(?<=[" + CJK + r"])，(?=[" + CJK + r"])", text))
    if not hits:
        return None
    m = hits[len(hits) // 2]
    return Injected("comma_halfwidth", "正文半角逗号", "rule", _replace_at(text, m.start(), m.end(), ","), m.start(), m.start() + 1, ",", "，")


def drop_date(text: str) -> Injected | None:
    lines = text.rstrip().split("\n")
    for i in range(len(lines) - 1, max(-1, len(lines) - 5), -1):
        if re.match(r"^\s*\d{4}年\d{1,2}月\d{1,2}日\s*$", lines[i]):
            new = "\n".join(lines[:i] + lines[i + 1 :]).rstrip() + "\n"
            at = len(new.rstrip())
            return Injected("drop_date", "缺少成文日期", "rule", new, at, at, "", lines[i].strip())
    return None


# (correct, wrong) pairs commonly confused in official writing
TYPO_PAIRS = [
    ("部署", "布署"),
    ("即日起", "既日起"),
    ("贯彻", "贯切"),
    ("必须", "必需"),
    ("签订", "签定"),
    ("截止日期", "截至日期"),
    ("截止时间", "截至时间"),
    ("一律", "一率"),
    ("竣工", "峻工"),
    ("精神", "经神"),
    ("报送", "抱送"),
    ("汇报", "会报"),
    ("申报", "申抱"),
    ("考核", "考合"),
]


def typo(text: str) -> Injected | None:
    for right, wrong in TYPO_PAIRS:
        at = text.find(right)
        if at >= 0:
            return Injected("typo", "错别字", "model", _replace_at(text, at, at + len(right), wrong), at, at + len(wrong), wrong, right)
    return None


def wrong_closing(text: str) -> Injected | None:
    head = "\n".join(text.split("\n")[:4])
    if "请示" in head or "妥否" in text:
        return None
    lines = text.rstrip().split("\n")
    # insert before the signature block: the last line that is not a date or org name
    idx = len(lines)
    for i in range(len(lines) - 1, max(-1, len(lines) - 4), -1):
        if re.match(r"^\s*\d{4}年\d{1,2}月\d{1,2}日\s*$", lines[i]) or len(lines[i].strip()) <= 20:
            idx = i
        else:
            break
    phrase = "　　妥否，请批示。"
    new_lines = lines[:idx] + [phrase] + lines[idx:]
    new = "\n".join(new_lines) + "\n"
    at = new.find(phrase) + 2
    return Injected("wrong_closing", "文种用语不当", "model", new, at, at + len("妥否，请批示。"), "妥否，请批示。", "")


INJECTORS: list[Callable[[str], Injected | None]] = [
    docno_bracket,
    title_period,
    colon_halfwidth,
    comma_halfwidth,
    drop_date,
    typo,
    wrong_closing,
]


def variants(text: str) -> list[Injected]:
    return [v for f in INJECTORS if (v := f(text)) is not None]
