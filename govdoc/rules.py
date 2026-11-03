"""Rule engine: deterministic checks for official-document format and punctuation.

Rules cover what can be decided without understanding the text (GB/T 9704
layout elements, punctuation width, document-number brackets, missing date).
Wording, typos and genre-specific phrasing are left to the model channel.

Every issue carries a character span into the text, so results from the rule
channel and the model channel can be compared against known errors.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

CJK = r"一-鿿"


@dataclass
class Issue:
    start: int
    end: int
    text: str
    kind: str
    suggestion: str
    reason: str
    channel: str = "rule"

    def to_dict(self) -> dict:
        return asdict(self)


def _lines(text: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for line in text.split("\n"):
        out.append((pos, line))
        pos += len(line) + 1
    return out


_DOCNO = re.compile(r"([" + CJK + r"]{1,12})([（(\[［【])(\d{4})([)）\]］】])(\d+)号")
_DATE = re.compile(r"^\s*\d{4}年\d{1,2}月\d{1,2}日\s*$")
_HALF_PUNCT = re.compile(r"(?<=[" + CJK + r"])([,;:?!])(?=[" + CJK + r"“‘《（]|$)", re.M)
_TITLE_END = re.compile(r"[。．.，,；;：:！!？?]\s*$")
_FULL_STOP_RUN = re.compile(r"[。，；：、]{2,}")


def check(text: str) -> list[Issue]:
    issues: list[Issue] = []
    lines = _lines(text)
    nonempty = [(p, l) for p, l in lines if l.strip()]

    # 1. document number must use 〔〕 around the year
    for m in _DOCNO.finditer(text):
        s, e = m.start(2), m.end(4)
        issues.append(Issue(s, e, text[s:e], "格式", f"〔{m.group(3)}〕", "发文字号中的年份应使用六角括号“〔〕”括入。"))

    # 2. title: first line that starts with 关于 or ends with 通知/报告/请示…
    for p, l in nonempty[:4]:
        stripped = l.strip()
        if stripped.startswith("关于") or re.search(r"(通知|报告|请示|决定|意见|函|纪要|方案)[。．.，,；;：:！!？?]?\s*$", stripped):
            m = _TITLE_END.search(l)
            if m:
                issues.append(Issue(p + m.start(), p + m.start() + 1, l[m.start()], "标点", "删除", "公文标题末尾一般不使用标点符号。"))
            break

    # 3. half-width punctuation inside Chinese text
    for m in _HALF_PUNCT.finditer(text):
        full = {",": "，", ";": "；", ":": "：", "?": "？", "!": "！"}[m.group(1)]
        issues.append(Issue(m.start(1), m.end(1), m.group(1), "标点", full, "中文语句中应使用全角标点。"))

    # 4. doubled punctuation
    for m in _FULL_STOP_RUN.finditer(text):
        issues.append(Issue(m.start(), m.end(), m.group(0), "标点", m.group(0)[-1], "标点重复。"))

    # 5. missing date after the signature (last few non-empty lines)
    tail = nonempty[-4:]
    if nonempty and not any(_DATE.match(l) for _, l in tail):
        end = len(text.rstrip())
        issues.append(Issue(end, end, "", "格式", "补充成文日期", "缺少成文日期，应在发文机关署名下方标注。"))

    issues.sort(key=lambda i: (i.start, i.end))
    return issues
