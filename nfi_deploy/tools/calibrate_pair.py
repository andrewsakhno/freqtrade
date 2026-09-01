"""
Calibrate all PAIR_PARAM_SPECS knobs for a single pair over a lookback
window of N days ending now.

Read-only by default (prints the derived params, touches nothing) - per
this repo's local-first-workflow rule, writing to the live bot needs an
explicit --write. Pass --write to also persist into
/opt/nfi/user_data/pair_strategy_params.json via
pair_param_calibration.compute_and_cache (only works where that path is
reachable, i.e. run inside the nfi-mcp container / on the server, not from
a local Windows/WSL checkout).

Usage:
    python calibrate_pair.py BAND/USDT:USDT --days 90
    python calibrate_pair.py BAND/USDT:USDT --days 90 --write --reason "why"
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for nfi_mcp_server package

from nfi_mcp_server.pair_param_calibration import (  # noqa: E402
    CalibrationError,
    analyze_pair,
    compute_and_cache,
    get_calibration_status,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pair", help='e.g. "BAND/USDT:USDT"')
    parser.add_argument("--days", type=int, default=90, help="lookback window in days, ending now (default: 90)")
    parser.add_argument("--write", action="store_true",
                         help="persist into the live pair_strategy_params.json (default: read-only)")
    parser.add_argument("--reason", default="", help="journal reason, required with --write")
    args = parser.parse_args()

    if args.write and not args.reason.strip():
        parser.error("--write requires --reason (it is the audit trail)")

    try:
        if args.write:
            before = get_calibration_status(args.pair)
            analysis = compute_and_cache(args.pair, lookback_days=args.days, reason=args.reason)
            after = get_calibration_status(args.pair)
            print(json.dumps({"before": before, "after": after}, indent=2, default=str))
        else:
            analysis = analyze_pair(args.pair, lookback_days=args.days)
            print(json.dumps({
                "pair": analysis["pair"],
                "lookback_days": analysis["lookback_days"],
                "corr_with_btc": analysis["corr_with_btc"],
                "profile": analysis["profile"],
                "params": analysis["params"],
            }, indent=2, default=str))
    except CalibrationError as exc:
        print(f"calibration failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
