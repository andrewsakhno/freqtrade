"""
MCP server exposing the freqtrade bots (NFI X7 dry-run and SampleStrategy)
for analysis and runtime control from Claude Desktop / Claude Code.

Run: python -m nfi_mcp.server  (stdio transport).
"""

import time
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .bots import BOTS, DEFAULT_BOT, get_bot
from .freqtrade_api import FreqtradeApi
from .signal_control import SignalControl
from .ssh_link import SshRunner
from .stats import aggregate_by_enter_tag

mcp = FastMCP("freqtrade")

_runner = SshRunner()
_signal_control = SignalControl(_runner)


def _api(bot: str) -> FreqtradeApi:
    return FreqtradeApi(_runner, get_bot(bot))


# --- Analytics (read-only) ---------------------------------------------------

@mcp.tool()
def ping() -> dict:
    """Check connectivity: server reachable over ssh and each bot's API alive.
    Call this first when any other tool fails, to distinguish transport
    problems from bot problems."""
    out: dict = {}
    for name in BOTS:
        try:
            version = _api(name).get("version")
            out[name] = {"ok": True, "version": version.get("version")}
        except Exception as exc:
            out[name] = {"ok": False, "error": str(exc)}
    return out


@mcp.tool()
def get_profit(bot: str = DEFAULT_BOT) -> dict:
    """Overall closed-trade statistics for a bot (profit, winrate, drawdown,
    best pair, trade counts). Bots: 'nfi' (NFI X7 dry-run) or 'sample'."""
    return _api(bot).get("profit")


@mcp.tool()
def get_open_trades(bot: str = DEFAULT_BOT) -> list:
    """Currently open trades with entry tag, current profit and duration.
    Use before force_exit to find the trade_id."""
    trades = _api(bot).get("status")
    return [
        {
            "trade_id": t.get("trade_id"),
            "pair": t.get("pair"),
            "is_short": t.get("is_short"),
            "enter_tag": t.get("enter_tag"),
            "open_date": t.get("open_date"),
            "stake_amount": t.get("stake_amount"),
            "leverage": t.get("leverage"),
            "profit_ratio": t.get("profit_ratio"),
            "profit_abs": t.get("profit_abs"),
        }
        for t in trades
    ]


@mcp.tool()
def stats_by_enter_tag(bot: str = DEFAULT_BOT, days: int = 7) -> dict:
    """Closed-trade statistics for the last N days grouped by entry signal
    (side:enter_tag): trades, winrate, total/mean profit, avg duration, pairs.
    This is the primary tool for judging which NFI entry signals to keep,
    disable or keep watching. Compare different `days` windows to see trends."""
    cutoff_ms = int((time.time() - days * 86400) * 1000)
    trades = _api(bot).closed_trades_since(cutoff_ms)
    result = aggregate_by_enter_tag(trades)
    result["window_days"] = days
    return result


@mcp.tool()
def get_enabled_signals() -> dict:
    """Effective NFI X7 entry-signal state: enabled signal ids per side, plus
    every signal whose state differs from the strategy default (showing
    default vs config.json vs hot override vs effective value), and the
    current strategy_control.json content including the EMA200 guard flag.
    Call before toggling signals to see the current state."""
    return _signal_control.get_state()


@mcp.tool()
def get_control_log(limit: int = 20) -> dict:
    """Journal of runtime control actions (signal toggles, EMA200 guard
    changes) with timestamps, reasons and before/after state. Use it when
    judging signal statistics so you know when a signal was actually active."""
    return _signal_control.journal_tail(limit)


