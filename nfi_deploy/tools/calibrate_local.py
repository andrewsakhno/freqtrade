"""
Local (non-deploying) calibration run: computes all 20 PAIR_PARAM_SPECS
values for every pair in seed_pair_params.WHITELIST_SYMBOLS using a 30-day
lookback, and writes the result to pair_strategy_params.calibrated.json in
this directory.

Deliberately does NOT call pair_param_calibration.compute_and_cache[_all] -
those write to the hardcoded /opt/nfi/user_data/... server paths. This
script only calls the read-only analyze_pair() and writes locally, so
nothing touches the server (per "don't deploy yet, finish the logic
first"). Output feeds the strategy-tester backtest comparison, not the live
bot.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for nfi_mcp_server package

from nfi_mcp_server.pair_param_calibration import CalibrationError, analyze_pair  # noqa: E402
from seed_pair_params import WHITELIST_SYMBOLS  # noqa: E402

LOOKBACK_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
TTL_HOURS = 168.0
POLITE_DELAY_S = 0.2
OUT_PATH = Path(__file__).with_name(f"pair_strategy_params.calibrated.{LOOKBACK_DAYS}d.json")
REPORT_PATH = Path(__file__).with_name(f"calibration_report.{LOOKBACK_DAYS}d.json")


def main() -> None:
    pairs = sorted({f"{sym}/USDT:USDT" for sym in WHITELIST_SYMBOLS})
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    cache: dict[str, dict] = {}
    failed: dict[str, str] = {}
    analyses: dict[str, dict] = {}

    for i, pair in enumerate(pairs):
        try:
            result = analyze_pair(pair, lookback_days=LOOKBACK_DAYS)
        except CalibrationError as exc:
            failed[pair] = str(exc)
            print(f"[{i + 1}/{len(pairs)}] FAIL {pair}: {exc}")
        else:
            cache[pair] = {
                key: {"value": value, "computed_at": now_iso, "ttl_hours": TTL_HOURS, "source": "calibrated_local_30d"}
                for key, value in result["params"].items()
            }
            analyses[pair] = {
                "corr_with_btc": result["corr_with_btc"],
                "n_candles": result["profile"]["n_candles"],
                "daily_vol_pct": round(result["profile"]["daily_vol_pct"], 6),
                "cascade_rate_72h": result["profile"]["cascade_rate"].get(72),
            }
            print(f"[{i + 1}/{len(pairs)}] OK {pair} (n={result['profile']['n_candles']})")
        if i < len(pairs) - 1:
            time.sleep(POLITE_DELAY_S)

    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2, sort_keys=True)
        fh.write("\n")

    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump({"computed_at": now_iso, "lookback_days": LOOKBACK_DAYS,
                    "calibrated": len(cache), "failed": failed, "profiles": analyses}, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print(f"\ncalibrated={len(cache)} failed={len(failed)}")
    if failed:
        print("failed pairs:", ", ".join(failed))


if __name__ == "__main__":
    main()
