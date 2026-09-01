# freqtrade MCP server

Local MCP server (stdio transport) that lets Claude Desktop / Claude Code
analyze and control the freqtrade bots running on the remote server, via the
existing `freqtrade-ui` ssh alias. No API keys, no cloud calls — this runs
entirely on your Claude subscription.

## Requirements

- Python 3.12+ with `mcp` installed (`pip install -r requirements.txt`)
- `ssh freqtrade-ui` already working from WSL Debian (see the project's
  `docker_wsl_access` memory) — this is how every tool reaches the server
- `wsl.exe` on PATH (Windows)

## Registration

Already wired up in:
- `C:\Users\<you>\AppData\Roaming\Claude\claude_desktop_config.json` (server key `freqtrade`)
- `E:\projects\freqtrade\.mcp.json` (Claude Code, this project)

Both invoke `python.exe -m nfi_mcp.server` with `PYTHONPATH` pointed at this
repo, so no `pip install -e .` / packaging step is needed.

## Architecture

```
server.py           MCP tool definitions (thin — delegates everything below)
freqtrade_api.py     REST client for the freqtrade API, via ssh+curl
signal_control.py    Hot NFI signal control: reads/writes strategy_control.json
                      on the server via a small embedded remote Python program
stats.py             Pure aggregation of trades by entry signal (no I/O)
bots.py              Registry of the two bots (nfi :8087, sample :8086)
ssh_link.py          Low-level: runs a bash script on the server over
                      wsl.exe -> ssh, stdin as bytes with \n (Windows'
                      text-mode stdin would otherwise inject \r\n and break
                      bash)
remote_snippets.py   Python source shared between the two remote scripts
                      above (they run as separate ssh sessions, so this is
                      the single source of truth for the duplicated text)
```

## Tools

Analytics (read-only): `ping`, `get_profit`, `get_open_trades`,
`stats_by_enter_tag`, `get_enabled_signals`, `get_control_log`,
`request_risk_analysis`.

Hot signal control (no reload/restart, applies within ~5-20s on the next
candle): `toggle_signal`, `clear_signal_override`, `set_ema200_guard`,
`set_risk_adjustment`, `clear_risk_adjustment`.

**Grey-zone risk-adjustment tools** (see the canonical `../nfi_mcp_server/README.md`
for the full design — this package mirrors it exactly, just over ssh instead
of native file ops): `request_risk_analysis(pair)` returns the strategy's
grey-zone-exit calibration for that pair plus any open trades' live
distance-to-forced-exit and a prompt for reasoning about a possible
adjustment; `set_risk_adjustment(pair, multiplier, ttl_hours, reason)` writes
a bounded (`multiplier` 0.25-4.0, `ttl_hours` 0.25-24.0), self-expiring entry
into `strategy_control.json`'s `risk_adjustments` — no autonomous scheduler,
no LLM call from this server itself, just a human/Claude-session round trip;
`clear_risk_adjustment(pair, reason)` revokes one before its TTL lapses. The
embedded remote Python program in `signal_control.py`'s `_REMOTE_TEMPLATE`
is a **literal duplicate** of `nfi_mcp_server/signal_control.py`'s
validation/calibration logic (this package has no import to share code with
it) — any change to the risk-adjustment math or bounds in one must be
mirrored in the other by hand.

Immediate bot control (freqtrade REST): `stop_entry`, `resume_bot`,
`force_exit`, `get_blacklist`, `blacklist_pair`, `unblacklist_pair`,
`reload_config` (the one tool that's NOT instant — ~10-20s, needed only after
editing config.json's `nfi_parameters` directly, not for hot toggles).

## How hot signal control works

`toggle_signal("long", "170", False, "reason")` writes
`/opt/nfi/user_data/strategy_control.json` on the server (atomic replace) and
appends an entry to `strategy_control_log.jsonl` there. The NFI subclass
(`NostalgiaForInfinityX7EMA200`, deployed separately — see the
`nfi_llm_control_design` memory) re-reads that file every bot loop and mutates
`self.long_entry_signal_params` in place; `populate_entry_trend` reads that
dict on every call, so the change is live on the next 5m candle. No
`/reload_config`, no container restart.

Overrides apply on top of the config.json-derived baseline. Clearing an
override (`clear_signal_override`) restores that baseline, not "all enabled".

## Credentials

`get_credentials` logic in `freqtrade_api.py` tries the bot's container
environment first (`docker exec <container> printenv
FREQTRADE__API_SERVER__USERNAME/PASSWORD` — how the `nfi` bot is configured),
falling back to the `api_server` block in the bot's own `config.json` (how
the `sample` bot is configured). Nothing is stored in this repo or passed
through the client; every credential read happens server-side, over ssh.

## Testing

No test framework wired up (small, single-purpose codebase) — offline logic
(`stats.py`, `signal_control`'s in-strategy behavior) was verified with a
stubbed-dependency script during development; live behavior was verified
end-to-end against the running bot (toggle a signal, confirm the bot's own
log line, restore). Re-run similar checks after changing `signal_control.py`
or the subclass file.
