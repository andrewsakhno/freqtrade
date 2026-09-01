---
name: strategy-tester
description: Use when the user wants to test a freqtrade strategy locally against recent market data — "test the strategy locally", "backtest the last N hours", "проверить стратегию локально", "прогнать стратегию за последние N часов", "потюнить параметры стратегии", "hyperopt this strategy", "прогнать на всех данных за N дней и сравнить", "как стратегия реагирует на свечки/покупает/продаёт". Downloads the needed candles via the local docker-compose freqtrade image, runs a backtest over a user-given lookback window, and optionally searches for better parameters (hyperopt or NFI signal toggles) to improve the result. Also covers full-whitelist multi-day A/B comparisons (step 6) and signal-level behavioral analysis — what fired, DCA frequency, rejected entries (step 7).
---

# Strategy tester

Runs a strategy against a fresh, short window of real market data using the
project's own `docker-compose.yml` (`freqtradeorg/freqtrade:stable`, config
at `user_data/config.json`) — no server access required for plain
backtesting, since OHLCV candles are public exchange data.

**Docker is NOT on Windows.** It only exists inside WSL Debian (verified
2026-08-26: `docker ps` works there, unrelated containers already running —
`job_searcher-app`, `pgvector`). Every `docker compose` call in this skill
must go through `wsl -d Debian -- bash -lc "..."`, never plain `docker`/
`docker compose` from PowerShell or git-bash directly.

