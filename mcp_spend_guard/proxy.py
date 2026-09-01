"""The stdio JSON-RPC relay.

The guard sits between an MCP client (Claude Code, Claude Desktop, ...) and the
real MCP server. Newline-delimited JSON goes in both directions. Anything the
guard does not understand is forwarded byte-for-byte, unreordered. The only
frames it ever withholds are `tools/call` requests that trip a cap, and those are
answered with a well-formed JSON-RPC error so the client never hangs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import IO, Any, Dict, Optional, Tuple

from .config import GuardConfig
from .limits import CAP_CIRCUIT_BREAKER, Decision, LimitEngine
from .store import Store

# JSON-RPC error code for "the guard refused this call". -32000..-32099 is the
# reserved implementation-defined server-error range.
ERROR_CODE_CAP = -32011

EXIT_OK = 0
EXIT_BREAKER = 3

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40, "silent": 100}


class Logger:
    def __init__(self, level: str = "info", stream: IO[str] = sys.stderr):
        self.level = _LEVELS.get(str(level).lower(), 20)
        self.stream = stream
        self._lock = threading.Lock()

    def log(self, level: str, message: str) -> None:
        if _LEVELS.get(level, 20) < self.level:
            return
        with self._lock:
            try:
                self.stream.write(f"[mcp-spend-guard] {level}: {message}\n")
                self.stream.flush()
            except (ValueError, OSError):
                pass

    def debug(self, m: str) -> None:
        self.log("debug", m)

    def info(self, m: str) -> None:
        self.log("info", m)

    def warn(self, m: str) -> None:
        self.log("warn", m)

    def error(self, m: str) -> None:
        self.log("error", m)


def build_cap_error(request_id: Any, decision: Decision) -> Dict[str, Any]:
    """The JSON-RPC error response returned to the client when a cap trips."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": ERROR_CODE_CAP,
            "message": (
                f"mcp-spend-guard blocked this call [{decision.cap}]: {decision.message}"
            ),
            "data": decision.as_error_data(),
        },
    }


def classify_response(msg: Dict[str, Any]) -> Optional[bool]:
    """True if this response is a tool error, False if a success, None if neither."""
    if "error" in msg:
        return True
    result = msg.get("result")
    if isinstance(result, dict):
        # MCP reports tool-level failures inside a successful envelope.
        return bool(result.get("isError"))
    if "result" in msg:
        return False
    return None


