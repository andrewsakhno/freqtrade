"""
Macro trend filter + runtime control wrapper around NostalgiaForInfinityX7.

The upstream NostalgiaForInfinityX7.py file is overwritten daily by the
nfi-updater sidecar, so all customizations live in this subclass instead of
the base file (Open/Closed Principle: extend the strategy, do not modify it).

Features on top of bare X7:
- EMA200 macro trend guard on X7's own 1h informative timeframe (long entries
  only above EMA200_1h, short entries only below it), toggleable at runtime.
  Reuses the EMA_200_1h column X7 already computes for its own signals
  instead of recomputing a 5m-only EMA200 (~16.6h window, too short to be a
  macro filter).
- Stale-trade exit: closes trades that have sat in a narrow band around
  breakeven for too long instead of tying up stake indefinitely, applied only
  after all of X7's own custom_exit logic has had a chance to fire. The band
  is two-sided (`-stale_exit_max_loss <= total_profit_ratio <
  stale_exit_profit_band`) so it only ever closes genuinely flat/dead trades —
  a trade sitting at a real loss beyond `stale_exit_max_loss` is left to X7's
  own DCA/grind ladder or to the catastrophic breaker below, never swept up by
  the stale-trade timer regardless of how long it's been open.
- EMA200 macro guard exemption for reversal signals: the guard blocks entries
  against the 1h trend for trend-following signals, but the whole point of
  the reversal-family longs (#10/#11/#170/#192/#193 — CCI/MFI divergence,
  crash-bounce, quad-rotation pivots) is to buy oversold *against* the
  prevailing 1h trend. Applying the same trend filter to them would block
  every one of their entries by construction, so they're exempted from the
  EMA_200_1h check (their own indicator conditions in the base file already
  gate them).
- Momentum entry ("buy strength"): a new long signal, tag "900", toggleable at
  runtime via `momentum_entry_enabled` (default OFF). It is condition #7
  (4h ADX > 20, +DI > -DI, close > EMA_200, `protections_long_global`) with
  the "trend must be freshly born" gate removed — #7 additionally requires
  ADX to have been <= 20 exactly 48 4h-candles (8 days) ago, which by
  construction rejects an already-mature, still-strong trend. Added
  2026-08-28 after a live SOL rally (2026-08-27, ~08:00-23:00, +28%) fired
  none of X7's enabled long signals: #7 had 5 of 6 gates true the entire
  session, blocked only by that one freshness check; the reversal family
  (#10/#11/#170/#192/#193) structurally cannot fire on a low-pullback rally
  (they gate on RSI/CCI/Williams%R oversold readings); #9 (momentum
  continuation) mostly failed on rsi_14_1h's 70 ceiling once the rally
  accelerated. Momentum entry is still subject to the EMA_200_1h guard above
  (it is trend-following, not a reversal signal) and to
  `protections_long_global` (X7's own anti-pump/dump filter is left
  in place — this only removes the "must not already be trending" gate, not
  X7's blow-off-top protection). Tracked under the same
  `correlated_loss_guard_long_signals` family as #7 (see below), since it
  shares #7's exact correlated-cluster mechanism, just with a wider trigger
  window.
- Momentum entry green-streak gate: further restricts "900" to fire only on
  a pair's Nth consecutive green 1h candle (`green_streak_1h == N`), instead
  of any candle satisfying the ADX/DI/EMA200 condition. N is per-pair
  (`green_streak_cache.json`, written by an external analysis job — see
  `nfi_mcp_server`'s `compute_green_streak_n` tool — keyed by pair, each
  entry `{"n", "computed_at", "ttl_hours"}`) with `green_streak_default_n`
  as fallback when no cache entry exists or it's older than
  `green_streak_ttl_hours` (default 168h/1 week — an analysis older than
  that is treated as stale ("мусор") rather than trusted). Both knobs are
  also hot-reloadable via `strategy_control.json`. `green_streak_default_n`
  (currently 4) comes from a 2026-08-28 backtest: for each candidate N,
  24h-forward-return expectancy (`win_rate*avg_gain - loss_rate*avg_loss`)
  over 90 days of 1h candles across 8 representative pairs (BTC/ETH/SOL/XRP/
  DOGE/ADA/LINK/AVAX); best N per pair was {2,3,3,3,5,5,5,5}, median 4. Only
  N<=5 could be measured with >=20 historical samples in 90 days, so this is
  a starting default, not a settled per-pair optimum — the whole point of
  the cache is to let a live analysis job supersede it per pair over time.
- Momentum entry overbought guard: blocks "900" once RSI_14_1h (already
  computed by X7 for its own signals) reaches `momentum_entry_rsi_overbought`
  (default 70.0, classic RSI overbought line), toggleable via
  `momentum_entry_rsi_guard_enabled`. Global default only until calibrated;
  per-pair overridable via `pair_strategy_params.json` exactly like
  `falling_knife_drop_pct` (see PAIR_PARAM_SPECS/_param_for_pair and the
  per-pair calibration bullet below), so a periodic backtest sweep can refit
  it per pair from a rolling lookback window instead of every pair sharing
  one hand-picked constant. Added 2026-08-28 after an ENA "900" loss;
  caveat from that same incident: RSI_14_1h was only 56.0 at the actual
  entry candle, so a 70 ceiling would not have blocked that specific trade
  — it guards against a different failure mode (buying an already-overbought
  spike, e.g. the RSI_14_1h 81.7 print on 2026-08-28 01:00 for the same
  pair), not "weak bounce inside a wider downtrend", which is what actually
  happened.
- Per-pair calibration of exit/guard knobs: all 23 numeric exit/guard
  parameters (stale_exit_*, catastrophic_exit_*,
  correlated_loss_guard_loss_threshold, correlated_loss_guard_min_losing,
  entry_rate_limit_window_hours, entry_rate_limit_max_entries,
  signal_666_volume_spike_mult, signal_666_min_funding_rate,
  funding_settlement_buffer_minutes, grey_zone_exit_* including
  grey_zone_exit_pair_sensitivity, falling_knife_lookback_candles,
  falling_knife_drop_pct, momentum_entry_rsi_overbought — see
  PAIR_PARAM_SPECS for the exact list)
  can be overridden per pair via `pair_strategy_params.json` (analogous to
  green_streak_cache.json above — external analysis job writes it,
  `_apply_pair_params_cache` polls it every bot_loop_start, `_param_for_pair`
  resolves pair override -> plain self.X value). The three family/basket-wide
  guards (entry_rate_limit_*, correlated_loss_guard_min_losing) are keyed by
  the CANDIDATE pair being evaluated, not a per-pair count — see
  PAIR_PARAM_SPECS comment. Seeded 2026-08-28 with every whitelist pair set
  to the then-current global default for each knob (i.e. no behavior change
  on deploy); intended to be superseded pair-by-pair as a 90-day-backtest
  calibration job (see nfi_mcp_server's pair_param_calibration.py) writes
  real per-pair values over time.
- Per-pair money-allocation weight (`money_weight`, same PAIR_PARAM_SPECS/
  pair_strategy_params.json/_param_for_pair mechanism as the knobs above, so
  no new cache file or polling code): `custom_stake_amount` multiplies
  whatever X7's own rebuy/grind/rapid-mode sizing proposes by this weight.
  Written by nfi_mcp_server's `pair_priority.py` / `calibrate_pair_priority`
  tool, which buckets a pair's trade history (two lookback windows, e.g.
  90d/180d live or 3mo/6mo from a backtest) into consistent_winner (1.0,
  today's default), consistent_loser (0.5), volatile_profitable (0.1 — has
  losses but net profitable, e.g. BTW/LAB in the 2026-08-29 per-pair
  backtest report; a raw "has any loss" flag would have wrongly starved
  these of stake), no_win (0.0 — a weight of 0 makes custom_stake_amount
  return 0, which freqtradebot/wallets.validate_stake_amount treats as
  "skip this entry", the standard way to veto a trade via stake sizing) or
  insufficient_data (1.0, unchanged until judged). consistent_loser/
  volatile_profitable are for calibration refinement, not blacklisting.
  Runtime-toggleable via strategy_control.json (`money_weight_enabled`).
- Concept-drift fail-safe (adaptive-control / self-tuning pattern on top of
  the calibration above): pair_param_calibration.py stores a profile
  snapshot alongside every calibration and can later re-measure a short
  recent window and compare it back (detect_pair_drift /
  auto_flag_if_drifted MCP tools) — there is no scheduler inside that
  package (same "caller decides, no autonomous polling" philosophy as
  request_risk_analysis), so a human or an LLM agent decides how often to
  call it. A pair whose deviation crosses the threshold gets an entry in
  pair_drift_flags.json, hot-reloaded here every bot_loop_start
  (_apply_drift_flags). An active flag has two effects: (1) _param_for_pair
  unconditionally ignores that pair's pair_strategy_params.json overrides,
  falling back to plain global defaults — automatic, not gated by any
  toggle, because a calibration nobody currently trusts should not keep
  steering exits; (2) if drift_block_entries_enabled (default True,
  hot-reloadable) is on, confirm_trade_entry blocks new entries on the pair
  outright until the flag clears or self-expires (ttl_hours, default 72h) —
  the "fail on buy" half of the pattern. Existing open trades are never
  touched by a flag, same entries-only principle as every other guard here.
  For a case where automatic fail-safe isn't the right call (the deviation
  looks like a genuine regime shift worth reasoning about, not noise),
  request_drift_analysis assembles the same evidence into a prompt for an
  external LLM caller to judge, mirroring request_risk_analysis exactly.
- Falling-knife entry guard: blocks LONG entries (all conditions except
  REVERSAL_LONG_SIGNALS, which carry their own bespoke money-flow "knife, not
  a dip" checks) when `is_falling_knife` is True for that candle — close_1h
  down `falling_knife_drop_pct` or more over the last
  `falling_knife_lookback_candles` 1h candles (both per-pair calibrated,
  global defaults 4 candles / 10%). Added 2026-08-28 after a 30-day/96-pair
  backtest found condition #144 (Top Coins mode) buying straight into an
  ongoing dump 5/5 times (GALA -20% catastrophic_exit) — #144 has no
  per-condition money-flow guard of its own, unlike #10/#192/#193. Existing
  open trades are never touched (entries-only, same principle as every other
  guard here). Runtime-toggleable via `falling_knife_guard_enabled` in
  `strategy_control.json`.
- Catastrophic-loss circuit breaker: closes a trade once its lifetime profit
  (realized + unrealized, relative to the largest stake ever committed to it)
  breaches a hard floor, independent of the stale-exit profit band, but only
  once the trade has been open at least `catastrophic_exit_min_hours`. This
  exists because the stale-exit band alone can be satisfied by a near-flat
  *current* remainder even when earlier de-risk/re-grind cycles already
  locked in a large realized loss on the same trade — the band was observed
  to fire at a -36.6% lifetime loss in exactly that scenario. The min-hours
  gate exists because the breaker itself, fired instantly with no age floor
  and a too-tight ratio (0.05), was observed to cut grind trades on ordinary
  volatility before DCA averaging had a chance to work — see CHANGELOG.md.
- Grey-zone time-decay exit: closes the gap between the two guards above. A
  trade whose lifetime profit sits strictly between `-catastrophic_exit_loss_ratio`
  (-20%) and `-stale_exit_max_loss` (-1.5%) is a real, non-flat loss but not
  yet a catastrophe — today that trade has NO time-based exit at all, at any
  age, and depends entirely on X7's own DCA/grind signals to ever close. The
  fix is a per-pair calibrated threshold that starts wide (= today's
  `catastrophic_exit_loss_ratio`, so young losing trades are untouched and
  X7's own DCA ladder gets its normal chance to work) and narrows toward
  `stale_exit_max_loss` as the trade ages toward a pair-specific horizon —
  the longer a trade lingers in real loss without recovering, the more
  likely (empirically) it is to keep sliding toward -20%, so it gets cut
  sooner. The comparison is against the trade's own peak `total_profit_ratio`
  (persisted via `trade.set_custom_data`, clamped at a floor of 0.0), not
  against zero: a trade that never went positive behaves exactly as before
  (peak == 0, so giveback-from-peak == loss-from-zero), but a trade that
  reached e.g. +5% and is now decaying back toward breakeven is caught by the
  same aging threshold instead of needing a separate mechanism — the two
  looked like different problems ("losing trade keeps sliding" vs "winning
  trade is giving it back") but are the same signal measured from different
  reference points, so one curve covers both. Calibrated from a rolling-peak
  drawdown study across the 8
  whitelisted pairs (~2 months of 5m futures candles, thresholds converted
  from leveraged P&L to raw price via `futures_mode_leverage = 3.0`): at a
  72h horizon a grey-zone drawdown cascades through to -20% only 4.9% of the
  time for BTC but 30.4% of the time for ADA, and the weighted-average
  cascade rate itself rises with elapsed time (0.8% @6h, 8.4% @24h, 20.7%
  @72h) — hence a convex decay curve rather than a flat one. Trade-off,
  stated plainly: by ~60h on a mid-tier pair the effective threshold sits
  inside X7's own `grinding_v2_derisk_level_1_futures` gate region, so this
  can preempt de-risk on old trades — intentional (that is the point), but
  why it defaults OFF (`grey_zone_exit_enabled = False`) pending backtest
  confirmation on more than one window. Runtime-toggleable via
  `strategy_control.json` like everything else here.
- Bounded, self-expiring risk-adjustment override: `risk_adjustments` in
  `strategy_control.json` lets an operator (human or LLM, via the nfi-mcp
  `request_risk_analysis`/`set_risk_adjustment` tools) temporarily scale the
  grey-zone exit's cascade-rate assumption for one pair (or `"*"` for all),
  bounded by an explicit `expires_at`. There is no scheduler and no
  autonomous polling of news — a caller reads the current calibration and
  open trades via `request_risk_analysis`, decides on a multiplier, and
  commits it via `set_risk_adjustment`. The override self-expires: every
  `custom_exit` call compares its own `current_time` against `expires_at`
  and simply stops applying the multiplier once it lapses — no cleanup job,
  no separate revert step. Regardless of the multiplier, the underlying
  curve can never fully tighten before `grey_zone_exit_min_full_hours` (24h)
  or later than `grey_zone_exit_max_full_hours` (168h) — the clamp, not the
  multiplier's own bounds, is the real safety limit.
- De-risk/re-grind guard: once a trade has had at least one de-risk
  (`derisk_level_*`) reduction, further grind-mode entries on that same trade
  are blocked. A de-risk firing means the strategy itself judged the position
  bad enough to cut size; immediately re-buying back into it at a similar
  price re-creates the same exposure the de-risk was meant to remove.
- Hot signal control via <user_data>/strategy_control.json, re-read every bot
  loop (same idiom NFI itself uses for the hold-trades file). Changes take
  effect on the next candle without /reload_config or a container restart:

    {
      "long_signals_override":  {"170": false},
      "short_signals_override": {"666": true},
      "ema200_guard_enabled": true,
      "stale_exit_enabled": true,
      "stale_exit_hours": 6,
      "stale_exit_profit_band": 0.01,
      "stale_exit_max_loss": 0.015,
      "catastrophic_exit_enabled": true,
      "catastrophic_exit_loss_ratio": 0.20,
      "catastrophic_exit_min_hours": 2.0,
      "grey_zone_exit_enabled": true,
      "grey_zone_exit_start_hours": null,
      "grey_zone_exit_floor_ratio": null,
      "grey_zone_exit_ref_hours": 72.0,
      "grey_zone_exit_ref_cascade_pct": 20.7,
      "grey_zone_exit_pair_sensitivity": 1.0,
      "grey_zone_exit_min_full_hours": 24.0,
      "grey_zone_exit_max_full_hours": 168.0,
      "grey_zone_exit_curve_exponent": 2.0,
      "risk_adjustments": {
        "ADA/USDT:USDT": {
          "multiplier": 1.5,
          "expires_at": "2026-08-28T09:30:00+00:00",
          "set_at": "2026-08-27T21:30:00+00:00",
          "ttl_hours": 12.0,
          "reason": "ADA delisting rumor; cascade risk elevated vs the 30.4% baseline"
        }
      },
      "block_regrind_after_derisk": true,
      "conflicting_signal_guard_enabled": true,
      "correlated_loss_guard_enabled": true,
      "correlated_loss_guard_min_losing": 3,
      "correlated_loss_guard_loss_threshold": -0.01,
      "entry_rate_limit_enabled": true,
      "entry_rate_limit_window_hours": 6,
      "entry_rate_limit_max_entries": 2,
      "signal_666_volume_spike_enabled": true,
      "signal_666_volume_spike_mult": 1.5,
      "signal_666_funding_confirm_enabled": true,
      "signal_666_min_funding_rate": 0.0001,
      "funding_settlement_buffer_enabled": true,
      "funding_settlement_buffer_minutes": 20,
      "drift_block_entries_enabled": true,
      "falling_knife_guard_enabled": true,
      "falling_knife_lookback_candles": 4,
      "falling_knife_drop_pct": 10.0,
      "momentum_entry_rsi_guard_enabled": true,
      "momentum_entry_rsi_overbought": 70.0,
      "money_weight_enabled": true
    }

  Overrides are applied on top of the config-derived baseline captured at
  startup; removing a key from the file restores the baseline value. A missing
  or invalid file leaves the current state untouched.

  `risk_adjustments` entries additionally self-expire: `custom_exit` compares
  each entry's `expires_at` against its own `current_time` on every call, so
  an entry stops having any effect the instant it lapses — no cleanup job,
  no mutation of strategy state. An entry with `expires_at` absent or `null`
  never expires; that shape is intended for backtest configs only and logs a
  warning if it appears in a live control file.

Signal #666 (bull-trap SFP) was backtested 2026-08-27 over a 14-day window
(see nfi_signal_666_backtest_findings memory) and found responsible for 85%
of all trades and -245 of the strategy's -247 USDT total loss, including 7
simultaneous catastrophic losses on 2026-08-17..20 across unrelated pairs — a
market-wide rally producing correlated false "bull trap" signals that neither
the EMA_200_1h guard nor a 4h ADX/DI trend-strength confirmation (also tested,
see the same memory) caught, because the DI reading flips from bearish to
bullish on the very candle the trade enters on. Disabled via
`strategy_control.json`'s `short_signals_override` per that evidence, not via
a code change — the signal itself is architecturally fine to keep available
for statistics collection, it's simply switched off in the live config.

Directional exposure cap (limiting how many concurrent shorts a rally can
stack up) does not need code here either: X7 already exposes
`futures_max_open_trades_long` / `futures_max_open_trades_short` as
NFI_SAFE_PARAMETERS (plain config keys, 0 = unlimited) — set
`futures_max_open_trades_short` in config.json instead of duplicating that
check in this subclass.

Four further guards added 2026-08-27, all order-placement-time checks in
`confirm_trade_entry` (no base-file changes needed):

- Correlated-loss guard: the 2026-08-17..20 cluster wasn't one bad trade, it
  was 7 pairs firing the same false signal within days of each other. Once
  `correlated_loss_guard_min_losing` trades from the same tracked signal
  family (`_correlated_loss_guard_family`: `short_scalp_mode_tags` for
  shorts, `correlated_loss_guard_long_signals` for longs — #7 by default,
  see below) are simultaneously underwater past
  `correlated_loss_guard_loss_threshold`, new entries from that family are
  blocked until some resolve. Needs no new data, only counting currently-open
  trades — but it is *reactive*: it can only block once earlier trades have
  had time to go red, which the next guard's own discovery shows isn't
  always the case.
- Entry rate limiter: backtesting #7 (ADX trend-birth, long) turned up the
  exact same correlated-cluster mechanism as #666, on 2026-06-15 — but there
  the correlated-loss guard let 5 pairs pile in within a 3h window because
  none of the earlier entries were underwater *yet* (the loss only showed up
  1-3 days later). The rate limiter caps new entries per tracked family
  within a rolling `entry_rate_limit_window_hours`, independent of profit —
  it catches the pile-in itself, not its eventual outcome. Backtested
  2026-08-27 (100 days of data, see nfi_x7ema200_improvements memory):
  cut #7's 90-day loss from -295 to a still-negative but smaller number:
  a real, partial fix, not a complete one.
- Signal #666 volume-spike confirmation: a genuine stop-hunt/SFP wick usually
  comes with a volume spike (someone's stop got triggered); a continuation
  breakout dressed up as one usually doesn't. Requires the entry candle's
  volume >= `signal_666_volume_spike_mult` times its trailing 48-candle
  average. This targets the pattern's own conviction directly, unlike the
  4h ADX/DI trend filter tried and discarded earlier (see
  nfi_signal_666_backtest_findings memory) which failed because trend
  indicators lag exactly at the inflection points #666 tries to catch.
- Signal #666 funding-rate confirmation: Binance perpetual funding rate is a
  positioning signal price action can't see — a rich positive rate means the
  market is crowded long and paying for it, supporting the "blow-off top,
  ripe for a trap" thesis; nothing in X7's own dataframe carries this, so
  `populate_indicators` merges the funding-rate candle history in as
  `funding_rate` via `merge_asof` (ffilled backward — funding settles every
  8h, not every 5m). Requires `funding_rate >= signal_666_min_funding_rate`.
  Also blocks #666 entries within `funding_settlement_buffer_minutes` of a
  00:00/08:00/16:00 UTC settlement, when stop-hunt wicks are known to cluster.
- Shadow-mode for recently-unbanned pairs: a pair just removed from the
  static blacklist.json (see `unbanned_pairs` in strategy_control.json) opens
  real orders that are LOCALLY recorded as filled but never actually sent to
  Binance, on a bot that is otherwise fully live (dry_run=false). This is
  deliberately per-pair, not a second bot instance: freqtrade has no
  supported hook for "run this exchange call in dry-run for pair X only", so
  `bot_start` monkey-patches `self.dp._exchange.create_order` to route
  shadow-mode pairs through `Exchange.create_dry_run_order` (the exact same
  path freqtrade's own global `dry_run=true` takes) instead of the real
  `self._api.create_order`. A pair stays in shadow mode until a human sets a
  nonzero `risk_budget_pct` or `risk_budget_abs` for it (see
  `set_unbanned_pair_risk_budget` MCP tool / the FreqUI risk-budget modal on
  its Trade page pair button) — that is the ONLY way real orders start
  flowing for that pair. This is a private-internals patch (no supported
  freqtrade API for it), re-applied on every `bot_start` (so it survives
  `/reload_config`, which rebuilds the Exchange object), and MUST be checked
  against the freqtrade version on every upgrade — see `_patch_shadow_mode`.

All these new knobs are config-overridable for backtest A/B testing and
wired into strategy_control.json for live hot-toggling, consistent with the
rest of this file.
"""

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from freqtrade.enums import CandleType
from freqtrade.persistence import Trade
from pandas import DataFrame, Series

