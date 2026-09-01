"""Unit tests for config loading."""

from __future__ import annotations

import pytest
import yaml

from mcp_spend_guard.config import ConfigError, config_from_dict, load_config


def test_command_and_args_are_joined():
    cfg = config_from_dict({"server": {"command": "npx", "args": ["-y", "some-server"]}})
    assert cfg.command == ["npx", "-y", "some-server"]


def test_command_is_required():
    with pytest.raises(ConfigError):
        config_from_dict({"server": {"args": ["x"]}})


def test_missing_file_raises():
    with pytest.raises(ConfigError):
        load_config("definitely-not-here.yaml")


def test_zero_and_null_disable_a_cap():
    cfg = config_from_dict(
        {
            "server": {"command": "x"},
            "limits": {"max_calls_total": 0, "max_spend_usd": None, "max_calls_per_minute": 5},
        }
    )
    assert cfg.max_calls_total is None
    assert cfg.max_spend_usd is None
    assert cfg.max_calls_per_minute == 5.0


def test_cost_lookup_falls_back_to_default():
    cfg = config_from_dict(
        {
            "server": {"command": "x"},
            "costs": {"default_usd": 0.02, "tools": {"pricey": 1.5}},
        }
    )
    assert cfg.cost_for("pricey") == 1.5
    assert cfg.cost_for("anything_else") == 0.02


def test_non_numeric_cost_is_rejected():
    with pytest.raises(ConfigError):
        config_from_dict(
            {"server": {"command": "x"}, "costs": {"tools": {"bad": "one dollar"}}}
        )


def test_defaults_are_all_off():
    cfg = config_from_dict({"server": {"command": "x"}})
    assert cfg.max_calls_total is None
    assert cfg.max_calls_per_minute is None
    assert cfg.max_spend_usd is None
    assert cfg.wall_clock_timeout_s is None
    assert cfg.circuit_breaker_errors is None
    assert cfg.kill_switch_file is None
    assert cfg.session_id == "default"


def test_shipped_example_config_parses(tmp_path):
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config" / "guard.example.yaml"
    cfg = load_config(example)
    assert cfg.command[0] == "npx"
    assert cfg.max_calls_total == 200
    assert cfg.per_tool_cap("web_search") == 25
    assert cfg.cost_for("generate_image") == 0.04
    assert cfg.circuit_breaker_errors == 5
    # and it is valid YAML by any other reader too
    assert isinstance(yaml.safe_load(example.read_text(encoding="utf-8")), dict)
