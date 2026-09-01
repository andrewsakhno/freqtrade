"""
Per-pair money-allocation priority.

Classifies a pair's trading history into a stake-sizing category and the
money_weight that goes with it, meant to be written into
pair_strategy_params.json as a `money_weight` PAIR_PARAM_SPECS entry so
NostalgiaForInfinityX7EMA200's custom_stake_amount picks it up through the
exact same hot-reload path every other per-pair knob already uses
(_apply_pair_params_cache / _param_for_pair) - no new file, no new polling
code needed in the strategy.

Two data sources feed the same classifier by design, matching the project's
two-phase plan:
  - calibration phase: a one-off backtest run (e.g. via the strategy-tester
    skill), aggregated with stats.aggregate_by_pair over two lookback windows.
  - accumulating phase: live/dry-run closed trades, same aggregation, same
    windows, refreshed periodically as real history builds up and eventually
    supersedes the backtest-derived weights.

Categories and their money_weight (fraction of normal stake sizing):
  consistent_winner    (1.0)  - net-positive on both the mid and long window,
                                 enough trades in each to trust it (today's
                                 default behavior - full priority).
  consistent_loser     (0.5)  - net-negative on both the mid and long window.
  volatile_profitable  (0.1)  - has losses, but profit_factor and net P&L say
                                 the wins more than pay for them (e.g. BTW/LAB
                                 in the 2026-08-29 per-pair backtest report -
                                 flagging every pair with any loss as "bad"
                                 would have starved these of stake).
  no_win               (0.0)  - has closed at least one trade and never won.
  insufficient_data    (1.0)  - not enough history in either window to judge;
                                 keep today's behavior until it can be judged
                                 (same "seed = no behavior change" philosophy
                                 pair_strategy_params.json's initial seed used).

consistent_loser and volatile_profitable are explicitly NOT auto-blacklisted
- they stay tradeable at reduced size while `pair_param_calibration`'s
per-pair exit/guard tuning (or a human/LLM decision) refines the strategy for
them, per the project's own framing ("group 2 and 3 are for strategy
refinement, not exclusion").

Pure functions only, same philosophy as pair_quality.py: callers supply
already-aggregated window stats (stats.aggregate_by_pair-shaped dicts), no
I/O in the classifier itself.
"""

from typing import Optional

CONSISTENT_WINNER = "consistent_winner"
CONSISTENT_LOSER = "consistent_loser"
VOLATILE_PROFITABLE = "volatile_profitable"
NO_WIN = "no_win"
INSUFFICIENT_DATA = "insufficient_data"

MONEY_WEIGHT: dict[str, float] = {
    CONSISTENT_WINNER: 1.0,
    CONSISTENT_LOSER: 0.5,
    VOLATILE_PROFITABLE: 0.1,
    NO_WIN: 0.0,
    INSUFFICIENT_DATA: 1.0,
}

MIN_TRADES_FOR_VERDICT = 3
VOLATILE_PROFIT_FACTOR_MIN = 1.5


def _expectancy(stats: dict) -> float:
    trades = stats.get("trades", 0)
    return stats["profit_abs_sum"] / trades if trades else 0.0


def _profit_factor(stats: dict) -> Optional[float]:
    wins_sum = stats.get("profit_wins_abs_sum")
    losses_sum_abs = stats.get("profit_losses_abs_sum")
    if wins_sum is None or losses_sum_abs is None:
        return None
    if losses_sum_abs == 0:
        return float("inf") if wins_sum > 0 else None
    return wins_sum / losses_sum_abs


def classify_pair_priority(mid_stats: Optional[dict], long_stats: Optional[dict]) -> dict:
    """
    mid_stats / long_stats: stats.aggregate_by_pair-shaped dicts for a
    shorter and a longer lookback window covering the same pair (e.g. 90d
    and 180d live, or 3mo/6mo from a backtest) - either may be None/empty if
    that window has no closed trades yet.
    """
    largest = max(
        (s for s in (mid_stats, long_stats) if s),
        key=lambda s: s.get("trades", 0),
        default=None,
    )
    if largest and largest.get("trades", 0) >= 1 and largest.get("wins", 0) == 0:
        return _verdict(NO_WIN, mid_stats, long_stats)

    mid_ok = bool(mid_stats) and mid_stats.get("trades", 0) >= MIN_TRADES_FOR_VERDICT
    long_ok = bool(long_stats) and long_stats.get("trades", 0) >= MIN_TRADES_FOR_VERDICT

    if mid_ok and long_ok:
        mid_exp, long_exp = _expectancy(mid_stats), _expectancy(long_stats)
        if mid_exp > 0 and long_exp > 0:
            return _verdict(CONSISTENT_WINNER, mid_stats, long_stats)
        if mid_exp < 0 and long_exp < 0:
            return _verdict(CONSISTENT_LOSER, mid_stats, long_stats)

    reference = long_stats if long_ok else (mid_stats if mid_ok else None)
    if reference and reference.get("losses", 0) > 0 and reference.get("profit_abs_sum", 0.0) > 0:
        profit_factor = _profit_factor(reference)
        if profit_factor is not None and profit_factor >= VOLATILE_PROFIT_FACTOR_MIN:
            return _verdict(VOLATILE_PROFITABLE, mid_stats, long_stats)

    return _verdict(INSUFFICIENT_DATA, mid_stats, long_stats)


def _verdict(category: str, mid_stats: Optional[dict], long_stats: Optional[dict]) -> dict:
    return {
        "category": category,
        "money_weight": MONEY_WEIGHT[category],
        "mid_window": mid_stats,
        "long_window": long_stats,
    }


def classify_pairs_priority(mid_by_pair: dict[str, dict], long_by_pair: dict[str, dict]) -> dict[str, dict]:
    """Combine classify_pair_priority for every pair seen in either window,
    same union-of-keys pattern as pair_quality.classify_pairs."""
    pairs = sorted(set(mid_by_pair) | set(long_by_pair))
    return {
        pair: classify_pair_priority(mid_by_pair.get(pair), long_by_pair.get(pair))
        for pair in pairs
    }
