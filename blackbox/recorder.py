"""Append-only, hash-chained JSONL recorder.

Design rules (borrowed from aircraft flight recorders):

* Every event is written and fsync'ed the moment it happens, so a crash,
  timeout or killed session never loses what was already recorded.
* Each record carries the hash of the previous record. Editing or deleting a
  record in the middle of the file breaks the chain, and `blackbox verify`
  reports where.
* The log file is created with mode 0600 inside a 0700 directory, and should
  live outside the agent's working directory.

Limitation: a hash chain proves the file is internally consistent. Someone
who can rewrite the whole file can also recompute every hash, and truncating
the tail is not detectable from the file alone. Keep the final hash that the
proxy prints on shutdown somewhere the agent cannot write.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from . import LOG_FORMAT_VERSION

GENESIS_HASH = "0" * 64


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_hash(record_without_hash: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(record_without_hash).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds")


class Recorder:
    def __init__(self, path: str | os.PathLike[str], session: str, fsync: bool = True):
        self.path = Path(path)
        self.session = session
        self.fsync = fsync
        self._lock = threading.Lock()

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass

        self.seq, self.last_hash = self._resume()

        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self._fh = os.fdopen(fd, "a", encoding="utf-8")

    def _resume(self) -> tuple[int, str]:
        """Continue an existing chain if the file already has records."""
        if not self.path.exists():
            return 0, GENESIS_HASH
        last_line = ""
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last_line = line
        if not last_line:
            return 0, GENESIS_HASH
        last = json.loads(last_line)
        return int(last["seq"]), str(last["hash"])

    def write(self, event: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            record: dict[str, Any] = {
                "v": LOG_FORMAT_VERSION,
                "seq": self.seq + 1,
                "ts": utc_now(),
                "session": self.session,
                "event": event,
                **fields,
                "prev": self.last_hash,
            }
            record["hash"] = record_hash(record)
            self._fh.write(canonical_json(record) + "\n")
            self._fh.flush()
            if self.fsync:
                os.fsync(self._fh.fileno())
            self.seq = record["seq"]
            self.last_hash = record["hash"]
            return record

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()
