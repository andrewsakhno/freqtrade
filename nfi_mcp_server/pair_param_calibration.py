"""
Full per-pair calibration engine for every PAIR_PARAM_SPECS knob in
NostalgiaForInfinityX7EMA200, plus concept-drift detection/flagging on top
of it (adaptive-control / self-tuning pattern: calibrate once from a long
window, then periodically re-check a short recent window against the
calibration snapshot and fail safe - or hand off to an LLM caller - if the
pair's behavior has moved).

Pure-Python (no pandas/numpy), same minimal-dependency philosophy as
green_streak.py: fetch OHLCV + funding history from Binance's public
futures REST API (no key needed) and compute everything with plain lists.
Reuses green_streak's kline pagination/symbol-mapping helpers rather than
duplicating them.

Two file outputs on the server host (same "external job writes, strategy
polls" idiom as green_streak_cache.json / pair_strategy_params.json):
  - PAIR_PARAMS_PATH: {pair: {key: {"value","computed_at","ttl_hours",
    "source"}}} - same shape seed_pair_params.py originally seeded, so a
    calibrated entry silently supersedes the seed (_apply_pair_params_cache
    doesn't care which producer wrote it).
  - DRIFT_PROFILE_PATH: {pair: {snapshot metrics + computed_at + lookback}}
    - the "fingerprint" a later detect_pair_drift() call compares a fresh
      short-window measurement against.
  - DRIFT_FLAGS_PATH: {pair: {"flagged_at","score","reason","ttl_hours"}} -
    written only by flag_pair_drift (explicit, journaled), read by the
    strategy's _apply_drift_flags (see NostalgiaForInfinityX7EMA200 module
    docstring). A flagged pair's calibrated overrides are ignored (falls
    back to plain global defaults) and - if
    drift_block_entries_enabled (default True) - new entries on it are
    blocked, exactly like a pair_blocks entry, until the flag is cleared or
    expires. Existing open trades are never touched by a flag, same
    principle as every other guard in this codebase (only entries gate).

Formulas below are a first calibration pass, not a hand-tuned final answer
- same caveat green_streak.py states for its own N default. Every derived
value is clamped to the corresponding PAIR_PARAM_SPECS bound (duplicated
here rather than imported, same reasoning as signal_control.py's
_FALLBACK_DEFAULTS: importing the strategy module would pull in
pandas/talib just to read two dicts).
"""

import datetime
import json
import math
import os
import threading
import time
from typing import Optional

import requests

from .green_streak import _fetch_klines, _to_binance_symbol

PAIR_PARAMS_PATH = "/opt/nfi/user_data/pair_strategy_params.json"
PAIR_PARAMS_JOURNAL_PATH = "/opt/nfi/user_data/pair_params_calibration_log.jsonl"
DRIFT_PROFILE_PATH = "/opt/nfi/user_data/pair_calibration_profiles.json"
DRIFT_FLAGS_PATH = "/opt/nfi/user_data/pair_drift_flags.json"
DRIFT_JOURNAL_PATH = "/opt/nfi/user_data/concept_drift_log.jsonl"

BINANCE_FAPI_FUNDING_RATE = "https://fapi.binance.com/fapi/v1/fundingRate"

DEFAULT_LOOKBACK_DAYS = 90
DEFAULT_TIMEFRAME = "1h"
DEFAULT_TTL_HOURS = 168.0  # 1 week, matches every other cache in this package
LEVERAGE = 3.0  # matches the strategy's futures_mode_leverage
CASCADE_HORIZONS_H = (6, 24, 72)
REFERENCE_SYMBOL = "BTCUSDT"  # volatility/frequency normalization baseline

DRIFT_RECHECK_LOOKBACK_DAYS = 10  # short recent window vs the 90d calibration window
DRIFT_METRIC_WEIGHTS = {
    "daily_vol_pct": 1.0,
    "cascade_rate_72h": 1.0,
    "corr_with_btc": 0.5,
    "mean_abs_funding": 0.5,
    "p75_volume_ratio": 0.5,
}
DRIFT_SCORE_THRESHOLD = 0.5  # weighted mean relative deviation to flag drift
DRIFT_FLAG_TTL_HOURS = 72.0  # auto-expires; a stale flag is worse than none
DRIFT_FLAG_MAX_ENTRIES = 50

# (allow_none, min_value_exclusive) - MUST mirror NostalgiaForInfinityX7EMA200's
# PAIR_PARAM_SPECS exactly; only used to clamp calibration output, never to
# validate what the strategy itself accepts (that check lives server-side).
PAIR_PARAM_BOUNDS: dict[str, tuple[bool, Optional[float]]] = {
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
}

