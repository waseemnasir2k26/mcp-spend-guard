"""Unit tests for the decision engine and the SQLite counters."""

from __future__ import annotations

import time

import pytest

from mcp_spend_guard.config import config_from_dict
from mcp_spend_guard.limits import (
    CAP_CIRCUIT_BREAKER,
    CAP_KILL_SWITCH,
    CAP_PER_MINUTE,
    CAP_PER_TOOL,
    CAP_SPEND,
    CAP_TOTAL,
    CAP_WALL_CLOCK,
    LimitEngine,
)
from mcp_spend_guard.store import Store


def engine(tmp_path, limits=None, costs=None, safety=None, started_at=None):
    cfg = config_from_dict(
        {
            "server": {"command": "x"},
            "limits": limits or {},
            "costs": costs or {},
            "safety": safety or {},
            "storage": {"db_path": str(tmp_path / "g.db"), "session_id": "s1"},
        }
    )
    store = Store(cfg.resolved_db_path())
    store.start_session(cfg.session_id)
    return LimitEngine(cfg, store, started_at=started_at)


def test_allows_when_no_caps_are_set(tmp_path):
    e = engine(tmp_path)
    for i in range(20):
        assert e.evaluate("echo", request_id=i).allowed


def test_total_cap(tmp_path):
    e = engine(tmp_path, limits={"max_calls_total": 2})
    assert e.evaluate("echo").allowed
    assert e.evaluate("other").allowed
    d = e.evaluate("echo")
    assert not d.allowed
    assert d.cap == CAP_TOTAL
    assert d.limit == 2 and d.current == 2


def test_per_tool_cap_is_independent(tmp_path):
    e = engine(tmp_path, limits={"max_calls_per_tool": {"echo": 1}})
    assert e.evaluate("echo").allowed
    d = e.evaluate("echo")
    assert not d.allowed and d.cap == CAP_PER_TOOL
    # a different tool is untouched
    assert e.evaluate("other").allowed


def test_per_minute_cap_uses_a_sliding_window(tmp_path):
    e = engine(tmp_path, limits={"max_calls_per_minute": 2})
    now = time.time()
    assert e.evaluate("echo", now=now).allowed
    assert e.evaluate("echo", now=now).allowed
    assert e.evaluate("echo", now=now).cap == CAP_PER_MINUTE
    # 61 seconds later the window has rolled
    assert e.evaluate("echo", now=now + 61).allowed


def test_spend_cap_refuses_the_call_that_would_cross_the_line(tmp_path):
    e = engine(
        tmp_path,
        limits={"max_spend_usd": 0.10},
        costs={"tools": {"cheap": 0.04, "pricey": 0.09}},
    )
    assert e.evaluate("cheap").allowed  # 0.04
    assert e.evaluate("cheap").allowed  # 0.08
    d = e.evaluate("pricey")  # would be 0.17
    assert not d.allowed and d.cap == CAP_SPEND
    assert pytest.approx(0.08, abs=1e-9) == d.current
    # 0.08 + 0.04 = 0.12 also crosses, so it is refused too
    assert not e.evaluate("cheap").allowed
    assert e.store.spend("s1") <= 0.10 + 1e-9


def test_untracked_tools_cost_the_default(tmp_path):
    e = engine(tmp_path, limits={"max_spend_usd": 0.05}, costs={"default_usd": 0.03})
    assert e.evaluate("mystery").allowed
    assert not e.evaluate("mystery").allowed


def test_wall_clock_cap(tmp_path):
    start = time.time()
    e = engine(tmp_path, limits={"wall_clock_timeout_s": 10}, started_at=start)
    assert e.evaluate("echo", now=start + 1).allowed
    d = e.evaluate("echo", now=start + 11)
    assert not d.allowed and d.cap == CAP_WALL_CLOCK


def test_kill_switch_file(tmp_path):
    stop = tmp_path / "STOP"
    e = engine(tmp_path, safety={"kill_switch_file": str(stop)})
    assert e.evaluate("echo").allowed
    stop.write_text("halt", encoding="utf-8")
    d = e.evaluate("echo")
    assert not d.allowed and d.cap == CAP_KILL_SWITCH
    stop.unlink()
    assert e.evaluate("echo").allowed


def test_kill_switch_beats_every_other_cap(tmp_path):
    stop = tmp_path / "STOP"
    stop.write_text("", encoding="utf-8")
    e = engine(
        tmp_path,
        limits={"max_calls_total": 100},
        safety={"kill_switch_file": str(stop)},
    )
    assert e.evaluate("echo").cap == CAP_KILL_SWITCH


def test_circuit_breaker_trips_on_consecutive_errors_only(tmp_path):
    e = engine(tmp_path, safety={"circuit_breaker_errors": 3})
    assert e.note_error() is False
    assert e.note_error() is False
    e.note_success()  # the streak resets
    assert e.note_error() is False
    assert e.note_error() is False
    assert e.note_error() is True
    d = e.evaluate("echo")
    assert not d.allowed and d.cap == CAP_CIRCUIT_BREAKER


def test_blocked_calls_do_not_consume_budget(tmp_path):
    e = engine(tmp_path, limits={"max_calls_total": 1}, costs={"default_usd": 1.0})
    e.evaluate("echo")
    e.evaluate("echo")
    e.evaluate("echo")
    assert e.store.total_allowed("s1") == 1
    assert e.store.spend("s1") == 1.0
    assert e.store.block_reasons("s1") == {CAP_TOTAL: 2}


def test_counters_survive_a_new_engine_on_the_same_db(tmp_path):
    e1 = engine(tmp_path, limits={"max_calls_total": 2})
    assert e1.evaluate("echo").allowed
    e1.store.close()

    e2 = engine(tmp_path, limits={"max_calls_total": 2})
    assert e2.evaluate("echo").allowed
    assert not e2.evaluate("echo").allowed
