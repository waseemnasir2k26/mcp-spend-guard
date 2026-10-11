# mcp-spend-guard

A drop-in proxy that wraps any stdio MCP server and enforces a hard spend cap, per-tool rate limits, a circuit breaker and a kill switch — so a looping agent cannot quietly burn your budget.

## Status

Last reviewed: October 2026 · release v2026.10

```console
$ mcp-spend-guard run -c guard.yaml
[mcp-spend-guard] info: wrapping: npx -y @acme/search-mcp
[mcp-spend-guard] warn: tool 'web_search' returned an error (1 consecutive)
[mcp-spend-guard] warn: BLOCKED 'web_search': spend cap reached: $0.2000 estimated spend, this call adds $0.1000, limit $0.2500
[mcp-spend-guard] warn: BLOCKED 'web_search': spend cap reached: $0.2000 estimated spend, this call adds $0.1000, limit $0.2500
[mcp-spend-guard] info: session 'default': 2 calls forwarded, $0.2000 estimated spend

# the agent sees a clean JSON-RPC error, not a hang:
{"jsonrpc":"2.0","id":3,"error":{"code":-32011,
 "message":"mcp-spend-guard blocked this call [max_spend_usd]: spend cap reached: $0.2000 estimated spend, this call adds $0.1000, limit $0.2500",
 "data":{"cap":"max_spend_usd","limit":0.25,"current":0.2,"tool":"web_search","guard":"mcp-spend-guard"}}}

$ mcp-spend-guard report

session: default
  started 2026-09-01 17:02:03   last seen 2026-09-01 17:02:03   runs 1
  forwarded 2   blocked 2   tool errors 0   estimated spend $0.2000

  tool                           calls  blocked  errors      est $
  ---------------------------- ------- -------- ------- ----------
  web_search                         2        2       0     0.2000

  blocks by cap: max_spend_usd=2
```

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![tests](https://img.shields.io/badge/tests-39%20passing-brightgreen.svg)](tests/)

## Quick start

**1. Install**

```bash
pipx install .        # or: pip install .
```

**2. Write a config** (start from [`config/guard.example.yaml`](config/guard.example.yaml))

```yaml
server:
  command: npx
  args: ["-y", "@acme/search-mcp"]

limits:
  max_calls_total: 200
  max_calls_per_minute: 30
  max_spend_usd: 5.00

costs:
  tools:
    web_search: 0.01

safety:
  kill_switch_file: ./STOP
  circuit_breaker_errors: 5
```

**3. Point your client at the guard instead of the server**

`.mcp.json` / `claude_desktop_config.json`:

```diff
 {
   "mcpServers": {
     "search": {
-      "command": "npx",
-      "args": ["-y", "@acme/search-mcp"]
+      "command": "mcp-spend-guard",
+      "args": ["run", "-c", "/abs/path/to/guard.yaml"]
     }
   }
 }
```

The server command moves _into_ the YAML — that's the whole migration. Restart your client and the session runs exactly as before, with counters in SQLite.

**4. Stop a runaway agent from another terminal**

```bash
touch STOP     # every subsequent tool call is refused immediately
rm STOP        # and back to normal
```

**5. See where the money went**

```bash
mcp-spend-guard report
```

## What it protects against

- **Runaway loops.** An agent that retries the same tool 400 times because the result never satisfies its plan. `max_calls_total` and `max_calls_per_minute` end that in seconds instead of hours.
- **A compromised or over-eager agent.** Prompt injection in a fetched page tells the model to call your paid enrichment tool on every row of a CSV. `max_calls_per_tool` bounds the blast radius per tool, not just overall.
- **A retry storm on an expensive tool.** A flaky upstream returns errors, the agent retries, each attempt costs real money. The circuit breaker trips after N consecutive tool errors and stops the session; the spend cap is the backstop underneath it.
- **A budget that silently resets.** Counters live in SQLite keyed by session, so a crash-looping client that reconnects every 5 seconds does not get a fresh allowance each time.

It does **not** protect against a malicious MCP server (it is a meter, not a sandbox), and it does not inspect tool arguments for anything other than the tool name.

## Config reference

Every cap is optional. Omit it, set it to `null`, or set it to `0` to disable it. The first cap that trips wins, and the client is told which one it was.

| Key                             | Type           | Default                       | What it does                                                                                                                    |
| ------------------------------- | -------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `server.command`                | string \| list | _required_                    | The MCP server to wrap.                                                                                                         |
| `server.args`                   | list           | `[]`                          | Appended to `command`.                                                                                                          |
| `server.cwd`                    | path           | guard's cwd                   | Working directory for the wrapped server.                                                                                       |
| `server.env`                    | map            | `{}`                          | Merged on top of the guard's environment.                                                                                       |
| `limits.max_calls_total`        | int            | off                           | Hard ceiling on forwarded `tools/call` requests for the session.                                                                |
| `limits.max_calls_per_tool`     | map            | `{}`                          | Per-tool ceilings, e.g. `web_search: 25`.                                                                                       |
| `limits.max_calls_per_minute`   | int            | off                           | Sliding 60-second window across all tools.                                                                                      |
| `limits.max_spend_usd`          | float          | off                           | Estimated USD ceiling. A call is refused _before_ it runs if it would cross the line.                                           |
| `limits.wall_clock_timeout_s`   | float          | off                           | After this many seconds of session life, tool calls are refused. The connection stays open so the client can shut down cleanly. |
| `costs.default_usd`             | float          | `0.0`                         | Estimated cost of any tool not in the table.                                                                                    |
| `costs.tools`                   | map            | `{}`                          | Per-tool USD estimates, e.g. `generate_image: 0.04`.                                                                            |
| `safety.kill_switch_file`       | path           | off                           | If this file exists, every tool call is refused. Human override, checked per call.                                              |
| `safety.circuit_breaker_errors` | int            | off                           | N _consecutive_ tool errors trip the breaker. A success resets the streak.                                                      |
| `safety.exit_on_breaker_trip`   | bool           | `true`                        | On trip, shut the wrapped server down and exit `3`. Set `false` to keep the session alive but frozen.                           |
| `storage.db_path`               | path           | `~/.mcp-spend-guard/guard.db` | SQLite file holding the `sessions` and `calls` tables.                                                                          |
| `storage.session_id`            | string         | `default`                     | Budgets are keyed by this. Change it to start a fresh allowance.                                                                |
| `log_level`                     | enum           | `info`                        | `debug`, `info`, `warn`, `error`, `silent`. Always on stderr, never stdout.                                                     |

CLI overrides: `run --session-id`, `run --db`, `run --log-level`. Other subcommands: `report [--db|-c] [--session-id]` and `reset --session-id`.

## How it works

The guard is a stdio JSON-RPC relay. It spawns the real server as a child process and pumps newline-delimited JSON in both directions. Frames it does not understand are forwarded byte-for-byte and in order; the only ones it ever withholds are `tools/call` requests that trip a cap.

```
   MCP client                mcp-spend-guard                   real MCP server
  (Claude Code)                                              (npx / uvx / python)
        |                            |                                |
        |--- tools/call ------------>|  inspect: tool name, cost       |
        |                            |  check caps against SQLite      |
        |                            |                                |
        |                       allowed? --yes--> forward verbatim --->|
        |<-------------------------- |<---------- result -------------|
        |                            |  (errors feed the breaker)      |
        |                            |                                |
        |<-- JSON-RPC error -------- no (cap tripped, nothing sent)    |
        |    code -32011                                              |
        |    data.cap = which cap                                     |
                                     |
                             ~/.mcp-spend-guard/guard.db
                             sessions + calls  (survives restarts)
```

When a cap trips the client receives a normal JSON-RPC error response carrying `error.code = -32011` and an `error.data` object naming the cap, its limit and the current counter. Every request keeps getting exactly one reply, which is the point: a malformed or missing response makes an MCP client wait forever.

Responses are inspected on the way back only to feed the circuit breaker — both JSON-RPC `error` objects and MCP's `result.isError` count as tool errors.

## Limitations

Being honest about what this v1 is not:

- **Cost is estimated, not billed.** It comes from the per-tool table _you_ write in the config. The guard has no view of any provider's invoice. If your estimates are wrong, your cap is wrong — treat it as a circuit breaker, not accounting.
- **stdio only.** HTTP and SSE transports are not wrapped yet; that is the next transport on the list.
- **Per-call granularity.** Cost is charged per call, not per token or per byte returned. A tool whose price varies with input size cannot be modelled precisely.
- **Not a sandbox.** It counts and refuses calls. It does not inspect arguments, sanitize results, or defend against a hostile MCP server.
- **Rate limiting is a sliding count**, not a token bucket — bursts up to the limit are allowed within any 60-second window.
- **One guard per server.** Wrapping five servers means five configs and five entries; budgets are not pooled across them unless they share a `db_path` _and_ a `session_id`.

## Contributing

Issues and PRs welcome — especially HTTP/SSE transport support, and cost tables for real-world MCP servers.

```bash
git clone <this repo> && cd mcp-spend-guard
pip install -e ".[dev]"
python -m pytest
```

The test suite spawns a real subprocess and speaks real JSON-RPC to `tests/fake_mcp_server.py`, a tiny fake MCP echo server, so it needs no network and no API key. Any change to the relay needs a test that pipes traffic through it.

---

## Work with SkynetLabs

Built by [Waseem Nasir](https://www.skynetjoe.com/?utm_source=github&utm_medium=readme&utm_campaign=mcp-spend-guard) at SkynetLabs — n8n + AI automation for small businesses.

- Hire on Fiverr: https://www.fiverr.com/agencies/skynetjoellc
- Hire on Upwork: https://www.upwork.com/freelancers/muhammadwaseemnasir
- Book a free call: https://calendly.com/skynetlabs/schedule-a-free-consultation

## License

MIT — see [LICENSE](LICENSE).
