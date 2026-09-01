"""Shared fixtures: a config builder and a one-shot guard runner."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_SERVER = Path(__file__).resolve().parent / "fake_mcp_server.py"


def call(idx: int, tool: str, **arguments) -> Dict[str, Any]:
    """Build one tools/call request."""
    return {
        "jsonrpc": "2.0",
        "id": idx,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


class GuardRun:
    """The result of piping a script of JSON-RPC requests through the guard."""

    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.messages: List[Dict[str, Any]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if line:
                self.messages.append(json.loads(line))

    def by_id(self, idx: Any) -> Optional[Dict[str, Any]]:
        for m in self.messages:
            if m.get("id") == idx:
                return m
        return None

    def blocked_caps(self) -> List[str]:
        caps = []
        for m in self.messages:
            err = m.get("error")
            if isinstance(err, dict) and isinstance(err.get("data"), dict):
                cap = err["data"].get("cap")
                if cap:
                    caps.append(cap)
        return caps


@pytest.fixture
def make_config(tmp_path: Path):
    """Write a guard YAML wired to the fake echo server; return its path."""

    def _make(
        limits: Optional[Dict[str, Any]] = None,
        costs: Optional[Dict[str, Any]] = None,
        safety: Optional[Dict[str, Any]] = None,
        session_id: str = "test",
        db_path: Optional[str] = None,
        name: str = "guard.yaml",
    ) -> Path:
        doc = {
            "server": {"command": sys.executable, "args": [str(FAKE_SERVER)]},
            "limits": limits or {},
            "costs": costs or {},
            "safety": safety or {},
            "storage": {
                "db_path": db_path or str(tmp_path / "guard.db"),
                "session_id": session_id,
            },
            "log_level": "warn",
        }
        path = tmp_path / name
        path.write_text(yaml.safe_dump(doc), encoding="utf-8")
        return path

    return _make


@pytest.fixture
def run_guard_cli():
    """Pipe a list of JSON-RPC messages through `python -m mcp_spend_guard run`."""

    def _run(config_path: Path, messages: List[Dict[str, Any]], timeout: float = 30.0) -> GuardRun:
        payload = "".join(json.dumps(m) + "\n" for m in messages)
        proc = subprocess.run(
            [sys.executable, "-m", "mcp_spend_guard", "run", "-c", str(config_path)],
            input=payload.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPO_ROOT),
            timeout=timeout,
        )
        return GuardRun(
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )

    return _run


class GuardSession:
    """A live guard process driven one request at a time.

    `run_guard_cli` pipes every request in at once, which is how a fast client
    behaves; some caps (wall clock, the circuit breaker) only make sense when
    each request is sent after the previous response has come back. This harness
    does that.
    """

    def __init__(self, config_path: Path, timeout: float = 20.0):
        self.timeout = timeout
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_spend_guard", "run", "-c", str(config_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPO_ROOT),
        )
        self._lines: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self):
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def request(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Send one message and return the first response carrying its id."""
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        wanted = message.get("id")
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                line = self._lines.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                break
            if line is None:
                return None
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line.decode("utf-8"))
            if msg.get("id") == wanted:
                return msg
        raise AssertionError(f"no response for id {wanted!r} within {self.timeout}s")

    def finish(self) -> GuardRun:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.wait(timeout=self.timeout)
        self._reader.join(timeout=5)
        return GuardRun(self.proc.returncode, "", self.proc.stderr.read().decode("utf-8", "replace"))


@pytest.fixture
def guard_session():
    live: List[GuardSession] = []

    def _open(config_path: Path, timeout: float = 20.0) -> GuardSession:
        s = GuardSession(config_path, timeout=timeout)
        live.append(s)
        return s

    yield _open

    for s in live:
        if s.proc.poll() is None:
            s.proc.kill()
        try:
            s.proc.stdout.close()
            s.proc.stderr.close()
        except OSError:
            pass
