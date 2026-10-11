"""黑匣子 (skill-blackbox): a flight recorder for agent plugins and skills.

v0.1 ships the API-layer recorder: a pass-through proxy that writes every
request/response to an append-only, hash-chained JSONL log.
"""

__version__ = "0.1.0"
LOG_FORMAT_VERSION = 1
