"""
CLI entry point for the momentum-scalp backtest (volume-delta + trade-flow
imbalance only - see data_fetch.py for why liquidation-spike is excluded
here).

Usage:
    python run_backtest.py BAND/USDT:USDT --days 14
    python run_backtest.py BAND/USDT:USDT --days 14 --cache-dir E:\\scratch\\momentum_cache --trades-csv out.csv
"""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # signals.py / simulate.py are siblings, not a package

from data_fetch import load_agg_trades  # noqa: E402
from signals import SignalParams  # noqa: E402
from simulate import TradeParams, simulate_pair, summarize  # noqa: E402

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "nfi_momentum_backtest"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pair", help='e.g. "BAND/USDT:USDT"')
    parser.add_argument("--days", type=int, default=14, help="lookback window in days, ending yesterday UTC (default: 14)")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help="where to cache downloaded daily CSVs")
    parser.add_argument("--bucket-seconds", type=int, default=30,
                         help="5s leaves most buckets empty on a thin pair, inflating the z-score on noise (default: 30)")
    parser.add_argument("--delta-z-threshold", type=float, default=SignalParams.delta_z_threshold)
    parser.add_argument("--imbalance-ratio-threshold", type=float, default=SignalParams.imbalance_ratio_threshold)
    parser.add_argument("--activation-pct", type=float, default=TradeParams.activation_pct)
    parser.add_argument("--trail-pct", type=float, default=TradeParams.trail_pct)
    parser.add_argument("--hard-stop-pct", type=float, default=TradeParams.hard_stop_pct)
    parser.add_argument("--candle-timeframe-minutes", type=int, default=TradeParams.candle_timeframe_minutes)
    parser.add_argument("--max-candles", type=int, default=TradeParams.max_candles)
    parser.add_argument("--trades-csv", default=None, help="optional path to dump the full trade list as CSV")
    args = parser.parse_args()

    # data.binance.vision daily files only settle a day or so after UTC midnight - today/yesterday may 404.
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=args.days - 1)

    signal_params = SignalParams(
        bucket_seconds=args.bucket_seconds,
        delta_z_threshold=args.delta_z_threshold,
        imbalance_ratio_threshold=args.imbalance_ratio_threshold,
    )
    trade_params = TradeParams(
        activation_pct=args.activation_pct,
        trail_pct=args.trail_pct,
        hard_stop_pct=args.hard_stop_pct,
        candle_timeframe_minutes=args.candle_timeframe_minutes,
        max_candles=args.max_candles,
    )

    cache_dir = Path(args.cache_dir)
    agg_trades = load_agg_trades(args.pair, start, end, cache_dir)
    trades = simulate_pair(agg_trades, signal_params, trade_params)
    stats = summarize(trades)

    print(json.dumps({
        "pair": args.pair,
        "range": f"{start} .. {end}",
        "signal_params": vars(signal_params),
        "trade_params": vars(trade_params),
        "stats": stats,
    }, indent=2, default=str))

    if args.trades_csv and not trades.empty:
        trades.to_csv(args.trades_csv, index=False)
        print(f"wrote {len(trades)} trades to {args.trades_csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
