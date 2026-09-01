"""
Historical data fetch for the momentum-scalp backtest, from Binance's public
data.binance.vision archive (no API key needed - same public bucket the REST
klines endpoint is backed by).

Only klines (1m) and aggTrades are pulled: liquidation history is NOT
available anywhere (data.binance.vision's liquidationSnapshot dataset is
empty even for BTCUSDT - Binance appears to have discontinued it - and the
REST /fapi/v1/allForceOrders per-symbol endpoint 404s). The liquidation-spike
leg of the momentum signal can only be validated live, once a websocket
listener starts accumulating its own history - see simulate.py docstring.

Each day's CSV is downloaded once and cached on disk as-is (raw, before any
parsing) so re-running a backtest over the same range doesn't re-hit the
network. Cache is *not* placed anywhere under the repo by default - these
files add up to tens of MB per pair-month - callers should point cache_dir at
a scratch location.
"""

import io
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator

import pandas as pd
import requests

BASE_URL = "https://data.binance.vision/data/futures/um/daily"


class DataFetchError(RuntimeError):
    pass


def to_binance_symbol(pair: str) -> str:
    # "BAND/USDT:USDT" -> "BANDUSDT"
    return pair.split(":", 1)[0].replace("/", "").upper()


def _date_range(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _download_daily_csv(dataset: str, symbol: str, day: date, cache_dir: Path) -> Path | None:
    """Download+extract one day's CSV for a dataset ('klines/1m' or
    'aggTrades'), cache it, and return the local path. Returns None if the
    day has no data (e.g. pair wasn't listed yet - a 404 is not an error)."""
    day_str = day.isoformat()
    if dataset == "klines/1m":
        fname = f"{symbol}-1m-{day_str}"
        url = f"{BASE_URL}/klines/{symbol}/1m/{fname}.zip"
    elif dataset == "aggTrades":
        fname = f"{symbol}-aggTrades-{day_str}"
        url = f"{BASE_URL}/aggTrades/{symbol}/{fname}.zip"
    else:
        raise ValueError(f"unknown dataset {dataset!r}")

    cache_subdir = cache_dir / dataset.replace("/", "_") / symbol
    cache_subdir.mkdir(parents=True, exist_ok=True)
    cached_csv = cache_subdir / f"{fname}.csv"
    if cached_csv.exists():
        return cached_csv

    resp = requests.get(url, timeout=60)
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise DataFetchError(f"{url} -> HTTP {resp.status_code}")

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        if len(names) != 1:
            raise DataFetchError(f"{url}: expected 1 file in zip, got {names}")
        cached_csv.write_bytes(zf.read(names[0]))
    return cached_csv


KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
]
AGGTRADE_COLUMNS = [
    "agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id",
    "transact_time", "is_buyer_maker",
]


def load_klines(pair: str, start: date, end: date, cache_dir: Path) -> pd.DataFrame:
    symbol = to_binance_symbol(pair)
    frames = []
    for day in _date_range(start, end):
        path = _download_daily_csv("klines/1m", symbol, day, cache_dir)
        if path is None:
            continue
        df = pd.read_csv(path)
        frames.append(df)
    if not frames:
        raise DataFetchError(f"no klines data found for {symbol} in [{start}, {end}]")
    out = pd.concat(frames, ignore_index=True)
    out["open_time"] = pd.to_datetime(out["open_time"], unit="ms", utc=True)
    out["close_time"] = pd.to_datetime(out["close_time"], unit="ms", utc=True)
    return out.sort_values("open_time").reset_index(drop=True)


def load_agg_trades(pair: str, start: date, end: date, cache_dir: Path) -> pd.DataFrame:
    symbol = to_binance_symbol(pair)
    frames = []
    for day in _date_range(start, end):
        path = _download_daily_csv("aggTrades", symbol, day, cache_dir)
        if path is None:
            continue
        df = pd.read_csv(path)
        frames.append(df)
    if not frames:
        raise DataFetchError(f"no aggTrades data found for {symbol} in [{start}, {end}]")
    out = pd.concat(frames, ignore_index=True)
    out["transact_time"] = pd.to_datetime(out["transact_time"], unit="ms", utc=True)
    out["is_buyer_maker"] = out["is_buyer_maker"].astype(bool)
    return out.sort_values("transact_time").reset_index(drop=True)
