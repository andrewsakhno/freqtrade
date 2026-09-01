"""
Trade simulator for the momentum-scalp signal (signals.py). Walks the raw
tick (aggTrade) path rather than 1m OHLC bars for trade management, because a
hard trailing stop needs to know whether the high or the low of a candle
happened first - something a 1m bar alone can't tell you, and this signal
lives and dies on exactly that kind of intra-candle detail.

Trade management, matching the user's description:
  - below `activation_pct` favorable move: a hard safety stop only
    (`hard_stop_pct`) - the impulse hasn't proven itself yet.
  - once favorable move >= `activation_pct`: a trailing stop `trail_pct`
    below the running peak (long) / above the running trough (short) takes
    over; the hard safety stop is dropped once trailing is live.
  - regardless of the above, force-exit at `max_candles *
    candle_timeframe_minutes` minutes if neither has fired - "выходят ...
    еще до завершения 6-й свечи" is a hard ceiling, not just a target.

Liquidation-spike is NOT part of the entry condition here (see
data_fetch.py) - trigger is volume-delta z-score + buy/sell-share confluence
from signals.py only. Add the liquidation leg once a live listener has
accumulated its own history.

Tick data is pulled into plain numpy arrays once (`_TickPath`) so both the
per-trade walk and the (72-combo) threshold sweep in sweep_pair.py avoid
pandas' row-by-row overhead - relevant here because the trade loop below
runs once per triggered event per parameter combo.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signals import SignalParams, bucket_agg_trades, compute_triggers


@dataclass
class TradeParams:
    activation_pct: float = 0.005  # 0.5% - matches the low end of the user's 0.5-2% target band
    trail_pct: float = 0.003  # trail distance once activated
    hard_stop_pct: float = 0.01  # safety stop before activation
    candle_timeframe_minutes: int = 1
    max_candles: int = 6
    cooldown_seconds: int = 300  # don't re-trigger the same pair while flat-but-recent, avoids re-entering into the same exhausted impulse
    taker_fee_pct_per_side: float = 0.0004  # 0.04% Binance USDT-M futures taker fee, VIP0; both entry and exit assumed taker (market-speed entries/exits)


@dataclass
class TickPath:
    """Plain-numpy view of aggTrades, built once and reused across an
    entire sweep instead of re-touching the pandas frame per trigger."""
    times_ns: np.ndarray  # int64, sorted ascending
    prices: np.ndarray  # float64

    @classmethod
    def from_agg_trades(cls, agg_trades: pd.DataFrame) -> "TickPath":
        return cls(
            times_ns=agg_trades["transact_time"].values.astype("datetime64[ns]").astype("int64"),
            prices=agg_trades["price"].to_numpy(dtype=float),
        )


def _simulate_one_trade(direction: int, entry_idx: int, entry_price: float,
                         path: TickPath, trade_params: TradeParams) -> dict:
    max_hold_ns = int(trade_params.candle_timeframe_minutes * trade_params.max_candles * 60e9)
    deadline_ns = path.times_ns[entry_idx] + max_hold_ns
    end_idx = np.searchsorted(path.times_ns, deadline_ns, side="right")

    peak_favorable_price = entry_price
    activated = False

    for i in range(entry_idx + 1, end_idx):
        price = path.prices[i]
        favorable_move = (price / entry_price - 1.0) * direction

        if direction > 0:
            peak_favorable_price = max(peak_favorable_price, price)
            peak_move = peak_favorable_price / entry_price - 1.0
        else:
            peak_favorable_price = min(peak_favorable_price, price)
            peak_move = entry_price / peak_favorable_price - 1.0

        if not activated and peak_move >= trade_params.activation_pct:
            activated = True

        if activated:
            if (peak_move - favorable_move) >= trade_params.trail_pct:
                return {
                    "exit_time_ns": path.times_ns[i], "exit_price": price,
                    "exit_reason": "trailing_tp", "pnl_pct_gross": favorable_move,
                    "activated": True, "peak_move_pct": peak_move,
                }
        elif favorable_move <= -trade_params.hard_stop_pct:
            return {
                "exit_time_ns": path.times_ns[i], "exit_price": price,
                "exit_reason": "hard_stop", "pnl_pct_gross": favorable_move,
                "activated": False, "peak_move_pct": peak_move,
            }

    if end_idx <= entry_idx + 1:
        return {
            "exit_time_ns": deadline_ns, "exit_price": entry_price,
            "exit_reason": "no_ticks", "pnl_pct_gross": 0.0,
            "activated": activated, "peak_move_pct": 0.0,
        }
    last_price = path.prices[end_idx - 1]
    final_move = (last_price / entry_price - 1.0) * direction
    peak_move = (peak_favorable_price / entry_price - 1.0) if direction > 0 else (entry_price / peak_favorable_price - 1.0)
    return {
        "exit_time_ns": path.times_ns[end_idx - 1], "exit_price": last_price,
        "exit_reason": "timeout_6candle", "pnl_pct_gross": final_move,
        "activated": activated, "peak_move_pct": peak_move,
    }


def simulate_from_triggers(path: TickPath, triggers: list[tuple], trade_params: TradeParams) -> pd.DataFrame:
    """triggers: list of (bucket_end_ns, direction) pairs, already filtered
    to nonzero triggers and sorted by time - see signals.extract_triggers."""
    trades = []
    cooldown_until_ns = None
    for bucket_end_ns, direction in triggers:
        if cooldown_until_ns is not None and bucket_end_ns < cooldown_until_ns:
            continue
        entry_idx = np.searchsorted(path.times_ns, bucket_end_ns, side="right")
        if entry_idx >= len(path.times_ns):
            continue
        entry_price = path.prices[entry_idx]
        result = _simulate_one_trade(direction, entry_idx, entry_price, path, trade_params)
        result["pnl_pct"] = result["pnl_pct_gross"] - 2 * trade_params.taker_fee_pct_per_side
        trades.append({
            "entry_time_ns": path.times_ns[entry_idx],
            "direction": "long" if direction > 0 else "short",
            "entry_price": entry_price,
            **result,
        })
        cooldown_until_ns = result["exit_time_ns"] + trade_params.cooldown_seconds * 1_000_000_000

    df = pd.DataFrame(trades)
    if not df.empty:
        df["entry_time"] = pd.to_datetime(df["entry_time_ns"], unit="ns", utc=True)
        df["exit_time"] = pd.to_datetime(df["exit_time_ns"], unit="ns", utc=True)
    return df


def simulate_pair(agg_trades: pd.DataFrame, signal_params: SignalParams, trade_params: TradeParams) -> pd.DataFrame:
    bucketed = bucket_agg_trades(agg_trades, signal_params.bucket_seconds)
    triggered = compute_triggers(bucketed, signal_params)
    fired = triggered[triggered["trigger"] != 0]
    bucket_end_ns = (fired["transact_time"] + pd.Timedelta(seconds=signal_params.bucket_seconds)).values.astype(
        "datetime64[ns]"
    ).astype("int64")
    triggers = list(zip(bucket_end_ns.tolist(), fired["trigger"].tolist()))
    path = TickPath.from_agg_trades(agg_trades)
    return simulate_from_triggers(path, triggers, trade_params)


def summarize(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"n_trades": 0}
    wins = trades[trades["pnl_pct"] > 0]
    losses = trades[trades["pnl_pct"] <= 0]
    return {
        "n_trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 4),
        "avg_pnl_pct": round(trades["pnl_pct"].mean(), 5),
        "avg_win_pct": round(wins["pnl_pct"].mean(), 5) if not wins.empty else None,
        "avg_loss_pct": round(losses["pnl_pct"].mean(), 5) if not losses.empty else None,
        "sum_pnl_pct_net_of_fees": round(trades["pnl_pct"].sum(), 5),
        "sum_pnl_pct_gross": round(trades["pnl_pct_gross"].sum(), 5),
        "by_exit_reason": trades["exit_reason"].value_counts().to_dict(),
        "by_direction": trades["direction"].value_counts().to_dict(),
    }
