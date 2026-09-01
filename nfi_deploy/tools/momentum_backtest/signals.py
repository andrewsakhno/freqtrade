"""
Momentum trigger signal, built from aggTrades only (see data_fetch.py for why
liquidation data is excluded).

Two of the three signals the user described map onto the same underlying
data (trade-by-trade aggressor side from the `is_buyer_maker` flag), just
summarized two different ways:

  - "Volume Delta"      -> signed buy-minus-sell volume per bucket, expressed
                            as a z-score against a trailing baseline (catches
                            an aggressive burst regardless of the pair's
                            normal activity level).
  - "Bid/Ask imbalance"  -> buy-volume share of total volume per bucket
                            (0..1). Not real order-book depth (Binance
                            doesn't archive that), but the closest available
                            proxy: sustained one-sided aggression.

A trigger requires both to agree (confluence), per the user's original
description of the signal as a combination of conditions rather than either
one alone.

A third condition, price confirmation, was added after the first BAND sweep
came back with no edge at any volume-only threshold: a raw volume burst can
be one large order getting absorbed with no follow-through (common on a thin
book - it just as often gets faded as extended), so `price_confirm_min_move`
requires the price itself to already have moved in the trigger's direction
over the preceding `price_confirm_window_buckets`, i.e. only fire once the
burst is visibly *moving* price, not just present in the tape.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class SignalParams:
    bucket_seconds: int = 5
    baseline_window_buckets: int = 720  # 720 * 5s = 1h trailing baseline
    min_baseline_buckets: int = 360  # need at least 30min of history before trusting the z-score
    delta_z_threshold: float = 3.0
    imbalance_ratio_threshold: float = 0.70  # buy_share >= 0.70 (long) or <= 0.30 (short)
    price_confirm_window_buckets: int = 3  # lookback for the price-already-moving check
    price_confirm_min_move_pct: float = 0.0  # 0.0 disables the check (pure volume/imbalance signal)


def bucket_agg_trades(agg_trades: pd.DataFrame, bucket_seconds: int) -> pd.DataFrame:
    """Resample raw aggTrades into fixed-width time buckets with signed
    volume delta, buy share, notional and trade count per bucket."""
    df = agg_trades.copy()
    signed_qty = np.where(df["is_buyer_maker"], -df["quantity"], df["quantity"])
    df["buy_qty"] = np.where(df["is_buyer_maker"], 0.0, df["quantity"])
    df["sell_qty"] = np.where(df["is_buyer_maker"], df["quantity"], 0.0)
    df["signed_qty"] = signed_qty
    df["notional"] = df["price"] * df["quantity"]

    bucketed = (
        df.set_index("transact_time")
        .resample(f"{bucket_seconds}s")
        .agg(
            buy_vol=("buy_qty", "sum"),
            sell_vol=("sell_qty", "sum"),
            delta=("signed_qty", "sum"),
            notional=("notional", "sum"),
            trade_count=("price", "count"),
            last_price=("price", "last"),
        )
    )
    bucketed["last_price"] = bucketed["last_price"].ffill()
    total_vol = bucketed["buy_vol"] + bucketed["sell_vol"]
    bucketed["buy_share"] = np.where(total_vol > 0, bucketed["buy_vol"] / total_vol, np.nan)
    return bucketed.reset_index()


def compute_triggers(bucketed: pd.DataFrame, params: SignalParams) -> pd.DataFrame:
    """Add delta_z and a `trigger` column (+1 long / -1 short / 0 none) to a
    bucketed dataframe. Baseline stats are trailing-only (shifted by one
    bucket) so no future data leaks into a bucket's own trigger decision."""
    out = bucketed.copy()
    roll = out["delta"].rolling(params.baseline_window_buckets, min_periods=params.min_baseline_buckets)
    baseline_mean = roll.mean().shift(1)
    baseline_std = roll.std().shift(1)
    out["delta_z"] = (out["delta"] - baseline_mean) / baseline_std.replace(0, np.nan)
    out["price_move"] = out["last_price"] / out["last_price"].shift(params.price_confirm_window_buckets) - 1.0

    long_signal = (
        (out["delta_z"] >= params.delta_z_threshold)
        & (out["buy_share"] >= params.imbalance_ratio_threshold)
        & (out["price_move"] >= params.price_confirm_min_move_pct)
    )
    short_signal = (
        (out["delta_z"] <= -params.delta_z_threshold)
        & (out["buy_share"] <= (1.0 - params.imbalance_ratio_threshold))
        & (out["price_move"] <= -params.price_confirm_min_move_pct)
    )
    out["trigger"] = np.select([long_signal, short_signal], [1, -1], default=0)
    return out
