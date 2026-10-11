"""Command line entry point: `blackbox proxy | verify | summary`."""

from __future__ import annotations

import argparse
import datetime as _dt
import ipaddress
import json
import os
import sys
from pathlib import Path

from . import __version__
from .proxy import DEFAULT_MAX_RECORD_BYTES, BlackboxServer, Upstream
from .recorder import Recorder
from .summary import render_json, render_text, summarise
from .verify import verify_log

DEFAULT_UPSTREAM = "https://api.deepseek.com"
DEFAULT_LOG_DIR = "~/.blackbox/logs"


def _parse_listen(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return (host or "127.0.0.1"), int(port)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def cmd_proxy(args: argparse.Namespace) -> int:
    upstream = Upstream(args.upstream, timeout=args.timeout)
    host, port = _parse_listen(args.listen)
    if not _is_loopback(host):
        print(
            f"warning: listening on {host}, not loopback. Anyone who can reach this "
            "port can use your API key through the proxy.",
            file=sys.stderr,
        )

    session = args.session or _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = Path(os.path.expanduser(args.log_dir)) / f"{session}.jsonl"
    recorder = Recorder(log_path, session=session, fsync=not args.no_fsync)

    server = BlackboxServer(
        (host, port),
        upstream,
        recorder,
        max_record_bytes=int(args.max_record_mb * 1024 * 1024),
        verbose=args.verbose,
    )
    server.record_session_start()
    real_port = server.server_address[1]

    print(f"skill-blackbox {__version__}")
    print(f"  listening  http://{host}:{real_port}")
    print(f"  upstream   {upstream.url}")
    print(f"  log        {log_path}")
    print()
    print("Point DeepSeek Harness at it, for example:")
    print(f"  DEEPSEEK_BASE_URL=http://{host}:{real_port} npx @deepseek-ai/dsh web")
    print("Press Ctrl+C to stop.")
    sys.stdout.flush()

    reason = "stopped"
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        reason = "keyboard_interrupt"
    finally:
        server.server_close()
        end = server.record_session_end(reason)
        recorder.close()
        print()
        print(f"session closed, {end['seq']} records")
        print(f"final hash  {end['hash']}")
        print("Keep this hash somewhere the agent cannot write; `blackbox verify` checks against it.")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    result = verify_log(args.log)
    if result["ok"] and args.expect_hash and result["last_hash"] != args.expect_hash:
        result = {
            "ok": False,
            "records": result["records"],
            "error": {"reason": "last hash differs from the expected one (truncated or rewritten?)"},
            "last_hash": result["last_hash"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def cmd_summary(args: argparse.Namespace) -> int:
    s = summarise(args.log)
    print(render_json(s) if args.json else render_text(s))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="blackbox", description="黑匣子: record what an agent's API traffic actually did.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("proxy", help="run the recording proxy")
    pp.add_argument("--upstream", default=os.environ.get("BLACKBOX_UPSTREAM", DEFAULT_UPSTREAM))
    pp.add_argument("--listen", default="127.0.0.1:8787", help="host:port (default 127.0.0.1:8787)")
    pp.add_argument("--log-dir", default=os.environ.get("BLACKBOX_LOG_DIR", DEFAULT_LOG_DIR))
    pp.add_argument("--session", help="session name (default: timestamp)")
    pp.add_argument("--timeout", type=float, default=600.0, help="upstream timeout in seconds")
    pp.add_argument("--max-record-mb", type=float, default=DEFAULT_MAX_RECORD_BYTES / 1024 / 1024)
    pp.add_argument("--no-fsync", action="store_true", help="faster, but a crash may lose the last events")
    pp.add_argument("--verbose", action="store_true")
    pp.set_defaults(func=cmd_proxy)

    pv = sub.add_parser("verify", help="check a log's hash chain")
    pv.add_argument("log")
    pv.add_argument("--expect-hash", help="final hash printed when the session closed")
    pv.set_defaults(func=cmd_verify)

    ps = sub.add_parser("summary", help="per-request table and totals")
    ps.add_argument("log")
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=cmd_summary)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