class GuardProxy:
    """Owns the child process, the two relay pumps and the shutdown handshake."""

    def __init__(
        self,
        config: GuardConfig,
        store: Store,
        stdin: Optional[IO[bytes]] = None,
        stdout: Optional[IO[bytes]] = None,
        logger: Optional[Logger] = None,
    ):
        self.config = config
        self.store = store
        self.stdin = stdin if stdin is not None else sys.stdin.buffer
        self.stdout = stdout if stdout is not None else sys.stdout.buffer
        self.log = logger or Logger(config.log_level)

        self.engine = LimitEngine(config, store)
        self.proc: Optional[subprocess.Popen] = None
        self.exit_code = EXIT_OK

        self._out_lock = threading.Lock()
        self._pending: Dict[str, Tuple[str, float]] = {}
        self._pending_lock = threading.Lock()
        self._shutdown = threading.Event()

    # ---------------- plumbing ----------------

    def _write_client(self, raw: bytes) -> None:
        with self._out_lock:
            try:
                self.stdout.write(raw if raw.endswith(b"\n") else raw + b"\n")
                self.stdout.flush()
            except (BrokenPipeError, ValueError, OSError):
                self._shutdown.set()

    def _write_json(self, obj: Dict[str, Any]) -> None:
        self._write_client(json.dumps(obj).encode("utf-8"))

    def _spawn(self) -> subprocess.Popen:
        env = os.environ.copy()
        env.update(self.config.env)
        self.log.info("wrapping: " + " ".join(self.config.command))
        return subprocess.Popen(
            self.config.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.config.cwd,
            env=env,
        )

    # ---------------- pumps ----------------

    def _pump_server_to_client(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = raw.rstrip(b"\r\n")
            if not line.strip():
                continue
            tripped = False
            try:
                msg = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._write_client(line)
                continue

            if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                key = str(msg.get("id"))
                with self._pending_lock:
                    entry = self._pending.pop(key, None)
                if entry is not None:
                    is_error = classify_response(msg)
                    if is_error:
                        tripped = self.engine.note_error()
                        self.log.warn(
                            f"tool {entry[0]!r} returned an error "
                            f"({self.engine.consecutive_errors} consecutive)"
                        )
                    elif is_error is False:
                        self.engine.note_success()

            self._write_client(line)

            if tripped:
                self.log.error(
                    "circuit breaker TRIPPED after "
                    f"{self.engine.consecutive_errors} consecutive tool errors"
                )
                self.exit_code = EXIT_BREAKER
                if self.config.exit_on_breaker_trip:
                    self._shutdown.set()
                    self._hard_exit(EXIT_BREAKER)
                    return
        self._shutdown.set()

    def _hard_exit(self, code: int) -> None:
        """Stop the session now.

        The main thread is parked on a blocking read of the client's stdin, so a
        cooperative shutdown would wait for a request that may never come. Every
        response is already flushed and every counter already committed, so it is
        safe to leave immediately.
        """
        self.log.info(f"shutting down the wrapped server (exit {code})")
        self._close_child_stdin()
        self._terminate_child()
        self.store.touch_session(self.config.session_id)
        self.store.close()
        try:
            self.stdout.flush()
            sys.stderr.flush()
        except (ValueError, OSError):
            pass
        os._exit(code)

    def _pump_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for raw in self.proc.stderr:
            try:
                sys.stderr.buffer.write(raw)
                sys.stderr.buffer.flush()
            except (ValueError, OSError):
                return

    # ---------------- request inspection ----------------

    def _handle_client_line(self, line: bytes) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._forward(line)
            return

        if not isinstance(msg, dict) or msg.get("method") != "tools/call":
            self._forward(line)
            return

        params = msg.get("params")
        tool = "<unknown>"
        if isinstance(params, dict) and isinstance(params.get("name"), str):
            tool = params["name"]
        request_id = msg.get("id", None)

        decision = self.engine.evaluate(tool, request_id=request_id)

        if not decision.allowed:
            self.log.warn(f"BLOCKED {tool!r}: {decision.message}")
            if request_id is None:
                # A notification cannot be answered; dropping it is the only
                # way to honour the cap.
                return
            self._write_json(build_cap_error(request_id, decision))
            if decision.cap == CAP_CIRCUIT_BREAKER and self.config.exit_on_breaker_trip:
                self._shutdown.set()
            return

        if request_id is not None:
            with self._pending_lock:
                self._pending[str(request_id)] = (tool, time.time())
        self.log.debug(f"allow {tool!r} (est ${decision.cost_usd:.4f})")
        self._forward(line)

    def _forward(self, line: bytes) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        try:
            self.proc.stdin.write(line + b"\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            self._shutdown.set()

    # ---------------- lifecycle ----------------

    def run(self) -> int:
        self.store.start_session(self.config.session_id, self.config.source_path)
        self.proc = self._spawn()

        reader = threading.Thread(target=self._pump_server_to_client, daemon=True)
        reader.start()
        errs = threading.Thread(target=self._pump_stderr, daemon=True)
        errs.start()

        try:
            for raw in self.stdin:
                if self._shutdown.is_set():
                    break
                line = raw.rstrip(b"\r\n")
                if not line.strip():
                    continue
                self._handle_client_line(line)
        except (KeyboardInterrupt, BrokenPipeError):
            pass
        finally:
            self._close_child_stdin()
            reader.join(timeout=10)
            self._terminate_child()
            self.store.touch_session(self.config.session_id)

        spent = self.store.spend(self.config.session_id)
        used = self.store.total_allowed(self.config.session_id)
        self.log.info(
            f"session {self.config.session_id!r}: {used} calls forwarded, "
            f"${spent:.4f} estimated spend"
        )
        return self.exit_code

    def _close_child_stdin(self) -> None:
        if self.proc is not None and self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except (BrokenPipeError, ValueError, OSError):
                pass

    def _terminate_child(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def run_guard(config: GuardConfig, logger: Optional[Logger] = None) -> int:
    store = Store(config.resolved_db_path())
    try:
        return GuardProxy(config, store, logger=logger).run()
    finally:
        store.close()