FALLING_KNIFE_LOOKBACK_CANDLES = 4  # fixed/structural, matches the strategy's global default

_lock = threading.Lock()
_reference_lock = threading.Lock()
_reference_cache: Optional[dict] = None


class CalibrationError(RuntimeError):
    pass


# --- pure-python stats -------------------------------------------------------

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _percentile(xs: list[float], pct: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * (pct / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    n = min(len(xs), len(ys))
    if n < 10:
        return None
    xs, ys = xs[-n:], ys[-n:]
    mx, my = _mean(xs), _mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return cov / (sx * sy)


def _clamp(value: float, lo: Optional[float], hi: Optional[float]) -> float:
    if lo is not None:
        value = max(value, lo)
    if hi is not None:
        value = min(value, hi)
    return value


def _enforce_bound(key: str, value: float) -> float:
    """Nudge a computed value strictly past PAIR_PARAM_BOUNDS' exclusive
    min_value if it landed exactly on (or under) it - mirrors the strategy's
    own `value <= min_value` rejection in _apply_pair_params_cache, but we'd
    rather nudge than silently drop a whole key from calibration output."""
    _, min_value = PAIR_PARAM_BOUNDS[key]
    if min_value is not None and value <= min_value:
        value = min_value + max(abs(min_value), 1.0) * 1e-6 + 1e-9
    return value


# --- data fetch ---------------------------------------------------------------

def _fetch_funding_rates(symbol: str, days: float, limit_total: int = 1000) -> list[dict]:
    start_ms = int((time.time() - days * 86400) * 1000)
    try:
        resp = requests.get(
            BINANCE_FAPI_FUNDING_RATE,
            params={"symbol": symbol, "startTime": start_ms, "limit": limit_total},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise CalibrationError(f"funding-rate fetch failed for {symbol}: {exc}") from exc
    return data if isinstance(data, list) else []


# --- pair profile ---------------------------------------------------------------

def _cascade_episodes(closes: list[float], floor: float, ceiling: float, leverage: float,
                       horizons: tuple[int, ...]) -> dict:
    """
    Rolling-peak drawdown study (same methodology as the original 8-pair
    grey-zone calibration, see NostalgiaForInfinityX7EMA200's module
    docstring): every time the leveraged drawdown-from-peak crosses `floor`
    it starts a "grey zone" episode; the episode resolves as cascaded (hits
    `ceiling` before recovering - elapsed candle count recorded), recovered
    (drops back under `floor` first), or censored (data runs out mid-episode
    - excluded from any horizon it didn't survive long enough to resolve for,
    to avoid survivorship bias toward "no cascade").
    """
    horizons = tuple(sorted(horizons))
    max_h = horizons[-1]
    denom = {h: 0 for h in horizons}
    cascaded = {h: 0 for h in horizons}
    n = len(closes)
    if n == 0:
        return {"cascade_rate": {h: None for h in horizons}, "episode_counts": denom}

    peak = closes[0]
    in_episode = False
    # Set once an episode resolves (cascaded or timed out) while the price is
    # STILL at/above floor from the same still-elevated peak - without this,
    # `peak` never drops back down, so the very next candle immediately
    # re-triggers "not in_episode and lev_dd >= floor" and opens a brand-new
    # one-candle episode that instantly cascades again, turning one sustained
    # drawdown regime into hundreds of double-counted "episodes" (observed:
    # cascade_rate_72h ~99% for pairs that simply never made a new high
    # during the lookback window). Cleared only once lev_dd actually recovers
    # under `floor`, so a single regime counts once.
    waiting_for_recovery = False
    episode_start = 0
    for i in range(n):
        peak = max(peak, closes[i])
        lev_dd = (peak - closes[i]) / peak * leverage if peak > 0 else 0.0

        if waiting_for_recovery:
            if lev_dd < floor:
                waiting_for_recovery = False
            else:
                continue

        if not in_episode and lev_dd >= floor:
            in_episode = True
            episode_start = i
        if not in_episode:
            continue
        elapsed = i - episode_start
        if lev_dd >= ceiling:
            for h in horizons:
                denom[h] += 1
                if elapsed <= h:
                    cascaded[h] += 1
            in_episode = False
            waiting_for_recovery = True
        elif lev_dd < floor:
            for h in horizons:
                denom[h] += 1
            in_episode = False
        elif i == n - 1:
            for h in horizons:
                if elapsed >= h:
                    denom[h] += 1
            in_episode = False
        elif elapsed >= max_h:
            for h in horizons:
                denom[h] += 1
            in_episode = False
            waiting_for_recovery = True

    rates = {h: (cascaded[h] / denom[h] if denom[h] else None) for h in horizons}
    return {"cascade_rate": rates, "episode_counts": denom}


def _compute_profile(klines: list[list], funding_rows: list[dict],
                      floor: float = 0.015, ceiling: float = 0.20) -> dict:
    """Pure computation from raw kline rows + funding rows -> the stats
    every _derive_* formula reads. No I/O."""
    closes = [float(k[4]) for k in klines]
    opens = [float(k[1]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    volumes = [float(k[5]) for k in klines]
    n = len(closes)

    returns = [closes[i] / closes[i - 1] - 1.0 for i in range(1, n)] if n > 1 else []
    hourly_vol = _stdev(returns)
    daily_vol_pct = hourly_vol * math.sqrt(24.0)

    tr_pcts = []
    for i in range(n):
        prev_close = closes[i - 1] if i > 0 else opens[i]
        tr = max(highs[i] - lows[i], abs(highs[i] - prev_close), abs(lows[i] - prev_close))
        if closes[i] > 0:
            tr_pcts.append(tr / closes[i])
    atr_pct = _mean(tr_pcts)

    cascade = _cascade_episodes(closes, floor, ceiling, LEVERAGE, CASCADE_HORIZONS_H)

    volume_ratios = []
    for i in range(20, n):
        window = volumes[i - 20:i]
        avg = _mean(window)
        if avg > 0:
            volume_ratios.append(volumes[i] / avg)
    p75_volume_ratio = _percentile(volume_ratios, 75.0) if volume_ratios else 1.5

    funding_rates = [float(r.get("fundingRate", 0.0)) for r in funding_rows]
    mean_abs_funding = _mean([abs(f) for f in funding_rates]) if funding_rates else 0.0

    funding_times_ms = {int(r.get("fundingTime", 0)) // 3_600_000 for r in funding_rows}
    settlement_returns, other_returns = [], []
    for i in range(1, n):
        candle_hour = int(klines[i][0]) // 3_600_000
        r = abs(returns[i - 1])
        (settlement_returns if candle_hour in funding_times_ms else other_returns).append(r)
    settlement_vol_ratio = (
        (_mean(settlement_returns) / _mean(other_returns))
        if settlement_returns and other_returns and _mean(other_returns) > 0
        else 1.0
    )

    return {
        "n_candles": n,
        "hourly_vol": hourly_vol,
        "atr_pct": atr_pct,
        "daily_vol_pct": daily_vol_pct,
        "cascade_rate": cascade["cascade_rate"],
        "cascade_episode_counts": cascade["episode_counts"],
        "p75_volume_ratio": p75_volume_ratio,
        "mean_abs_funding": mean_abs_funding,
        "settlement_vol_ratio": settlement_vol_ratio,
        "returns": returns,  # kept for corr_with_btc; stripped before persisting
        "closes": closes,  # kept for falling-knife threshold; stripped before persisting
    }


def _reference_profile(lookback_days: int) -> dict:
    """BTC's own profile, fetched once per process and cached - every other
    pair's volatility/frequency knobs are calibrated RELATIVE to this
    baseline (mirrors how GREY_ZONE_CASCADE_PCT_72H already treats BTC as
    the calmest reference point)."""
    global _reference_cache
    with _reference_lock:
        if _reference_cache is None or _reference_cache.get("lookback_days") != lookback_days:
            klines = _fetch_klines(REFERENCE_SYMBOL, DEFAULT_TIMEFRAME, lookback_days * 24 + 30)
            funding = _fetch_funding_rates(REFERENCE_SYMBOL, lookback_days)
            profile = _compute_profile(klines, funding)
            profile["lookback_days"] = lookback_days
            _reference_cache = profile
        return _reference_cache


# --- derivation: profile -> all 20 PAIR_PARAM_SPECS values -------------------

def _fit_sensitivity(cascade_rate: dict[int, Optional[float]]) -> tuple[float, float]:
    """Fit grey_zone_exit_pair_sensitivity from this pair's OWN cascade curve
    convexity (solve for s such that the pair's 24h point matches the
    full_hours formula anchored at its own 72h point): s =
    ln(24/72) / ln(cascade_72/cascade_24). Falls back to the strategy's
    global default (3.0) when either point is missing/degenerate (too few
    episodes, or risk not actually rising with time for this pair).
    Returns (sensitivity, curve_exponent) - curve_exponent shares the same
    convexity signal, scaled into its own typical range."""
    c72 = cascade_rate.get(72)
    c24 = cascade_rate.get(24)
    default_s = 3.0
    if not c72 or not c24 or c72 <= c24:
        s = default_s
    else:
        try:
            s = math.log(24.0 / 72.0) / math.log(c72 / c24)
        except (ValueError, ZeroDivisionError):
            s = default_s
        if not math.isfinite(s):
            s = default_s
    s = _clamp(s, 1.0, 6.0)
    curve_exponent = _clamp(s * 1.5, 2.0, 8.0)
    return s, curve_exponent


def _falling_knife_drop_pct(closes: list[float], lookback_candles: int) -> float:
    """
    97.5th percentile of this pair's own N-candle (1h) drawdown distribution
    - "how big a drop over N hours is unusually extreme for THIS pair", not a
    flat threshold across every pair. Falls back to the strategy's global
    default (10.0) when there's too little history to measure a percentile
    meaningfully.
    """
    drops = []
    for i in range(lookback_candles, len(closes)):
        prior = closes[i - lookback_candles]
        if prior > 0:
            drops.append((prior - closes[i]) / prior * 100.0)
    if len(drops) < 30:
        return 10.0
    return _clamp(_percentile(drops, 97.5), 3.0, 40.0)


def _derive_params(profile: dict, ref_profile: dict, corr_with_btc: Optional[float]) -> dict[str, float]:
    """The formula bank: every PAIR_PARAM_SPECS key, each documented with the
    single statistic driving it. All ratios are computed against BTC's own
    profile so a single pair can be calibrated in isolation (no whole-basket
    batch pass required) while still being meaningfully differentiated."""
    vol_ratio = profile["daily_vol_pct"] / ref_profile["daily_vol_pct"] if ref_profile["daily_vol_pct"] > 0 else 1.0
    vol_ratio = _clamp(vol_ratio, 0.2, 5.0)  # guard against a near-zero-history pair producing an absurd ratio

    cascade = profile["cascade_rate"]
    c72 = cascade.get(72)
    sensitivity, curve_exponent = _fit_sensitivity(cascade)

    corr = _clamp(corr_with_btc if corr_with_btc is not None else 0.5, 0.0, 1.0)

    # -- volatility-driven family: how patient/tolerant this pair's exits are --
    stale_exit_hours = _clamp(6.0 / math.sqrt(vol_ratio), 3.0, 48.0)
    stale_exit_profit_band = _clamp(profile["atr_pct"] * LEVERAGE * 0.5, 0.003, 0.03)
    stale_exit_max_loss = _clamp(profile["daily_vol_pct"] * LEVERAGE * 0.3, 0.008, 0.04)
    correlated_loss_guard_loss_threshold = -_clamp(profile["daily_vol_pct"] * LEVERAGE * 0.2, 0.005, 0.03)

    # -- tail-risk family: catastrophic circuit breaker --
    # 95th-percentile leveraged drawdown actually observed among this pair's
    # own grey-zone episodes stands in for a measured tail-loss ratio; falls
    # back to the class default when there's too little episode history.
    n_episodes_72h = profile["cascade_episode_counts"].get(72, 0)
    if n_episodes_72h >= 5 and c72 is not None:
        # c72 is a 0..1 fraction; grey_zone_exit_ref_cascade_pct's own 20.7
        # baseline is in percent (see its computation a few lines below,
        # `round(c72 * 100.0, 2)`) - c72 must be scaled the same way before
        # comparing against it, or the ratio is crushed to ~0 regardless of
        # input.
        catastrophic_exit_loss_ratio = _clamp(0.20 * (0.6 + 0.8 * (c72 * 100.0 / 20.7)), 0.10, 0.35)
    else:
        catastrophic_exit_loss_ratio = 0.20
    catastrophic_exit_min_hours = _clamp(2.0 * vol_ratio, 1.0, 8.0)

    # -- correlated-loss / entry-rate family: how "noisy"/systemic this pair is --
    correlated_loss_guard_min_losing = round(_clamp(2 + (1.0 - corr) * 3.0, 2, 6))
    freq_ratio = vol_ratio  # big-move frequency tracks volatility closely enough to reuse the same ratio
    entry_rate_limit_window_hours = _clamp(6.0 * math.sqrt(freq_ratio), 2.0, 24.0)
    entry_rate_limit_max_entries = round(_clamp(2.0 / math.sqrt(freq_ratio), 1, 4))

    # -- signal #666 confirmation family --
    signal_666_volume_spike_mult = _clamp(profile["p75_volume_ratio"], 1.1, 3.0)
    signal_666_min_funding_rate = _clamp(profile["mean_abs_funding"] * 0.5, 0.00002, 0.001)
    funding_settlement_buffer_minutes = _clamp(20.0 * profile["settlement_vol_ratio"], 5.0, 60.0)

    # -- grey-zone decay curve family --
    # ref_hours/ref_cascade_pct here are THIS pair's own measured 72h anchor
    # (independent of, and may differ from, the separate hardcoded
    # GREY_ZONE_CASCADE_PCT_72H lookup table baked into the strategy file -
    # this calibration job does not touch that table).
    grey_zone_exit_ref_hours = 72.0
    grey_zone_exit_ref_cascade_pct = round(c72 * 100.0, 2) if (c72 is not None and n_episodes_72h >= 5) else 20.7
    grey_zone_exit_min_full_hours = _clamp(24.0 / math.sqrt(vol_ratio), 12.0, 48.0)
    grey_zone_exit_max_full_hours = _clamp(168.0 / math.sqrt(vol_ratio), 72.0, 336.0)
    # explicit rather than left None, per PAIR_PARAM_SPECS's allow_none
    # late-bind - mirrors stale_exit_hours/stale_exit_max_loss exactly so
    # behavior at calibration time is unchanged either way.
    grey_zone_exit_start_hours = stale_exit_hours
    grey_zone_exit_floor_ratio = stale_exit_max_loss

    # -- falling-knife entry guard family --
    falling_knife_lookback_candles = float(FALLING_KNIFE_LOOKBACK_CANDLES)
    falling_knife_drop_pct = _falling_knife_drop_pct(
        profile.get("closes", []), FALLING_KNIFE_LOOKBACK_CANDLES
    )

    values = {
        "stale_exit_hours": stale_exit_hours,
        "stale_exit_profit_band": stale_exit_profit_band,
        "stale_exit_max_loss": stale_exit_max_loss,
        "catastrophic_exit_loss_ratio": catastrophic_exit_loss_ratio,
        "catastrophic_exit_min_hours": catastrophic_exit_min_hours,
        "correlated_loss_guard_loss_threshold": correlated_loss_guard_loss_threshold,
        "correlated_loss_guard_min_losing": correlated_loss_guard_min_losing,
        "entry_rate_limit_window_hours": entry_rate_limit_window_hours,
        "entry_rate_limit_max_entries": entry_rate_limit_max_entries,
        "signal_666_volume_spike_mult": signal_666_volume_spike_mult,
        "signal_666_min_funding_rate": signal_666_min_funding_rate,
        "funding_settlement_buffer_minutes": funding_settlement_buffer_minutes,
        "grey_zone_exit_start_hours": grey_zone_exit_start_hours,
        "grey_zone_exit_floor_ratio": grey_zone_exit_floor_ratio,
        "grey_zone_exit_ref_hours": grey_zone_exit_ref_hours,
        "grey_zone_exit_ref_cascade_pct": grey_zone_exit_ref_cascade_pct,
        "grey_zone_exit_min_full_hours": grey_zone_exit_min_full_hours,
        "grey_zone_exit_max_full_hours": grey_zone_exit_max_full_hours,
        "grey_zone_exit_curve_exponent": curve_exponent,
        "grey_zone_exit_pair_sensitivity": sensitivity,
        "falling_knife_lookback_candles": falling_knife_lookback_candles,
        "falling_knife_drop_pct": falling_knife_drop_pct,
    }
    return {k: round(_enforce_bound(k, v), 6) for k, v in values.items()}


# --- public: calibration --------------------------------------------------------

def analyze_pair(pair: str, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict:
    """Full read-only analysis: fetch, compute profile, derive all 20
    values. No file writes - see compute_and_cache for the writing half."""
    symbol = _to_binance_symbol(pair)
    klines = _fetch_klines(symbol, DEFAULT_TIMEFRAME, lookback_days * 24 + 30)
    if len(klines) < 24 * 7:
        raise CalibrationError(f"only {len(klines)} candles returned for {symbol} - too little history to calibrate")
    funding = _fetch_funding_rates(symbol, lookback_days)
    profile = _compute_profile(klines, funding)

    ref_profile = _reference_profile(lookback_days) if symbol != REFERENCE_SYMBOL else profile
    corr = None if symbol == REFERENCE_SYMBOL else _pearson(profile["returns"], ref_profile["returns"])

    params = _derive_params(profile, ref_profile, corr)
    profile_public = {k: v for k, v in profile.items() if k not in ("returns", "closes")}
    return {
        "pair": pair,
        "symbol": symbol,
        "lookback_days": lookback_days,
        "profile": profile_public,
        "corr_with_btc": round(corr, 4) if corr is not None else None,
        "params": params,
    }


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _save_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _journal_append(path: str, entry: dict) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _profile_snapshot(profile: dict, corr: Optional[float], lookback_days: int) -> dict:
    """The small subset of `profile` worth persisting as a drift baseline -
    everything DRIFT_METRIC_WEIGHTS compares against later."""
    return {
        "computed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "lookback_days": lookback_days,
        "daily_vol_pct": profile["daily_vol_pct"],
        "cascade_rate_72h": profile["cascade_rate"].get(72),
        "cascade_episodes_72h": profile["cascade_episode_counts"].get(72),
        "corr_with_btc": corr,
        "mean_abs_funding": profile["mean_abs_funding"],
        "p75_volume_ratio": profile["p75_volume_ratio"],
    }


def compute_and_cache(pair: str, lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                       ttl_hours: float = DEFAULT_TTL_HOURS, reason: str = "") -> dict:
    """
    Run analyze_pair and write every one of its 20 derived values into
    pair_strategy_params.json (source="calibrated", same entry shape the
    seed used), plus a profile snapshot into pair_calibration_profiles.json
    for later detect_pair_drift calls to compare against. The bot's next
    bot_loop_start poll (within ~20-55s) picks up the new values - no
    reload, no restart.
    """
    analysis = analyze_pair(pair, lookback_days=lookback_days)
    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.isoformat(timespec="seconds")

    with _lock:
        params_cache = _load_json(PAIR_PARAMS_PATH)
        before = params_cache.get(pair)
        entry = {
            key: {"value": value, "computed_at": now_iso, "ttl_hours": ttl_hours, "source": "calibrated"}
            for key, value in analysis["params"].items()
        }
        # Merge, don't replace: other producers (e.g. pair_priority's
        # money_weight) write different keys into the same per-pair dict, and
        # a full pair_cache[pair] = entry here would silently wipe them.
        params_cache.setdefault(pair, {}).update(entry)
        _save_json(PAIR_PARAMS_PATH, params_cache)
        _journal_append(PAIR_PARAMS_JOURNAL_PATH, {
            "ts": now_iso, "pair": pair, "lookback_days": lookback_days,
            "before": before, "after": entry, "reason": reason,
        })

        profiles = _load_json(DRIFT_PROFILE_PATH)
        profiles[pair] = _profile_snapshot(analysis["profile"], analysis["corr_with_btc"], lookback_days)
        _save_json(DRIFT_PROFILE_PATH, profiles)

    return analysis


def compute_and_cache_all(pairs: list[str], lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                           ttl_hours: float = DEFAULT_TTL_HOURS, reason: str = "",
                           polite_delay_s: float = 0.25) -> dict:
    """Bulk driver: calibrate every pair sequentially (Binance's public
    endpoints have no key, so a per-call delay is just courtesy, not
    auth-required throttling), collecting per-pair errors instead of
    aborting the whole batch on one bad symbol."""
    ok, failed = {}, {}
    for i, pair in enumerate(pairs):
        try:
            result = compute_and_cache(pair, lookback_days=lookback_days, ttl_hours=ttl_hours, reason=reason)
            ok[pair] = {"params": result["params"]}
        except CalibrationError as exc:
            failed[pair] = str(exc)
        if polite_delay_s and i < len(pairs) - 1:
            time.sleep(polite_delay_s)
    return {"calibrated": len(ok), "failed": failed, "ok_pairs": list(ok)}


def write_pair_params(entries_by_pair: dict[str, dict[str, float]], ttl_hours: float = DEFAULT_TTL_HOURS,
                       source: str = "priority", reason: str = "") -> None:
    """
    Generic merge-write into PAIR_PARAMS_PATH for producers other than this
    module's own compute_and_cache (e.g. pair_priority's money_weight) - same
    file, same {"value","computed_at","ttl_hours","source"} entry shape,
    merged per pair (never a full pair_cache[pair] = ... replace) so two
    producers writing different PAIR_PARAM_SPECS keys for the same pair never
    clobber each other.
    """
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with _lock:
        params_cache = _load_json(PAIR_PARAMS_PATH)
        before = {pair: params_cache.get(pair) for pair in entries_by_pair}
        for pair, kv in entries_by_pair.items():
            entry = {
                key: {"value": value, "computed_at": now_iso, "ttl_hours": ttl_hours, "source": source}
                for key, value in kv.items()
            }
            params_cache.setdefault(pair, {}).update(entry)
        _save_json(PAIR_PARAMS_PATH, params_cache)
        _journal_append(PAIR_PARAMS_JOURNAL_PATH, {
            "ts": now_iso, "pairs": sorted(entries_by_pair), "source": source, "reason": reason, "before": before,
        })


def get_profile_snapshots() -> dict:
    """Read-only: every pair's persisted calibration profile snapshot (see
    _profile_snapshot), keyed by pair - the data pair_quality.classify_whitelist
    consumes without needing to know about DRIFT_PROFILE_PATH itself."""
    return _load_json(DRIFT_PROFILE_PATH)


def get_calibration_status(pair: str) -> Optional[dict]:
    """Read-only: this pair's current pair_strategy_params.json entries plus
    the drift-profile snapshot they were calibrated from, or None if never
    calibrated (still on the seed/global default)."""
    params_cache = _load_json(PAIR_PARAMS_PATH)
    entry = params_cache.get(pair)
    if entry is None:
        return None
    profiles = _load_json(DRIFT_PROFILE_PATH)
    return {"pair": pair, "params": entry, "profile_snapshot": profiles.get(pair)}


# --- public: concept drift detection / flagging ---------------------------------

def detect_pair_drift(pair: str, lookback_days: int = DRIFT_RECHECK_LOOKBACK_DAYS) -> dict:
    """
    Read-only: re-measure a SHORT recent window (default 10d, vs the 90d
    calibration window) and compare it against this pair's calibration
    snapshot in pair_calibration_profiles.json. Per-metric relative
    deviation, weighted by DRIFT_METRIC_WEIGHTS, into one drift score;
    drifted=True once the score crosses DRIFT_SCORE_THRESHOLD (0.5, i.e. the
    weighted-average metric has moved by half its calibrated value).

    Does not write anything - call flag_pair_drift separately (or pass
    auto_flag=True there, which calls this internally) to actually gate the
    strategy on the result. Returns has_baseline=False if the pair was never
    calibrated - there is nothing to drift-detect against yet.
    """
    profiles = _load_json(DRIFT_PROFILE_PATH)
    baseline = profiles.get(pair)
    if baseline is None:
        return {"pair": pair, "has_baseline": False}

    symbol = _to_binance_symbol(pair)
    klines = _fetch_klines(symbol, DEFAULT_TIMEFRAME, lookback_days * 24 + 30)
    if len(klines) < 24 * 3:
        return {"pair": pair, "has_baseline": True, "error": f"only {len(klines)} recent candles available"}
    funding = _fetch_funding_rates(symbol, lookback_days)
    current = _compute_profile(klines, funding)

    ref_profile = _reference_profile(DEFAULT_LOOKBACK_DAYS) if symbol != REFERENCE_SYMBOL else current
    corr = None if symbol == REFERENCE_SYMBOL else _pearson(current["returns"], ref_profile["returns"])

    current_metrics = {
        "daily_vol_pct": current["daily_vol_pct"],
        "cascade_rate_72h": current["cascade_rate"].get(72),
        "corr_with_btc": corr,
        "mean_abs_funding": current["mean_abs_funding"],
        "p75_volume_ratio": current["p75_volume_ratio"],
    }

    deviations = {}
    weighted_sum, weight_total = 0.0, 0.0
    for metric, weight in DRIFT_METRIC_WEIGHTS.items():
        base_val = baseline.get(metric)
        cur_val = current_metrics.get(metric)
        if base_val is None or cur_val is None:
            continue
        denom = max(abs(base_val), 1e-9)
        rel_dev = abs(cur_val - base_val) / denom
        deviations[metric] = {"baseline": base_val, "current": round(cur_val, 6), "relative_deviation": round(rel_dev, 4)}
        weighted_sum += rel_dev * weight
        weight_total += weight
    score = (weighted_sum / weight_total) if weight_total > 0 else 0.0

    return {
        "pair": pair,
        "has_baseline": True,
        "baseline_computed_at": baseline.get("computed_at"),
        "baseline_lookback_days": baseline.get("lookback_days"),
        "recheck_lookback_days": lookback_days,
        "deviations": deviations,
        "drift_score": round(score, 4),
        "drifted": score >= DRIFT_SCORE_THRESHOLD,
        "threshold": DRIFT_SCORE_THRESHOLD,
    }


def _load_flags() -> dict:
    return _load_json(DRIFT_FLAGS_PATH)


def get_drift_flags() -> dict:
    """Read-only: every currently-tracked drift flag, bucketed into active
    vs expired (mirrors signal_control.py's _pair_blocks_state pattern)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    active, expired = {}, {}
    for pair, entry in _load_flags().items():
        if not isinstance(entry, dict):
            continue
        flagged_at = entry.get("flagged_at")
        ttl_hours = entry.get("ttl_hours", DRIFT_FLAG_TTL_HOURS)
        row = dict(entry)
        try:
            flagged_dt = datetime.datetime.fromisoformat(str(flagged_at).replace("Z", "+00:00"))
            age_hours = (now - flagged_dt).total_seconds() / 3600.0
            row["age_hours"] = round(age_hours, 1)
            bucket = expired if age_hours > ttl_hours else active
        except (TypeError, ValueError):
            bucket = expired
        bucket[pair] = row
    return {"active": active, "expired": expired}


def flag_pair_drift(pair: str, reason: str, score: Optional[float] = None,
                     ttl_hours: float = DRIFT_FLAG_TTL_HOURS) -> dict:
    """
    Write an active drift flag for `pair` into pair_drift_flags.json. The
    strategy's _apply_drift_flags (see NostalgiaForInfinityX7EMA200) picks
    this up on its next bot_loop_start poll: the pair's calibrated
    pair_strategy_params.json overrides are ignored (falls back to global
    defaults - the automatic fail-safe half of the pattern) and, if
    drift_block_entries_enabled is on (default), new entries on the pair are
    blocked entirely until the flag clears or expires (TTL, default 72h -
    same self-expiring idiom as risk_adjustments/pair_blocks). Existing open
    trades are never touched.

    This is the manual/explicit half of the adaptive-control loop: call
    detect_pair_drift first (or use auto_flag_if_drifted below) and decide
    whether the deviation is worth reacting to - flagging is not automatic
    just because a score crossed the threshold.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise CalibrationError("reason is required - it is the audit trail")
    with _lock:
        flags = _load_flags()
        if pair not in flags and len(flags) >= DRIFT_FLAG_MAX_ENTRIES:
            raise CalibrationError(f"too many drift flags ({DRIFT_FLAG_MAX_ENTRIES}); clear one first")
        now = datetime.datetime.now(datetime.timezone.utc)
        before = flags.get(pair)
        flags[pair] = {
            "flagged_at": now.isoformat(timespec="seconds"),
            "score": score,
            "reason": reason.strip(),
            "ttl_hours": ttl_hours,
        }
        _save_json(DRIFT_FLAGS_PATH, flags)
        _journal_append(DRIFT_JOURNAL_PATH, {
            "ts": now.isoformat(timespec="seconds"), "action": "flag", "pair": pair,
            "before": before, "after": flags[pair],
        })
    return {"pair": pair, "flag": flags[pair]}


def clear_pair_drift_flag(pair: str, reason: str) -> dict:
    """Remove a drift flag (active or expired) before/regardless of its own
    TTL - use once a recalibration (compute_pair_params) has been run and
    the pair's numbers can be trusted again."""
    with _lock:
        flags = _load_flags()
        before = flags.pop(pair, None)
        _save_json(DRIFT_FLAGS_PATH, flags)
        now = datetime.datetime.now(datetime.timezone.utc)
        _journal_append(DRIFT_JOURNAL_PATH, {
            "ts": now.isoformat(timespec="seconds"), "action": "clear", "pair": pair,
            "before": before, "after": None, "reason": reason,
        })
    return {"pair": pair, "cleared": before is not None}


def auto_flag_if_drifted(pair: str, lookback_days: int = DRIFT_RECHECK_LOOKBACK_DAYS,
                          ttl_hours: float = DRIFT_FLAG_TTL_HOURS) -> dict:
    """Convenience: detect_pair_drift + flag_pair_drift in one call, only
    writing a flag when the score actually crosses the threshold (mirrors
    compute_green_streak_n's "only write if the result clears its own bar"
    idiom). Use this from a periodic caller (there is no scheduler inside
    this package, by design - see request_risk_analysis's docstring on the
    same point); a human or an LLM agent decides how often "periodic" is."""
    result = detect_pair_drift(pair, lookback_days=lookback_days)
    if not result.get("has_baseline") or result.get("error"):
        return result
    if result["drifted"]:
        reason = (
            f"auto-flagged: drift_score {result['drift_score']} >= threshold "
            f"{result['threshold']} vs baseline from {result['baseline_computed_at']}"
        )
        result["flag"] = flag_pair_drift(pair, reason, score=result["drift_score"], ttl_hours=ttl_hours)["flag"]
    return result
