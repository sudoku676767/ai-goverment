"""Check a log's hash chain."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .recorder import GENESIS_HASH, record_hash


def verify_log(path: str | Path) -> dict[str, Any]:
    expected_prev = GENESIS_HASH
    expected_seq = 1
    count = 0
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue

            def fail(reason: str, seq: Any = None) -> dict[str, Any]:
                return {
                    "ok": False,
                    "records": count,
                    "error": {"line": lineno, "seq": seq, "reason": reason},
                    "last_good_hash": expected_prev,
                }

            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                return fail(f"not valid JSON: {exc}")
            seq = rec.get("seq")
            if seq != expected_seq:
                return fail(f"sequence gap: expected {expected_seq}, found {seq}", seq)
            if rec.get("prev") != expected_prev:
                return fail("prev hash does not match the previous record", seq)
            stored = rec.pop("hash", None)
            if stored != record_hash(rec):
                return fail("record content does not match its hash (edited?)", seq)

            count += 1
            expected_prev = stored
            expected_seq += 1

    return {"ok": True, "records": count, "last_hash": expected_prev}