# The strategy files live side by side in /freqtrade; make the base
# module importable regardless of the interpreter's working directory.
sys.path.append(str(Path(__file__).parent))

from NostalgiaForInfinityX7 import NostalgiaForInfinityX7

log = logging.getLogger(__name__)

CONTROL_FILE_NAME = "strategy_control.json"
GREEN_STREAK_CACHE_FILE_NAME = "green_streak_cache.json"
PAIR_PARAMS_CACHE_FILE_NAME = "pair_strategy_params.json"
PAIR_DRIFT_FLAGS_FILE_NAME = "pair_drift_flags.json"

# Numeric exit/guard knobs that make sense calibrated PER PAIR (different
# pairs have different volatility/liquidity/cascade profiles) and are
# resolved via _param_for_pair, falling back to the plain self.X class/
# control-file value when a pair has no fresh cache entry. Value is
# (allow_none, min_value_or_None) — min_value_or_None is an EXCLUSIVE lower
# bound (must be strictly greater), matching the corresponding
# _apply_*_control validator for that key.
#
# entry_rate_limit_window_hours / entry_rate_limit_max_entries and
# correlated_loss_guard_min_losing evaluate a whole signal FAMILY across
# pairs, not a single pair's own state — there is no per-pair "count of
# trades" to measure. They're still exposed here per pair keyed by the
# CANDIDATE entry's pair (the pair confirm_trade_entry is currently
# evaluating), i.e. "how strict should the guard be when THIS pair wants to
# enter" rather than "this pair's own count". grey_zone_exit_pair_sensitivity
# is likewise keyed by the pair whose _grey_zone_full_hours is being
# computed. min_value is None for all four to match the (looser) bounds
# already enforced by their _apply_*_control validators.
PAIR_PARAM_SPECS: dict[str, tuple[bool, Optional[float]]] = {
    "stale_exit_hours": (False, 0.0),
    "stale_exit_profit_band": (False, None),
    "stale_exit_max_loss": (False, None),
    "catastrophic_exit_loss_ratio": (False, 0.0),
    "catastrophic_exit_min_hours": (False, None),
    "correlated_loss_guard_loss_threshold": (False, None),
    "correlated_loss_guard_min_losing": (False, None),
    "entry_rate_limit_window_hours": (False, None),
    "entry_rate_limit_max_entries": (False, None),
    "signal_666_volume_spike_mult": (False, 0.0),
    "signal_666_min_funding_rate": (False, None),
    "funding_settlement_buffer_minutes": (False, None),
    "grey_zone_exit_start_hours": (True, 0.0),
    "grey_zone_exit_floor_ratio": (True, 0.0),
    "grey_zone_exit_ref_hours": (False, 0.0),
    "grey_zone_exit_ref_cascade_pct": (False, 0.0),
    "grey_zone_exit_min_full_hours": (False, 0.0),
    "grey_zone_exit_max_full_hours": (False, 0.0),
    "grey_zone_exit_curve_exponent": (False, 0.0),
    "grey_zone_exit_pair_sensitivity": (False, None),
    "falling_knife_lookback_candles": (False, 0.0),
    "falling_knife_drop_pct": (False, 0.0),
    "momentum_entry_rsi_overbought": (False, 0.0),
    "money_weight": (False, None),
}


class StrategyControlFile:
    """
    Watches a JSON control file and reports its parsed content when it changes.

    Single responsibility: file watching + parsing. It knows nothing about
    signals or strategies; the strategy decides how to apply the content.
    """

    _NEVER = object()

    def __init__(self, path: Path) -> None:
        self._path = path
        self._last_signature: Any = self._NEVER
        self._last_content: Optional[dict] = None

    def poll(self) -> tuple[bool, Optional[dict]]:
        """
        Return (changed, content).

        content is the parsed dict, or None when the file is absent. changed is
        True only when the file appeared, disappeared, or its mtime changed and
        it parsed successfully. A file that fails to parse is reported once as
        a warning and treated as "no change".
        """
        try:
            signature = self._path.stat().st_mtime_ns
        except OSError:
            signature = None

        if signature == self._last_signature:
            return False, self._last_content

        self._last_signature = signature

        if signature is None:
            self._last_content = None
            return True, None

        try:
            content = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(content, dict):
                raise ValueError("control file root must be a JSON object")
        except (OSError, ValueError) as exc:
            log.warning("Strategy control: ignoring invalid %s: %s", self._path, exc)
            return False, self._last_content

        self._last_content = content
        return True, content


