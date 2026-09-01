"""End-to-end tests: real subprocesses, real stdio, real SQLite.

Every test here pipes JSON-RPC through `python -m mcp_spend_guard run`, which
spawns tests/fake_mcp_server.py as the wrapped server.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import REPO_ROOT, call

INIT = {
    "jsonrpc": "2.0",
    "id": "init",
    "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
}
LIST = {"jsonrpc": "2.0", "id": "list", "method": "tools/list", "params": {}}


def text_of(msg):
    return msg["result"]["content"][0]["text"]


# --------------------------------------------------------------------------
# passthrough
# --------------------------------------------------------------------------


def test_passthrough_is_faithful(make_config, run_guard_cli):
    cfg = make_config()
    run = run_guard_cli(cfg, [INIT, LIST, call(1, "echo", text="hello"), call(2, "echo", text="two")])

    assert run.returncode == 0, run.stderr
    assert len(run.messages) == 4

    assert run.by_id("init")["result"]["serverInfo"]["name"] == "fake-mcp-echo"
    names = [t["name"] for t in run.by_id("list")["result"]["tools"]]
    assert "echo" in names and "expensive" in names
    assert text_of(run.by_id(1)) == "hello"
    assert text_of(run.by_id(2)) == "two"


def test_order_is_preserved(make_config, run_guard_cli):
    cfg = make_config()
    msgs = [call(i, "echo", text=str(i)) for i in range(1, 11)]
    run = run_guard_cli(cfg, msgs)
    assert [m["id"] for m in run.messages] == list(range(1, 11))


def test_unknown_methods_and_notifications_pass_through(make_config, run_guard_cli):
    cfg = make_config()
    run = run_guard_cli(
        cfg,
        [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 7, "method": "resources/list", "params": {}},
            call(8, "echo", text="still here"),
        ],
    )
    # the notification gets no reply, the unknown method reaches the server and
    # gets the server's own error back, unmodified
    assert run.by_id(7)["error"]["code"] == -32601
    assert text_of(run.by_id(8)) == "still here"


# --------------------------------------------------------------------------
# caps
# --------------------------------------------------------------------------


def test_max_calls_total_trips_with_a_wellformed_error(make_config, run_guard_cli):
    cfg = make_config(limits={"max_calls_total": 2})
    run = run_guard_cli(cfg, [call(i, "echo", text="x") for i in range(1, 5)])

    assert run.returncode == 0
    assert text_of(run.by_id(1)) == "x"
    assert text_of(run.by_id(2)) == "x"

    blocked = run.by_id(3)
    assert blocked["jsonrpc"] == "2.0"
    assert blocked["id"] == 3
    assert "result" not in blocked
    assert blocked["error"]["code"] == -32011
    assert blocked["error"]["data"]["cap"] == "max_calls_total"
    assert blocked["error"]["data"]["limit"] == 2
    assert blocked["error"]["data"]["current"] == 2
    assert "max_calls_total" in blocked["error"]["message"]

    # every request still gets exactly one reply -> the client never hangs
    assert sorted(m["id"] for m in run.messages) == [1, 2, 3, 4]


def test_max_calls_per_tool(make_config, run_guard_cli):
    cfg = make_config(limits={"max_calls_per_tool": {"expensive": 1}})
    run = run_guard_cli(
        cfg,
        [
            call(1, "expensive", text="a"),
            call(2, "expensive", text="b"),
            call(3, "echo", text="c"),
        ],
    )
    assert text_of(run.by_id(1)) == "a"
    assert run.by_id(2)["error"]["data"]["cap"] == "max_calls_per_tool"
    assert run.by_id(2)["error"]["data"]["tool"] == "expensive"
    assert text_of(run.by_id(3)) == "c"  # other tools unaffected


def test_max_calls_per_minute(make_config, run_guard_cli):
    cfg = make_config(limits={"max_calls_per_minute": 3})
    run = run_guard_cli(cfg, [call(i, "echo", text="x") for i in range(1, 6)])
    assert text_of(run.by_id(3)) == "x"
    assert run.by_id(4)["error"]["data"]["cap"] == "max_calls_per_minute"
    assert run.by_id(5)["error"]["data"]["cap"] == "max_calls_per_minute"


def test_max_spend_usd(make_config, run_guard_cli):
    cfg = make_config(
        limits={"max_spend_usd": 0.25},
        costs={"default_usd": 0.0, "tools": {"expensive": 0.10}},
    )
    run = run_guard_cli(cfg, [call(i, "expensive", text="x") for i in range(1, 5)])
    assert text_of(run.by_id(1)) == "x"
    assert text_of(run.by_id(2)) == "x"
    err = run.by_id(3)["error"]
    assert err["data"]["cap"] == "max_spend_usd"
    assert err["data"]["limit"] == 0.25
    assert abs(err["data"]["current"] - 0.20) < 1e-9
    assert "$" in err["message"]


def test_wall_clock_timeout(make_config, guard_session):
    cfg = make_config(limits={"wall_clock_timeout_s": 0.75})
    s = guard_session(cfg)
    # a real client waits for each response, so the clock genuinely advances
    assert text_of(s.request(call(1, "slow", sleep_ms=1200, text="slept"))) == "slept"
    blocked = s.request(call(2, "echo", text="too late"))
    assert blocked["error"]["data"]["cap"] == "wall_clock_timeout_s"
    assert blocked["error"]["data"]["limit"] == 0.75
    assert s.finish().returncode == 0


# --------------------------------------------------------------------------
# kill switch
# --------------------------------------------------------------------------


def test_kill_switch_file_stops_everything(make_config, run_guard_cli, tmp_path):
    stop = tmp_path / "STOP"
    stop.write_text("halt", encoding="utf-8")
    cfg = make_config(safety={"kill_switch_file": str(stop)})
    run = run_guard_cli(cfg, [INIT, call(1, "echo", text="x")])

    # non-tool traffic still flows, so the client can shut down cleanly
    assert run.by_id("init")["result"]["serverInfo"]["name"] == "fake-mcp-echo"
    err = run.by_id(1)["error"]
    assert err["data"]["cap"] == "kill_switch"
    assert str(stop) in err["message"]


def test_without_the_kill_switch_file_calls_flow(make_config, run_guard_cli, tmp_path):
    cfg = make_config(safety={"kill_switch_file": str(tmp_path / "STOP")})
    run = run_guard_cli(cfg, [call(1, "echo", text="fine")])
    assert text_of(run.by_id(1)) == "fine"


# --------------------------------------------------------------------------
# circuit breaker
# --------------------------------------------------------------------------


def test_circuit_breaker_trips_and_exits_non_zero(make_config, run_guard_cli):
    cfg = make_config(safety={"circuit_breaker_errors": 2})
    run = run_guard_cli(
        cfg,
        [call(1, "fail"), call(2, "fail"), call(3, "echo", text="never")],
    )
    assert run.returncode == 3, run.stderr
    assert run.by_id(1)["result"]["isError"] is True
    assert run.by_id(2)["result"]["isError"] is True
    assert "circuit breaker TRIPPED" in run.stderr


def test_jsonrpc_level_errors_also_feed_the_breaker(make_config, run_guard_cli):
    cfg = make_config(safety={"circuit_breaker_errors": 2})
    run = run_guard_cli(cfg, [call(1, "hard_fail"), call(2, "hard_fail")])
    assert run.returncode == 3
    assert run.by_id(1)["error"]["code"] == -32603


def test_a_success_resets_the_error_streak(make_config, run_guard_cli):
    cfg = make_config(safety={"circuit_breaker_errors": 2})
    run = run_guard_cli(
        cfg,
        [call(1, "fail"), call(2, "echo", text="ok"), call(3, "fail"), call(4, "echo", text="ok")],
    )
    assert run.returncode == 0, run.stderr
    assert text_of(run.by_id(4)) == "ok"


def test_breaker_can_be_configured_not_to_exit(make_config, guard_session):
    cfg = make_config(safety={"circuit_breaker_errors": 2, "exit_on_breaker_trip": False})
    s = guard_session(cfg)
    assert s.request(call(1, "fail"))["result"]["isError"] is True
    assert s.request(call(2, "fail"))["result"]["isError"] is True
    blocked = s.request(call(3, "echo", text="never"))
    assert blocked["error"]["data"]["cap"] == "circuit_breaker"
    assert blocked["error"]["data"]["current"] == 2
    assert s.finish().returncode == 3


# --------------------------------------------------------------------------
# persistence + report
# --------------------------------------------------------------------------


def test_counters_survive_a_restart(make_config, run_guard_cli, tmp_path):
    db = str(tmp_path / "shared.db")
    cfg_a = make_config(limits={"max_calls_total": 3}, db_path=db, name="a.yaml")
    cfg_b = make_config(limits={"max_calls_total": 3}, db_path=db, name="b.yaml")

    first = run_guard_cli(cfg_a, [call(1, "echo", text="x"), call(2, "echo", text="x")])
    assert text_of(first.by_id(1)) == "x"
    assert text_of(first.by_id(2)) == "x"

    second = run_guard_cli(cfg_b, [call(3, "echo", text="x"), call(4, "echo", text="x")])
    assert text_of(second.by_id(3)) == "x"  # the third call of the session
    assert second.by_id(4)["error"]["data"]["cap"] == "max_calls_total"
    assert second.by_id(4)["error"]["data"]["current"] == 3


def test_a_different_session_id_gets_its_own_budget(make_config, run_guard_cli, tmp_path):
    db = str(tmp_path / "shared.db")
    cfg_a = make_config(limits={"max_calls_total": 1}, db_path=db, session_id="one", name="a.yaml")
    cfg_b = make_config(limits={"max_calls_total": 1}, db_path=db, session_id="two", name="b.yaml")

    a = run_guard_cli(cfg_a, [call(1, "echo", text="x"), call(2, "echo", text="x")])
    assert a.by_id(2)["error"]["data"]["cap"] == "max_calls_total"

    b = run_guard_cli(cfg_b, [call(1, "echo", text="x")])
    assert text_of(b.by_id(1)) == "x"


def test_report_subcommand(make_config, run_guard_cli, tmp_path):
    db = str(tmp_path / "report.db")
    cfg = make_config(
        limits={"max_calls_total": 2},
        costs={"tools": {"expensive": 0.5}},
        db_path=db,
        session_id="reported",
    )
    run_guard_cli(cfg, [call(1, "expensive"), call(2, "echo"), call(3, "echo")])

    out = subprocess.run(
        [sys.executable, "-m", "mcp_spend_guard", "report", "--db", db],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    text = out.stdout.decode("utf-8", "replace")
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    assert "session: reported" in text
    assert "expensive" in text
    assert "0.5000" in text
    assert "forwarded 2" in text
    assert "blocked 1" in text
    assert "max_calls_total=1" in text


def test_report_on_an_empty_db_is_graceful(tmp_path):
    out = subprocess.run(
        [sys.executable, "-m", "mcp_spend_guard", "report", "--db", str(tmp_path / "empty.db")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    assert out.returncode == 0
    assert "No sessions recorded yet." in out.stdout.decode()


def test_stdout_carries_only_json_rpc(make_config, run_guard_cli):
    """Log lines must never leak into the protocol stream."""
    cfg = make_config(limits={"max_calls_total": 1})
    run = run_guard_cli(cfg, [call(1, "echo", text="x"), call(2, "echo", text="x")])
    for line in run.stdout.splitlines():
        if line.strip():
            assert json.loads(line)["jsonrpc"] == "2.0"
    assert "[mcp-spend-guard]" in run.stderr
