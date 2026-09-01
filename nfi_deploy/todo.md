# NFI blacklist/unban follow-up — deferred work

Context: 2026-08-28 90-day backtest of all 185 currently-tradable
blacklisted tickers (see CHANGELOG.md and the blacklist-backtest artifact)
found 89 with positive expectancy. 75 of those (excluding tokenized-stock/
commodity/synthetic-instrument bans, which stay banned regardless of
backtest profit — see blacklist-binance.json's category comments) were
removed from the blacklist and deployed to the dry-run bot on 2026-08-28,
alongside two new hot-reloadable strategy_control.json mechanisms:
`pair_blocks` (date-gated entry bans, pre-stageable ahead of a known future
event) and `unbanned_pairs` (shadow-mode: a pair can trade "for real" on a
live bot while its orders are locally simulated and never sent to the
exchange, via a monkey-patched `Exchange.create_order` — see the strategy's
module docstring). Both are implemented, syntax-checked, and deployed to
the dry-run bot (`unbanned_pairs` currently empty — nothing is tracked as
shadow-mode yet, this is plumbing only).

None of the pieces below are built yet. Each is its own session-sized task.

## 1. `retest_blacklist` MCP command

Goal: let an external model manually trigger a real strategy backtest
against currently-blacklisted pairs ("вдруг будет найдено противоядие").

Decided architecture (2026-08-28): a **new dedicated sidecar container**,
same pattern as `nfi-updater` — gets `docker.sock` (the *only* container
that does), `nfi-mcp` itself stays unprivileged. Protocol: `nfi-mcp` writes
a request JSON to a shared volume, the sidecar polls, downloads any missing
OHLCV data, runs `docker compose run --rm freqtrade backtesting` against
the pair(s), writes a result JSON back. New MCP tools: `retest_blacklist`
(submit a job, since a single backtest can take many minutes — see the
185-pair run that took ~2.5h before it was batched down to ~15-20min/batch
of 37 pairs) and `get_retest_status(job_id)`.

Open questions for that session: sidecar image/compose file, request/result
file schema, how much history to auto-download for a pair that's never been
in the live whitelist (no cached candles at all), timeout/cleanup policy
for stale jobs.

## 2. Telegram notification for shadow-mode trade closes

Goal: when a shadow-mode (or fully-dry-run) trade on a recently-unbanned
pair closes, push a Telegram message tagged "recently unbanned" — profit
prominently, whether it was shadow or real.

Currently: `NostalgiaForInfinityX7EMA200.order_filled` only logs this
(clearly tagged, searchable) — no Telegram push yet.

Blocker: the Telegram bot token/chat_id today lives only in the **sample**
bot's config.json (`/opt/freqtrade-bot/user_data/config.json`), read by
`nfi_telegram_bot` (see `_telegram_credentials()` in that project). The NFI
container has no access to that file/credential. Need to decide: share the
credential into `/opt/nfi` (env var or mounted file), or have
`nfi_telegram_bot` itself poll for newly-closed unbanned-pair trades via
each bot's REST API (`/opt/nfi` already exposes trades over its own API) —
the second option needs no new credential sharing and fits
`nfi_telegram_bot`'s existing poll-based design better than a push from the
strategy would.

## 3. FreqUI (frequi_custom) changes

Not started — `frequi_custom` hasn't been opened this session. Three asks,
on the Trade page's Whitelist Methods / Whitelist pair-button grid:

1. Badge on a pair's button showing it was recently removed from the
   blacklist (data source: `unbanned_pairs` via a new read endpoint, or a
   new MCP-fronted read the frontend can call — not decided).
2. Clicking a "recently unbanned" pair opens a modal for risk amount or %
   of account. Backend for this already exists:
   `set_unbanned_pair_risk_budget(pair, risk_budget_pct, risk_budget_abs,
   reason)` MCP tool (nfi_mcp_server, not yet deployed to the server — see
   below). Setting either value above 0 is what flips the pair out of
   shadow mode.
3. Needs frequi_custom's build/deploy pipeline understood (mounted into
   both bots per `docker_wsl_access`/`frequi_custom_ui` memory) before any
   of this can ship.

## 4. ~~Deploy the updated nfi_mcp_server to the server's nfi-mcp container~~ DONE 2026-08-28

Deployed and smoke-tested: `signal_control.py`/`server.py` pushed to
`/opt/nfi-mcp/` (backed up as `.bak-20260828-pre-pairblocks`), image
rebuilt (`docker compose build`), container recreated. Verified by calling
`schedule_pair_block`/`clear_pair_block`/`mark_pair_unbanned`/
`set_unbanned_pair_risk_budget`/`clear_unbanned_pair` directly inside the
container against the real `/opt/nfi/user_data/strategy_control.json` —
all worked, no leftover state after cleanup.

Bonus fix found in the process: the server's `server.py` already called
`signal_control.grey_zone_calibration`/`set_risk_adjustment`/etc. (deployed
some earlier session) but the matching backend was **never actually
deployed to signal_control.py** — those MCP tools have been silently
broken (AttributeError on call) until this deploy fixed it too.

## 5. Decide whether to backfill `mark_pair_unbanned` for the 75 pairs just unbanned

Once (4) is done, decide whether all 75 pairs removed from the blacklist on
2026-08-28 should get an `unbanned_pairs` entry (shadow mode by default) via
`mark_pair_unbanned`, or whether shadow-mode tracking is opt-in per pair
going forward only. Leaning toward "yes, backfill all 75" since that's the
whole point of the safety mechanism, but not decided/done.
