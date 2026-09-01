"""Command line entry point: `mcp-spend-guard run` and `mcp-spend-guard report`."""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
from typing import Optional, Sequence

from . import __version__
from .config import ConfigError, GuardConfig, load_config
from .proxy import Logger, run_guard
from .store import Store


def _ts(value: float) -> str:
    return _dt.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-spend-guard",
        description="Wrap any stdio MCP server in a hard spend cap, rate limits and a kill switch.",
    )
    parser.add_argument("--version", action="version", version=f"mcp-spend-guard {__version__}")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="run the guard around a wrapped MCP server (stdio)")
    run_p.add_argument("-c", "--config", required=True, help="path to a guard YAML config")
    run_p.add_argument("--session-id", help="override storage.session_id")
    run_p.add_argument("--db", help="override storage.db_path")
    run_p.add_argument(
        "--log-level",
        choices=["debug", "info", "warn", "error", "silent"],
        help="override log_level (logs go to stderr, never stdout)",
    )

    rep_p = sub.add_parser("report", help="print calls and estimated spend from the database")
    rep_p.add_argument("--db", help="path to the guard db (default ~/.mcp-spend-guard/guard.db)")
    rep_p.add_argument("-c", "--config", help="read the db path from a guard config instead")
    rep_p.add_argument("--session-id", help="limit the report to one session")

    reset_p = sub.add_parser("reset", help="delete all recorded calls for a session")
    reset_p.add_argument("--db", help="path to the guard db (default ~/.mcp-spend-guard/guard.db)")
    reset_p.add_argument("-c", "--config", help="read the db path from a guard config instead")
    reset_p.add_argument("--session-id", required=True, help="session to clear")

    return parser


def _config_for(args) -> Optional[GuardConfig]:
    if getattr(args, "config", None):
        return load_config(args.config)
    return None


DEFAULT_DB = "~/.mcp-spend-guard/guard.db"


def _db_path_for(args) -> str:
    """--db wins, then the db_path of a --config, then the default location."""
    if getattr(args, "db", None):
        return os.path.expanduser(args.db)
    cfg = _config_for(args)
    if cfg is not None:
        return str(cfg.resolved_db_path())
    return os.path.expanduser(DEFAULT_DB)


def cmd_run(args) -> int:
    config = load_config(args.config)
    if args.session_id:
        config.session_id = args.session_id
    if args.db:
        config.db_path = args.db
    if args.log_level:
        config.log_level = args.log_level
    return run_guard(config, logger=Logger(config.log_level))


def cmd_report(args, out=sys.stdout) -> int:
    store = Store(_db_path_for(args))
    try:
        sessions = store.sessions()
        if args.session_id:
            sessions = [s for s in sessions if s.session_id == args.session_id]
        if not sessions:
            out.write("No sessions recorded yet.\n")
            return 0

        for s in sessions:
            out.write(f"\nsession: {s.session_id}\n")
            out.write(
                f"  started {_ts(s.created_at)}   last seen {_ts(s.last_seen_at)}   "
                f"runs {s.runs}\n"
            )
            out.write(
                f"  forwarded {s.allowed}   blocked {s.blocked}   tool errors {s.errors}   "
                f"estimated spend ${s.spend_usd:.4f}\n"
            )
            rows = store.tools(s.session_id)
            if rows:
                out.write(
                    f"\n  {'tool':<28} {'calls':>7} {'blocked':>8} {'errors':>7} {'est $':>10}\n"
                )
                out.write(f"  {'-' * 28} {'-' * 7} {'-' * 8} {'-' * 7} {'-' * 10}\n")
                for r in rows:
                    out.write(
                        f"  {r.tool[:28]:<28} {r.allowed:>7} {r.blocked:>8} {r.errors:>7} "
                        f"{r.spend_usd:>10.4f}\n"
                    )
            reasons = store.block_reasons(s.session_id)
            if reasons:
                pretty = ", ".join(f"{k}={v}" for k, v in reasons.items())
                out.write(f"\n  blocks by cap: {pretty}\n")
        out.write("\n")
        return 0
    finally:
        store.close()


def cmd_reset(args, out=sys.stdout) -> int:
    store = Store(_db_path_for(args))
    try:
        cur = store.conn.execute("DELETE FROM calls WHERE session_id=?", (args.session_id,))
        store.conn.execute("DELETE FROM sessions WHERE session_id=?", (args.session_id,))
        store.conn.commit()
        out.write(f"cleared {cur.rowcount} call(s) for session {args.session_id!r}\n")
        return 0
    finally:
        store.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.command:
        parser.print_help()
        return 2
    try:
        if args.command == "run":
            return cmd_run(args)
        if args.command == "report":
            return cmd_report(args)
        if args.command == "reset":
            return cmd_reset(args)
    except ConfigError as exc:
        sys.stderr.write(f"[mcp-spend-guard] config error: {exc}\n")
        return 2
    except FileNotFoundError as exc:
        sys.stderr.write(f"[mcp-spend-guard] error: {exc}\n")
        return 2
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
