"""
Green-streak N calibration for the NFI X7EMA200 momentum entry ("900").

Pure-Python (no pandas/numpy — same minimal-dependency philosophy as the
rest of this package, see signal_control.py/stats.py) analysis: for a pair,
fetch 1h OHLC history from Binance's public futures REST API (no API key
needed — the same public endpoint the bot's own exchange client hits), bucket
historical candles by "how many consecutive green 1h candles ended here",
and for each bucket compute the K-candle-forward-return expectancy
(win_rate*avg_gain - loss_rate*avg_loss). The N with the highest expectancy
(subject to a minimum sample count, to avoid picking a rare high-N streak
that only has a handful of historical occurrences) is written to
green_streak_cache.json for NostalgiaForInfinityX7EMA200 to read on its next
bot_loop_start poll (see that file's _apply_green_streak_cache).

The bot never blocks on a missing/stale cache entry — it falls back to its
own green_streak_default_n class attribute. This module only ever tries to
improve on that default, never gates entries directly.
"""

import datetime
import json
import os
import threading
from typing import Any, Optional

import requests

GREEN_STREAK_CACHE_PATH = "/opt/nfi/user_data/green_streak_cache.json"
GREEN_STREAK_JOURNAL_PATH = "/opt/nfi/user_data/green_streak_log.jsonl"

BINANCE_FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"

DEFAULT_LOOKBACK_DAYS = 90
DEFAULT_TIMEFRAME = "1h"
DEFAULT_K_FORWARD = 24  # candles ahead the forward-return is measured over (24 x 1h = 24h)
DEFAULT_MIN_SAMPLES = 20
DEFAULT_MAX_N = 12
DEFAULT_TTL_HOURS = 168.0  # 1 week — see NostalgiaForInfinityX7EMA200.green_streak_ttl_hours

_lock = threading.Lock()


class GreenStreakError(RuntimeError):
    pass


def _to_binance_symbol(pair: str) -> str:
    # "SOL/USDT:USDT" -> "SOLUSDT"; "BTC/USDT" -> "BTCUSDT"
    base_quote = pair.split(":", 1)[0]
    return base_quote.replace("/", "").upper()


def _fetch_klines(symbol: str, interval: str, limit_total: int) -> list[list]:
    out: list[list] = []
    end_time: Optional[int] = None
    while len(out) < limit_total:
        params = {"symbol": symbol, "interval": interval, "limit": 1500}
        if end_time is not None:
            params["endTime"] = end_time
        try:
            resp = requests.get(BINANCE_FAPI_KLINES, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise GreenStreakError(f"klines fetch failed for {symbol}: {exc}") from exc
        if not isinstance(data, list) or not data:
            break
        out = data + out
        end_time = data[0][0] - 1
        if len(data) < 1500:
            break
    return out[-limit_total:]


def _streak_lengths(klines: list[list]) -> list[int]:
    streak = 0
    out = []
    for k in klines:
        is_green = float(k[4]) > float(k[1])  # close > open
        streak = streak + 1 if is_green else 0
        out.append(streak)
    return out


def compute_optimal_n(
    klines: list[list],
    k_forward: int = DEFAULT_K_FORWARD,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    max_n: int = DEFAULT_MAX_N,
) -> dict:
    """
    Pure computation, no I/O — given raw Binance kline rows (list of
    [open_time, open, high, low, close, volume, ...]), bucket by streak
    length and pick the N with max forward-return expectancy. Returns
    best_n=None if no bucket meets min_samples (caller should keep the
    existing default rather than cache a None).
    """
    closes = [float(k[4]) for k in klines]
    streaks = _streak_lengths(klines)
    n_candles = len(klines)

    per_n_returns: dict[int, list[float]] = {}
    for i in range(n_candles - k_forward):
        s = streaks[i]
        if s < 1 or s > max_n:
            continue
        fwd_ret = closes[i + k_forward] / closes[i] - 1.0
        per_n_returns.setdefault(s, []).append(fwd_ret)

    per_n_stats: dict[str, dict] = {}
    best_n, best_expectancy = None, None
    for candidate_n in range(1, max_n + 1):
        rets = per_n_returns.get(candidate_n, [])
        count = len(rets)
        if count < min_samples:
            per_n_stats[str(candidate_n)] = {"count": count, "insufficient_samples": True}
            continue
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]
        win_rate = len(wins) / count
        loss_rate = len(losses) / count
        avg_gain = sum(wins) / len(wins) if wins else 0.0
        avg_loss = -sum(losses) / len(losses) if losses else 0.0
        expectancy = win_rate * avg_gain - loss_rate * avg_loss
        per_n_stats[str(candidate_n)] = {
            "count": count,
            "win_rate": round(win_rate, 4),
            "avg_gain": round(avg_gain, 6),
            "avg_loss": round(avg_loss, 6),
            "expectancy": round(expectancy, 6),
        }
        if best_expectancy is None or expectancy > best_expectancy:
            best_n, best_expectancy = candidate_n, expectancy

    return {
        "best_n": best_n,
        "best_expectancy": round(best_expectancy, 6) if best_expectancy is not None else None,
        "per_n": per_n_stats,
        "candles_analyzed": n_candles,
    }


