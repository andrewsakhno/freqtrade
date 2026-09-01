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


def aggregate_by_pair(trades: list[dict]) -> dict[str, dict]:
    """
    Aggregate closed trades into per-pair statistics (same shape as each
    aggregate_by_enter_tag bucket, keyed by pair instead of signal). Feeds
    pair_quality.classify_junk_currency - a pair's net P&L across every
    signal is what decides whether it's worth keeping vs blacklisting - and
    pair_priority.classify_pair_priority, which also needs profit_wins_abs_sum/
    profit_losses_abs_sum (profit_factor) alongside the net sum.
    """
    buckets: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        pair = trade.get("pair")
        if pair:
            buckets[pair].append(trade)

    result = {}
    for pair, items in buckets.items():
        profits_abs = [t.get("profit_abs") or 0.0 for t in items]
        ratios = [t.get("profit_ratio") or 0.0 for t in items]
        wins = sum(1 for p in profits_abs if p > 0)
        result[pair] = {
            "trades": len(items),
            "wins": wins,
            "losses": len(items) - wins,
            "winrate": round(wins / len(items), 4) if items else 0.0,
            "profit_abs_sum": round(sum(profits_abs), 6),
            "profit_wins_abs_sum": round(sum(p for p in profits_abs if p > 0), 6),
            "profit_losses_abs_sum": round(-sum(p for p in profits_abs if p < 0), 6),
            "profit_ratio_mean": round(sum(ratios) / len(ratios), 6) if ratios else 0.0,
        }
    return result


def exit_summary(trades: list[dict]) -> dict:
    """
    Per-trade exit summary (pair, signal, exit reason, profit, close time),
    oldest first, plus a net total.

    Reads the same closed-trade list stats_by_enter_tag uses (the freqtrade
    REST API via closed_trades_since), not docker logs - a 24h RPC-log scrape
    over the ssh tunnel to this host is hundreds of thousands of DEBUG lines
    and reliably times out the connection; this answers the same question in
    one small API call.
    """
    ordered = sorted(trades, key=lambda t: t.get("close_timestamp") or 0)
    exits = []
    for t in ordered:
        profit_abs = t.get("profit_abs") or 0.0
        exits.append(
            {
                "trade_id": t.get("trade_id"),
                "pair": t.get("pair"),
                "enter_tag": t.get("enter_tag"),
                "gain": "win" if profit_abs > 0 else "loss",
                "profit_abs": round(profit_abs, 6),
                "profit_ratio": t.get("profit_ratio"),
                "exit_reason": t.get("exit_reason"),
                "close_date": t.get("close_date"),
            }
        )
    return {
        "closed_trades": len(exits),
        "net_profit_abs": round(sum(e["profit_abs"] for e in exits), 6),
        "exits": exits,
    }
