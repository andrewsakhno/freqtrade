"""
Per-pair quality classification: two independent verdicts per pair.

- junk_currency: trade-history verdict (stats.aggregate_by_pair) - a pair
  that has racked up enough closed trades with net non-positive P&L is
  "no particular profit, not worth reverse-engineering a per-pair algorithm
  for" (the user's own framing) - a blacklist candidate.
- whitelist: behavioral verdict (pair_param_calibration's persisted profile
  snapshot) - does this pair's price action actually show the reversal /
  grey-zone cascade signature NFI's signals target, independent of whatever
  its P&L happens to be right now.

Pure functions only - callers supply already-computed data, no I/O here,
same minimal-dependency philosophy as stats.py. Every verdict is Optional[bool]:
None means "not enough data to judge yet", which is a different thing from
False and must not be treated as "clean"/"not junk" by a caller.
"""

from typing import Optional

JUNK_MIN_TRADES = 5
JUNK_MAX_PROFIT_ABS = 0.0

WHITELIST_MIN_EPISODES = 5
WHITELIST_CASCADE_RATE_MIN = 0.05
WHITELIST_CASCADE_RATE_MAX = 0.60
WHITELIST_VOL_RATIO_MIN = 0.5
WHITELIST_VOL_RATIO_MAX = 3.0


def classify_junk_currency(pair_stats: Optional[dict]) -> Optional[bool]:
    """True once a pair has at least JUNK_MIN_TRADES closed trades with net
    non-positive profit_abs_sum. None when there isn't enough trade history
    yet to judge - a brand-new pair must not default to "clean"."""
    if not pair_stats or pair_stats.get("trades", 0) < JUNK_MIN_TRADES:
        return None
    return pair_stats.get("profit_abs_sum", 0.0) <= JUNK_MAX_PROFIT_ABS


def classify_whitelist(
    snapshot: Optional[dict], basket_median_daily_vol_pct: Optional[float]
) -> Optional[bool]:
    """
    True when this pair's calibration profile snapshot shows the
    reversal-pattern signature NFI's grey-zone signals target:
      - enough resolved 72h cascade episodes to trust the rate
        (WHITELIST_MIN_EPISODES, same trustworthy-sample gate
        pair_param_calibration._derive_params uses for grey_zone_exit_ref_cascade_pct)
      - a cascade rate that's neither ~0 (price never dips into the grey
        zone - no reversal setup ever triggers) nor extreme (dips almost
        always cascade to catastrophic - a one-way drop, not a clean
        reversal)
      - volatility that's a representative multiple of the rest of the
        tradable basket (too calm -> no signal to trade; too wild ->
        noise-dominated, indistinguishable pattern)

    None when there's no snapshot yet, or no basket median to compare
    against (needs at least one other calibrated pair).
    """
    if not snapshot or not basket_median_daily_vol_pct or basket_median_daily_vol_pct <= 0:
        return None
    episodes = snapshot.get("cascade_episodes_72h") or 0
    cascade_rate = snapshot.get("cascade_rate_72h")
    daily_vol_pct = snapshot.get("daily_vol_pct")
    if episodes < WHITELIST_MIN_EPISODES or cascade_rate is None or daily_vol_pct is None:
        return None
    vol_ratio = daily_vol_pct / basket_median_daily_vol_pct
    return (
        WHITELIST_CASCADE_RATE_MIN <= cascade_rate <= WHITELIST_CASCADE_RATE_MAX
        and WHITELIST_VOL_RATIO_MIN <= vol_ratio <= WHITELIST_VOL_RATIO_MAX
    )


def _median(xs: list[float]) -> Optional[float]:
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return None
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def classify_pairs(
    pair_stats_by_pair: dict[str, dict], profile_snapshots: dict[str, dict]
) -> dict[str, dict]:
    """
    Combine both classifiers for every pair seen in either input, plus the
    underlying metrics each verdict was based on - a verdict without its
    inputs isn't auditable (same transparency pattern as
    pair_param_calibration.detect_pair_drift's "deviations").
    """
    basket_median = _median(
        [
            s["daily_vol_pct"]
            for s in profile_snapshots.values()
            if isinstance(s, dict) and s.get("daily_vol_pct") is not None
        ]
    )

    pairs = sorted(set(pair_stats_by_pair) | set(profile_snapshots))
    result = {}
    for pair in pairs:
        stats = pair_stats_by_pair.get(pair)
        snapshot = profile_snapshots.get(pair)
        result[pair] = {
            "junk_currency": classify_junk_currency(stats),
            "whitelist": classify_whitelist(snapshot, basket_median),
            "trade_stats": stats,
            "profile_snapshot": snapshot,
        }
    return result