class NostalgiaForInfinityX7EMA200(NostalgiaForInfinityX7):
    """
    NFI X7 with a toggleable EMA200 macro trend guard and hot signal control.
    """

    # Runtime-toggleable via strategy_control.json; True is the safe default.
    ema200_guard_enabled = True

    # Falling-knife guard: blocks long entries when the pair has dropped
    # falling_knife_drop_pct or more over the last falling_knife_lookback_candles
    # 1h candles - added 2026-08-28 after condition #144 (Top Coins mode) kept
    # buying pairs mid-dump (GALA -20% catastrophic_exit) with no per-condition
    # money-flow guard of its own, unlike the reversal-family signals (see
    # REVERSAL_LONG_SIGNALS below), which each hand-tune their own "knife, not
    # a dip" CMF/ROC checks. This is a blunter, generic backstop for every
    # OTHER long condition. Runtime-toggleable via strategy_control.json;
    # lookback_candles/drop_pct are both per-pair overridable (see
    # PAIR_PARAM_SPECS/_param_for_pair) - global defaults below are the
    # illustrative starting point ("4 candles down and 10% = a knife"), a
    # calibration job derives the real per-pair drop_pct from each pair's own
    # measured 4-candle drawdown distribution (see pair_param_calibration.py).
    falling_knife_guard_enabled = True
    falling_knife_lookback_candles = 4
    falling_knife_drop_pct = 10.0

    # Stale-trade exit: runtime-toggleable via strategy_control.json. The
    # band is two-sided — stale_exit_max_loss caps how far underwater a trade
    # may be and still count as "flat/dead"; beyond that it's a real loss and
    # is left to X7's own DCA/grind ladder or the catastrophic breaker below,
    # never force-closed by the stale-trade timer alone. See CHANGELOG.md
    # 2026-08-27 (stale_exit closed a trade at -36.6% before this existed).
    stale_exit_enabled = True
    stale_exit_hours = 6.0
    stale_exit_profit_band = 0.01
    stale_exit_max_loss = 0.015

    # Reversal-family long signals are deliberately counter-trend (buying
    # oversold against the prevailing 1h trend), so the EMA200 macro guard
    # below does not apply to them — see module docstring.
    REVERSAL_LONG_SIGNALS = frozenset({"10", "11", "170", "192", "193"})

    # Momentum entry ("buy strength"): runtime-toggleable via
    # strategy_control.json. Defaults OFF — see module docstring.
    momentum_entry_enabled = False

    # Green-streak gate for momentum entry: only fire "900" on the pair's
    # Nth consecutive green 1h candle, N calibrated per pair. Both
    # runtime-toggleable via strategy_control.json.
    # green_streak_default_n: computed 2026-08-28 by backtesting streak-N vs
    # 24h-forward-return expectancy (win_rate*avg_gain - loss_rate*avg_loss)
    # over 90 days / 1h candles on 8 representative pairs (BTC/ETH/SOL/XRP/
    # DOGE/ADA/LINK/AVAX): best-N per pair was {2,3,3,3,5,5,5,5}, median 4.
    # Caveat: N=6+ had under 20 historical occurrences per pair in 90 days
    # (too few to trust), so several pairs' true optimum may sit above the
    # N=5 ceiling this backtest could actually measure — treat 4 as a
    # reasonable starting default, not a settled optimum. AVAX had negative
    # expectancy at every N tested; per-pair cache entries (once computed)
    # override this default and should let a pair like that simply not
    # qualify. See CHANGELOG.md.
    green_streak_default_n = 4
    # TTL for a cached per-pair N before it's treated as stale ("мусор") and
    # the default above is used instead. 168h = 1 week.
    green_streak_ttl_hours = 168.0

    # Overbought guard for momentum entry: blocks "900" once RSI_14_1h (X7's
    # own 1h RSI, already computed for its own signals — no new indicator)
    # reaches momentum_entry_rsi_overbought, so the signal cannot buy a
    # blow-off top the way it would have on the 2026-08-28 01:00 ENA candle
    # (RSI_14_1h 81.7, immediately followed by a reversal). Added
    # 2026-08-28 after an ENA loss on "900"; note the classic 70 threshold
    # would NOT have blocked that specific trade (RSI_14_1h was 56.0 at
    # entry — a weak bounce inside a wider downtrend, not an overbought
    # spike), so this guard covers a different, narrower failure mode than
    # the one observed and is not a full fix for that case. Global default
    # only; per-pair overridable via pair_strategy_params.json exactly like
    # falling_knife_drop_pct (see PAIR_PARAM_SPECS/_param_for_pair) so an
    # external calibration job can periodically refit it from a rolling
    # backtest window instead of everyone sharing one hand-picked constant.
    # Both knobs hot-reloadable via strategy_control.json.
    momentum_entry_rsi_guard_enabled = True
    momentum_entry_rsi_overbought = 70.0

    # Per-pair money-allocation weight: multiplies whatever stake X7's own
    # custom_stake_amount (rebuy/grind/rapid mode sizing) proposes, applied
    # AFTER it (see custom_stake_amount below - Open/Closed: extend, don't
    # replace X7's sizing). 1.0 = full stake (today's behavior), down to 0.0
    # (freqtrade skips the entry outright - see wallets.validate_stake_amount
    # - the standard way to veto a trade via stake sizing, same mechanism
    # confirm_trade_entry's guards use, just from the sizing side instead).
    # Global default only; per-pair overridable via pair_strategy_params.json
    # exactly like every other PAIR_PARAM_SPECS knob - written by
    # nfi_mcp_server's pair_priority.classify_pair_priority /
    # calibrate_pair_priority tool, which buckets a pair's trade history into
    # consistent_winner (1.0) / consistent_loser (0.5) / volatile_profitable
    # (0.1, has losses but net profitable - e.g. BTW/LAB in the 2026-08-29
    # per-pair backtest report) / no_win (0.0) / insufficient_data (1.0,
    # unchanged until judged). Runtime-toggleable via strategy_control.json.
    money_weight_enabled = True
    money_weight = 1.0

    # Catastrophic-loss circuit breaker: runtime-toggleable via strategy_control.json.
    # 0.05 was observed to fire inside NFI's own grind range (-5%...-10% is
    # normal DCA noise for X7, see CHANGELOG.md 2026-08-27) before grind/DCA
    # entries got a chance to average price down, causing 11 consecutive
    # premature stops (-970 USDT). Raised to 0.15 and gated by min-hours below
    # so the breaker only trips after the grind ladder has had time to act.
    # 0.15 was itself still too tight on futures: X7's own
    # grinding_v2_derisk_level_1_futures threshold is -0.18 leveraged, and
    # grind_1/2/3 require a prior de-risk to fire at all, so a 0.15 breaker
    # still preempts de-risk (and therefore the whole main grind ladder)
    # before it can ever activate on a leveraged futures trade. Raised to
    # 0.20 (above the -0.18 de-risk threshold) so de-risk gets a chance to
    # fire first. See CHANGELOG.md 2026-08-27.
    catastrophic_exit_enabled = True
    catastrophic_exit_loss_ratio = 0.20
    catastrophic_exit_min_hours = 2.0

    # Grey-zone time-decay exit calibration: measured empirically, NOT a
    # runtime-tunable knob (an operator can only scale it via the bounded
    # `risk_adjustments` multiplier below, never rewrite it directly). Keyed
    # by base symbol (pair.split("/")[0]) so it survives quote/settle
    # changes. "*" is the fallback for any pair not measured (= the
    # weighted average across all 8). Source: rolling-peak-from-trailing-high
    # drawdown episodes on ~2 months of 5m futures candles per pair, with
    # thresholds converted from leveraged P&L to raw price by dividing by
    # futures_mode_leverage=3.0 (-0.20/3=-6.67%, -0.015/3=-0.5%), tracking
    # each episode until it recovers above -0.5% or cascades through -6.67%.
    # Percentages below are "% of grey-zone episodes that cascaded through
    # to -20% P&L" at a 72h rolling-peak lookback. Methodology currently
    # lives in a session scratchpad script; promote to
    # nfi_deploy/tools/greyzone_calibration.py before the next recalibration.
    GREY_ZONE_CASCADE_PCT_72H = {
        "BTC": 4.9,
        "ETH": 15.1,
        "SOL": 22.5,
        "XRP": 22.8,
        "DOGE": 27.0,
        "ADA": 30.4,
        "LINK": 23.7,
        "AVAX": 20.1,
        "*": 20.7,
    }
    # Weighted-average cascade% vs rolling-peak lookback horizon (hours,
    # pct). Not consumed by the exit math directly — documents why the decay
    # curve is convex (risk is low early, rises superlinearly with time) and
    # is what request_risk_analysis reports to an LLM caller as the
    # baseline. Only the 72h column above is measured per-pair; 24h/6h
    # points are modelled per-pair by scaling this global shape (see
    # _grey_zone_full_hours).
    GREY_ZONE_CASCADE_GLOBAL_SHAPE = ((6.0, 0.8), (24.0, 8.4), (72.0, 20.7))

    # Grey-zone time-decay exit: runtime-toggleable via strategy_control.json.
    # Defaults OFF pending backtest confirmation on more than one window —
    # see module docstring. start_hours/floor_ratio of None late-bind to
    # stale_exit_hours/stale_exit_max_loss at call time, so they track those
    # knobs automatically unless explicitly pinned to a different value.
    grey_zone_exit_enabled = False
    grey_zone_exit_start_hours: Optional[float] = None
    grey_zone_exit_floor_ratio: Optional[float] = None
    grey_zone_exit_ref_hours = 72.0
    grey_zone_exit_ref_cascade_pct = 20.7
    # Backtest-calibrated 2026-08-27 on two windows (validation: -6.38% ->
    # +6.37% profit, 26.70% -> 16.93% drawdown, catastrophic_exit trades
    # 4 -> 0; rally: 104.29% -> 100.19% profit, drawdown unchanged at
    # 14.51% — a broad, stable plateau of good values around this point,
    # not a fragile single-cell optimum). See CHANGELOG.md.
    grey_zone_exit_pair_sensitivity = 3.0
    grey_zone_exit_min_full_hours = 24.0
    grey_zone_exit_max_full_hours = 168.0
    grey_zone_exit_curve_exponent = 5.0

    # Bounded, self-expiring operator override for the grey-zone exit's
    # cascade-rate assumption. Keyed by full pair, or "*" for a global
    # fallback. See module docstring and _active_risk_multiplier. Mutable
    # class-level default — __init__ and _apply_risk_adjustments_control
    # must always REBIND self.risk_adjustments to a new dict, never mutate
    # this one in place, or instances would share state.
    risk_adjustments: dict = {}

    # Pre-staged, date-gated entry bans — a hot-reloadable complement to the
    # static blacklist.json regex list. Keyed by full pair. Each entry has
    # three independent timestamps: created_at (when the rule was written,
    # always "now" at creation time), effective_from (when it starts
    # blocking entries — may be in the future, letting you pre-stage a ban
    # ahead of a known event like a token unlock instead of remembering to
    # flip it live that day), and expires_at (when it stops, or None for an
    # open-ended/structural block). Only entries are blocked; open trades on
    # the pair are untouched, same as the static blacklist. See
    # _apply_pair_blocks_control / _normalize_pair_blocks and the
    # schedule_pair_block MCP tool. Mutable class-level default — always
    # REBIND self.pair_blocks to a new dict, never mutate this one in place.
    pair_blocks: dict = {}

    # Recently-unbanned pairs currently in shadow mode (see module docstring
    # and _patch_shadow_mode). Keyed by full pair: {"unbanned_at": datetime,
    # "risk_budget_pct": float, "risk_budget_abs": float}. A pair is in
    # shadow mode iff both budget fields are <= 0 — there is no separate
    # bool to keep out of sync with them. Mutable class-level default —
    # always REBIND, never mutate in place.
    unbanned_pairs: dict = {}

    # De-risk/re-grind guard: runtime-toggleable via strategy_control.json.
    block_regrind_after_derisk = True

    # Conflicting-signal guard: runtime-toggleable via strategy_control.json.
    # A candle can satisfy a long condition's formula and a short condition's
    # formula at the same time (X7 evaluates both independently); when that
    # happens, whichever direction's dataframe column is set wins and the
    # trade's enter_tag carries both ids (e.g. "8 666" on a long trade). Those
    # specific trades were net losers in isolation on both a rally window and
    # a choppier one tested 2026-08-27 (-129 USDT and -53/-56 USDT), but
    # blocking them is NOT a clean win: with only 3 concurrent trade slots,
    # a blocked entry frees a slot the *next* candidate signal takes instead,
    # which is not always better. Backtest result was regime-dependent —
    # +15.6pp profit / -7.4pp drawdown on the trending window, but -9.1pp
    # profit / +4.9pp drawdown on the choppy one. Defaults OFF until tested
    # on more windows; available as an opt-in knob. See
    # nfi_x7ema200_improvements memory.
    conflicting_signal_guard_enabled = False

    # Correlated-loss guard: runtime-toggleable via strategy_control.json.
    # See module docstring — targets the 2026-08-17..20 cluster mechanism
    # directly (many pairs, same signal family, losing at once) rather than
    # any one pair's indicators.
    correlated_loss_guard_enabled = True
    correlated_loss_guard_min_losing = 3
    correlated_loss_guard_loss_threshold = -0.01
    # None -> defaults to short_scalp_mode_tags at call time (see
    # _correlated_loss_guard_family). Long side has no built-in "family" of
    # its own, so it's tracked explicitly: #7 (ADX trend-birth) showed the
    # same correlated-cluster mechanism on 2026-06-15 (6 pairs, same day).
    correlated_loss_guard_short_signals = None
    correlated_loss_guard_long_signals = frozenset({"7", "900"})

    # Entry rate limiter: runtime-toggleable via strategy_control.json.
    # Shares the same tracked families as the correlated-loss guard above
    # (see _correlated_loss_guard_family) — this one catches the mechanism
    # that guard structurally cannot: several pairs entering within hours of
    # each other, none yet underwater, that only turn bad together 1-3 days
    # later (2026-06-15's #7 cluster let 4 winning entries through, then 5
    # more piled in within a 3h window before any of them had time to go
    # red — the P&L-reactive guard had nothing to react to yet). Caps new
    # entries per tracked family within a rolling time window regardless of
    # their current profit.
    entry_rate_limit_enabled = True
    entry_rate_limit_window_hours = 6.0
    entry_rate_limit_max_entries = 2

    # Signal #666 extra confirmation: runtime-toggleable via
    # strategy_control.json. See module docstring.
    signal_666_volume_spike_enabled = True
    signal_666_volume_spike_mult = 1.5
    signal_666_funding_confirm_enabled = True
    signal_666_min_funding_rate = 0.0001
    funding_settlement_buffer_enabled = True
    funding_settlement_buffer_minutes = 20

    # Concept-drift fail-safe: runtime-toggleable via strategy_control.json.
    # See pair_drift_flags.json handling (_apply_drift_flags) and
    # nfi_mcp_server's pair_param_calibration.py module docstring for the
    # full adaptive-control loop this is the strategy-side half of. A pair
    # with an active flag always falls back to plain global defaults for
    # every PAIR_PARAM_SPECS knob regardless of this toggle (that half is
    # not optional — a calibration nobody trusts anymore is worse than none);
    # this toggle only controls whether new ENTRIES on a flagged pair are
    # blocked outright. Existing open trades are never touched by a flag.
    drift_block_entries_enabled = True

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._control_file = StrategyControlFile(
            Path(self.config["user_data_dir"]) / CONTROL_FILE_NAME
        )
        self._green_streak_cache_file = StrategyControlFile(
            Path(self.config["user_data_dir"]) / GREEN_STREAK_CACHE_FILE_NAME
        )
        # pair -> cached N, populated by _apply_green_streak_cache; pairs
        # absent here (no fresh cache entry) fall back to
        # green_streak_default_n at lookup time.
        self._green_streak_n_by_pair: dict[str, int] = {}
        self._pair_params_cache_file = StrategyControlFile(
            Path(self.config["user_data_dir"]) / PAIR_PARAMS_CACHE_FILE_NAME
        )
        # pair -> {param_name: value}, populated by _apply_pair_params_cache;
        # a pair/key absent here falls back to the plain self.<param_name>
        # value at lookup time (see _param_for_pair). Separate from
        # _green_streak_n_by_pair/green_streak_cache.json, which predates
        # this and stays its own file/mechanism.
        self._pair_params_by_pair: dict[str, dict[str, float]] = {}
        self._pair_drift_flags_file = StrategyControlFile(
            Path(self.config["user_data_dir"]) / PAIR_DRIFT_FLAGS_FILE_NAME
        )
        # pair -> {"reason","score","age_hours"} for every pair with a
        # currently-active concept-drift flag (see _apply_drift_flags). Empty
        # dict means no pairs are flagged, same convention as
        # _pair_params_by_pair.
        self._drift_flagged_pairs: dict[str, dict] = {}
        # Config-derived baseline the overrides are applied on top of, so that
        # removing an override from the control file restores this state.
        self._baseline_long_params = dict(self.long_entry_signal_params)
        self._baseline_short_params = dict(self.short_entry_signal_params)
        self._long_signal_ids = self._signal_ids(self.long_entry_signal_params, "long_entry_condition_")
        self._short_signal_ids = self._signal_ids(self.short_entry_signal_params, "short_entry_condition_")
        self._is_futures_mode = self.config.get("trading_mode") == "futures"
        # Config-overridable for backtest A/B comparison (see module docstring).
        # bot_loop_start only applies strategy_control.json in live/dry_run
        # (backtest has no wall-clock loop to poll a file on), so this is the
        # only way to vary these knobs across backtest runs.
        for _attr in (
            "ema200_guard_enabled",
            "momentum_entry_enabled",
            "green_streak_default_n",
            "green_streak_ttl_hours",
            "conflicting_signal_guard_enabled",
            "stale_exit_enabled",
            "stale_exit_hours",
            "stale_exit_profit_band",
            "stale_exit_max_loss",
            "catastrophic_exit_enabled",
            "catastrophic_exit_loss_ratio",
            "catastrophic_exit_min_hours",
            "grey_zone_exit_enabled",
            "grey_zone_exit_start_hours",
            "grey_zone_exit_floor_ratio",
            "grey_zone_exit_ref_hours",
            "grey_zone_exit_ref_cascade_pct",
            "grey_zone_exit_pair_sensitivity",
            "grey_zone_exit_min_full_hours",
            "grey_zone_exit_max_full_hours",
            "grey_zone_exit_curve_exponent",
            "risk_adjustments",
            "pair_blocks",
            "block_regrind_after_derisk",
            "correlated_loss_guard_enabled",
            "correlated_loss_guard_min_losing",
            "correlated_loss_guard_loss_threshold",
            "entry_rate_limit_enabled",
            "entry_rate_limit_window_hours",
            "entry_rate_limit_max_entries",
            "signal_666_volume_spike_enabled",
            "signal_666_volume_spike_mult",
            "signal_666_funding_confirm_enabled",
            "signal_666_min_funding_rate",
            "funding_settlement_buffer_enabled",
            "funding_settlement_buffer_minutes",
            "drift_block_entries_enabled",
            "falling_knife_guard_enabled",
            "falling_knife_lookback_candles",
            "falling_knife_drop_pct",
            "momentum_entry_rsi_guard_enabled",
            "momentum_entry_rsi_overbought",
        ):
            if _attr in config:
                setattr(self, _attr, config[_attr])
        # Route both the class default ({}) and any config-supplied dict
        # through the same validation/expires_at-parsing path the live
        # control file uses, so risk_adjustments is fully backtestable.
        self.risk_adjustments = self._normalize_risk_adjustments(self.risk_adjustments)
        self.pair_blocks = self._normalize_pair_blocks(self.pair_blocks)
        self.unbanned_pairs = self._normalize_unbanned_pairs(self.unbanned_pairs)

    def bot_start(self, **kwargs) -> None:
        super().bot_start(**kwargs)
        if self.config["runmode"].value in ("live", "dry_run"):
            self._patch_shadow_mode()
        # bot_loop_start (where these two normally poll) never runs in
        # backtest/hyperopt - there is no wall-clock loop to drive it. Without
        # this, pair_strategy_params.json/pair_drift_flags.json would be
        # silently ignored by every backtest, making per-pair calibration
        # untestable outside live/dry_run. One-time load here at wall-clock
        # "now" is enough since these files aren't expected to change mid-run.
        now = datetime.now(timezone.utc)
        self._apply_pair_params_cache(now)
        self._apply_drift_flags(now)

    def _patch_shadow_mode(self) -> None:
        """
        Monkey-patch this run's Exchange.create_order so a pair currently in
        shadow mode (see module docstring) gets a locally-simulated fill via
        Exchange.create_dry_run_order — freqtrade's own dry-run code path —
        instead of a real order, regardless of the bot's global dry_run
        setting. No-op (and safely re-appliable) if the strategy is loaded
        outside a live/dry_run runmode (backtest/hyperopt: self.dp._exchange
        may be None there) or if this exact Exchange object was already
        patched (guarded by _nfi_shadow_patched, since bot_start re-runs
        after /reload_config against a freshly-built Exchange instance).
        """
        exchange = getattr(self.dp, "_exchange", None)
        if exchange is None or getattr(exchange, "_nfi_shadow_patched", False):
            return
        original_create_order = exchange.create_order

        def shadow_aware_create_order(
            *, pair, ordertype, side, amount, rate, leverage,
            time_in_force="GTC", reduceOnly=False, initial_order=True,
        ):
            if self._pair_in_shadow_mode(pair):
                order = exchange.create_dry_run_order(
                    pair, ordertype, side, amount, exchange.price_to_precision(pair, rate), leverage
                )
                log.info(
                    "Shadow mode: simulated %s %s order for %s (amount=%s, rate=%s) — "
                    "NOT sent to the exchange, see unbanned_pairs",
                    side, ordertype, pair, amount, rate,
                )
                return order
            return original_create_order(
                pair=pair, ordertype=ordertype, side=side, amount=amount, rate=rate,
                leverage=leverage, time_in_force=time_in_force, reduceOnly=reduceOnly,
                initial_order=initial_order,
            )

        exchange.create_order = shadow_aware_create_order
        exchange._nfi_shadow_patched = True
        log.info("Shadow mode: Exchange.create_order patched for per-pair dry-run override")

    def _pair_in_shadow_mode(self, pair: str) -> bool:
        entry = self.unbanned_pairs.get(pair)
        if entry is None:
            return False
        return entry["risk_budget_pct"] <= 0 and entry["risk_budget_abs"] <= 0

    def order_filled(self, pair: str, trade: Any, order: Any, current_time: datetime, **kwargs) -> None:
        super().order_filled(pair, trade, order, current_time, **kwargs)
        if pair not in self.unbanned_pairs:
            return
        exit_side = getattr(trade, "exit_side", "sell")
        if getattr(order, "ft_order_side", None) == exit_side and not trade.is_open:
            log.info(
                "Shadow mode: recently-unbanned pair %s closed a %s trade, profit_ratio=%.4f "
                "(shadow=%s) — notify externally (Telegram) with an 'unbanned' tag",
                pair,
                "shadow" if self._pair_in_shadow_mode(pair) else "real",
                getattr(trade, "close_profit", None) or 0.0,
                self._pair_in_shadow_mode(pair),
            )

    # Indicators
    # -------------------------------------------------------------------------
    def populate_indicators(self, df: DataFrame, metadata: dict) -> DataFrame:
        df = super().populate_indicators(df, metadata)
        if self._is_futures_mode:
            df = self._merge_funding_rate(df, metadata["pair"])
        if self.momentum_entry_enabled:
            df = self._merge_green_streak_1h(df, metadata["pair"])
        if self.falling_knife_guard_enabled:
            df = self._populate_falling_knife(df, metadata["pair"])
        return df

    def _populate_falling_knife(self, df: DataFrame, pair: str) -> DataFrame:
        # is_falling_knife: True when close has dropped falling_knife_drop_pct
        # (or more) over the last falling_knife_lookback_candles 1h candles.
        # X7's own dataframe keeps raw OHLC only for the 15m informative merge
        # (see NostalgiaForInfinityX7.py's keep_ohlcv - every other timeframe
        # only carries derived indicators like EMA_200_1h/ADX_14_4h), so fetch
        # the 1h informative pair directly, same approach as
        # _merge_green_streak_1h just above.
        n = int(self._param_for_pair(pair, "falling_knife_lookback_candles"))
        drop_pct = self._param_for_pair(pair, "falling_knife_drop_pct")
        try:
            htf_df = self.dp.get_pair_dataframe(pair, "1h")
        except Exception as exc:  # pragma: no cover - defensive, data-source dependent
            log.warning("Falling-knife merge failed for %s: %s", pair, exc)
            htf_df = None
        if htf_df is None or htf_df.empty:
            df["is_falling_knife"] = False
            return df
        prior_close = htf_df["close"].shift(n)
        drop = (prior_close - htf_df["close"]) / prior_close * 100.0
        knife_1h = htf_df[["date"]].copy()
        knife_1h["is_falling_knife"] = (prior_close > 0) & (drop >= drop_pct)
        df = pd.merge_asof(df.sort_values("date"), knife_1h.sort_values("date"), on="date", direction="backward")
        df["is_falling_knife"] = df["is_falling_knife"].fillna(False)
        return df

    def _is_reversal_long(self, df: DataFrame) -> Series:
        # Reversal-family longs (see REVERSAL_LONG_SIGNALS) are deliberately
        # counter-trend/counter-momentum entries; guards built for the rest of
        # the entry conditions (EMA200 macro trend, falling-knife) do not
        # apply to them - each already carries its own hand-tuned protection.
        # A row can carry more than one signal's id in enter_tag
        # (space-separated) when several conditions fire on the same candle;
        # it's exempted if ANY of them is a reversal signal.
        tags = df["enter_tag"].fillna("")
        return tags.apply(lambda tag: any(token in self.REVERSAL_LONG_SIGNALS for token in tag.split()))

    def _merge_green_streak_1h(self, df: DataFrame, pair: str) -> DataFrame:
        # Consecutive-green-1h-candle counter for the momentum entry's
        # streak gate (see _populate_momentum_entry). X7's own dataframe has
        # no raw 1h OHLC (only derived indicators like EMA_200_1h), so fetch
        # the 1h informative pair directly — freqtrade caches this per its
        # own refresh cycle, this does not hit the exchange on every call.
        try:
            htf_df = self.dp.get_pair_dataframe(pair, "1h")
        except Exception as exc:  # pragma: no cover - defensive, data-source dependent
            log.warning("Green-streak merge failed for %s: %s", pair, exc)
            htf_df = None
        if htf_df is None or htf_df.empty:
            df["green_streak_1h"] = 0
            return df
        green = htf_df["close"] > htf_df["open"]
        # Standard run-length trick: group id increments on every red candle,
        # so cumcount() within a group gives 0,1,2,... for a run of greens;
        # multiplying by the green mask zeroes out red rows.
        streak = (green.groupby((~green).cumsum()).cumcount() + 1) * green
        htf_streak = htf_df[["date"]].copy()
        htf_streak["green_streak_1h"] = streak.astype(int)
        df = pd.merge_asof(df.sort_values("date"), htf_streak.sort_values("date"), on="date", direction="backward")
        df["green_streak_1h"] = df["green_streak_1h"].fillna(0).astype(int)
        return df

    def _merge_funding_rate(self, df: DataFrame, pair: str) -> DataFrame:
        # X7's own dataframe carries no funding-rate column — it's only used
        # internally by freqtrade's futures P&L engine, not exposed to
        # strategy code. Funding settles every 8h (00:00/08:00/16:00 UTC),
        # not every 5m, so merge_asof/backward-fill it onto the working
        # timeframe instead of treating it like a same-cadence indicator.
        try:
            funding_df = self.dp.get_pair_dataframe(pair, "1h", candle_type=CandleType.FUNDING_RATE)
        except Exception as exc:  # pragma: no cover - defensive, data-source dependent
            log.warning("Funding-rate merge failed for %s: %s", pair, exc)
            funding_df = None
        if funding_df is None or funding_df.empty or "open" not in funding_df.columns:
            df["funding_rate"] = float("nan")
            return df
        funding_df = funding_df[["date", "open"]].rename(columns={"open": "funding_rate"}).sort_values("date")
        df = pd.merge_asof(df.sort_values("date"), funding_df, on="date", direction="backward")
        return df

    # Entry trend
    # -------------------------------------------------------------------------
    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        df = super().populate_entry_trend(df, metadata)
        if self.momentum_entry_enabled:
            df = self._populate_momentum_entry(df, metadata["pair"])
        if self.ema200_guard_enabled:
            # Strict macro trend guard applied on top of NFI's trend-following
            # entry signals. Reuses X7's own 1h-informative EMA200 (already
            # merged into df by super().populate_indicators()) instead of a
            # 5m-only EMA200, which only spans ~16.6h and is too short-horizon
            # to be a macro filter. The negated comparison also blocks entries
            # while EMA_200_1h is NaN during the warm-up period.
            #
            # Reversal-family longs are exempted (see module docstring / class
            # docstring on REVERSAL_LONG_SIGNALS): they exist specifically to
            # buy oversold against the prevailing 1h trend, so gating them on
            # "close > EMA_200_1h" would block every one of their entries by
            # construction.
            block_long = ~(df["close"] > df["EMA_200_1h"]) & ~self._is_reversal_long(df)
            df.loc[block_long, "enter_long"] = 0
            df.loc[~(df["close"] < df["EMA_200_1h"]), "enter_short"] = 0

        if self.falling_knife_guard_enabled:
            # Generic backstop for conditions with no money-flow guard of
            # their own (see class docstring on falling_knife_guard_enabled).
            # Reversal-family longs are exempted for the same reason as the
            # EMA200 guard above - they exist specifically to buy this
            # situation, with their own bespoke CMF/ROC "knife, not a dip"
            # checks (see NostalgiaForInfinityX7.py conditions #10/#192/#193).
            block_knife_long = df["is_falling_knife"].fillna(False) & ~self._is_reversal_long(df)
            df.loc[block_knife_long, "enter_long"] = 0

        # Pre-staged, date-gated entry ban (strategy_control.json pair_blocks
        # / schedule_pair_block MCP tool). Checked per-candle against df["date"]
        # rather than "now" so a future-dated effective_from and an expires_at
        # both resolve correctly in backtest, not just live. Open trades are
        # untouched - populate_exit_trend/custom_exit are not affected.
        block = self.pair_blocks.get(metadata["pair"])
        if block is not None:
            in_window = df["date"] >= block["effective_from"]
            if block["expires_at"] is not None:
                in_window &= df["date"] < block["expires_at"]
            df.loc[in_window, "enter_long"] = 0
            df.loc[in_window, "enter_short"] = 0

        return df

    def _populate_momentum_entry(self, df: DataFrame, pair: str) -> DataFrame:
        # Condition #7 (ADX trend-birth) minus the "trend must be freshly
        # born" gate — see module docstring. allowed_empty_candles_288 is
        # hardcoded to 60 (X7's non-BTC-stake value; this deployment is
        # USDT-stake) rather than recomputed from stake currency, since it
        # never varies for this bot.
        #
        # Green-streak gate (added 2026-08-28): only buy on the pair's Nth
        # consecutive green 1h candle, not on every candle that happens to
        # satisfy the ADX/DI/EMA200 condition — an unqualified version fires
        # on every bar of a long trend, which is not "buying strength" so
        # much as "buying every bar of a trend, good and bad alike". N is
        # per-pair (self._green_streak_n_by_pair, populated by
        # _apply_green_streak_cache from an external analysis job) or
        # green_streak_default_n when no fresh cache entry exists. Requires
        # green_streak_1h == N exactly (not >=): fires once, on the specific
        # candle the streak reaches N, rather than re-firing on every later
        # candle of an even longer streak.
        streak_n = self._green_streak_n_by_pair.get(pair, self.green_streak_default_n)
        momentum_condition = (
            (df["num_empty_288"] <= 60)
            & (df["protections_long_global"] == True)  # noqa: E712 - pandas boolean column
            & (df["ADX_14_4h"] > 20.0)
            & (df["PLUS_DI_14_4h"] > df["MINUS_DI_14_4h"])
            & (df["close"] > df["EMA_200"])
            & (df["green_streak_1h"] == streak_n)
        )
        if self.momentum_entry_rsi_guard_enabled:
            # Per-pair calibrated ceiling when fresh (see class docstring on
            # momentum_entry_rsi_overbought), else the global default.
            rsi_ceiling = self._param_for_pair(pair, "momentum_entry_rsi_overbought")
            momentum_condition &= df["RSI_14_1h"] < rsi_ceiling
        already_long = df["enter_long"] == 1
        new_entries = momentum_condition & ~already_long
        df.loc[new_entries, "enter_tag"] = "900"
        df.loc[new_entries, "enter_long"] = 1
        stacked = momentum_condition & already_long
        df.loc[stacked, "enter_tag"] = df.loc[stacked, "enter_tag"].fillna("") + " 900"
        return df

    # Trade confirmation
    # -------------------------------------------------------------------------
    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> bool:
        confirmed = super().confirm_trade_entry(
            pair, order_type, amount, rate, time_in_force, current_time, entry_tag, side, **kwargs
        )
        if not confirmed:
            return False

        tags = (entry_tag or "").split()

        if self.drift_block_entries_enabled and pair in self._drift_flagged_pairs:
            log.info(
                "Strategy control: blocked %s entry %r on %s — concept-drift flag active (%s)",
                side,
                entry_tag,
                pair,
                self._drift_flagged_pairs[pair].get("reason", ""),
            )
            return False

        if self.conflicting_signal_guard_enabled:
            opposite_ids = self._short_signal_ids if side == "long" else self._long_signal_ids
            if any(tag in opposite_ids for tag in tags):
                log.info(
                    "Strategy control: blocked %s entry %r on %s — conflicting long/short signal guard tripped",
                    side,
                    entry_tag,
                    pair,
                )
                return False

        family = self._correlated_loss_guard_family(side)
        family_matches = any(tag in family for tag in tags)

        if (
            self.correlated_loss_guard_enabled
            and family_matches
            and self._too_many_correlated_losses(pair, side, family)
        ):
            log.info(
                "Strategy control: blocked %s entry %r on %s — correlated-loss guard tripped",
                side,
                entry_tag,
                pair,
            )
            return False

        if (
            self.entry_rate_limit_enabled
            and family_matches
            and self._too_many_recent_entries(pair, side, family, current_time)
        ):
            log.info(
                "Strategy control: blocked %s entry %r on %s — entry rate limit tripped",
                side,
                entry_tag,
                pair,
            )
            return False

        if "666" in tags and side == "short" and not self._signal_666_extra_confirmed(pair, current_time):
            return False

        return True

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: Optional[float],
        max_stake: float,
        leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> float:
        # Open/Closed: X7's own rebuy/grind/rapid-mode sizing runs first and
        # is left untouched; money_weight only scales the result afterwards
        # (see class attribute docstring above / pair_priority module in
        # nfi_mcp_server). A weight of 0.0 (no_win pairs) makes this return
        # 0, which freqtradebot/wallets.validate_stake_amount treats as "skip
        # this entry" - the standard way to veto a trade via stake sizing.
        base_stake = super().custom_stake_amount(
            pair, current_time, current_rate, proposed_stake, min_stake, max_stake, leverage, entry_tag, side,
            **kwargs,
        )
        if not self.money_weight_enabled:
            return base_stake
        weight = self._param_for_pair(pair, "money_weight")
        return base_stake * weight

    def _too_many_recent_entries(
        self, pair: str, side: str, family: frozenset, current_time: datetime
    ) -> bool:
        # Counts trades (open or already closed) from the tracked family that
        # OPENED within the last entry_rate_limit_window_hours, regardless of
        # their profit — catches a fast simultaneous pile-in before any of
        # the trades have had time to go red (see class docstring). Window/
        # threshold are resolved for the CANDIDATE pair (the one currently
        # trying to enter), not any single trade in the family.
        window_hours = self._param_for_pair(pair, "entry_rate_limit_window_hours")
        window_start = current_time - timedelta(hours=window_hours)
        recent = 0
        for trade in Trade.get_trades_proxy(open_date=window_start):
            if trade.trade_direction != side:
                continue
            trade_tags = (trade.enter_tag or "").split()
            if any(tag in family for tag in trade_tags):
                recent += 1
        return recent >= self._param_for_pair(pair, "entry_rate_limit_max_entries")

    def _correlated_loss_guard_family(self, side: str) -> frozenset:
        # Short side defaults to the scalp-mode family (#661-671, includes
        # #666); long side is a smaller, explicitly tracked set — #7 (ADX
        # trend-birth) exhibits the identical correlated-cluster mechanism on
        # the long side (see nfi_x7ema200_improvements memory, 2026-06-15
        # cluster: 6 pairs hit catastrophic_exit the same day) despite not
        # being part of any X7-defined "mode" family.
        if side == "short":
            return self.correlated_loss_guard_short_signals or self.short_scalp_mode_tags
        return self.correlated_loss_guard_long_signals

    def _too_many_correlated_losses(self, pair: str, side: str, family: frozenset) -> bool:
        # Counts currently-open trades from the same signal family that are
        # already underwater — see module docstring. Each open trade's own
        # last analyzed candle supplies its mark price, so this needs no
        # extra data fetch beyond what's already cached. The count threshold
        # is resolved for the CANDIDATE pair (the one currently trying to
        # enter), not any single trade in the family.
        losing = 0
        for trade in Trade.get_trades_proxy(is_open=True):
            if trade.trade_direction != side:
                continue
            trade_tags = (trade.enter_tag or "").split()
            if not any(tag in family for tag in trade_tags):
                continue
            dataframe, _ = self.dp.get_analyzed_dataframe(trade.pair, self.timeframe)
            if dataframe is None or len(dataframe) < 1:
                continue
            last_close = dataframe.iloc[-1]["close"]
            loss_threshold = self._param_for_pair(trade.pair, "correlated_loss_guard_loss_threshold")
            if trade.calc_profit_ratio(last_close) <= loss_threshold:
                losing += 1
        return losing >= self._param_for_pair(pair, "correlated_loss_guard_min_losing")

    def _signal_666_extra_confirmed(self, pair: str, current_time: datetime) -> bool:
        if self.funding_settlement_buffer_enabled:
            # Funding settles every 8h at 00:00/08:00/16:00 UTC; stop-hunt
            # wicks are known to cluster around settlement.
            buffer_minutes = self._param_for_pair(pair, "funding_settlement_buffer_minutes")
            minutes_of_day = current_time.hour * 60 + current_time.minute
            minutes_since_settlement = minutes_of_day % (8 * 60)
            minutes_to_next_settlement = (8 * 60) - minutes_since_settlement
            if minutes_since_settlement < buffer_minutes or minutes_to_next_settlement < buffer_minutes:
                return False

        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or len(dataframe) < 49:
            return True
        last_candle = dataframe.iloc[-1]

        if self.signal_666_volume_spike_enabled:
            avg_volume = dataframe["volume"].iloc[-49:-1].mean()
            spike_mult = self._param_for_pair(pair, "signal_666_volume_spike_mult")
            if not (avg_volume > 0 and last_candle["volume"] >= spike_mult * avg_volume):
                return False

        if self.signal_666_funding_confirm_enabled:
            funding_rate = last_candle.get("funding_rate")
            min_funding_rate = self._param_for_pair(pair, "signal_666_min_funding_rate")
            if funding_rate is None or pd.isna(funding_rate) or funding_rate < min_funding_rate:
                return False

        return True

    # Exit trend
    # -------------------------------------------------------------------------
    def custom_exit(
        self,
        pair: str,
        trade: Any,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ):
        exit_signal = super().custom_exit(pair, trade, current_time, current_rate, current_profit, **kwargs)
        if exit_signal:
            return exit_signal

        # current_profit (== trade.calc_profit_ratio()) only reflects the currently
        # open remainder of the position. For NFI's grinding/DCA trades that have
        # partially exited before, it ignores trade.realized_profit and can look
        # flat while the trade's true lifetime P&L is deeply negative. Use the
        # total profit ratio (realized + unrealized, relative to the largest
        # stake ever committed to this trade) for both checks below.
        total_profit_ratio = trade.calculate_profit(current_rate).total_profit_ratio
        trade_hours = (current_time - trade.open_date_utc).total_seconds() / 3600.0
        # Tracked unconditionally (not just while grey_zone_exit_enabled) so the
        # peak reflects the trade's whole life even if the toggle is flipped on
        # mid-trade — see _update_peak_profit.
        peak_total_profit_ratio = self._update_peak_profit(trade, total_profit_ratio)

        # Catastrophic-loss circuit breaker: independent of the stale-exit band,
        # but gated by catastrophic_exit_min_hours so it does not preempt X7's
        # own grind/DCA ladder. A de-risk sell followed by a re-grind buy-back
        # near the same low can put a trade deeply underwater on a lifetime
        # basis while its *current* remainder still looks near-flat (the exact
        # combination that let a trade close at a -36.6% realized loss via
        # stale_exit below, instead of being stopped out earlier). The min-hours
        # gate exists because firing this instantly, with no age floor, was
        # itself observed to cut grind trades on ordinary -5%...-10% noise
        # before averaging had a chance to work (see CHANGELOG.md 2026-08-27).
        catastrophic_ratio = self._param_for_pair(pair, "catastrophic_exit_loss_ratio")
        if (
            self.catastrophic_exit_enabled
            and trade_hours >= self._param_for_pair(pair, "catastrophic_exit_min_hours")
            and total_profit_ratio <= -catastrophic_ratio
        ):
            return f"catastrophic_exit_{catastrophic_ratio:g}"

        # Grey-zone time-decay exit: closes the gap between the two guards
        # above (see module docstring). Must run before the stale-exit check
        # below — a trade at e.g. -6% after 60h would otherwise fall through
        # stale_exit's lower bound and return None, completely unguarded.
        # Measured as giveback from the trade's own peak profit, not loss from
        # zero (see module docstring and _update_peak_profit) — subsumes both
        # "losing trade keeps sliding" and "winning trade giving it back".
        if self.grey_zone_exit_enabled:
            risk_multiplier = self._active_risk_multiplier(pair, current_time)
            gz_start_hours = self._param_for_pair(pair, "grey_zone_exit_start_hours")
            start_hours = (
                self._param_for_pair(pair, "stale_exit_hours") if gz_start_hours is None else gz_start_hours
            )
            if trade_hours >= start_hours:
                threshold = self._grey_zone_threshold(pair, trade_hours, risk_multiplier)
                giveback = peak_total_profit_ratio - total_profit_ratio
                if giveback >= threshold:
                    tag = f"grey_zone_exit_{threshold:.3f}"
                    if risk_multiplier != 1.0:
                        tag += f"_adj{risk_multiplier:g}"
                    if peak_total_profit_ratio > 0:
                        tag += f"_pk{peak_total_profit_ratio:.3f}"
                    return tag

        if not self.stale_exit_enabled:
            return None
        stale_hours = self._param_for_pair(pair, "stale_exit_hours")
        if trade_hours < stale_hours:
            return None
        # Two-sided band: only a genuinely flat/dead trade counts as "stale".
        # A trade beyond stale_exit_max_loss on the downside is a real loss,
        # not an idle one — it stays with X7's own DCA/grind ladder (or the
        # catastrophic breaker above) instead of being swept up by the timer
        # regardless of how long it's been open. See CHANGELOG.md 2026-08-27.
        stale_max_loss = self._param_for_pair(pair, "stale_exit_max_loss")
        stale_band = self._param_for_pair(pair, "stale_exit_profit_band")
        if -stale_max_loss <= total_profit_ratio < stale_band:
            return f"stale_exit_{stale_hours:g}h"
        return None

    @staticmethod
    def _pair_base(pair: str) -> str:
        # "ADA/USDT:USDT" -> "ADA"; tolerate a bare "ADA" too.
        return pair.split("/", 1)[0].strip().upper()

    _PEAK_PROFIT_CUSTOM_DATA_KEY = "grey_zone_peak_total_profit_ratio"

    def _update_peak_profit(self, trade: Any, total_profit_ratio: float) -> float:
        """
        Track the highest total_profit_ratio a trade has ever reached,
        persisted via trade.set_custom_data so it survives bot restarts.
        Clamped at a floor of 0.0: a trade that has never been profitable
        gets peak == 0, making giveback-from-peak identical to today's
        loss-from-zero comparison for that trade — only a trade that
        actually went positive gets a peak above zero, and therefore the new
        giveback coverage in _grey_zone_threshold's caller.
        """
        stored_peak = trade.get_custom_data(self._PEAK_PROFIT_CUSTOM_DATA_KEY, default=0.0)
        peak = max(stored_peak, total_profit_ratio, 0.0)
        if peak > stored_peak:
            trade.set_custom_data(self._PEAK_PROFIT_CUSTOM_DATA_KEY, peak)
        return peak

    def _grey_zone_full_hours(self, pair: str, risk_multiplier: float = 1.0) -> float:
        """
        Trade age at which the grey-zone threshold reaches its tight end
        (grey_zone_exit_floor_ratio). Higher assumed cascade rate -> smaller
        H -> the curve tightens sooner. risk_multiplier scales the pair's
        measured cascade rate (see _active_risk_multiplier / risk_adjustments)
        and is the ONLY place that multiplier enters the exit math.
        """
        table = self.GREY_ZONE_CASCADE_PCT_72H
        cascade_pct = table.get(self._pair_base(pair), table["*"]) * risk_multiplier
        cascade_pct = max(cascade_pct, 0.01)  # guard against /0 and absurd H
        ref_hours = self._param_for_pair(pair, "grey_zone_exit_ref_hours")
        ref_cascade_pct = self._param_for_pair(pair, "grey_zone_exit_ref_cascade_pct")
        pair_sensitivity = self._param_for_pair(pair, "grey_zone_exit_pair_sensitivity")
        full_hours = ref_hours * (ref_cascade_pct / cascade_pct) ** pair_sensitivity
        return min(
            max(full_hours, self._param_for_pair(pair, "grey_zone_exit_min_full_hours")),
            self._param_for_pair(pair, "grey_zone_exit_max_full_hours"),
        )

    def _grey_zone_threshold(self, pair: str, trade_hours: float, risk_multiplier: float = 1.0) -> float:
        """
        Effective force-exit drawdown threshold as a positive magnitude, in
        [floor, catastrophic_exit_loss_ratio]. Monotonically non-increasing
        in trade_hours. Compare as
        `(peak_total_profit_ratio - total_profit_ratio) >= threshold`, i.e.
        against giveback from the trade's own peak profit (see
        _update_peak_profit), not against zero.
        """
        gz_start_hours = self._param_for_pair(pair, "grey_zone_exit_start_hours")
        start_hours = self._param_for_pair(pair, "stale_exit_hours") if gz_start_hours is None else gz_start_hours
        ceiling = self._param_for_pair(pair, "catastrophic_exit_loss_ratio")
        gz_floor_ratio = self._param_for_pair(pair, "grey_zone_exit_floor_ratio")
        floor = self._param_for_pair(pair, "stale_exit_max_loss") if gz_floor_ratio is None else gz_floor_ratio
        full_hours = self._grey_zone_full_hours(pair, risk_multiplier)

        if full_hours <= start_hours or floor >= ceiling:
            return ceiling  # misconfigured -> inert, identical to today's behavior

        progress = (trade_hours - start_hours) / (full_hours - start_hours)
        progress = min(max(progress, 0.0), 1.0)
        shaped = progress ** self._param_for_pair(pair, "grey_zone_exit_curve_exponent")
        effective = ceiling - (ceiling - floor) * shaped
        return min(max(effective, floor), ceiling)

    def _active_risk_multiplier(self, pair: str, current_time: datetime) -> float:
        """
        Bounded, self-expiring operator override lookup. Pair-specific entry
        takes precedence over "*"; an EXPIRED pair-specific entry must fall
        through to an active "*" entry rather than short-circuit (a naive
        `self.risk_adjustments.get(pair) or self.risk_adjustments.get("*")`
        would pick the expired pair entry and stop there). Pure read, no
        mutation — the instant current_time passes expires_at this reverts
        to 1.0 on its own, with no cleanup job anywhere.
        """
        for key in (pair, "*"):
            adjustment = self.risk_adjustments.get(key)
            if adjustment is None:
                continue
            expires_at = adjustment.get("expires_at")
            if expires_at is None or current_time < expires_at:
                return adjustment["multiplier"]
        return 1.0

    # Position adjustment
    # -------------------------------------------------------------------------
    def adjust_trade_position(
        self,
        trade: Any,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        min_stake: Optional[float],
        max_stake: float,
        current_entry_rate: float,
        current_exit_rate: float,
        current_entry_profit: float,
        current_exit_profit: float,
        **kwargs,
    ):
        result = super().adjust_trade_position(
            trade,
            current_time,
            current_rate,
            current_profit,
            min_stake,
            max_stake,
            current_entry_rate,
            current_exit_rate,
            current_entry_profit,
            current_exit_profit,
            **kwargs,
        )
        if not self.block_regrind_after_derisk or result is None:
            return result

        stake, tag = (result, None) if isinstance(result, (int, float)) else (result[0], result[1])
        if stake is not None and stake > 0 and tag and "grind" in tag and self._has_derisked(trade):
            log.info(
                "Strategy control: blocked re-grind entry %r on %s after a prior de-risk reduction",
                tag,
                trade.pair,
            )
            return None
        return result

    @staticmethod
    def _signal_ids(signal_params: dict, prefix: str) -> frozenset:
        # "long_entry_condition_8_enable" -> "8". Built from the full
        # baseline dict (not just currently-enabled ids) so the guard still
        # recognizes a tag id even if that condition is toggled off later.
        ids = set()
        for key in signal_params:
            if key.startswith(prefix) and key.endswith("_enable"):
                ids.add(key[len(prefix):-len("_enable")])
        return frozenset(ids)

    @staticmethod
    def _has_derisked(trade: Any) -> bool:
        return any((getattr(order, "ft_order_tag", None) or "").startswith("derisk_level") for order in trade.orders)

    # Bot loop
    # -------------------------------------------------------------------------
    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        if self.config["runmode"].value in ("live", "dry_run"):
            self._apply_strategy_control()
            self._apply_green_streak_cache(current_time)
            self._apply_pair_params_cache(current_time)
            self._apply_drift_flags(current_time)
        return super().bot_loop_start(current_time, **kwargs)

    # Strategy control
    # -------------------------------------------------------------------------
    def _apply_strategy_control(self) -> None:
        changed, control = self._control_file.poll()
        if not changed:
            return
        if control is None:
            log.info("Strategy control: no control file, using config baseline")
            control = {}

        desired_long = self._overridden_params(
            self._baseline_long_params,
            control.get("long_signals_override"),
            "long_entry_condition_{}_enable",
        )
        desired_short = self._overridden_params(
            self._baseline_short_params,
            control.get("short_signals_override"),
            "short_entry_condition_{}_enable",
        )
        self._log_param_changes("long", self.long_entry_signal_params, desired_long)
        self._log_param_changes("short", self.short_entry_signal_params, desired_short)
        # Mutate in place: populate_entry_trend reads these dicts on every call.
        self.long_entry_signal_params.update(desired_long)
        self.short_entry_signal_params.update(desired_short)

        guard = control.get("ema200_guard_enabled", True)
        if not isinstance(guard, bool):
            log.warning("Strategy control: ema200_guard_enabled must be a boolean, got %r", guard)
        elif guard != self.ema200_guard_enabled:
            log.info("Strategy control: EMA200 guard %s", "enabled" if guard else "DISABLED")
            self.ema200_guard_enabled = guard

        momentum = control.get("momentum_entry_enabled", False)
        if not isinstance(momentum, bool):
            log.warning("Strategy control: momentum_entry_enabled must be a boolean, got %r", momentum)
        elif momentum != self.momentum_entry_enabled:
            log.info("Strategy control: momentum entry (900) %s", "enabled" if momentum else "DISABLED")
            self.momentum_entry_enabled = momentum

        money_weight_on = control.get("money_weight_enabled", True)
        if not isinstance(money_weight_on, bool):
            log.warning("Strategy control: money_weight_enabled must be a boolean, got %r", money_weight_on)
        elif money_weight_on != self.money_weight_enabled:
            log.info(
                "Strategy control: per-pair money_weight stake sizing %s",
                "enabled" if money_weight_on else "DISABLED",
            )
            self.money_weight_enabled = money_weight_on

        default_n = control.get("green_streak_default_n", 4)
        if not isinstance(default_n, int) or isinstance(default_n, bool) or default_n < 1:
            log.warning("Strategy control: green_streak_default_n must be a positive int, got %r", default_n)
        elif default_n != self.green_streak_default_n:
            log.info("Strategy control: green_streak_default_n %s -> %s", self.green_streak_default_n, default_n)
            self.green_streak_default_n = default_n

        streak_ttl = control.get("green_streak_ttl_hours", 168.0)
        if not isinstance(streak_ttl, (int, float)) or isinstance(streak_ttl, bool) or streak_ttl <= 0:
            log.warning("Strategy control: green_streak_ttl_hours must be a positive number, got %r", streak_ttl)
        elif streak_ttl != self.green_streak_ttl_hours:
            log.info("Strategy control: green_streak_ttl_hours %s -> %s", self.green_streak_ttl_hours, streak_ttl)
            self.green_streak_ttl_hours = streak_ttl

        drift_block = control.get("drift_block_entries_enabled", True)
        if not isinstance(drift_block, bool):
            log.warning("Strategy control: drift_block_entries_enabled must be a boolean, got %r", drift_block)
        elif drift_block != self.drift_block_entries_enabled:
            log.info(
                "Strategy control: concept-drift entry block %s",
                "enabled" if drift_block else "DISABLED",
            )
            self.drift_block_entries_enabled = drift_block

        self._apply_stale_exit_control(control)
        self._apply_catastrophic_exit_control(control)
        self._apply_signal_guards_control(control)
        self._apply_grey_zone_exit_control(control)
        self._apply_risk_adjustments_control(control)
        self._apply_pair_blocks_control(control)
        self._apply_unbanned_pairs_control(control)

        regrind_guard = control.get("block_regrind_after_derisk", True)
        if not isinstance(regrind_guard, bool):
            log.warning(
                "Strategy control: block_regrind_after_derisk must be a boolean, got %r", regrind_guard
            )
        elif regrind_guard != self.block_regrind_after_derisk:
            log.info(
                "Strategy control: block-regrind-after-derisk %s",
                "enabled" if regrind_guard else "DISABLED",
            )
            self.block_regrind_after_derisk = regrind_guard

    def _apply_green_streak_cache(self, current_time: datetime) -> None:
        # Separate file from strategy_control.json (green_streak_cache.json)
        # since it's written by an external analysis job (MCP tool), not
        # hand-edited like the control file. Each entry:
        # {"n": int, "computed_at": ISO8601, "ttl_hours": float (optional,
        # defaults to green_streak_ttl_hours)}. A missing/expired/malformed
        # entry falls back to green_streak_default_n for that pair — never
        # blocks momentum entry outright, just widens/narrows its gate.
        changed, cache = self._green_streak_cache_file.poll()
        if not changed:
            return
        if cache is None:
            cache = {}

        resolved: dict[str, int] = {}
        for pair, entry in cache.items():
            if not isinstance(entry, dict):
                continue
            n = entry.get("n")
            if not isinstance(n, int) or isinstance(n, bool) or n < 1:
                log.warning("Green-streak cache: bad n for %s: %r", pair, n)
                continue
            ttl_hours = entry.get("ttl_hours", self.green_streak_ttl_hours)
            try:
                ttl_hours = float(ttl_hours)
            except (TypeError, ValueError):
                ttl_hours = self.green_streak_ttl_hours
            computed_at_raw = entry.get("computed_at")
            try:
                computed_at = datetime.fromisoformat(str(computed_at_raw).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                log.warning("Green-streak cache: bad computed_at for %s: %r", pair, computed_at_raw)
                continue
            age_hours = (current_time - computed_at).total_seconds() / 3600.0
            if age_hours > ttl_hours:
                log.info(
                    "Green-streak cache: %s is stale (%.1fh old, ttl %.1fh) — using default N=%s",
                    pair,
                    age_hours,
                    ttl_hours,
                    self.green_streak_default_n,
                )
                continue
            resolved[pair] = n

        if resolved != self._green_streak_n_by_pair:
            log.info(
                "Green-streak cache: resolved N for %d pair(s) (default N=%s for the rest): %s",
                len(resolved),
                self.green_streak_default_n,
                resolved,
            )
        self._green_streak_n_by_pair = resolved

    def _apply_pair_params_cache(self, current_time: datetime) -> None:
        # pair_strategy_params.json, written by an external calibration job
        # (see nfi_mcp_server's calibrate_pair_params tool), not hand-edited.
        # Shape: {pair: {param_name: {"value": number, "computed_at": ISO8601,
        # "ttl_hours": float}}}. Only keys in PAIR_PARAM_SPECS are honored; a
        # missing/expired/malformed entry falls back to the plain self.X
        # value for that pair at lookup time (see _param_for_pair) — this
        # file only ever narrows/widens those knobs per pair, it never blocks
        # anything outright and a bad file just means "no per-pair overrides
        # yet", same failure mode as green_streak_cache.json.
        changed, cache = self._pair_params_cache_file.poll()
        if not changed:
            return
        if cache is None:
            cache = {}

        resolved: dict[str, dict[str, float]] = {}
        for pair, params in cache.items():
            if not isinstance(params, dict):
                continue
            resolved_for_pair: dict[str, float] = {}
            for key, entry in params.items():
                spec = PAIR_PARAM_SPECS.get(key)
                if spec is None or not isinstance(entry, dict):
                    continue
                allow_none, min_value = spec
                value = entry.get("value")
                if value is None and allow_none:
                    resolved_for_pair[key] = None
                    continue
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or (min_value is not None and value <= min_value)
                ):
                    log.warning("Pair-params cache: bad %s for %s: %r", key, pair, value)
                    continue
                ttl_hours = entry.get("ttl_hours", self.green_streak_ttl_hours)
                try:
                    ttl_hours = float(ttl_hours)
                except (TypeError, ValueError):
                    ttl_hours = self.green_streak_ttl_hours
                computed_at_raw = entry.get("computed_at")
                try:
                    computed_at = datetime.fromisoformat(str(computed_at_raw).replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    log.warning("Pair-params cache: bad computed_at for %s/%s: %r", pair, key, computed_at_raw)
                    continue
                age_hours = (current_time - computed_at).total_seconds() / 3600.0
                if age_hours > ttl_hours:
                    continue
                resolved_for_pair[key] = value
            if resolved_for_pair:
                resolved[pair] = resolved_for_pair

        if resolved != self._pair_params_by_pair:
            log.info(
                "Pair-params cache: resolved overrides for %d pair(s): %s",
                len(resolved),
                sorted(resolved),
            )
        self._pair_params_by_pair = resolved

    def _param_for_pair(self, pair: str, key: str) -> Any:
        """
        Effective value of a PAIR_PARAM_SPECS-listed knob for `pair`: the
        cached per-pair override if one is fresh, else the plain self.<key>
        value (itself hot-reloadable via strategy_control.json / config).
        Callers must only pass keys listed in PAIR_PARAM_SPECS.

        A pair with an active concept-drift flag (see _apply_drift_flags)
        never gets its cached override here, regardless of
        drift_block_entries_enabled — a calibration nobody currently trusts
        should not keep steering exits/guards just because entries are still
        allowed. This is the automatic fail-safe half of the adaptive-control
        loop; entry blocking (confirm_trade_entry) is the other half.
        """
        overrides = None if pair in self._drift_flagged_pairs else self._pair_params_by_pair.get(pair)
        if overrides is not None and key in overrides:
            return overrides[key]
        return getattr(self, key)

    def _apply_drift_flags(self, current_time: datetime) -> None:
        # pair_drift_flags.json, written by nfi_mcp_server's flag_pair_drift /
        # auto_flag_if_drifted tools (concept-drift detection — see
        # pair_param_calibration.py's module docstring for the full
        # adaptive-control loop: calibrate from a long window, periodically
        # re-check a short recent window, flag on divergence). Shape:
        # {pair: {"flagged_at": ISO8601, "score": float, "reason": str,
        # "ttl_hours": float}}. A missing/malformed file just means "no pairs
        # flagged", same failure mode as every other cache here — this file
        # only ever narrows trust, it never widens it.
        changed, flags = self._pair_drift_flags_file.poll()
        if not changed:
            return
        if flags is None:
            flags = {}

        resolved: dict[str, dict] = {}
        for pair, entry in flags.items():
            if not isinstance(entry, dict):
                continue
            ttl_hours = entry.get("ttl_hours", 72.0)
            try:
                ttl_hours = float(ttl_hours)
            except (TypeError, ValueError):
                log.warning("Drift flags: bad ttl_hours for %s: %r", pair, entry.get("ttl_hours"))
                continue
            flagged_at_raw = entry.get("flagged_at")
            try:
                flagged_at = datetime.fromisoformat(str(flagged_at_raw).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                log.warning("Drift flags: bad flagged_at for %s: %r", pair, flagged_at_raw)
                continue
            age_hours = (current_time - flagged_at).total_seconds() / 3600.0
            if age_hours > ttl_hours:
                continue
            resolved[pair] = {
                "reason": entry.get("reason", ""),
                "score": entry.get("score"),
                "age_hours": round(age_hours, 1),
            }

        if resolved != self._drift_flagged_pairs:
            log.info("Drift flags: %d pair(s) currently flagged: %s", len(resolved), sorted(resolved))
        self._drift_flagged_pairs = resolved

    def _apply_catastrophic_exit_control(self, control: dict) -> None:
        enabled = control.get("catastrophic_exit_enabled", True)
        ratio = control.get("catastrophic_exit_loss_ratio", 0.20)
        min_hours = control.get("catastrophic_exit_min_hours", 2.0)

        if not isinstance(enabled, bool):
            log.warning("Strategy control: catastrophic_exit_enabled must be a boolean, got %r", enabled)
            enabled = self.catastrophic_exit_enabled
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or ratio <= 0:
            log.warning(
                "Strategy control: catastrophic_exit_loss_ratio must be a positive number, got %r", ratio
            )
            ratio = self.catastrophic_exit_loss_ratio
        if not isinstance(min_hours, (int, float)) or isinstance(min_hours, bool) or min_hours < 0:
            log.warning(
                "Strategy control: catastrophic_exit_min_hours must be a non-negative number, got %r", min_hours
            )
            min_hours = self.catastrophic_exit_min_hours

        if enabled != self.catastrophic_exit_enabled:
            log.info("Strategy control: catastrophic-loss exit %s", "enabled" if enabled else "DISABLED")
            self.catastrophic_exit_enabled = enabled
        if ratio != self.catastrophic_exit_loss_ratio:
            log.info(
                "Strategy control: catastrophic_exit_loss_ratio %s -> %s",
                self.catastrophic_exit_loss_ratio,
                ratio,
            )
            self.catastrophic_exit_loss_ratio = ratio
        if min_hours != self.catastrophic_exit_min_hours:
            log.info(
                "Strategy control: catastrophic_exit_min_hours %s -> %s",
                self.catastrophic_exit_min_hours,
                min_hours,
            )
            self.catastrophic_exit_min_hours = min_hours

    def _apply_stale_exit_control(self, control: dict) -> None:
        enabled = control.get("stale_exit_enabled", True)
        hours = control.get("stale_exit_hours", 6.0)
        band = control.get("stale_exit_profit_band", 0.01)
        max_loss = control.get("stale_exit_max_loss", 0.015)

        if not isinstance(enabled, bool):
            log.warning("Strategy control: stale_exit_enabled must be a boolean, got %r", enabled)
            enabled = self.stale_exit_enabled
        if not isinstance(hours, (int, float)) or isinstance(hours, bool) or hours <= 0:
            log.warning("Strategy control: stale_exit_hours must be a positive number, got %r", hours)
            hours = self.stale_exit_hours
        if not isinstance(band, (int, float)) or isinstance(band, bool) or band < 0:
            log.warning("Strategy control: stale_exit_profit_band must be a non-negative number, got %r", band)
            band = self.stale_exit_profit_band
        if not isinstance(max_loss, (int, float)) or isinstance(max_loss, bool) or max_loss < 0:
            log.warning("Strategy control: stale_exit_max_loss must be a non-negative number, got %r", max_loss)
            max_loss = self.stale_exit_max_loss

        if enabled != self.stale_exit_enabled:
            log.info("Strategy control: stale-trade exit %s", "enabled" if enabled else "DISABLED")
            self.stale_exit_enabled = enabled
        if hours != self.stale_exit_hours:
            log.info("Strategy control: stale_exit_hours %s -> %s", self.stale_exit_hours, hours)
            self.stale_exit_hours = hours
        if band != self.stale_exit_profit_band:
            log.info("Strategy control: stale_exit_profit_band %s -> %s", self.stale_exit_profit_band, band)
            self.stale_exit_profit_band = band
        if max_loss != self.stale_exit_max_loss:
            log.info("Strategy control: stale_exit_max_loss %s -> %s", self.stale_exit_max_loss, max_loss)
            self.stale_exit_max_loss = max_loss

    def _apply_signal_guards_control(self, control: dict) -> None:
        bool_keys = (
            "conflicting_signal_guard_enabled",
            "correlated_loss_guard_enabled",
            "entry_rate_limit_enabled",
            "signal_666_volume_spike_enabled",
            "signal_666_funding_confirm_enabled",
            "funding_settlement_buffer_enabled",
            "falling_knife_guard_enabled",
            "momentum_entry_rsi_guard_enabled",
        )
        for key in bool_keys:
            value = control.get(key, getattr(self, key))
            if not isinstance(value, bool):
                log.warning("Strategy control: %s must be a boolean, got %r", key, value)
                continue
            if value != getattr(self, key):
                log.info("Strategy control: %s -> %s", key, "enabled" if value else "DISABLED")
                setattr(self, key, value)

        numeric_keys = (
            "correlated_loss_guard_min_losing",
            "correlated_loss_guard_loss_threshold",
            "entry_rate_limit_window_hours",
            "entry_rate_limit_max_entries",
            "signal_666_volume_spike_mult",
            "signal_666_min_funding_rate",
            "funding_settlement_buffer_minutes",
            "falling_knife_lookback_candles",
            "falling_knife_drop_pct",
            "momentum_entry_rsi_overbought",
        )
        for key in numeric_keys:
            default = getattr(self, key)
            value = control.get(key, default)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                log.warning("Strategy control: %s must be a number, got %r", key, value)
                continue
            if value != default:
                log.info("Strategy control: %s %s -> %s", key, default, value)
                setattr(self, key, value)

    def _apply_grey_zone_exit_control(self, control: dict) -> None:
        bool_keys = ("grey_zone_exit_enabled",)
        for key in bool_keys:
            value = control.get(key, getattr(self, key))
            if not isinstance(value, bool):
                log.warning("Strategy control: %s must be a boolean, got %r", key, value)
                continue
            if value != getattr(self, key):
                log.info("Strategy control: %s -> %s", key, "enabled" if value else "DISABLED")
                setattr(self, key, value)

        # None is a valid value for these two -> late-bind to
        # stale_exit_hours / stale_exit_max_loss at call time.
        nullable_positive_keys = ("grey_zone_exit_start_hours", "grey_zone_exit_floor_ratio")
        for key in nullable_positive_keys:
            default = getattr(self, key)
            value = control.get(key, default)
            if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0):
                log.warning("Strategy control: %s must be null or a positive number, got %r", key, value)
                continue
            if value != default:
                log.info("Strategy control: %s %s -> %s", key, default, value)
                setattr(self, key, value)

        positive_numeric_keys = (
            "grey_zone_exit_ref_hours",
            "grey_zone_exit_ref_cascade_pct",
            "grey_zone_exit_min_full_hours",
            "grey_zone_exit_max_full_hours",
            "grey_zone_exit_curve_exponent",
        )
        for key in positive_numeric_keys:
            default = getattr(self, key)
            value = control.get(key, default)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                log.warning("Strategy control: %s must be a positive number, got %r", key, value)
                continue
            if value != default:
                log.info("Strategy control: %s %s -> %s", key, default, value)
                setattr(self, key, value)

        # 0.0 is meaningful here (disables per-pair differentiation), so it
        # gets its own non-negative check rather than joining the tuple above.
        key = "grey_zone_exit_pair_sensitivity"
        default = getattr(self, key)
        value = control.get(key, default)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            log.warning("Strategy control: %s must be a non-negative number, got %r", key, value)
        elif value != default:
            log.info("Strategy control: %s %s -> %s", key, default, value)
            setattr(self, key, value)

        if self.grey_zone_exit_min_full_hours > self.grey_zone_exit_max_full_hours:
            log.warning(
                "Strategy control: grey_zone_exit_min_full_hours (%s) > grey_zone_exit_max_full_hours (%s)",
                self.grey_zone_exit_min_full_hours,
                self.grey_zone_exit_max_full_hours,
            )
        resolved_floor = self.stale_exit_max_loss if self.grey_zone_exit_floor_ratio is None else self.grey_zone_exit_floor_ratio
        if resolved_floor >= self.catastrophic_exit_loss_ratio:
            log.warning(
                "Strategy control: grey-zone floor (%s) >= catastrophic_exit_loss_ratio (%s) -> grey-zone exit is inert",
                resolved_floor,
                self.catastrophic_exit_loss_ratio,
            )

    def _apply_risk_adjustments_control(self, control: dict) -> None:
        normalized = self._normalize_risk_adjustments(control.get("risk_adjustments", {}))
        if normalized == self.risk_adjustments:
            return
        for key, adjustment in normalized.items():
            if self.risk_adjustments.get(key) != adjustment:
                log.info(
                    "Strategy control: risk adjustment %s -> x%.2f until %s (%s)",
                    key,
                    adjustment["multiplier"],
                    adjustment.get("expires_at") or "never",
                    adjustment.get("reason", ""),
                )
        for key in self.risk_adjustments:
            if key not in normalized:
                log.info("Strategy control: risk adjustment %s cleared", key)
        self.risk_adjustments = normalized  # REBIND, never mutate in place

    def _apply_pair_blocks_control(self, control: dict) -> None:
        normalized = self._normalize_pair_blocks(control.get("pair_blocks", {}))
        if normalized == self.pair_blocks:
            return
        now = datetime.now(timezone.utc)
        for pair, block in normalized.items():
            if self.pair_blocks.get(pair) != block:
                state = "scheduled for" if block["effective_from"] > now else "active from"
                log.info(
                    "Strategy control: pair block %s %s %s until %s (%s)",
                    pair,
                    state,
                    block["effective_from"].isoformat(timespec="seconds"),
                    block["expires_at"].isoformat(timespec="seconds") if block["expires_at"] else "never",
                    block.get("reason", ""),
                )
        for pair in self.pair_blocks:
            if pair not in normalized:
                log.info("Strategy control: pair block %s cleared", pair)
        self.pair_blocks = normalized  # REBIND, never mutate in place

    def _apply_unbanned_pairs_control(self, control: dict) -> None:
        normalized = self._normalize_unbanned_pairs(control.get("unbanned_pairs", {}))
        if normalized == self.unbanned_pairs:
            return
        for pair, entry in normalized.items():
            old = self.unbanned_pairs.get(pair)
            if old != entry:
                was_shadow = old is None or (old["risk_budget_pct"] <= 0 and old["risk_budget_abs"] <= 0)
                now_shadow = entry["risk_budget_pct"] <= 0 and entry["risk_budget_abs"] <= 0
                if was_shadow and not now_shadow:
                    log.info(
                        "Strategy control: %s GRADUATED from shadow mode — risk budget %s%% / %s abs (%s)",
                        pair, entry["risk_budget_pct"], entry["risk_budget_abs"], entry.get("reason", ""),
                    )
                elif not was_shadow and now_shadow:
                    log.info("Strategy control: %s reverted to shadow mode (%s)", pair, entry.get("reason", ""))
                else:
                    log.info("Strategy control: unbanned_pairs[%s] updated -> %s", pair, entry)
        for pair in self.unbanned_pairs:
            if pair not in normalized:
                log.info("Strategy control: unbanned_pairs[%s] cleared", pair)
        self.unbanned_pairs = normalized  # REBIND, never mutate in place

    @staticmethod
    def _normalize_unbanned_pairs(raw: Any) -> dict:
        """
        Validate/parse a raw unbanned_pairs dict into {pair: {"unbanned_at":
        str, "risk_budget_pct": float, "risk_budget_abs": float, "reason":
        str}}. A malformed entry is warned about and skipped, never
        discarding the whole dict. Both budget fields default to 0.0
        (shadow mode) if absent or invalid, rather than skipping the entry —
        failing closed into shadow mode (no real orders) is the safe
        direction for a parse error here, unlike pair_blocks/risk_adjustments
        where skipping the entry is the safe direction.
        """
        if not isinstance(raw, dict):
            if raw:
                log.warning("Strategy control: unbanned_pairs must be an object, got %r", raw)
            return {}

        def _budget(key: str, entry: dict, field: str) -> float:
            value = entry.get(field, 0.0)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value != value:
                log.warning(
                    "Strategy control: unbanned_pairs[%r].%s must be a number, got %r — treating as 0 (shadow)",
                    key, field, value,
                )
                return 0.0
            return max(float(value), 0.0)

        normalized: dict = {}
        for key, entry in raw.items():
            if not isinstance(key, str) or not key:
                log.warning("Strategy control: unbanned_pairs key must be a non-empty string, got %r", key)
                continue
            if not isinstance(entry, dict):
                log.warning("Strategy control: unbanned_pairs[%r] must be an object, got %r", key, entry)
                continue
            normalized[key] = {
                "unbanned_at": entry.get("unbanned_at"),
                "risk_budget_pct": _budget(key, entry, "risk_budget_pct"),
                "risk_budget_abs": _budget(key, entry, "risk_budget_abs"),
                "reason": entry.get("reason", ""),
            }
        return normalized

    @staticmethod
    def _normalize_pair_blocks(raw: Any) -> dict:
        """
        Validate/parse a raw pair_blocks dict (from strategy_control.json or
        a backtest config) into {pair: {"effective_from": datetime,
        "expires_at": datetime | None, ...passthrough}}. A malformed ENTRY
        is warned about and skipped; it never discards the whole dict. An
        unparseable effective_from/expires_at fails closed (entry skipped).
        Unlike risk_adjustments, expires_at=None is a normal, expected state
        here (an open-ended/structural block), not backtest-only — no
        warning for it.
        """
        if not isinstance(raw, dict):
            if raw:
                log.warning("Strategy control: pair_blocks must be an object, got %r", raw)
            return {}

        def _parse_ts(key: str, field: str, value: Any):
            if not isinstance(value, str):
                log.warning(
                    "Strategy control: pair_blocks[%r].%s must be an ISO-8601 string, got %r — skipping entry",
                    key, field, value,
                )
                return "invalid"
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                log.warning(
                    "Strategy control: pair_blocks[%r].%s %r is not parseable ISO-8601 — skipping entry",
                    key, field, value,
                )
                return "invalid"
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed

        normalized: dict = {}
        for key, entry in raw.items():
            if not isinstance(key, str) or not key:
                log.warning("Strategy control: pair_blocks key must be a non-empty string, got %r", key)
                continue
            if not isinstance(entry, dict):
                log.warning("Strategy control: pair_blocks[%r] must be an object, got %r", key, entry)
                continue

            effective_from = _parse_ts(key, "effective_from", entry.get("effective_from"))
            if effective_from == "invalid":
                continue

            expires_raw = entry.get("expires_at")
            if expires_raw is None:
                expires_at = None
            else:
                expires_at = _parse_ts(key, "expires_at", expires_raw)
                if expires_at == "invalid":
                    continue

            normalized[key] = {
                "effective_from": effective_from,
                "expires_at": expires_at,
                "reason": entry.get("reason", ""),
                "created_at": entry.get("created_at"),
            }
        return normalized

    @staticmethod
    def _overridden_params(baseline: dict, overrides: Any, key_template: str) -> dict:
        desired = dict(baseline)
        if overrides is None:
            return desired
        if not isinstance(overrides, dict):
            log.warning("Strategy control: signal overrides must be an object, got %r", overrides)
            return desired
        for signal_id, enabled in overrides.items():
            key = key_template.format(signal_id)
            if key not in desired:
                log.warning("Strategy control: unknown signal id %r, ignored", signal_id)
                continue
            if not isinstance(enabled, bool):
                log.warning("Strategy control: value for signal %r must be a boolean, got %r", signal_id, enabled)
                continue
            desired[key] = enabled
        return desired

    @staticmethod
    def _log_param_changes(side: str, current: dict, desired: dict) -> None:
        for key, value in desired.items():
            if current.get(key) != value:
                log.info(
                    "Strategy control: %s signal %s -> %s",
                    side,
                    key,
                    "enabled" if value else "DISABLED",
                )

    @staticmethod
    def _normalize_risk_adjustments(raw: Any) -> dict:
        """
        Validate/parse a raw risk_adjustments dict (from strategy_control.json
        or a backtest config) into {pair_or_"*": {"multiplier": float,
        "expires_at": datetime | None, ...passthrough fields}}. A malformed
        ENTRY is warned about and skipped; it never discards the whole dict.
        An unparseable expires_at fails closed (entry skipped), never
        silently treated as "never expires".
        """
        if not isinstance(raw, dict):
            if raw:
                log.warning("Strategy control: risk_adjustments must be an object, got %r", raw)
            return {}

        normalized: dict = {}
        for key, entry in raw.items():
            if not isinstance(key, str) or not key:
                log.warning("Strategy control: risk_adjustments key must be a non-empty string, got %r", key)
                continue
            if not isinstance(entry, dict):
                log.warning("Strategy control: risk_adjustments[%r] must be an object, got %r", key, entry)
                continue

            multiplier = entry.get("multiplier")
            if (
                not isinstance(multiplier, (int, float))
                or isinstance(multiplier, bool)
                or multiplier != multiplier  # NaN
                or multiplier in (float("inf"), float("-inf"))
                or multiplier <= 0
            ):
                log.warning(
                    "Strategy control: risk_adjustments[%r].multiplier must be a positive finite number, got %r",
                    key,
                    multiplier,
                )
                continue
            multiplier = min(max(multiplier, 0.25), 4.0)

            expires_raw = entry.get("expires_at")
            expires_at = None
            if expires_raw is not None:
                if not isinstance(expires_raw, str):
                    log.warning(
                        "Strategy control: risk_adjustments[%r].expires_at must be an ISO-8601 string or null, got %r — skipping entry",
                        key,
                        expires_raw,
                    )
                    continue
                try:
                    expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
                except ValueError:
                    log.warning(
                        "Strategy control: risk_adjustments[%r].expires_at %r is not parseable ISO-8601 — skipping entry",
                        key,
                        expires_raw,
                    )
                    continue
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
            else:
                log.warning(
                    "Strategy control: risk_adjustments[%r] has no expiry — it will never lapse; "
                    "this is intended for backtest configs only",
                    key,
                )

            normalized[key] = {
                "multiplier": multiplier,
                "expires_at": expires_at,
                "reason": entry.get("reason", ""),
                "set_at": entry.get("set_at"),
                "ttl_hours": entry.get("ttl_hours"),
            }
        return normalized
