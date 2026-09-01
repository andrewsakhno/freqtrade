# NostalgiaForInfinityX7EMA200 — changelog

Logic/config changes to the custom exit-safety layer (`custom_exit`,
`strategy_control.json` defaults) in `NostalgiaForInfinityX7EMA200.py`.
Newest entry first. Each entry: what changed, why, where it was applied
(local repo `nfi_deploy/` vs server `/opt/nfi/`).

---

## 2026-08-31 (latest) — Market-regime gate + per-pair signal audit trail: built, backtested, then fully reverted at user's request

**Update, same day, after the numbers below:** despite the narrow-scope fix
recovering most of the lost performance (see below), the user judged the
whole regime-gate direction not worth pursuing ("значит у меня плохая
стратегия... откатить весь новый рефакторинг") and asked to revert
everything from this refactor — both the market-regime gate AND the
earlier per-pair signal-config/entry-exit audit-trail groundwork
(`pair_signal_config.json`, `entry_signal_key`/`entry_signals_all`/
`exit_signal_key`/`exit_active_*_signals` custom_data fields) it was built
on top of. All of it has been removed from
`nfi_deploy/NostalgiaForInfinityX7EMA200.py`; the file is back to its
2026-08-29 (ENA risk management) state. Kept below for the record — the
backtest methodology and the "narrow the gate's scope" lesson may be useful
if a similar regime-aware idea comes up again.

### Original entry: Market-regime gate for "buying strength" / "catching the bottom", scope narrowed after real backtest data

**What was asked:** classify every candle into a market regime (STRENGTH /
BOTTOM / DECLINE_RANGE / CHOP) and only let the "buy strength" signals
(#7/#900/#65/#120) fire in STRENGTH and the reversal-bounce signals
(#47/#101-104/#41-46) fire in BOTTOM, each with its own exit profile
(Chandelier trailing stop under 1h EMA20 for STRENGTH, fast TP or timeout for
BOTTOM). First version, explicitly requested as "весь бот через шлюз", gated
*every* long entry condition behind the same classification — anything not
STRENGTH/BOTTOM-permitted was blocked outright, including in DECLINE_RANGE/CHOP.

**Backtested 2026-08-31, 75 days / 96 pairs (StaticPairList, see
`nfi_deploy/tools/config-calib-backtest.json`):**

| | baseline (no gate) | whole-bot gate |
|---|---|---|
| Total trades | 53 (23 long / 30 short) | 32 (**2 long** / 30 short) |
| Total profit | +8.42% (+84.19 USDT) | +0.52% (+5.19 USDT) |
| Long-side profit | +81.8 USDT | +2.8 USDT |
| Sharpe (closed trades) | 3.46 | 0.83 |

Root cause: the biggest long-side winners in the baseline run were tags 42
(Quick), 163 (Scalp), 143 (Top Coins), 6 (Normal), 64/63 (Rebuy) — none of
them STRENGTH/BOTTOM signals, all silenced by the whole-bot gate. Meanwhile
STRENGTH/BOTTOM themselves barely fired even where permitted (2 of #65's 3
occurrences, 0 of #47's 1) — each already has a strict formula of its own,
and ANDing a second independent regime condition (ADX/RSI/drawdown/volume) on
top shrinks the intersection to almost nothing.

**Fix:** narrowed `_apply_market_regime_gate`'s scope to only ever touch
`MARKET_REGIME_STRENGTH_SIGNALS`/`MARKET_REGIME_BOTTOM_SIGNALS` tokens —
every other signal fires exactly as before, in every regime. Also fixed a
related correctness issue this surfaced: the EMA200 guard's BOTTOM exemption
was checking `market_regime == "BOTTOM"` (whole-candle), which — once
unrelated signals could survive on a BOTTOM-regime candle again — could wrongly
exempt them from the trend guard too. Replaced with `_is_bottom_regime_long`,
a token-level check mirroring the existing `_is_reversal_long` pattern.

**Re-backtested with the narrow scope, same window/pairs:** 49 trades (19
long / 30 short), +6.25% total (+62.5 USDT), Sharpe 3.92, max drawdown 1.50%
— vs. baseline's 53 trades/+8.42%/Sharpe 3.46/DD 3.56%. Every previously-
silenced family (161/163 Scalp, 63/64 Rebuy, 6 Normal, 143 Top Coins) is back
to baseline-level numbers, confirming the scope fix. The remaining gap vs.
baseline (+60.2 vs +81.8 USDT long-side) is fully attributable to id 42
(Quick mode, but explicitly grouped under `MARKET_REGIME_BOTTOM_SIGNALS`
per the user's own "41-46 = ДНО" spec) not firing under the BOTTOM regime
constraint in this window — its 2 baseline occurrences were the single
biggest winner (+35.9 USDT). Net effect: modestly lower raw profit, higher
Sharpe, and roughly a third less drawdown — a defensible trade-off, not a
correctness problem.

Thresholds (ADX 20 / RSI 35 / drawdown 12%&4% / Chandelier buffer 0.5% / fast
TP 2% / timeout 90min) are still first-pass defaults, none per-pair
calibrated. Applied to `nfi_deploy/NostalgiaForInfinityX7EMA200.py` only, not
yet deployed to the server.

---

## 2026-08-29 — ENA idiosyncratic-crash risk management: tighter catastrophic stop + backtestable unlock-calendar block

**Problem:** the outstanding caveat carried over from the two entries below —
"900" entry's RSI guard converges on ~60 but 1 catastrophic exit (the
2026-08-25 -17.49% ENA "falling knife") still gets through no matter how the
RSI ceiling is tuned, because RSI describes momentum, not tail risk. User
asked for two concrete mitigations: (1) a genuinely tighter
`catastrophic_exit_loss_ratio` for ENA specifically, and (2) a date-gated
entry ban for ENA's known token-unlock dates (Ethena vests Core
Contributor/Investor allocations on a roughly monthly cliff schedule — see
tokenomist.ai/coinglass sources), leaving a general news/sentiment NLP filter
explicitly out of scope (unverifiable by backtest, not requested).

**catastrophic_exit_loss_ratio calibration — TPE, corrected after a
methodology bug:** first sweep (Optuna TPE, [0.05, 0.30], 90d + 180d windows)
came back completely flat — identical profit/trades across the *entire*
range, which would have meant "tightening does nothing." Root cause: ENA
already carried a per-pair `catastrophic_exit_loss_ratio` override
(0.127366, from the 2026-08-28 12:04 `calibrated_local_30d` run) in
`pair_strategy_params.json`, and `_param_for_pair()` always prefers a fresh
per-pair cache entry over the global config-level value the sweep was
setting — so every trial was silently a no-op for this pair. Fixed by
stripping just that one key from ENA's cached entry before rerunning, so the
sweep's config override could actually reach `self.catastrophic_exit_loss_ratio`.
The corrected sweep is a real, monotonic result: **flat-optimal plateau at
ratio ≤ 0.1436** (any value from 0.05 to 0.1436 gives numerically identical
Calmar — 33.91 @ 90d, 25.23 @ 180d — because the crash outpaces the 5m check
granularity below that point), then **degrading smoothly above it** (Calmar
33.9 → 5.0 by ratio 0.29 as the stop is allowed to loosen). Chose **0.1436**
(top of the plateau, not the lowest tested value) to stay clear of the
documented -0.05...-0.10 danger zone where ordinary X7 grind/DCA noise can
trigger false positives on *other* pairs (see the two entries below) — this
value caps the worst historical ENA trade at **-14.54%** vs **-17.49%**
uncalibrated (global 0.20 default).
Written to `nfi_deploy/tools/pair_strategy_params.calibrated.json`
(`source: tpe_optuna_calmar_90d_180d`, `ttl_hours: 168`). **Not yet deployed**
to the server's live `pair_strategy_params.json`.

**pair_blocks made backtestable (code change):** `pair_blocks` was listed in
`_normalize_pair_blocks`'s own docstring as loadable "from strategy_control.json
or a backtest config", but was missing from the `_attr` config-override loop
in `__init__` that makes every other custom knob here config-overridable for
backtest A/B testing — so a `pair_blocks` key in a `--config` JSON was
silently ignored during backtesting even though the runtime
(`_apply_pair_blocks_control`) fully supported it. Added `"pair_blocks"` to
that loop (one line, same pattern as the neighboring `"risk_adjustments"`)
in `NostalgiaForInfinityX7EMA200.py` — no other behavior changed.

**pair_blocks validated against a real historical unlock:** Ethena unlocked
171.88M ENA (~2% of supply) on 2026-08-05. Blocking ENA entries
2026-08-03..2026-08-07 in a 30-day backtest (`config-pairblock-aug5.json`)
removed exactly the 3-trade `stale_exit` cluster that fired around that date
(2026-08-04/06, all small losses) — the 30-day result went from 8 trades/4
losses to 6 trades/1 loss (83.3% win rate), leaving only the untouched,
unrelated 2026-08-25 catastrophic trade as a loss. Cross-checking all 5
originally-identified ENA losses against Ethena's public unlock calendar
(2026-07-02, 2026-08-05, next: 2026-09-01, ~275M ENA / 2.9% of market cap):
**4 of 5 losses cluster within ±2 days of a real unlock date; the single
catastrophic one does not** (nearest unlock was 20 days prior) — confirming
the RSI/catastrophic-ratio fixes above and the unlock-calendar block are
complementary, not redundant: the calendar catches the news-driven bleed,
the tightened stop caps the pure idiosyncratic tail event.
**No pair_blocks entry deployed** (this was a historical-validation-only
run) — seeding the real upcoming 2026-09-01 unlock into
`strategy_control.json` is a separate, not-yet-taken step pending user
confirmation before touching the live/dry-run bot's control file.

**Final combined backtest** (RSI=60 guard + catastrophic_exit_loss_ratio=0.1436,
ENA/USDT:USDT only, "900" isolated, `pair_strategy_params.json` cache active
for all 96 whitelist pairs though only ENA has open positions in this
single-pair test):

| Window | Trades | Win% | Total profit | Max DD | Worst trade |
|---|---|---|---|---|---|
| 30d  | 11 | 63.6% (7W/4L)  | +2.34% | 2.52% | -14.54% (capped) |
| 90d  | 17 | 58.8% (10W/7L) | +4.01% | 2.54% | -14.54% (capped) |
| 180d | 31 | 61.3% (19W/12L)| +6.01% | 2.54% | -14.54% (capped) |

Max drawdown down from 3.03% (RSI-only, default 0.20 ratio) to ~2.53% across
all three windows — the tightened stop is the only thing that moved this
number; trade count/profit are essentially unchanged since it only clips one
tail event per window, it doesn't touch normal grind trades.

## 2026-08-28 — TPE-calibrated momentum_entry_rsi_overbought for ENA

**Follow-up to the RSI guard below.** A manual grid sweep (60/65/70/75/80/85/90,
isolated "900"-only, ENA/USDT:USDT, 3-month + 3-week windows) found the
classic 70 default is dominated: it carries the same catastrophic-exit count
as no guard at all (2-3 hits, same magnitude) while cutting profit versus a
looser threshold, and 65 specifically is a local dip (worse profit than 70
at equal risk). Only 60 measurably cut catastrophic exits (3→1 / 2→1), so
user asked for a proper search: Optuna TPE sampler, continuous range
[40, 95], 25 trials each over independent 30-day and 60-day windows,
objective = Calmar ratio (profit_total / max_drawdown_account) so the
search is risk-adjusted rather than raw-profit-seeking (raw profit
monotonically favors "no guard", which is not what a guard is for).

**Result:** both windows converge independently on the same narrow optimum,
**RSI_14_1h ceiling ≈ 59-61** (30d best plateau 58.2-61.7, 60d best
58.2-60.6 — all giving numerically identical backtest stats within each
plateau, i.e. no ENA candle's RSI falls inside those sub-bands). Below ~55
the search craters (1 trade / -2 to -3% / negative Calmar — too strict,
starves the signal); 63-92 forms a second, worse plateau (progressively more
catastrophic exposure as the ceiling loosens, Calmar falling from ~18 to
~9 while raw profit keeps rising — confirms profit and risk pull in
opposite directions here, hence choosing Calmar as the objective mattered).
This independently reproduces the grid sweep's "60 is the standout" finding
via a different search method on different window lengths — treated as
confirmed, not a fluke of one particular timerange.

**Change:** wrote `momentum_entry_rsi_overbought = 60.0` for
`ENA/USDT:USDT` into `nfi_deploy/tools/pair_strategy_params.calibrated.json`
(`source: tpe_optuna_calmar_30d_60d`, `ttl_hours: 168` — same 1-week
staleness convention as every other calibrated knob, auto-falls back to the
global 70.0 default if not refreshed). Global class default in
`NostalgiaForInfinityX7EMA200.py` left at 70.0 — this was a single-pair
calibration, not evidence about the other ~95 whitelist pairs.
**Not yet deployed** to the server's live `pair_strategy_params.json` — this
only updates the local calibrated snapshot.

**Caveat carried over from the RSI-guard entry below still applies**: even
at the optimal ~60 ceiling, 1 catastrophic exit still gets through in every
window tested — RSI alone does not fully prevent this class of loss (the
2026-08-28 ENA incident itself entered at RSI 56, under even this tighter
threshold). Tooling: `nfi_deploy/../tpe_optimize_rsi.py`-equivalent script
run ad hoc in the WSL strategy-tester scratch dir (Optuna 4.9, TPESampler,
`n_startup_trials=8`, seed=42) — not yet promoted into a reusable
`nfi_mcp_server` tool the way `compute_green_streak_n`/`calibrate_pair_params`
are; follow-up if this pattern gets reused for other pairs/knobs.

## 2026-08-28 (even later same day) — RSI overbought guard for momentum entry

**Problem:** live "900" entry on ENA/USDT:USDT (2026-08-28 16:01) went to
~-15% (leveraged) within hours. Investigation found RSI_14_1h had spiked to
81.7 on the same pair at 01:00 that day before reversing hard — a genuine
blow-off-top pattern "900" has no defense against. User asked for a classic
RSI-overbought guard (TA-Lib), reusing per-pair calibration (like
`falling_knife_drop_pct`) instead of one hand-picked global constant, refit
periodically like `green_streak_default_n`.

**Caveat found during investigation, before implementing:** RSI_14_1h at the
ENA trade's *actual* entry candle (15:00, 2026-08-28) was only 56.0 — the
70 ceiling would NOT have blocked that specific loss. That trade looks more
like "bought a weak bounce inside a wider intraday downtrend" than "bought
an overbought spike". This guard is still worth having (it would have
blocked a "900" entry on the 01:00 spike candle had one fired there), but it
addresses a different, narrower failure mode than the one actually observed
— flagged here so it isn't mistaken for a fix to the ENA incident itself.

**Change:**
- Strategy (`NostalgiaForInfinityX7EMA200.py`): `_populate_momentum_entry`
  now additionally requires `RSI_14_1h < momentum_entry_rsi_overbought`
  (reuses X7's own `RSI_14_1h` column — no new TA-Lib call) when
  `momentum_entry_rsi_guard_enabled` (default on). `momentum_entry_rsi_overbought`
  defaults to 70.0 (classic RSI overbought line) and is registered in
  `PAIR_PARAM_SPECS`, so it resolves via `_param_for_pair` exactly like
  `falling_knife_drop_pct`: a fresh per-pair entry in
  `pair_strategy_params.json` overrides it, a stale/missing one falls back
  to the global default. Both knobs hot-reloadable via
  `strategy_control.json`.
- No per-pair calibration job for this knob exists yet — every pair
  currently runs the global 70.0 default until one is written (see
  `nfi_mcp_server`'s `compute_green_streak_n`/`calibrate_pair_params` for the
  pattern to follow: backtest sweep over a rolling lookback window, write
  `{value, computed_at, ttl_hours}` per pair). Tracked as follow-up, not
  done in this change.
- Status: local repo only (`nfi_deploy/`), not yet deployed to
  `/opt/nfi/user_data/strategies/` on the server.

## 2026-08-28 (later same day) — Green-streak gate for momentum entry, MCP-calibrated

**Problem:** the momentum entry ("900", above) fires on every candle satisfying
its ADX/DI/EMA200 condition, which for a long-running trend means every bar,
not just "strength" in any meaningful sense. User asked to gate it on "the
Nth consecutive green candle" instead, with N discovered per-pair from
history rather than guessed, refreshed periodically (stale results — "мусор"
— past 1 week should fall back to a default) and controllable via MCP.

**Change:**
- Strategy (`NostalgiaForInfinityX7EMA200.py`): `_merge_green_streak_1h`
  merges a `green_streak_1h` column (consecutive-green-1h-candle counter,
  via `self.dp.get_pair_dataframe(pair, "1h")` + a groupby run-length trick)
  into the dataframe when `momentum_entry_enabled`. `_populate_momentum_entry`
  now additionally requires `green_streak_1h == N` (exact match, not `>=` —
  fires once per streak, not on every later candle of a longer one). N comes
  from `_green_streak_n_by_pair` (resolved each `bot_loop_start` by
  `_apply_green_streak_cache` from `green_streak_cache.json`, TTL-checked
  against `green_streak_ttl_hours`) or `green_streak_default_n` (4) when no
  fresh entry exists for that pair. Both knobs hot-reloadable via
  `strategy_control.json`, same pattern as everything else in this file.
- MCP (`nfi_mcp_server/green_streak.py`, new file; `server.py`): pure-Python
  (no pandas — this package deliberately avoids that dependency) analysis —
  fetch 1h klines from Binance's public futures REST (no key), bucket by
  streak length, pick the N with the highest 24h-forward-return expectancy
  (`win_rate*avg_gain - loss_rate*avg_loss`) among buckets with >=20 historical
  samples. New tools `compute_green_streak_n(pair, lookback_days=90, reason)`
  (writes the cache, mirrors `set_risk_adjustment`'s style) and
  `get_green_streak_n(pair)` (read-only, reports cache age/staleness).
  `Dockerfile`'s `COPY` line updated to include the new module — this
  container bakes its source into the image (`build: .`), no bind mount, so
  a rebuild is required after any `.py` change (already documented in
  `nfi_mcp_server/README.md`).

**`green_streak_default_n = 4`:** backtested 2026-08-28 across 8 pairs
(BTC/ETH/SOL/XRP/DOGE/ADA/LINK/AVAX, 90 days, 1h candles, same
expectancy metric as the live calibration) — best N per pair was
`{2,3,3,3,5,5,5,5}`, median 4. Caveat: N=6+ never reached the 20-sample
floor in 90 days for any tested pair, so the true per-pair optimum may sit
above what this backtest could actually measure — treat 4 as a reasonable
starting point, not a settled optimum; that's what the per-pair MCP
calibration is for. AVAX had negative expectancy at every N from 1-5 — a
pair like that should ideally not fire "900" at all, which this mechanism
doesn't yet express (a future refinement: skip momentum entry entirely below
some minimum expectancy, rather than always falling back to a positive N).

**Deployed 2026-08-28:** strategy file backed up
(`.bak-20260828-pre-greenstreak`), pushed, `docker compose restart freqtrade`
in `/opt/nfi` — clean restart, 0 crashes, strategy resolved OK. `/opt/nfi-mcp`
backed up wholesale (`.bak-20260828-pre-greenstreak`), new files installed,
`docker compose build && up -d` — clean startup. Verified end-to-end inside
the live `nfi-mcp` container: `green_streak.compute_and_cache("SOL/USDT:USDT")`
reproduced the exact same numbers as the standalone backtest (best N=2,
expectancy 0.4122%), wrote `green_streak_cache.json` (world-readable,
`/opt/nfi/user_data/`, so the bot's uid-1000 process can read what the
root-owned MCP container writes — same pattern as `strategy_control.json`).

**Not yet done:** only SOL has been analyzed so far (the smoke test above) —
every other pair is still on the N=4 default until `compute_green_streak_n`
is called for it. No scheduled/automatic refresh exists; re-calibration is a
manual MCP call per pair, same as `set_risk_adjustment`.

---

## 2026-08-28 — Momentum entry ("buy strength"), tag "900"

**Problem:** live SOL rally 2026-08-27 ~08:00-23:00 (+28%, low-pullback) fired
none of X7's enabled long signals, even though the bot had an open slot the
whole time. Investigated by replaying real Binance data through each
enabled condition's formula: #7 (ADX trend-birth) had 5 of its 6 gates true
for the entire session; the reversal family (#10/#11/#170/#192/#193)
structurally cannot fire on a low-pullback rally (they require RSI/CCI/
Williams%R oversold readings); #9 (momentum continuation) mostly failed once
`rsi_14_1h` pushed past its 70 ceiling as the rally accelerated.

**Root cause:** #7's one failing gate — `np_shift(adx_14_4h, 48) <= 20.0`
(ADX must have been ≤20 exactly 8 days ago) — exists to reject a trend that
was already mature, not newly born. SOL's uptrend predated the visible
8-day window, so #7 rejected it purely on "how long has this been going",
independent of trend strength or direction quality.

**Change:** new long signal, tag `"900"`, added in `populate_entry_trend`
via `_populate_momentum_entry` — condition #7's exact formula (4h ADX > 20,
+DI > -DI, close > EMA_200, `protections_long_global`, data-completeness)
with the freshness gate removed. Still subject to the `EMA_200_1h` guard
above (not added to `REVERSAL_LONG_SIGNALS`) and to `protections_long_global`
(X7's own anti-pump/dump filter is untouched — this only removes "must not
already be trending", not the blow-off-top protection). Toggle:
`momentum_entry_enabled` (class default `False`, hot-reloadable via
`strategy_control.json`). Tracked under
`correlated_loss_guard_long_signals = {"7", "900"}` so it inherits #7's
correlated-loss guard and entry-rate-limit coverage, since it shares the
identical trend-following mechanism and #7 already showed a real
correlated-cluster failure mode (2026-06-15, 6 pairs same day).

**Applied to:** local repo AND server (`/opt/nfi/NostalgiaForInfinityX7EMA200.py`,
`/opt/nfi/user_data/strategy_control.json` — `momentum_entry_enabled: true`).
Deployed live 2026-08-28 (dry-run), `docker compose restart freqtrade`,
container came up clean (0 restarts, no traceback). Not yet backtested —
enabled directly at the user's request to catch momentum moves going
forward, not validated against historical loss/win rate first. Watch pair
stats before trusting tag "900" the way #7/#8/#9 are trusted.

---

## 2026-08-27 — Grey-zone exit measured from trade's own peak profit, not zero

**Problem:** `grey_zone_exit` (below) only ever compared `total_profit_ratio`
against zero, so it structurally could not see a trade that reached real
profit (e.g. +5%) and is now decaying back toward breakeven/loss — it stays
completely unguarded by the age-based tightening curve until it actually
crosses into negative territory, at which point most of the aging "head
start" the curve was supposed to give it is already gone.

**Root cause:** loss-from-zero and giveback-from-peak look like two
different problems ("losing trade keeps sliding" vs "winning trade giving it
back") but are the same signal measured from different reference points.
Building a second parallel decay mechanism for the peak case would mean
tuning two curves against overlapping signals — for a trade that never went
positive, "peak" ≈ 0, so a standalone peak-decay guard would nearly
duplicate `grey_zone_exit` on that population of trades.

**Change:** `custom_exit` now tracks each trade's highest-ever
`total_profit_ratio` via `trade.set_custom_data` (persisted across restarts,
key `grey_zone_peak_total_profit_ratio`, floor-clamped at 0.0), and
`grey_zone_exit` compares `(peak - total_profit_ratio) >= threshold` instead
of `-total_profit_ratio >= threshold`. The floor clamp means a trade that
never went positive gets `peak == 0`, so its behavior is bit-for-bit
identical to before this change — only trades that actually reached positive
profit get new coverage. `catastrophic_exit` is intentionally left comparing
against zero (a hard capital-protection floor, not a relative-giveback
guard).

**Applied to:** local repo only (`nfi_deploy/NostalgiaForInfinityX7EMA200.py`
— `custom_exit`, new `_update_peak_profit`, module docstring). Inherits
`grey_zone_exit_enabled = False` default from the entry below, so this is
inert until that's turned on.

**Backtest confirmation (8-pair whitelist, stale_exit_hours=18, same setup as
the base grey-zone entry below):**
- Off-state, both a 2-month window (2026-06-28..2026-08-27) and the rally
  sub-window (2026-07-28..2026-08-27): results are byte-for-byte identical to
  the pre-change code (rally: 109 trades, +1042.90 USDT, +104.29%, 14.51%
  drawdown — exact match) — provably inert when off, confirmed on real data
  not just by code inspection.
- On-state (defaults, exponent=5.0/sensitivity=3.0): of all `grey_zone_exit`
  closes, **50% carried a positive peak** (`_pk` tag) in both windows — 7/14
  over 2 months, 3/6 in the rally month (DOGE/ADA/AVAX/XRP/ETH, peaks
  +0.4%..+4.8%) — direct evidence the "profit tapering, not yet negative
  from zero" case is real and roughly as common as the plain-loss case this
  feature already covered.
- Aggregate P&L barely moved vs. the pre-change on-state on the same rally
  window (same 118-trade count both ways): +100.19% → +99.83% profit,
  15.69% → 15.72% drawdown — a small, expected perturbation from the handful
  of peak-tagged trades closing at slightly different prices/times, not a
  regression.
- 2-month window on-state remains strongly positive overall (+107.68%
  profit, 25.03% drawdown vs +74.51%/33.72% off) — consistent with the base
  grey-zone feature's validated behavior; this change did not disturb it.

**Not yet deployed** — same gating as the base grey-zone feature: pending
`grey_zone_exit_enabled` being flipped on, which is its own separate
decision.

---

## 2026-08-27 — Grey-zone time-decay exit + bounded LLM risk override

**Problem:** analysis of a 4-catastrophic_exit validation-window backtest
(-6.38% total, 4 trades at avg -20.2% each) found all 4 were single-order
trades (X7's own DCA/grind ladder never fired to rescue them) that sat for
hours-to-days with **no time-based exit at all** before hitting the -20%
cliff. `custom_exit` only guarded two bands — `stale_exit` (near-flat,
`[-1.5%, +1%)`) and `catastrophic_exit` (a hard -20% cliff) — leaving
everything strictly between them (a real, non-flat loss, but not yet a
catastrophe) with zero time-based protection.

**Root cause:** by design, not a bug — `stale_exit` was deliberately scoped
to flat/dead trades only, and `catastrophic_exit` to genuine disasters, so
the space between them was simply never covered.

**Calibration:** a rolling-peak-from-trailing-high drawdown study across all
8 whitelisted pairs (~2 months of 5m futures candles, thresholds converted
from leveraged P&L to raw price via `futures_mode_leverage = 3.0`) found the
gap is real and pair-dependent: at a 72h horizon a grey-zone drawdown
cascades through to -20% only 4.9% of the time for BTC but 30.4% of the time
for ADA, and the weighted-average cascade rate itself rises with elapsed
time (0.8% @6h, 8.4% @24h, 20.7% @72h) — hence a convex decay curve.

**Change:** added `grey_zone_exit_*` — a per-pair calibrated threshold that
starts wide (= `catastrophic_exit_loss_ratio`, today's behavior, so young
losing trades are untouched and X7's DCA ladder gets its normal chance) and
narrows toward `stale_exit_max_loss` as the trade ages toward a pair-specific
horizon `H_pair` (24h-168h clamp). Also added `risk_adjustments` — a
bounded, self-expiring operator override (human or LLM, via planned nfi-mcp
tools) that can temporarily scale the cascade-rate assumption per pair, with
a hard `expires_at`; `custom_exit` compares its own `current_time` against
it on every call, so it reverts to baseline automatically with no cleanup
job. Defaults: `grey_zone_exit_enabled = False` (opt-in), `curve_exponent =
5.0`, `pair_sensitivity = 3.0` (backtest-chosen, see below).

**Backtest confirmation (two windows, same 8-pair/#8+#9/stale_exit_hours=18
baseline used throughout this project):**
- A/B control: `grey_zone_exit_enabled: false` reproduced the exact baseline
  (64 trades, -63.84 USDT, -6.38%, 26.70% drawdown) — the code insertion is
  provably inert when off.
- Grid search (`curve_exponent` x `pair_sensitivity`) on the validation
  window found a broad, stable plateau (not a fragile single-cell optimum)
  around `exponent≈5-8, sensitivity≈2.5-4`, peaking at **exponent=5.0,
  sensitivity=3.0**: 65 trades, **+63.74 USDT (+6.37%)**, drawdown
  **16.93%** (vs -63.84 USDT/-6.38%/26.70% off) — a ~13pp profit swing and
  ~10pp drawdown cut. `catastrophic_exit` trades: **4 → 0**; all 4 were
  instead caught by `grey_zone_exit` at roughly half the severity (avg
  ≈-10.2% vs -20.2%), plus 2 marginal trades swept in cheaply.
- Rally window (same config, `exponent=5.0/sensitivity=3.0`): 109→118
  trades, +104.29% → +100.19% profit, drawdown unchanged at 14.51% — a small
  cost in a strong trending market (a few would-be-recovering grey-zone dips
  get cut early) with no drawdown penalty, which is the correct trade-off
  profile for a downside-protection feature: cheap insurance in good times,
  large payout in bad times.
- Confirmed the class defaults (`exponent=5.0`, `sensitivity=3.0`) wired
  correctly: a config with only `grey_zone_exit_enabled: true` and no other
  override reproduced the grid-search winner exactly.

**Applied to:** local repo only (`nfi_deploy/NostalgiaForInfinityX7EMA200.py`
class constants/attributes, `custom_exit`, `__init__` config-override tuple,
`_apply_grey_zone_exit_control`/`_apply_risk_adjustments_control`, module
docstring). `risk_adjustments` strategy-side logic (`_normalize_risk_adjustments`,
`_active_risk_multiplier`) also backtest-validated for its multiplier/expiry
math via the config-override tuple. **NOT yet deployed to `/opt/nfi/`** —
`grey_zone_exit_enabled` stays `False` in `strategy_control.json` pending the
nfi-mcp `request_risk_analysis`/`set_risk_adjustment` tooling (separate work
item) and a dry-run observation period per the project's established
gate-then-deploy practice.

**Files touched:** `NostalgiaForInfinityX7EMA200.py` (calibration constants,
tunable attributes, `_grey_zone_full_hours`/`_grey_zone_threshold`/
`_active_risk_multiplier`/`_normalize_risk_adjustments`, `custom_exit`,
`__init__`, `_apply_grey_zone_exit_control`, `_apply_risk_adjustments_control`,
module docstring).

---

## 2026-08-27 — catastrophic_exit: 0.15 still too tight, raised to 0.20

**Problem:** backtested the 0.15 + 2h-gate fix (previous entry below) over a
real 7-day/19-pair window using the `strategy-tester` skill and it performed
*worse* than the still-deployed buggy 0.05 on that window (-6.24% vs -3.48%
total), driven by one XRP trade that bled to -17.79% before the looser
breaker finally cut it.

**Root cause:** `NostalgiaForInfinityX7.py`'s own
`grinding_v2_derisk_level_1_futures = [-0.18, -0.35]` requires -18% leveraged
`profit_ratio` before de-risk fires, and `grind_1`/`grind_2`/`grind_3` (the
main DCA rungs) are gated behind `is_derisk_1_found`/`2`/`3` — only the much
shallower fallback `grind_4` can fire without a prior de-risk. At 3x
leverage, -18% leveraged = -6% raw price move, while
`catastrophic_exit_loss_ratio = 0.15` at the same leverage = only -5% raw.
Since 0.15 < the de-risk threshold, `catastrophic_exit` on a futures trade
will essentially always fire before de-risk_level_1 can even unlock
`grind_1-3` — the breaker still cuts the trade before the risk-management
ladder it was meant to give room to ever turns on. Confirmed against the
actual XRP trade's orders: only `grind_4_entry` fired, on the very same
candle as the exit, contributing ~5% of position size — no real averaging
happened before the stop.

**Change:** `catastrophic_exit_loss_ratio`: `0.15` → `0.20` (above the -0.18
futures de-risk threshold, so de-risk gets a chance to fire — and therefore
unlock `grind_1-3` — before the circuit breaker does). `catastrophic_exit_min_hours`
unchanged at 2.0.

**Applied to:** local repo only so far (`nfi_deploy/NostalgiaForInfinityX7EMA200.py`
class default + docstring example + control-file fallback default,
`nfi_deploy/strategy_control.json`). NOT yet deployed to `/opt/nfi/` on the
server.

**Confirmed by re-running the same 7-day/19-pair backtest with 0.20:** the
XRP trade no longer hit `catastrophic_exit` at all — it rode out the same
dip and closed via X7's own normal `exit_long_tc_d_0_42` signal, **in
profit** (+1.33%, +4.60 USDT) after 4h15m, instead of being cut at -17.79%.
DOGE unchanged (-0.23% via `stale_exit_6h`). Total window result: **+0.38%
(+3.83 USDT)**, vs -6.24% at 0.15 and -3.48% at the currently-deployed 0.05.
Same 2-trade/7-day sample-size caveat applies, but this is a mechanism-level
confirmation, not just a different random outcome — the trade took a
qualitatively different, non-breaker exit path once the threshold cleared
the de-risk gate.

**Files touched:** `NostalgiaForInfinityX7EMA200.py` (class default, docstring,
control-file fallback default in `_apply_catastrophic_exit_control`),
`strategy_control.json`.

---

## 2026-08-27 — catastrophic_exit: raised threshold + added min-hours gate

**Problem:** `catastrophic_exit_loss_ratio = 0.05` (deployed on the server,
synced to local earlier the same day) fired unconditionally on every
`custom_exit` call from the moment a trade opened, with no age gate. X7's own
grind/DCA position adjustment is designed to average through normal noise in
roughly the -5%...-10% range before recovering. A -5% hard floor with no time
buffer sat inside that normal grind range, so the breaker routinely fired
before grind entries got a chance to fill and average price down — instead of
protecting against tail risk, it became a de facto tight stop-loss. Result:
11 consecutive premature stop-outs (-970 USDT).

**Root cause:** two compounding design gaps —
1. Threshold (0.05) tighter than NFI's own grind depth.
2. No minimum trade age before the breaker could fire (unlike `stale_exit`,
   which already waited `stale_exit_hours`).

**Change:**
- `catastrophic_exit_loss_ratio`: `0.05` → `0.15` (back to the ratio already
  used as the control-file fallback default in code — deep enough to sit
  below the -36.6% blowup this breaker exists to prevent, but above X7's
  normal -5%...-10% grind noise).
- Added `catastrophic_exit_min_hours = 2.0`: the breaker only evaluates once
  `trade_hours >= catastrophic_exit_min_hours`, giving the grind ladder time
  to act before the safety net can preempt it. Hot-reloadable via
  `strategy_control.json` like the other controls.

**Applied to:** local repo only so far (`nfi_deploy/NostalgiaForInfinityX7EMA200.py`,
`nfi_deploy/strategy_control.json`). NOT yet deployed to
`/opt/nfi/` on the server — pending explicit go-ahead (deploying requires a
`docker compose restart freqtrade` for the `.py` change to take effect; the
`strategy_control.json` half would hot-reload without a restart).

**Files touched:** `NostalgiaForInfinityX7EMA200.py` (class defaults, docstring,
`custom_exit`, `_apply_catastrophic_exit_control`), `strategy_control.json`.

---

## 2026-08-27 — stale_exit: missing `abs()` fixed, catastrophic_exit_loss_ratio synced from server

**Problem:** `custom_exit`'s stale-exit check compared
`total_profit_ratio < self.stale_exit_profit_band` without `abs()`, so after
`stale_exit_hours` (6h) it matched *any* loss below +1%, not just a
near-flat position. Combined with grind/de-risk position sizing (a de-risk
sell + re-grind buy-back can leave the *current* remainder looking flat while
lifetime realized P&L is deeply negative), this let a trade ride a drawdown
for the full 6h window with nothing catching it, then force-close at market
at the worst point. Actual incident: -36.6% / -1168 USDT after ~6h on a
meme-token pair.

**Change:**
- Added `abs()`: `if abs(total_profit_ratio) < self.stale_exit_profit_band:`.
  Local repo already had this; server did not.
- Deployed the `abs()` fix to `/opt/nfi/NostalgiaForInfinityX7EMA200.py` on
  the server (backup: `.bak-preabsfix-20260826225135`), `docker compose
  restart freqtrade` to reload. Verified live post-restart.
- Separately, synced `catastrophic_exit_loss_ratio` local repo value
  `0.15` → `0.05` to match what was already live-tuned on the server
  (server had independently been set tighter than local). This 0.05 value
  is the one reverted in the entry above, after it turned out to be too
  tight given X7's grind depth.
- Also synced `strategy_control.json`'s `long_signals_override` to include
  `"65": false` (already live on server, missing locally).

**Applied to:** both local repo and server (the `abs()` fix and the
signal-override sync were deployed; the ratio sync was local-repo-only,
matching the server's already-live value).

**Files touched:** `NostalgiaForInfinityX7EMA200.py`, `strategy_control.json`
(both local and server copies).
