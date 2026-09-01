# freqtrade MCP server — server-native deployment

Runs **on the same host as the bots** (85.122.114.77), not on your
workstation. Deployed as the docker container `nfi-mcp` at `/opt/nfi-mcp/`.
Reach it from Claude Desktop / Claude Code over the existing ssh tunnel to
this server — no API keys, no cloud calls, uses your Claude subscription.

For the local-Windows/stdio variant (reaches the bots over ssh+curl per
call), see `../nfi_mcp/` — kept as an offline-dev fallback, not currently
wired into any client config.

## Architecture

```
server.py             MCP tool definitions (streamable-http, 127.0.0.1:8765)
freqtrade_client.py    requests-based REST client -> http://127.0.0.1:8086/8087
signal_control.py      Native file ops on strategy_control.json + journal;
                        ast-parses NostalgiaForInfinityX7's class body for
                        signal defaults (no talib/pandas import needed)
stats.py               Pure aggregation of trades by entry signal (no I/O)
bots.py                Registry of the two bots (nfi :8087, sample :8086)
json_utils.py          strip_json_comments/load_json_with_comments - both
                        bots' config.json use rapidjson-style `//` comments,
                        which plain json.load() rejects
```

## Deployment

```
/opt/nfi-mcp/
  Dockerfile
  docker-compose.yml
  requirements.txt      mcp>=1.27,<2 (pin the upper bound! mcp 2.x renamed
                         FastMCP -> MCPServer and breaks this code)
  *.py
