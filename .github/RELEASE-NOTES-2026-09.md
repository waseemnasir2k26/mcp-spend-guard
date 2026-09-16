# 2026.09 — Maintenance release

- Maintenance review of mcp-spend-guard — a drop-in stdio proxy that wraps any MCP server and enforces a hard spend cap, per-tool rate limits, a circuit breaker and a kill switch, returning a clean JSON-RPC error to the agent instead of hanging when a cap trips.
- Stack: Python 3.11+, single runtime dependency (PyYAML), packaged via `pyproject.toml` (v0.1.0). Modules under `mcp_spend_guard/` (CLI, config, limits, proxy, SQLite-backed store); pytest suite in `tests/` with a fake MCP server fixture; example config at `config/guard.example.yaml`. MIT licensed.
- Status: v1, committed September 2026 in a single "mcp-spend-guard v1" commit. Provides `run` and `report` subcommands; the README badge claims 39 passing tests.
- Reviewed September 2026: docs refreshed, CHANGELOG started, versioned as v2026.09. No source, tests or packaging metadata changed.
- Known gaps (all stated by the README's own Limitations section): cost is estimated from a hand-written per-tool table, not billed; stdio transport only, no HTTP or SSE; per-call granularity, not per-token; not a sandbox — it counts and refuses calls, it does not inspect arguments; budgets are not pooled across servers. Repo-level: no CI workflow, no CHANGELOG before this release, not published to PyPI.
