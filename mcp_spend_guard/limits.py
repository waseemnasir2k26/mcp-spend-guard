"""The decision engine: given a tool call, may it through?

Kept deliberately free of I/O beyond the Store so it can be unit-tested directly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .config import GuardConfig
from .store import STATUS_ALLOWED, STATUS_BLOCKED, Store

# Cap identifiers, also used as the `data.cap` field of the JSON-RPC error we return.
CAP_KILL_SWITCH = "kill_switch"
CAP_CIRCUIT_BREAKER = "circuit_breaker"
CAP_WALL_CLOCK = "wall_clock_timeout_s"
CAP_PER_MINUTE = "max_calls_per_minute"
CAP_PER_TOOL = "max_calls_per_tool"
CAP_TOTAL = "max_calls_total"
CAP_SPEND = "max_spend_usd"


@dataclass
class Decision:
    """Outcome of evaluating one tools/call request."""

    allowed: bool
    tool: str
    cost_usd: float
    cap: Optional[str] = None
    message: Optional[str] = None
    limit: Optional[float] = None
    current: Optional[float] = None

    def as_error_data(self) -> dict:
        return {
            "cap": self.cap,
            "limit": self.limit,
            "current": self.current,
            "tool": self.tool,
            "guard": "mcp-spend-guard",
        }


class LimitEngine:
    """Evaluates and records every tools/call the guard sees."""

    def __init__(self, config: GuardConfig, store: Store, started_at: Optional[float] = None):
        self.config = config
        self.store = store
        self.session_id = config.session_id
        self.started_at = started_at if started_at is not None else time.time()
        self.consecutive_errors = 0
        self.breaker_tripped = False

    # ---------------- breaker bookkeeping ----------------

    def note_success(self) -> None:
        self.consecutive_errors = 0

    def note_error(self) -> bool:
        """Record one failed tool call. Returns True if this tripped the breaker."""
        self.consecutive_errors += 1
        cap = self.config.circuit_breaker_errors
        if cap is not None and self.consecutive_errors >= cap and not self.breaker_tripped:
            self.breaker_tripped = True
            return True
        return False

    # ---------------- the decision ----------------

    def evaluate(self, tool: str, request_id=None, now: Optional[float] = None) -> Decision:
        """Decide whether `tool` may run, and persist the outcome."""
        now = time.time() if now is None else now
        cfg = self.config
        cost = cfg.cost_for(tool)

        decision = self._check(tool, cost, now)

        if decision.allowed:
            self.store.record_call(
                self.session_id,
                tool,
                STATUS_ALLOWED,
                cost_usd=cost,
                request_id=None if request_id is None else str(request_id),
                ts=now,
            )
        else:
            self.store.record_call(
                self.session_id,
                tool,
                STATUS_BLOCKED,
                cost_usd=0.0,
                request_id=None if request_id is None else str(request_id),
                detail=decision.cap,
                ts=now,
            )
        return decision

    def _check(self, tool: str, cost: float, now: float) -> Decision:
        cfg = self.config

        # 1. Human override beats everything.
        kill = cfg.resolved_kill_switch()
        if kill is not None and kill.exists():
            return Decision(
                False,
                tool,
                cost,
                cap=CAP_KILL_SWITCH,
                message=(
                    f"kill switch engaged: {kill} exists. "
                    "Delete that file to resume tool calls."
                ),
            )

        # 2. Circuit breaker already tripped.
        if self.breaker_tripped:
            return Decision(
                False,
                tool,
                cost,
                cap=CAP_CIRCUIT_BREAKER,
                message=(
                    f"circuit breaker tripped after {self.consecutive_errors} consecutive "
                    "tool errors; no further calls will be forwarded"
                ),
                limit=cfg.circuit_breaker_errors,
                current=float(self.consecutive_errors),
            )

        # 3. Session wall clock.
        if cfg.wall_clock_timeout_s is not None:
            elapsed = now - self.started_at
            if elapsed >= cfg.wall_clock_timeout_s:
                return Decision(
                    False,
                    tool,
                    cost,
                    cap=CAP_WALL_CLOCK,
                    message=(
                        f"session wall-clock limit reached: {elapsed:.1f}s elapsed, "
                        f"limit {cfg.wall_clock_timeout_s:g}s"
                    ),
                    limit=cfg.wall_clock_timeout_s,
                    current=round(elapsed, 3),
                )

        # 4. Rate limit.
        if cfg.max_calls_per_minute is not None:
            recent = self.store.allowed_since(self.session_id, now - 60.0)
            if recent >= cfg.max_calls_per_minute:
                return Decision(
                    False,
                    tool,
                    cost,
                    cap=CAP_PER_MINUTE,
                    message=(
                        f"rate limit reached: {recent} calls in the last 60s, "
                        f"limit {cfg.max_calls_per_minute:g}/min"
                    ),
                    limit=cfg.max_calls_per_minute,
                    current=float(recent),
                )

        # 5. Per-tool cap.
        tool_cap = cfg.per_tool_cap(tool)
        if tool_cap is not None:
            used = self.store.tool_allowed(self.session_id, tool)
            if used >= tool_cap:
                return Decision(
                    False,
                    tool,
                    cost,
                    cap=CAP_PER_TOOL,
                    message=(
                        f"per-tool cap reached for {tool!r}: {used} calls used, "
                        f"limit {tool_cap:g}"
                    ),
                    limit=tool_cap,
                    current=float(used),
                )

        # 6. Total call cap.
        if cfg.max_calls_total is not None:
            used = self.store.total_allowed(self.session_id)
            if used >= cfg.max_calls_total:
                return Decision(
                    False,
                    tool,
                    cost,
                    cap=CAP_TOTAL,
                    message=(
                        f"total call cap reached: {used} calls used, "
                        f"limit {cfg.max_calls_total:g}"
                    ),
                    limit=cfg.max_calls_total,
                    current=float(used),
                )

        # 7. Spend cap (pre-emptive: we refuse the call that would cross the line).
        if cfg.max_spend_usd is not None:
            spent = self.store.spend(self.session_id)
            if spent + cost > cfg.max_spend_usd + 1e-9:
                return Decision(
                    False,
                    tool,
                    cost,
                    cap=CAP_SPEND,
                    message=(
                        f"spend cap reached: ${spent:.4f} estimated spend, this call "
                        f"adds ${cost:.4f}, limit ${cfg.max_spend_usd:.4f}"
                    ),
                    limit=cfg.max_spend_usd,
                    current=round(spent, 6),
                )

        return Decision(True, tool, cost)
