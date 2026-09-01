"""Configuration loading. Every cap lives in YAML; nothing is hardcoded."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class ConfigError(ValueError):
    """Raised when a config file is missing required fields or is malformed."""


def _as_optional_number(value: Any, key: str) -> Optional[float]:
    """Return a positive number, or None when the cap is disabled (null / 0 / absent)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key!r} must be a number or null, got {value!r}")
    if value <= 0:
        return None
    return float(value)


@dataclass
class GuardConfig:
    """Every knob the guard understands."""

    # --- wrapped server ---
    command: List[str]
    cwd: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)

    # --- caps (None == disabled) ---
    max_calls_total: Optional[float] = None
    max_calls_per_tool: Dict[str, float] = field(default_factory=dict)
    max_calls_per_minute: Optional[float] = None
    max_spend_usd: Optional[float] = None
    wall_clock_timeout_s: Optional[float] = None

    # --- cost model ---
    default_cost_usd: float = 0.0
    tool_costs: Dict[str, float] = field(default_factory=dict)

    # --- safety ---
    kill_switch_file: Optional[str] = None
    circuit_breaker_errors: Optional[float] = None
    exit_on_breaker_trip: bool = True

    # --- bookkeeping ---
    db_path: str = "~/.mcp-spend-guard/guard.db"
    session_id: str = "default"
    log_level: str = "info"

    source_path: Optional[str] = None

    def cost_for(self, tool: str) -> float:
        """Estimated USD cost of one call to `tool`."""
        return float(self.tool_costs.get(tool, self.default_cost_usd))

    def per_tool_cap(self, tool: str) -> Optional[float]:
        raw = self.max_calls_per_tool.get(tool)
        return _as_optional_number(raw, f"max_calls_per_tool.{tool}")

    def resolved_db_path(self) -> Path:
        return Path(os.path.expandvars(os.path.expanduser(self.db_path))).resolve()

    def resolved_kill_switch(self) -> Optional[Path]:
        if not self.kill_switch_file:
            return None
        return Path(os.path.expandvars(os.path.expanduser(self.kill_switch_file)))


def load_config(path: str | os.PathLike[str]) -> GuardConfig:
    """Read a guard YAML file into a GuardConfig."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping, got {type(raw).__name__}")
    return config_from_dict(raw, source_path=str(p.resolve()))


def config_from_dict(raw: Dict[str, Any], source_path: Optional[str] = None) -> GuardConfig:
    server = raw.get("server") or {}
    if not isinstance(server, dict):
        raise ConfigError("'server' must be a mapping")

    command = server.get("command")
    if isinstance(command, str):
        command = [command]
    args = server.get("args") or []
    if not isinstance(args, list):
        raise ConfigError("'server.args' must be a list")
    if not command:
        raise ConfigError("'server.command' is required (the MCP server you are wrapping)")
    command = [str(c) for c in command] + [str(a) for a in args]

    env = server.get("env") or {}
    if not isinstance(env, dict):
        raise ConfigError("'server.env' must be a mapping")

    limits = raw.get("limits") or {}
    if not isinstance(limits, dict):
        raise ConfigError("'limits' must be a mapping")

    per_tool = limits.get("max_calls_per_tool") or {}
    if not isinstance(per_tool, dict):
        raise ConfigError("'limits.max_calls_per_tool' must be a mapping of tool -> count")

    costs = raw.get("costs") or {}
    if not isinstance(costs, dict):
        raise ConfigError("'costs' must be a mapping")
    tool_costs = costs.get("tools") or {}
    if not isinstance(tool_costs, dict):
        raise ConfigError("'costs.tools' must be a mapping of tool -> usd")
    for k, v in tool_costs.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ConfigError(f"costs.tools.{k} must be a number, got {v!r}")

    safety = raw.get("safety") or {}
    if not isinstance(safety, dict):
        raise ConfigError("'safety' must be a mapping")

    storage = raw.get("storage") or {}
    if not isinstance(storage, dict):
        raise ConfigError("'storage' must be a mapping")

    return GuardConfig(
        command=command,
        cwd=server.get("cwd"),
        env={str(k): str(v) for k, v in env.items()},
        max_calls_total=_as_optional_number(limits.get("max_calls_total"), "limits.max_calls_total"),
        max_calls_per_tool={str(k): float(v) for k, v in per_tool.items() if v is not None},
        max_calls_per_minute=_as_optional_number(
            limits.get("max_calls_per_minute"), "limits.max_calls_per_minute"
        ),
        max_spend_usd=_as_optional_number(limits.get("max_spend_usd"), "limits.max_spend_usd"),
        wall_clock_timeout_s=_as_optional_number(
            limits.get("wall_clock_timeout_s"), "limits.wall_clock_timeout_s"
        ),
        default_cost_usd=float(costs.get("default_usd", 0.0) or 0.0),
        tool_costs={str(k): float(v) for k, v in tool_costs.items()},
        kill_switch_file=safety.get("kill_switch_file"),
        circuit_breaker_errors=_as_optional_number(
            safety.get("circuit_breaker_errors"), "safety.circuit_breaker_errors"
        ),
        exit_on_breaker_trip=bool(safety.get("exit_on_breaker_trip", True)),
        db_path=str(storage.get("db_path", "~/.mcp-spend-guard/guard.db")),
        session_id=str(storage.get("session_id", "default")),
        log_level=str(raw.get("log_level", "info")),
        source_path=source_path,
    )