```

`docker-compose.yml`:
- `network_mode: host` — needs to reach the bots on `127.0.0.1:8086/8087`.
  Binds its own MCP port to `127.0.0.1` only (`MCP_HOST` env, FastMCP's
  default) — **never** `0.0.0.0`, or it would be reachable from the public
  IP. Verified unreachable from `85.122.114.77:8765` after deploy.
- Volumes:
  - `/opt/nfi/user_data:/opt/nfi/user_data` (rw — control file + journal)
  - `/opt/nfi/NostalgiaForInfinityX7.py:ro` — **required**:
    `user_data/strategies/NostalgiaForInfinityX7.py` is a *symlink* to
    `../../NostalgiaForInfinityX7.py`, one level above `user_data` (where
    the nfi-updater sidecar keeps the base file). Without this mount the
    symlink target is missing inside the container and every tool touching
    signal state (`get_enabled_signals`, `toggle_signal`, ...) fails with
    `FileNotFoundError`.
  - `/opt/freqtrade-bot/user_data/config.json:ro` — sample bot's credentials
- **No `docker.sock` mount.** Credentials come from:
  - `nfi`: `env_file: /opt/nfi/.env` loads
    `FREQTRADE__API_SERVER__USERNAME/PASSWORD` directly into this
    container's own environment (both bots share credentials, so this
    covers `sample` too, but the config.json fallback below stays as
    defense-in-depth)
  - fallback: the `api_server` block in the bot's own `config.json`
    (`json_utils.load_json_with_comments`)

  Mounting the docker socket to `docker exec` into the bot containers for
  credentials was the first working version, but was removed: it grants
  root-equivalent host access for something that only needed two env vars.

Redeploy after editing any `.py`/`Dockerfile`/`docker-compose.yml`:

```bash
ssh freqtrade-ui   # or: ssh root@85.122.114.77
cd /opt/nfi-mcp
docker compose build
docker compose up -d
docker logs nfi-mcp --tail 30
```

## Client access

Added `LocalForward 8765 127.0.0.1:8765` to the `Host freqtrade-ui` block in
WSL's `~/.ssh/config`, alongside the existing FreqUI (`8086`) forward.
**Restarting the `ssh freqtrade-ui` session is required** for a new
LocalForward to take effect — SSH does not hot-reload forwards on an
already-open connection.

`claude_desktop_config.json` and the project's `.mcp.json` both point at:

```json
{"type": "http", "url": "http://localhost:8765/mcp"}
```

(`Host polemic` in the same ssh config is a second alias for this identical
server — not a separate machine.)

## Tools

The same tools as `../nfi_mcp/` (see that README for the full list and the
description of how hot signal control works) — this package is a drop-in
replacement with a native (no-ssh) implementation underneath — plus
`get_exit_summary(bot, hours)`: a per-trade exit log (pair, entry signal,
exit reason, profit, close time) built from the freqtrade REST API instead
of scraping docker logs, which times out over the ssh tunnel for any window
longer than a few minutes.

**Grey-zone risk-adjustment tools** (added alongside the strategy's
`grey_zone_exit`/`risk_adjustments` feature, see
`NostalgiaForInfinityX7EMA200.py`'s module docstring and `CHANGELOG.md`):

- `request_risk_analysis(pair)` — read-only. Returns the pair's baseline
  cascade-rate calibration (which points are measured vs. modelled), every
  currently open trade on that pair with its live distance-to-forced-exit
  at the current price, the multiplier/TTL bounds, and a prompt explaining
  how to reason about a potential adjustment. Reads
  `NostalgiaForInfinityX7EMA200.py`'s calibration constants via `ast` (no
  import) and degrades gracefully — with a `"warning"` field, not an
  exception — if the deployed strategy predates this feature.
- `set_risk_adjustment(pair, multiplier, ttl_hours, reason)` — writes a
  bounded, self-expiring entry into `strategy_control.json`'s
  `risk_adjustments`. `multiplier` is clamped to `[0.25, 4.0]`, `ttl_hours`
  to `[0.25, 24.0]`; both clamps are reported (`_requested` vs `_applied`)
  in the return value and the journal rather than applied silently. The
  *real* safety bound is the strategy's own
  `grey_zone_exit_min_full_hours`/`max_full_hours` clamp — no multiplier can
  escape it.
- `clear_risk_adjustment(pair, reason)` — revoke before TTL expiry (e.g. the
  catalyst resolved or turned out false).

There is **no autonomous scheduler** here — nothing periodically calls an
LLM or reads news on its own. The intended flow is a human/Claude-session
round trip: call `request_risk_analysis`, reason about it (bring your own
news/market judgment), then call `set_risk_adjustment` if — and only if —
you have a specific citable catalyst. Example round trip:

```
> request_risk_analysis("ADA/USDT:USDT")
  -> baseline 30.4% cascade @72h, trade #42 open 19h at -3.4%, ~9.2h until forced exit
> set_risk_adjustment("ADA/USDT:USDT", 1.5, 5.0, "ADA unlock event in 6h, headline risk elevated")
  -> writes risk_adjustments["ADA/USDT:USDT"] = {multiplier: 1.5, expires_at: now+5h, ...}
> (5 hours later) the entry's expires_at has passed; custom_exit's own
  current_time comparison stops applying it automatically - no further
  action, no cleanup call needed.
```

`EMA200_STRATEGY_PATH` (`/opt/nfi/user_data/strategies/NostalgiaForInfinityX7EMA200.py`)
needs no new volume mount beyond the existing
`/opt/nfi/user_data:/opt/nfi/user_data` rw mount — unlike the base
`NostalgiaForInfinityX7.py` symlink, this file lives directly under
`user_data/strategies/` already covered by that mount.

## Testing

Verified through the actual MCP protocol (`initialize` →
`notifications/initialized` → `tools/call` over the streamable-HTTP
endpoint, handling `mcp-session-id`), not just the underlying Python
functions: `ping`, `get_profit`, `get_enabled_signals`, `get_control_log`,
and a full `toggle_signal` → bot-log-confirms → `clear_signal_override`
round trip on a live signal. The bot's own `bot_loop_start` interval when
picking up a control-file change was observed at anywhere from ~20s to
~55s in testing — don't assume a fixed delay when verifying a change.