def analyze_pair(
    pair: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    timeframe: str = DEFAULT_TIMEFRAME,
    k_forward: int = DEFAULT_K_FORWARD,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    max_n: int = DEFAULT_MAX_N,
) -> dict:
    if timeframe != "1h":
        raise GreenStreakError("only timeframe='1h' is supported for now (matches the strategy's gate)")
    symbol = _to_binance_symbol(pair)
    candles_needed = lookback_days * 24 + k_forward + 10
    klines = _fetch_klines(symbol, timeframe, candles_needed)
    if len(klines) < min_samples + k_forward:
        raise GreenStreakError(
            f"only {len(klines)} candles returned for {symbol} — too little history to analyze"
        )
    result = compute_optimal_n(klines, k_forward=k_forward, min_samples=min_samples, max_n=max_n)
    result.update({
        "pair": pair,
        "symbol": symbol,
        "lookback_days": lookback_days,
        "timeframe": timeframe,
        "k_forward": k_forward,
        "min_samples": min_samples,
        "max_n": max_n,
    })
    return result


def _load_cache() -> dict:
    if not os.path.exists(GREEN_STREAK_CACHE_PATH):
        return {}
    with open(GREEN_STREAK_CACHE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _save_cache(data: dict) -> None:
    tmp = GREEN_STREAK_CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, GREEN_STREAK_CACHE_PATH)


def _journal_append(entry: dict) -> None:
    with open(GREEN_STREAK_JOURNAL_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def get_cached(pair: str) -> Optional[dict]:
    """Read-only: the pair's current cache entry plus computed staleness,
    or None if never analyzed. Does not fall back to the strategy default —
    that fallback lives in the strategy itself; this just reports cache
    state honestly."""
    cache = _load_cache()
    entry = cache.get(pair)
    if entry is None:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    ttl_hours = entry.get("ttl_hours", DEFAULT_TTL_HOURS)
    try:
        computed_at = datetime.datetime.fromisoformat(str(entry.get("computed_at")).replace("Z", "+00:00"))
        age_hours = (now - computed_at).total_seconds() / 3600.0
        stale = age_hours > ttl_hours
    except (TypeError, ValueError):
        age_hours, stale = None, True
    return {**entry, "pair": pair, "age_hours": round(age_hours, 1) if age_hours is not None else None, "stale": stale}


def compute_and_cache(
    pair: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    k_forward: int = DEFAULT_K_FORWARD,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    max_n: int = DEFAULT_MAX_N,
    reason: str = "",
) -> dict:
    """
    Run the analysis and, if a usable N was found, write it into
    green_streak_cache.json (created_at = now, so the strategy's TTL check
    starts fresh from this call). If no N met min_samples, the cache is left
    untouched — the strategy keeps using whatever it already had (or its own
    default), never overwritten with an explicit "no result".
    """
    analysis = analyze_pair(
        pair, lookback_days=lookback_days, timeframe="1h",
        k_forward=k_forward, min_samples=min_samples, max_n=max_n,
    )
    with _lock:
        cache = _load_cache()
        before_entry = cache.get(pair)
        if analysis["best_n"] is not None:
            now = datetime.datetime.now(datetime.timezone.utc)
            cache[pair] = {
                "n": analysis["best_n"],
                "expectancy": analysis["best_expectancy"],
                "computed_at": now.isoformat(timespec="seconds"),
                "ttl_hours": ttl_hours,
                "lookback_days": lookback_days,
                "k_forward": k_forward,
                "candles_analyzed": analysis["candles_analyzed"],
            }
            _save_cache(cache)
            _journal_append({
                "ts": now.isoformat(timespec="seconds"),
                "pair": pair,
                "before": before_entry,
                "after": cache[pair],
                "reason": reason,
            })
    return analysis
