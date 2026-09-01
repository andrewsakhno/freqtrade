"""
Threshold sweep for one pair: find whether *any* combination of signal/trade
parameters turns the momentum-scalp idea net-positive after fees, before
spending time rolling it out to more pairs.

Loads aggTrades once, buckets once (bucket_seconds is fixed for the sweep -
sweeping it too would multiply the grid for little expected gain), then for
each (delta_z_threshold, imbalance_ratio_threshold) pair recomputes triggers
cheaply (vectorized), and for each (activation_pct, trail_pct) pair replays
the tick-level trade simulation against that fixed trigger set. Cost is
roughly len(z)*len(ratio)*len(activation)*len(trail) trade-simulation
passes, each bounded by the max_candles hold time per trade - see
simulate.TickPath.

Usage:
    python sweep_pair.py BAND/USDT:USDT --days 30
    python sweep_pair.py BAND/USDT:USDT --days 30 --top 15 --min-trades 15
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402

from data_fetch import load_agg_trades  # noqa: E402
from signals import SignalParams, bucket_agg_trades, compute_triggers  # noqa: E402
from simulate import TickPath, TradeParams, simulate_from_triggers, summarize  # noqa: E402

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "nfi_momentum_backtest"

DELTA_Z_GRID = [3.0, 4.0, 5.0, 6.0]
IMBALANCE_RATIO_GRID = [0.60, 0.75, 0.90]
PRICE_CONFIRM_MIN_MOVE_GRID = [0.0, 0.001, 0.002, 0.004]  # 0.0 = direction-only, no minimum magnitude
ACTIVATION_PCT_GRID = [0.005, 0.01]
TRAIL_PCT_GRID = [0.002, 0.004]
HARD_STOP_PCT = 0.01
PRICE_CONFIRM_WINDOW_BUCKETS = 3


def _triggers_from(bucketed: pd.DataFrame, signal_params: SignalParams) -> list[tuple]:
    triggered = compute_triggers(bucketed, signal_params)
    fired = triggered[triggered["trigger"] != 0]
    bucket_end_ns = (fired["transact_time"] + pd.Timedelta(seconds=signal_params.bucket_seconds)).values.astype(
        "datetime64[ns]"
    ).astype("int64")
    return list(zip(bucket_end_ns.tolist(), fired["trigger"].tolist()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pair", help='e.g. "BAND/USDT:USDT"')
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--top", type=int, default=15, help="how many best combos to print")
    parser.add_argument("--min-trades", type=int, default=15, help="drop combos with fewer trades than this (too few to trust)")
    parser.add_argument("--bucket-seconds", type=int, default=30,
                         help="fixed bucket width for the whole sweep - too small on a thin pair means mostly-empty "
                              "buckets, near-zero rolling std, and z-score blowing up on noise (default: 30)")
    args = parser.parse_args()

    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=args.days - 1)
    cache_dir = Path(args.cache_dir)

    print(f"loading aggTrades for {args.pair} [{start} .. {end}] ...", file=sys.stderr)
    agg_trades = load_agg_trades(args.pair, start, end, cache_dir)
    print(f"{len(agg_trades):,} ticks loaded", file=sys.stderr)

    path = TickPath.from_agg_trades(agg_trades)
    bucketed = bucket_agg_trades(agg_trades, args.bucket_seconds)

    rows = []
    total_combos = (
        len(DELTA_Z_GRID) * len(IMBALANCE_RATIO_GRID) * len(PRICE_CONFIRM_MIN_MOVE_GRID)
        * len(ACTIVATION_PCT_GRID) * len(TRAIL_PCT_GRID)
    )
    done = 0
    for z in DELTA_Z_GRID:
        for ratio in IMBALANCE_RATIO_GRID:
            for price_confirm in PRICE_CONFIRM_MIN_MOVE_GRID:
                signal_params = SignalParams(
                    bucket_seconds=args.bucket_seconds, delta_z_threshold=z, imbalance_ratio_threshold=ratio,
                    price_confirm_window_buckets=PRICE_CONFIRM_WINDOW_BUCKETS,
                    price_confirm_min_move_pct=price_confirm,
                )
                triggers = _triggers_from(bucketed, signal_params)
                for activation in ACTIVATION_PCT_GRID:
                    for trail in TRAIL_PCT_GRID:
                        if trail >= activation:
                            continue  # trailing distance wider than the activation move makes activation meaningless
                        trade_params = TradeParams(
                            activation_pct=activation, trail_pct=trail, hard_stop_pct=HARD_STOP_PCT,
                        )
                        trades = simulate_from_triggers(path, triggers, trade_params)
                        stats = summarize(trades)
                        done += 1
                        if stats["n_trades"] < args.min_trades:
                            continue
                        rows.append({
                            "delta_z": z, "imbalance_ratio": ratio, "price_confirm_min_move": price_confirm,
                            "activation_pct": activation, "trail_pct": trail,
                            **stats,
                        })
    print(f"evaluated {done}/{total_combos} combos, {len(rows)} met min_trades={args.min_trades}", file=sys.stderr)

    if not rows:
        print("no combo met the min-trades bar - signal essentially never fires meaningfully at these thresholds")
        return

    result = pd.DataFrame(rows).drop(columns=["by_exit_reason", "by_direction"])
    result = result.sort_values("sum_pnl_pct_net_of_fees", ascending=False)
    with pd.option_context("display.max_columns", None, "display.width", 200):
        print(result.head(args.top).to_string(index=False))


if __name__ == "__main__":
    main()
