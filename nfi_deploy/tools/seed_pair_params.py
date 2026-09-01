"""
One-off generator for pair_strategy_params.json's initial seed: every
currently-whitelisted pair gets the CURRENT EFFECTIVE global value (class
default, overridden by whatever's live in strategy_control.json right now)
for each PAIR_PARAM_SPECS knob. This is a no-behavior-change deploy — the
strategy resolves the same numbers it already would have via the global
attributes; it only stops being a no-op once a calibration job overwrites
individual pair/key entries with real per-pair values.

Run locally, inspect the output, then push the resulting JSON to the server
(/opt/nfi/user_data/pair_strategy_params.json) — see nfi_local_first_workflow
memory: this repo never talks to the live bot directly.
"""

import json
from datetime import datetime, timezone

# Effective values as of 2026-08-28, cross-checked against
# /opt/nfi/user_data/strategy_control.json (which overrides stale_exit_hours
# to 18; everything else here matches the class defaults in
# NostalgiaForInfinityX7EMA200.py since strategy_control.json sets no other
# override for these specific keys).
SEED_VALUES = {
    "stale_exit_hours": 18.0,
    "stale_exit_profit_band": 0.01,
    "stale_exit_max_loss": 0.015,
    "catastrophic_exit_loss_ratio": 0.2,
    "catastrophic_exit_min_hours": 2.0,
    "correlated_loss_guard_loss_threshold": -0.01,
    # Family/basket-wide guards — user explicitly requested per-pair overrides
    # for ALL knobs, not just the single-pair-scoped ones. Keyed by the
    # candidate entry's pair (see PAIR_PARAM_SPECS comment in the strategy).
    "correlated_loss_guard_min_losing": 3,
    "entry_rate_limit_window_hours": 6.0,
    "entry_rate_limit_max_entries": 2,
    "signal_666_volume_spike_mult": 1.5,
    "signal_666_min_funding_rate": 0.0001,
    "funding_settlement_buffer_minutes": 20,
    # Both None in the class (late-bind at call time); seeded here with the
    # value they currently resolve to, so the cache never has to represent
    # "null" as an explicit per-pair choice.
    "grey_zone_exit_start_hours": 18.0,
    "grey_zone_exit_floor_ratio": 0.015,
    "grey_zone_exit_ref_hours": 72.0,
    "grey_zone_exit_ref_cascade_pct": 20.7,
    "grey_zone_exit_min_full_hours": 24.0,
    "grey_zone_exit_max_full_hours": 168.0,
    "grey_zone_exit_curve_exponent": 5.0,
    "grey_zone_exit_pair_sensitivity": 3.0,
}

# Live NFI whitelist as of 2026-08-28 09:22 UTC (observed directly from the
# running bot's own candle-fetch log lines — not re-derived from config,
# since config.json uses VolumePairList which has no literal pair list).
WHITELIST_SYMBOLS = [
    "RUNE", "SUI", "GIGGLE", "CRWD", "VIRTUAL", "MON", "ETH", "ASTER", "MOVE", "ENS",
    "ADA", "PUMP", "TAO", "VVV", "KERNEL", "MSFT", "COTI", "ATOM", "KMNO", "GALA",
    "AXTI", "FF", "SAMSUNG", "LAB", "ENA", "ARB", "SAND", "ETC", "VET", "JTO",
    "UAI", "4", "RENDER", "BMNR", "OP", "CRV", "SPK", "CBRS", "TIA", "AVAX",
    "RE", "ONDO", "AERO", "BTW", "XRP", "APT", "DASH", "WIF", "XPL", "HBAR",
    "ICP", "FARTCOIN", "CRCL", "NVDA", "WLD", "DOGE", "1000SHIB", "SPX", "XLM", "XMR",
    "ETHFI", "1000BONK", "BTC", "HUMA", "SKHYNIX", "MANTRA", "SOL", "SKR", "FIL", "SOXL",
    "PENGU", "POL", "KORU", "MORPHO", "PYTH", "DOT", "UNI", "DRAM", "PEOPLE", "MSTR",
    "FET", "ZRO", "PENDLE", "ZEC", "COIN", "WDC", "SEI", "LINK", "CHIP", "HYPE",
    "LTC", "NEAR", "KITE", "MVLL", "BOME", "CRM",
]

TTL_HOURS = 168.0  # 1 week, matching green_streak_ttl_hours default


def build() -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry = {
        key: {"value": value, "computed_at": now, "ttl_hours": TTL_HOURS, "source": "seed_default"}
        for key, value in SEED_VALUES.items()
    }
    pairs = sorted({f"{sym}/USDT:USDT" for sym in WHITELIST_SYMBOLS})
    return {pair: dict(entry) for pair in pairs}


if __name__ == "__main__":
    cache = build()
    print(f"# {len(cache)} pairs x {len(SEED_VALUES)} params")
    with open("pair_strategy_params.seed.json", "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2, sort_keys=True)
        fh.write("\n")