**The E: drive mount inside WSL is frequently wedged** (`/mnt/e/...` fails
with `No such device` / errno 19 — seen repeatedly, not a one-off). Don't
fight it and don't run `wsl --shutdown` to fix it (that's disruptive — kills
the ssh tunnel and the unrelated containers — and per [[freqtrade-server-deployment]]
warn the user first if it's ever truly needed). Instead work entirely on the
WSL-native filesystem:
1. `wsl -d Debian -- bash -lc "mkdir -p ~/strategy-tester/user_data/strategies"`
2. Copy in what's needed via the `\\wsl$\Debian\...` UNC path from
   **PowerShell** (this direction — Windows reading/writing into WSL — works
   even when the reverse drvfs mount doesn't):
   `Copy-Item "E:\projects\freqtrade\docker-compose.yml" "\\wsl$\Debian\home\sand\strategy-tester\docker-compose.yml"`
   and likewise for `user_data/config.json` and the strategy file(s).
3. Run every `docker compose` command with `cd ~/strategy-tester &&` inside
   the same `wsl -d Debian -- bash -lc "..."` call.

## 0. Resolve inputs before doing anything

Ask only if genuinely ambiguous; otherwise infer from context:
- **Strategy name** — check `user_data/strategies/` first, then
  `nfi_deploy/` (this repo's NostalgiaForInfinityX7EMA200 subclass). If the
  user just says "the strategy" and only one strategy is in active use per
  memory/recent conversation, use that one.
- **N hours** — the lookback window whose *results* the user cares about.
  This is NOT the amount of data to download — see step 2.
- **Pairs — must be explicit, always.** `user_data/config.json`'s
  `VolumePairList` is **not** usable for `download-data` or `backtesting`:
  `dynamic_expand_pairlist()` (`freqtrade/plugins/pairlist/pairlist_helpers.py`)
  only expands literal/wildcard `config["pairs"]` entries, never resolves a
  plugin like `VolumePairList` — `download-data` silently returns "No pairs
  available for download" otherwise, and `backtesting`/`hyperopt` hard-error
  with "Pairlist Handlers VolumePairList do not support backtesting." Always
  pass `--pairs <explicit list>` to `download-data`, AND layer a second
  `--config` file overriding `pairlists` to `StaticPairList` (see step 3) —
  the base `config.json` cannot be used unmodified for either command.
  Default to a handful of liquid futures pairs (e.g. `BTC/USDT:USDT
  ETH/USDT:USDT SOL/USDT:USDT XRP/USDT:USDT DOGE/USDT:USDT`) unless the user
  names others — this is not the bot's real-time top-20 by volume, say so.
  **If the user asks to test against "all pairs"/"the whole whitelist"/"all
  data"** (not a quick sanity check), don't hand-pick a subset — build the
  `StaticPairList` config from the bot's actual live whitelist snapshot in
  `nfi_deploy/tools/seed_pair_params.py`'s `WHITELIST_SYMBOLS` (96 pairs as of
  2026-08-28; re-derive fresh from the bot's own candle-fetch log if it's
  gone stale — see that file's own docstring for how). See step 6 for the
  full-whitelist multi-day workflow this drives.
- **Trading mode** — `user_data/config.json` has `"trading_mode": "futures"`.
  Pass `--trading-mode futures` on `download-data` so funding-rate/mark data
  also gets pulled, or futures backtests will fail validation.
- **`max_open_trades` in this repo's `user_data/config.json` is 3 — that is
  NOT what the real bot runs with.** The live server's `.env` sets
  `FREQTRADE__MAX_OPEN_TRADES=6` and `config.json` sets
  `futures_max_open_trades_short: 4` (values as of 2026-08-28; re-check
  `/opt/nfi/.env` and `/opt/nfi/user_data/config.json` on the server if this
  matters to the result — see [[freqtrade-server-deployment]]). Left at the
  repo default, the trade cap silently throttles activity and any
  behavior/trade-count report will understate how active the strategy really
  is. Override both keys in the same `--config` layer used for
  `StaticPairList` (step 4) whenever the result should reflect real bot
  behavior, not just a quick single-signal sanity check.

## 1. Special case: NostalgiaForInfinityX7-family strategies

`nfi_deploy/NostalgiaForInfinityX7EMA200.py` subclasses `NostalgiaForInfinityX7`
via `from NostalgiaForInfinityX7 import NostalgiaForInfinityX7` — the base
file is **not** in this repo (server's `nfi-updater` sidecar owns it, see
[[freqtrade-server-deployment]] memory). Before backtesting this strategy:

1. Find the exact files on the server (don't hardcode a path — it can drift):
   `wsl -d Debian -- ssh freqtrade-ui "find /opt/nfi -maxdepth 3 -iname 'NostalgiaForInfinityX7*.py'"`
2. Copy both the base file and the subclass into `user_data/strategies/`
   (that's the only directory the compose file mounts into the container):
   `wsl -d Debian -- ssh freqtrade-ui "cat /opt/nfi/.../NostalgiaForInfinityX7.py"` → write locally,
   or `scp freqtrade-ui:/opt/nfi/.../NostalgiaForInfinityX7.py "E:/projects/freqtrade/user_data/strategies/"`.
   Pulling from the server (not the public iterativv/NostalgiaForInfinity repo)
   matters — the server pins whatever the daily updater last fetched, and per
   [[nfi-local-first-workflow]] the server is the source of truth.
3. These are pulled from a third party / a running bot's live state, not this
   repo's own code — do not commit them. Leave them untracked (`user_data/`
   is already gitignored) and mention to the user they're local-only copies.

Skip this whole section for any other strategy already sitting in
`user_data/strategies/`.

## 2. Compute the download window (warm-up buffer matters)

Indicators need history *before* the window you actually want tested — e.g.
an EMA200 on 5m candles needs ~16.6h just to produce its first value, and NFI
X7 uses multiple higher informative timeframes (1h/4h/1d) on top of that.
Read the strategy's `timeframe`, `informative_pairs`/informative timeframes,
and `startup_candle_count` to size the buffer. If unsure, over-download
rather than under-download — freqtrade only *uses* what it needs for warm-up
and reports on `--timerange` regardless of how much extra you fetched.

Rule of thumb: `download_start = now - max(N hours, 3 days worth of candles for the largest informative timeframe used)`.
For an 8h test window with a 1d informative, download at least 10-14 days.

Compute both as **epoch seconds** (`date -u +%s` / `date -u -d "N hours ago" +%s`),
not the `yyyymmddThhmm` text format — the `freqtradeorg/freqtrade:stable`
image tested against (freqtrade 2026.7) rejected `20260826T1522-...` with
"Incorrect syntax for timerange" even though it matches the regex in this
repo's own `freqtrade/configuration/timerange.py`; epoch (`\d{10}`) worked
reliably. Also don't trust a `yyyymmdd`-only end date to mean "now" — in
testing it silently downloaded well past that bound. Always pass explicit
epoch endpoints for both `download_start` and `now`.
- `DOWNLOAD_RANGE` = `<download_start_epoch>-<now_epoch>`.
- `TEST_RANGE` = `<now_epoch - N*3600>-<now_epoch>` — this is what actually
  gets passed to `backtesting`/`hyperopt`'s `--timerange`; freqtrade
  auto-consumes the earlier downloaded candles for indicator warm-up.
- Recompute `now_epoch` fresh right before the backtest call too (don't reuse
  the value from the download step) — `download-data` only fetches the delta
  since the last cached candle, so if real time has moved on between the two
  calls, an earlier "now" leaves a gap right before the actual current time.

## 3. Download data

```bash
wsl -d Debian -- bash -lc "cd ~/strategy-tester && docker compose run --rm freqtrade download-data \
  --config /freqtrade/user_data/config.json \
  --pairs <explicit pairs...> \
  --timeframes <base_tf> <informative_tfs...> \
  --timerange <DOWNLOAD_RANGE> \
  --trading-mode futures"
```
Omit `--trading-mode futures` for spot-only strategies.

## 4. Run the backtest

`backtesting` errors out immediately if the config's `pairlists` method is
`VolumePairList` ("do not support backtesting") — write a second config
overriding it before running, don't edit `user_data/config.json` itself:
```bash
wsl -d Debian -- bash -lc "cat > ~/strategy-tester/user_data/config-backtest.json << 'EOF'
{ \"pairlists\": [ { \"method\": \"StaticPairList\" } ],
  \"exchange\": { \"pair_whitelist\": [ \"<pair1>\", \"<pair2>\", ... ] } }
EOF"
```
```bash
wsl -d Debian -- bash -lc "cd ~/strategy-tester && docker compose run --rm freqtrade backtesting \
  --config /freqtrade/user_data/config.json \
  --config /freqtrade/user_data/config-backtest.json \
  --strategy <StrategyClassName> \
  --strategy-path /freqtrade/user_data/strategies \
  --timerange <TEST_RANGE> \
  --export trades"
```
Report the summary table as-is (total profit, win rate, max drawdown, trade
count). **Flag explicitly if the trade count is very low, including zero** —
a handful of trades (or none at all) on an 8h window with 5 pairs is a
plausible, real result for a selective strategy like NFI, not necessarily a
bug — say so rather than letting a good/bad-looking percentage imply
confidence it doesn't have. Zero trades also means there is nothing to
compare for step 5 — say that explicitly and offer to widen the pair count
or window rather than silently running hyperopt/a sweep against an empty
result set.

## 5. Parameter tuning — pick the right mechanism

Don't default to classic hyperopt without checking how the strategy exposes
tunables:

- **Strategy defines `IntParameter`/`DecimalParameter`/`CategoricalParameter`
  hyperopt spaces** → use freqtrade's built-in hyperopt:
  ```bash
  wsl -d Debian -- bash -lc "cd ~/strategy-tester && docker compose run --rm freqtrade hyperopt \
    --config /freqtrade/user_data/config.json \
    --config /freqtrade/user_data/config-backtest.json \
    --strategy <StrategyClassName> \
    --strategy-path /freqtrade/user_data/strategies \
    --hyperopt-loss SharpeHyperOptLoss \
    --spaces buy sell \
    --timerange <TEST_RANGE> \
    --epochs 100 -j -1"
  ```
  Same `StaticPairList` override config from step 4 applies here too.
  **Warn the user first if `N hours` is small** (say, under a few days):
  hyperopt on a handful of trades will overfit to noise, not find a robust
  parameter set. Recommend widening the window for the *tuning* run even if
  N hours is what they want reported for the sanity-check backtest — these
  can be two different ranges.

- **NostalgiaForInfinityX7-family strategies** are architected around
  per-signal on/off toggles (`nfi_deploy/strategy_control.json`:
  `long_signals_override` / `short_signals_override`), not classic hyperopt
  buy/sell spaces — this project already uses that mechanism, see
  [[nfi-project-rules-focus]]. "Best result" here means: run the backtest
  once per candidate toggle combination and compare, rather than a genetic
  hyperopt run. Keep the sweep small and explain each combination tried and
  its result — don't silently pick a winner without showing the comparison.

Either way, present the *comparison* (baseline vs. each candidate/epoch
result), not just a final answer — the user asked to reach the best result,
which implies they want to see the search, not just trust a black box.

## 6. Full-whitelist, N-day A/B comparison

Use this when the user wants to know how a change performs "for real" — the
whole bot's whitelist over a proper window (days, not hours) with a before/
after comparison — not the quick single-run sanity check in steps 1-4. Built
and run once end-to-end on 2026-08-28 (96 pairs, 30 days, seed vs calibrated
`pair_strategy_params.json`); the steps below are that run generalized.

1. **Pick the pair list and window.** Full whitelist per step 0's new bullet;
   N days per the user. Compute epoch ranges once (`DOWNLOAD_RANGE` = N days +
   ~14 day buffer, `TEST_RANGE` = last N days) and **reuse the exact same
   `TEST_RANGE` for every leg of the comparison** — recomputing "now" between
   legs (unlike step 2's single-run advice) would shift the two runs onto
   different data and invalidate the diff.
2. **Download once for the whole pair list** (step 3's command, all pairs at
   once) — freqtrade only fetches the delta against whatever's already
   cached, so a second N-day run later is cheap.
3. **Write the `StaticPairList` + real trade-cap config once** (step 0's new
   `max_open_trades`/`futures_max_open_trades_short` bullet), reused
   unchanged across every leg — only the thing being A/B'd should vary
   between runs.
4. **Stage variant A, run, export; stage variant B, run, export; diff.** For
   an NFI per-pair calibration comparison specifically: copy the candidate
   `pair_strategy_params.json` into `user_data/` (baseline/seed vs
   calibrated — see `nfi_mcp_server/pair_param_calibration.py` and
   `nfi_deploy/tools/calibrate_local.py` for producing one), run `backtesting`
   with the same `--timerange` both times, and compare the SUMMARY METRICS
   block plus ENTER TAG STATS/EXIT REASON STATS tables side by side. For a
   signal-toggle A/B, use step 5's `nfi_parameters` config approach instead —
   don't conflate the two mechanisms.
5. **Verify the thing being varied actually took effect before trusting the
   diff.** On 2026-08-28 the first attempt at this produced byte-identical
   results for two different `pair_strategy_params.json` files — the strategy
   only polled that file (and `pair_drift_flags.json`) from `bot_loop_start`,
   which **freqtrade never calls during backtesting** (no wall-clock loop to
   drive it). Fixed in `nfi_deploy/NostalgiaForInfinityX7EMA200.py` by also
   loading both caches once from `bot_start` (called in every runmode). If a
   future strategy file regresses this, the tell is the same: identical
   results across legs that should differ. Grep the backtest log for
   `Pair-params cache: resolved overrides for` (or the equivalent log line
   for whatever's being varied) to confirm it actually fired before reading
   any numbers.

## 7. Signal-level behavioral analysis (what did the strategy actually do)

Use when the user wants strategy *behavior* — which signals fired, how often,
DCA/rebuy frequency, what got rejected and why — not just the profit summary
tables from step 4/6.

1. Backtest with `--export signals` instead of `--export trades` (or omit
   `--export`, default is `trades` — signals mode still includes trade
   results, just adds the extra `_signals.pkl`/`_exited.pkl`/`_rejected.pkl`
   payloads inside the same result zip). Skipping this and reusing a
   `trades`-only export fails outright: `backtesting-analysis` errors with
   "File ..._signals.pkl not found in zip".
2. Run the analysis command against the same result directory (no filename
   needed — it reads `.last_result.json` and picks the latest):
   ```bash
   wsl -d Debian -- bash -lc "cd ~/strategy-tester && docker compose run --rm freqtrade backtesting-analysis \
     --config /freqtrade/user_data/config.json \
     --config /freqtrade/user_data/config-backtest.json \
     --backtest-directory /freqtrade/user_data/backtest_results \
     --analysis-groups 1 2 5 \
     --rejected-signals"
   ```
   Group `1` = per enter_reason, `2` = enter_reason x exit_reason, `5` = per
   exit_reason — `0`/`3`/`4` exist too (see `--help`) but 1/2/5 covers "what
   fired, how it resolved, and how it exited" without the pair-level
   explosion of group 4. `--rejected-signals` surfaces entries the strategy
   wanted to take but `confirm_trade_entry` (or the framework's own
   `max_open_trades` cap) blocked — "There were no rejected signals" is a
   real, reportable finding (e.g. the entry rate-limiter/correlated-loss
   guard never actually engaged in this window), not a null result to
   discard.
3. `enter_reason`/`exit_reason` values are NFI's numeric condition-ID tags
   (e.g. `65`, `143`, `144`) plus this strategy's own custom exit reasons
   (`stale_exit_<Nh>`, `catastrophic_exit_<ratio>`, `grey_zone_exit_...`) —
   report the raw tags, don't invent English names for the numeric ones
   without checking `NostalgiaForInfinityX7.py`'s condition definitions first.

## Caveats to always surface

- An N-hour window is for a quick sanity check, not a validated backtest —
  say this plainly rather than let a clean-looking metric imply more
  confidence than the sample supports.
- The pair list used here is a fixed, hand-picked set (see step 0) — it is
  **not** the bot's real dynamic `VolumePairList` top-N, which shifts over
  time and cannot be resolved for backtesting at all (see step 0). Mention
  this if pair selection looks like it matters to the result.
- Never `docker compose up`/`start` the `freqtrade` service itself while
  doing this — it would start `SampleStrategy` live using the same Telegram
  token as the server's main bot and collide (`telegram.error.Conflict`, see
  [[freqtrade-server-deployment]]). Only ever `docker compose run --rm` for
  one-off `download-data`/`backtesting`/`hyperopt` commands, never `run`
  without `--rm`, and never touch the `trade` command.