@mcp.tool()
def request_risk_analysis(pair: str, bot: str = DEFAULT_BOT) -> dict:
    """
    Read-only briefing for deciding whether to call set_risk_adjustment on
    one pair. Returns the grey-zone exit's baseline cascade-rate calibration
    for this pair (measured vs modelled points, clearly labeled), every
    currently open trade on this pair with its live distance-to-forced-exit
    at the current price, the multiplier/TTL bounds you must respect, and a
    prompt explaining how to reason about it.

    This tool does not call any LLM itself and does not read news - it just
    assembles context. YOU (the calling model) are expected to bring
    whatever market/news judgment is relevant, then decide.

    IMPORTANT: inaction is the correct default. Only call
    set_risk_adjustment if you have a specific, citable catalyst (a
    scheduled event, a confirmed news item, an unusual funding/orderbook
    signal) - not a general "markets feel uncertain" vibe. The historical
    baseline already accounts for ordinary volatility.
    """
    calibration = _signal_control.grey_zone_calibration(pair)
    open_trades = [t for t in _api(bot).get("status") if t.get("pair") == pair]

    params = calibration["resolved_params"]
    ceiling = params["ceiling_ratio"]
    floor = params["floor_ratio"]
    start_hours = params["start_hours"]
    exponent = params["curve_exponent"]
    full_hours_baseline = calibration["full_hours_baseline"]

    def grey_zone_threshold(trade_hours: float, full_hours: float) -> float:
        if full_hours <= start_hours or floor >= ceiling:
            return ceiling
        p = min(max((trade_hours - start_hours) / (full_hours - start_hours), 0.0), 1.0)
        return min(max(ceiling - (ceiling - floor) * (p ** exponent), floor), ceiling)

    def hours_until_forced_exit(total_profit_ratio: float, hours_open: float, full_hours: float):
        loss = -total_profit_ratio
        if not (floor <= loss <= ceiling):
            return None
        q = (ceiling - loss) / (ceiling - floor)
        p = q ** (1.0 / exponent)
        t_forced = start_hours + p * (full_hours - start_hours)
        return round(max(t_forced - hours_open, 0.0), 1)

    active_adj = calibration.get("active_risk_adjustment")
    effective_full_hours = calibration.get("full_hours_with_active_adjustment") or full_hours_baseline

    trade_rows = []
    trade_lines = []
    now_ms = time.time() * 1000
    for t in open_trades:
        open_ts = t.get("open_timestamp")
        hours_open = (now_ms - open_ts) / 3_600_000 if open_ts else None
        tpr = t.get("total_profit_ratio")
        eff_threshold = None
        hours_until = None
        if hours_open is not None:
            eff_threshold = round(grey_zone_threshold(hours_open, effective_full_hours), 4)
        if hours_open is not None and tpr is not None:
            hours_until = hours_until_forced_exit(tpr, hours_open, effective_full_hours)
        row = {
            "trade_id": t.get("trade_id"),
            "enter_tag": t.get("enter_tag"),
            "hours_open": round(hours_open, 1) if hours_open is not None else None,
            "total_profit_ratio": tpr,
            "leverage": t.get("leverage"),
            "stake_amount": t.get("stake_amount"),
            "max_stake_amount": t.get("max_stake_amount"),
            "current_force_exit_threshold": eff_threshold,
            "hours_until_forced_exit_at_current_price": hours_until,
        }
        trade_rows.append(row)
        if hours_open is not None and tpr is not None:
            trade_lines.append(
                f"  #{row['trade_id']} {row['enter_tag']} — open {row['hours_open']}h, "
                f"lifetime P&L {tpr:+.2%}, current force-exit threshold "
                f"-{eff_threshold:.1%}, "
                + (
                    f"~{hours_until}h until forced exit at the current price"
                    if hours_until is not None
                    else "outside the grey-zone band right now"
                )
            )

    curve_lines = ", ".join(
        f"{p['hours']:g}h: {p['cascade_pct']}% ({p['source']})" for p in calibration["cascade_curve"]
    )
    mult_min, mult_max = calibration["bounds"]["multiplier"]
    ttl_min, ttl_max = calibration["bounds"]["ttl_hours"]

    def full_hours_at(multiplier: float) -> float:
        c = max(calibration["cascade_pct_72h"] * multiplier, 0.01)
        h = params["ref_hours"] * (params["ref_cascade_pct"] / c) ** params["pair_sensitivity"]
        return round(min(max(h, params["min_full_hours"]), params["max_full_hours"]), 1)

    prompt = f"""You are assessing short-horizon cascade risk for {pair} on a live futures bot
(3x leverage, NostalgiaForInfinityX7EMA200).

BASELINE (empirical, from 8 pairs x ~2 months of 5m candles):
{pair} enters a "grey zone" drawdown (a real, non-flat loss between
-{floor:.1%} and -{ceiling:.0%} lifetime P&L) sometimes. Historically
{calibration['cascade_pct_72h']}% of those episodes cascaded through the
-{ceiling:.0%} floor rather than recovering, measured over a 72h lookback
({calibration['calibration_source']} for this pair). The cascade rate rises
with elapsed time: {curve_lines}.

Because of that baseline, the bot force-closes a {pair} grey-zone trade on a
threshold that tightens from -{ceiling:.0%} lifetime P&L at {start_hours:g}h
of trade age to -{floor:.1%} at {full_hours_baseline:g}h
{"(currently adjusted to " + str(effective_full_hours) + "h by an active override)" if active_adj else ""}.

CURRENT OPEN TRADES ON {pair}:
{chr(10).join(trade_lines) if trade_lines else "  none"}

YOUR TASK:
Using current news, market structure, funding, and macro calendar for
{pair} and for crypto broadly, judge whether the RIGHT NOW cascade
probability is higher or lower than the {calibration['cascade_pct_72h']}%
historical baseline, and by roughly what factor.

Then call set_risk_adjustment(pair, multiplier, ttl_hours, reason) where:
  - multiplier is your factor on the cascade rate.
      >1 = riskier than baseline -> the bot tightens EARLIER
           (e.g. 1.5 moves the full-tightening horizon
            {full_hours_baseline:g}h -> {full_hours_at(1.5)}h)
      <1 = calmer than baseline -> the bot stays loose LONGER
           (e.g. 0.5 moves it {full_hours_baseline:g}h -> {full_hours_at(0.5)}h)
      1.0 = no change; do not call the tool at all if that is your answer
      allowed range {mult_min}..{mult_max} (values outside are clamped)
  - ttl_hours is how long your read should remain in force. Choose the
      horizon of the actual catalyst, not a default: a scheduled event that
      resolves in 4h gets ttl_hours=4, not 24. Allowed range
      {ttl_min}..{ttl_max}h (clamped). The adjustment self-expires - it
      cannot outlive its TTL even if this conversation ends.
  - reason must cite the specific catalyst, not a vibe. It is written to a
      permanent audit journal.

CONSTRAINTS YOU CANNOT OVERRIDE: whatever multiplier you set, the bot will
never fully tighten before {params['min_full_hours']:g}h of trade age nor
later than {params['max_full_hours']:g}h. You are adjusting a curve inside
fixed guardrails, not setting a stop-loss.

If you have no specific, citable reason to deviate from the historical
baseline, do nothing. The baseline is the default for a reason."""

    return {
        "pair": pair,
        "calibration": calibration,
        "open_trades": trade_rows,
        "bounds": calibration["bounds"],
        "prompt": prompt,
    }


