"""Pure aggregation of freqtrade trades by entry signal. No I/O — unit-testable."""

from collections import defaultdict
from typing import Any


def _signal_of(trade: dict) -> str:
    tag = trade.get("enter_tag") or "unknown"
    side = "short" if trade.get("is_short") else "long"
    return f"{side}:{tag}"


def aggregate_by_enter_tag(trades: list[dict]) -> dict[str, Any]:
    """
    Aggregate closed trades into per-signal statistics.

    Keys are "<side>:<enter_tag>" (an NFI enter_tag may contain several
    space-separated condition ids; it is kept verbatim so combined signals are
    visible as such).
    """
    buckets: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        buckets[_signal_of(trade)].append(trade)

    signals = {}
    for key, items in buckets.items():
        profits_abs = [t.get("profit_abs") or 0.0 for t in items]
        ratios = [t.get("profit_ratio") or 0.0 for t in items]
        durations = [t.get("trade_duration") or 0 for t in items]  # minutes
        wins = sum(1 for p in profits_abs if p > 0)
        signals[key] = {
            "trades": len(items),
            "wins": wins,
            "losses": len(items) - wins,
            "winrate": round(wins / len(items), 4) if items else 0.0,
            "profit_abs_sum": round(sum(profits_abs), 6),
            "profit_ratio_mean": round(sum(ratios) / len(ratios), 6) if ratios else 0.0,
            "avg_duration_min": round(sum(durations) / len(durations), 1) if durations else 0.0,
            "pairs": sorted({t.get("pair") for t in items if t.get("pair")}),
        }

    total_profit = sum(s["profit_abs_sum"] for s in signals.values())
    return {
        "total_closed_trades": len(trades),
        "total_profit_abs": round(total_profit, 6),
        "signals": dict(
            sorted(signals.items(), key=lambda kv: kv[1]["profit_abs_sum"], reverse=True)
        ),
    }