# --- Hot signal control (no reload, applies on the next candle) --------------

@mcp.tool()
def toggle_signal(side: str, signal_id: str, enabled: bool, reason: str) -> dict:
    """Enable or disable one NFI entry signal at runtime via
    strategy_control.json. Takes effect on the next 5m candle without any
    reload or restart. side: 'long' or 'short'; signal_id: bare number like
    '170' or '666'. Always pass a short reason - it is stored in the control
    journal. The override persists until cleared with clear_signal_override."""
    return _signal_control.set_override(side, signal_id, enabled, reason)


@mcp.tool()
def clear_signal_override(side: str, signal_id: str, reason: str) -> dict:
    """Remove a hot override for one signal, restoring the config.json/default
    behavior on the next candle."""
    return _signal_control.clear_override(side, signal_id, reason)


@mcp.tool()
def set_ema200_guard(enabled: bool, reason: str) -> dict:
    """Toggle the EMA200 macro-trend guard of the NFI bot at runtime (long
    entries only above EMA200, shorts only below). Disabling it lets NFI
    trade against the macro trend - do this only deliberately."""
    return _signal_control.set_ema200_guard(enabled, reason)


@mcp.tool()
def set_risk_adjustment(pair: str, multiplier: float, ttl_hours: float, reason: str) -> dict:
    """Temporarily scale the grey-zone exit's cascade-rate assumption for one
    pair (or '*' for all pairs). multiplier > 1 = riskier than the historical
    baseline, so the bot force-closes a lingering losing trade EARLIER;
    < 1 = calmer, so it stays patient longer. Bounded: multiplier is clamped
    to 0.25..4.0, ttl_hours to 0.25..24.0, and the underlying curve can never
    fully tighten before 24h of trade age nor later than 168h regardless of
    what you pass. The adjustment self-expires at set_at + ttl_hours with no
    further action needed - there is no cleanup step to remember. Call
    request_risk_analysis(pair) first - do not call this without a specific,
    citable catalyst."""
    return _signal_control.set_risk_adjustment(pair, multiplier, ttl_hours, reason)


@mcp.tool()
def clear_risk_adjustment(pair: str, reason: str) -> dict:
    """Revoke a risk adjustment before its TTL expires - use when the
    catalyst that justified it has resolved or turned out to be false."""
    return _signal_control.clear_risk_adjustment(pair, reason)


# --- Immediate bot control (freqtrade REST) ----------------------------------

@mcp.tool()
def stop_entry(bot: str = DEFAULT_BOT) -> dict:
    """Emergency brake: immediately stop opening new positions while still
    managing (and exiting) open ones. Resume with resume_bot."""
    return _api(bot).post("stopentry")


@mcp.tool()
def resume_bot(bot: str = DEFAULT_BOT) -> dict:
    """Resume normal operation after stop_entry (or after a full stop)."""
    return _api(bot).post("start")


@mcp.tool()
def force_exit(trade_id: str, bot: str = DEFAULT_BOT) -> dict:
    """Immediately close one open trade at market. Get trade_id from
    get_open_trades first. Pass 'all' to close every open trade."""
    return _api(bot).post("forceexit", {"tradeid": trade_id})


@mcp.tool()
def get_blacklist(bot: str = DEFAULT_BOT) -> dict:
    """Current pair blacklist of a bot."""
    return _api(bot).get("blacklist")


@mcp.tool()
def blacklist_pair(pair: str, bot: str = DEFAULT_BOT) -> dict:
    """Add a pair (e.g. 'BTC/USDT:USDT') to the bot's blacklist - immediately
    stops new entries for that pair. Open trades on it are not touched."""
    return _api(bot).post("blacklist", {"blacklist": [pair]})


@mcp.tool()
def unblacklist_pair(pair: str, bot: str = DEFAULT_BOT) -> dict:
    """Remove a pair from the bot's blacklist."""
    return _api(bot).delete("blacklist", {"pairs_to_delete": pair})


@mcp.tool()
def reload_config(bot: str = DEFAULT_BOT) -> dict:
    """Re-initialize the bot from config.json in-place (~10-20s, open trades
    preserved, container keeps running). Needed only after editing config.json
    itself - hot signal toggles via toggle_signal do NOT require this."""
    return _api(bot).post("reload_config")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
