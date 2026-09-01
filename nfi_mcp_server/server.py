"""
MCP server exposing the freqtrade bots (NFI X7 dry-run and SampleStrategy)
for analysis and runtime control from Claude Desktop / Claude Code.

Runs ON THE SAME HOST as the bots (streamable-http transport, bound to
127.0.0.1 only). Reach it from your workstation over the existing ssh
tunnel to this server (add a LocalForward for MCP_PORT alongside the
existing FreqUI one), then point your MCP client at
http://localhost:<MCP_PORT>/mcp with "type": "http".

Run: python -m nfi_mcp_server.server
Env: MCP_HOST (default 127.0.0.1), MCP_PORT (default 8765)
"""

import os
import time
from typing import Optional

from mcp.server.fastmcp import FastMCP

from . import green_streak, pair_param_calibration, signal_control
from .bots import BOTS, DEFAULT_BOT, get_bot
from .freqtrade_client import FreqtradeApi, FreqtradeApiError
from .green_streak import GreenStreakError
from .pair_param_calibration import CalibrationError
from .pair_priority import classify_pairs_priority
from .pair_quality import classify_pairs
from .stats import aggregate_by_enter_tag, aggregate_by_pair, exit_summary

mcp = FastMCP(
    "freqtrade",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8765")),
)

_api_cache: dict[str, FreqtradeApi] = {}


def _api(bot: str) -> FreqtradeApi:
    """One FreqtradeApi (and its fetched credentials) per bot, reused across
    calls instead of re-fetching creds via docker exec on every tool call."""
    if bot not in _api_cache:
        _api_cache[bot] = FreqtradeApi(get_bot(bot))
    return _api_cache[bot]


# --- Analytics (read-only) ---------------------------------------------------

@mcp.tool()
def ping() -> dict:
    """Check connectivity: each bot's API alive and reachable.
    Call this first when any other tool fails, to distinguish a credential
    or network problem from a bot-specific one."""
    out: dict = {}
    for name in BOTS:
        try:
            version = _api(name).get("version")
            out[name] = {"ok": True, "version": version.get("version")}
        except FreqtradeApiError as exc:
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
def get_pair_quality(bot: str = DEFAULT_BOT, days: int = 90) -> dict:
    """Per-pair whitelist / junk_currency verdicts (each Optional[bool] -
    None means not enough data yet, do not treat as a clean verdict):
      - junk_currency=True: net non-positive P&L over the last `days` with
        enough closed trades to judge - a blacklist candidate, cheaper than
        reverse-engineering a per-pair algorithm for it.
      - whitelist=True: the pair's calibration profile (from
        compute_pair_params / the drift-profile snapshot) matches the
        reversal/grey-zone cascade pattern NFI's signals target - a clean
        cascade rate and representative volatility, independent of current P&L.
    Each verdict ships with the metrics it was computed from for audit. Call
    compute_pair_params first for any pair with no profile_snapshot yet."""
    cutoff_ms = int((time.time() - days * 86400) * 1000)
    trades = _api(bot).closed_trades_since(cutoff_ms)
    pair_stats = aggregate_by_pair(trades)
    snapshots = pair_param_calibration.get_profile_snapshots()
    result = classify_pairs(pair_stats, snapshots)
    return {"window_days": days, "pairs": result}


def _priority_windows(
    bot: str, mid_days: int, long_days: int,
    mid_stats_override: Optional[dict], long_stats_override: Optional[dict],
) -> tuple[dict, dict]:
    """
    mid/long per-pair stats for pair_priority: an override (e.g. aggregated
    locally from a backtest --export trades run via the strategy-tester
    skill - the "calibration phase") wins over live bot history (the
    "accumulating phase") so the same classifier serves both without the
    caller needing two different tools.
    """
    mid_stats = mid_stats_override
    if mid_stats is None:
        cutoff_ms = int((time.time() - mid_days * 86400) * 1000)
        mid_stats = aggregate_by_pair(_api(bot).closed_trades_since(cutoff_ms))
    long_stats = long_stats_override
    if long_stats is None:
        cutoff_ms = int((time.time() - long_days * 86400) * 1000)
        long_stats = aggregate_by_pair(_api(bot).closed_trades_since(cutoff_ms))
    return mid_stats, long_stats


@mcp.tool()
def get_pair_priority(
    bot: str = DEFAULT_BOT,
    mid_days: int = 90,
    long_days: int = 180,
    mid_stats_override: Optional[dict] = None,
    long_stats_override: Optional[dict] = None,
) -> dict:
    """Per-pair money-allocation category and the stake-sizing money_weight
    that goes with it (read-only - see calibrate_pair_priority to persist):
      - consistent_winner (1.0): net-positive on both windows, n>=3 in each.
      - consistent_loser (0.5): net-negative on both windows, n>=3 in each.
      - volatile_profitable (0.1): has losses but profit_factor>=1.5 and net
        P&L positive - wins pay for the losses (e.g. a pair with 9 trades,
        2 losses, net +50 USDT should NOT be treated the same as a pair with
        1 trade that lost everything).
      - no_win (0.0): has closed at least one trade and never won.
      - insufficient_data (1.0, unchanged): not enough history yet to judge.
    consistent_loser/volatile_profitable are flagged for calibration
    refinement (pair_param_calibration), not auto-blacklisting.
    Pass *_stats_override (stats.aggregate_by_pair-shaped dicts, e.g. from a
    local backtest export) to classify from that instead of live bot
    history - the "calibration phase" before enough dry-run data exists."""
    mid_stats, long_stats = _priority_windows(bot, mid_days, long_days, mid_stats_override, long_stats_override)
    result = classify_pairs_priority(mid_stats, long_stats)
    return {"mid_days": mid_days, "long_days": long_days, "pairs": result}


@mcp.tool()
def calibrate_pair_priority(
    bot: str = DEFAULT_BOT,
    mid_days: int = 90,
    long_days: int = 180,
    reason: str = "",
    mid_stats_override: Optional[dict] = None,
    long_stats_override: Optional[dict] = None,
) -> dict:
    """Same classification as get_pair_priority, but also persists each
    pair's money_weight into pair_strategy_params.json (source="priority") -
    NostalgiaForInfinityX7EMA200's custom_stake_amount reads it per pair via
    the existing _apply_pair_params_cache/_param_for_pair hot-reload path
    within ~20-55s, no restart. Safe to call repeatedly (merge-write, other
    calibrated knobs for the same pair are untouched); re-run periodically
    as dry-run history accumulates to move pairs out of insufficient_data."""
    mid_stats, long_stats = _priority_windows(bot, mid_days, long_days, mid_stats_override, long_stats_override)
    result = classify_pairs_priority(mid_stats, long_stats)
    entries = {pair: {"money_weight": verdict["money_weight"]} for pair, verdict in result.items()}
    pair_param_calibration.write_pair_params(entries, source="priority", reason=reason)
    return {"mid_days": mid_days, "long_days": long_days, "written_pairs": len(entries), "pairs": result}


@mcp.tool()
def get_exit_summary(bot: str = DEFAULT_BOT, hours: float = 24.0) -> dict:
    """Per-trade exit log for the last N hours: pair, entry signal, exit
    reason, profit, close time, oldest first, plus a net total. Use this
    instead of asking for raw docker logs/RPC messages - scraping a 24h log
    window over ssh is huge and times out; this is one small API call. Call
    once per bot to compare NFI vs SampleStrategy."""
    cutoff_ms = int((time.time() - hours * 3600) * 1000)
    trades = _api(bot).closed_trades_since(cutoff_ms)
    result = exit_summary(trades)
    result["window_hours"] = hours
    return result


@mcp.tool()
def get_enabled_signals() -> dict:
    """Effective NFI X7 entry-signal state: enabled signal ids per side, plus
    every signal whose state differs from the strategy default (showing
    default vs config.json vs hot override vs effective value), and the
    current strategy_control.json content including the EMA200 guard flag.
    Call before toggling signals to see the current state."""
    return signal_control.get_state()


@mcp.tool()
def get_control_log(limit: int = 20) -> dict:
    """Journal of runtime control actions (signal toggles, EMA200 guard
    changes) with timestamps, reasons and before/after state. Use it when
    judging signal statistics so you know when a signal was actually active."""
    return signal_control.journal_tail(limit)


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
    calibration = signal_control.grey_zone_calibration(pair)
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
            return None  # not currently inside the grey-zone band at all
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
    return signal_control.set_override(side, signal_id, enabled, reason)


@mcp.tool()
def clear_signal_override(side: str, signal_id: str, reason: str) -> dict:
    """Remove a hot override for one signal, restoring the config.json/default
    behavior on the next candle."""
    return signal_control.clear_override(side, signal_id, reason)


@mcp.tool()
def set_ema200_guard(enabled: bool, reason: str) -> dict:
    """Toggle the EMA200 macro-trend guard of the NFI bot at runtime (long
    entries only above EMA200, shorts only below). Disabling it lets NFI
    trade against the macro trend - do this only deliberately."""
    return signal_control.set_ema200_guard(enabled, reason)


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
    return signal_control.set_risk_adjustment(pair, multiplier, ttl_hours, reason)


@mcp.tool()
def clear_risk_adjustment(pair: str, reason: str) -> dict:
    """Revoke a risk adjustment before its TTL expires - use when the
    catalyst that justified it has resolved or turned out to be false."""
    return signal_control.clear_risk_adjustment(pair, reason)


# --- Scheduled pair blocks (pre-staged, date-gated entry bans) ---------------

@mcp.tool()
def get_pair_blocks() -> dict:
    """List every pair_blocks entry, bucketed into active (blocking entries
    right now), scheduled (effective_from is still in the future) and
    expired. Unlike the static blacklist.json (regex, requires a strategy
    file deploy to change), these are hot-reloaded from
    strategy_control.json on the next 5m candle - same mechanism as
    toggle_signal."""
    return signal_control.get_pair_blocks()


@mcp.tool()
def schedule_pair_block(
    pair: str, reason: str, effective_from: str = "", ttl_days: float = 0
) -> dict:
    """Block new entries on one pair, with three independent dates: when the
    rule was written (always now, automatic), when it starts applying
    (effective_from), and when it stops (expires_at, derived from
    ttl_days). Only entries are blocked - open trades on the pair are not
    touched, same as the static blacklist.

    effective_from lets you PRE-STAGE a ban ahead of a known future event
    (a scheduled token unlock, a listing/delisting date, an announced
    hard-fork) instead of having to remember to flip it live the day of -
    pass an ISO-8601 timestamp (e.g. '2026-09-15T00:00:00Z'); omit it (or
    pass '') for an immediate block starting now. Capped at 90 days out.

    ttl_days controls how long the block lasts once it becomes effective:
    omit it (or pass 0) for an open-ended/structural block that stays until
    cleared with clear_pair_block; pass a number of days for a
    self-expiring one, same TTL philosophy as set_risk_adjustment (capped
    at 365 days).

    Always pass a specific reason - it is written to the audit journal, and
    unlike the static blacklist.json's free-text comments this one is
    structured and queryable via get_control_log."""
    return signal_control.schedule_pair_block(
        pair,
        reason,
        effective_from=effective_from or None,
        ttl_days=ttl_days or None,
    )


@mcp.tool()
def clear_pair_block(pair: str, reason: str) -> dict:
    """Remove a pair_blocks entry (scheduled, active, or already-expired)
    before/regardless of its own expiry - use when the reason for the block
    no longer applies, or when a pre-staged future block turns out to be
    unnecessary."""
    return signal_control.clear_pair_block(pair, reason)


# --- Recently-unbanned pairs (shadow-mode graduation) -------------------------

@mcp.tool()
def get_unbanned_pairs() -> dict:
    """List every pair tracked as 'recently unbanned' from the static
    blacklist, with its risk budget and shadow_mode (true = orders on this
    pair are locally simulated, never sent to the exchange, regardless of
    the bot's own dry_run setting - see NostalgiaForInfinityX7EMA200's
    module docstring). A pair stops being shadow-mode only when a human sets
    a nonzero budget via set_unbanned_pair_risk_budget - there is no
    automatic graduation."""
    return signal_control.get_unbanned_pairs()


@mcp.tool()
def mark_pair_unbanned(pair: str, reason: str) -> dict:
    """Start tracking a pair as 'recently unbanned' (shadow mode, zero risk
    budget) after removing it from the static blacklist.json. Call this
    right after the blacklist.json deploy that un-bans the pair - it does
    not touch blacklist.json itself, only strategy_control.json. The pair
    stays in shadow mode (real-looking trades that never touch the exchange)
    until set_unbanned_pair_risk_budget is called with a nonzero budget."""
    return signal_control.mark_pair_unbanned(pair, reason)


@mcp.tool()
def set_unbanned_pair_risk_budget(
    pair: str, risk_budget_pct: float, risk_budget_abs: float, reason: str
) -> dict:
    """Graduate a recently-unbanned pair from shadow mode to real trading by
    giving it a risk budget - THIS IS THE ONE ACTION THAT MAKES REAL MONEY
    START FLOWING for that pair. Pass risk_budget_pct=0 and
    risk_budget_abs=0 (with a reason) to push it back into shadow mode.
    risk_budget_pct is a % of account equity (capped at 5.0), risk_budget_abs
    is a stake-currency amount (capped at 1000) - both are informational caps
    tracked here, not yet enforced as a hard stake-amount ceiling by the
    strategy itself. The pair must already be tracked via mark_pair_unbanned.
    Takes effect on the next 5m candle, same as toggle_signal - no restart."""
    return signal_control.set_unbanned_pair_risk_budget(pair, risk_budget_pct, risk_budget_abs, reason)


@mcp.tool()
def clear_unbanned_pair(pair: str, reason: str) -> dict:
    """Stop tracking a pair as 'recently unbanned' entirely (removes it from
    unbanned_pairs) - use once a pair has proven itself and should just be
    treated as an ordinary whitelisted pair with no special tracking, or to
    revert mark_pair_unbanned if it was added by mistake. If the pair was
    still in shadow mode, this makes it trade for real immediately (no
    unbanned_pairs entry at all means shadow mode never applies) - prefer
    set_unbanned_pair_risk_budget(0, 0, ...) if you just want to pause it."""
    return signal_control.clear_unbanned_pair(pair, reason)


# --- Momentum-entry ("900") green-streak N calibration -----------------------

@mcp.tool()
def get_green_streak_n(pair: str) -> dict:
    """Read-only: the momentum entry's ("900") cached green-streak N for one
    pair, plus its age and whether it's stale (older than its ttl_hours,
    default 1 week). Returns cache_hit=False if the pair was never analyzed
    - in that case the strategy is using its own green_streak_default_n, not
    a per-pair value. Call before compute_green_streak_n to see whether a
    refresh is actually needed."""
    entry = green_streak.get_cached(pair)
    if entry is None:
        return {"pair": pair, "cache_hit": False}
    return {"cache_hit": True, **entry}


@mcp.tool()
def compute_green_streak_n(pair: str, lookback_days: int = 90, reason: str = "") -> dict:
    """
    Calibrate the momentum entry's ("900") green-streak gate for one pair:
    fetches lookback_days of 1h candles from Binance futures (public
    endpoint, no key needed), buckets historical candles by "how many
    consecutive green 1h candles ended here", and for each bucket size N
    computes the 24h-forward-return expectancy (win_rate*avg_gain -
    loss_rate*avg_loss). Writes the best N to green_streak_cache.json if one
    met the minimum sample-count floor (20 historical occurrences) - the
    bot's next bot_loop_start poll (within ~20-55s) picks it up and only
    fires "900" on that pair's Nth consecutive green 1h candle from then on.

    If no N met the sample floor (happens on pairs with too little history,
    or ones that just don't have enough long green streaks), the cache is
    left untouched - the strategy keeps its previous cached N (or its
    built-in default) rather than being overwritten with nothing. Returns
    the full per-N breakdown either way so you can see why.

    Re-running this is the intended way to keep a pair's N fresh - the
    strategy treats a cache entry older than its ttl_hours (default 1 week)
    as stale and falls back to the default, so pairs you care about need
    periodic re-calibration, not a one-time call.
    """
    try:
        return green_streak.compute_and_cache(pair, lookback_days=lookback_days, reason=reason)
    except GreenStreakError as exc:
        return {"pair": pair, "error": str(exc)}


# --- Per-pair parameter calibration (all 20 PAIR_PARAM_SPECS knobs) ----------

@mcp.tool()
def get_pair_calibration(pair: str) -> dict:
    """Read-only: this pair's current pair_strategy_params.json entries (all
    20 knobs, whatever their source - seed or calibrated) plus the
    calibration profile snapshot they were computed from. Returns None-ish
    (no 'params' key) if the pair was never calibrated/seeded - in that case
    the strategy is using plain global defaults for every knob. Call before
    calibrate_pair_params to see whether a refresh is actually needed."""
    status = pair_param_calibration.get_calibration_status(pair)
    if status is None:
        return {"pair": pair, "calibrated": False}
    return {"calibrated": True, **status}


@mcp.tool()
def calibrate_pair_params(pair: str, lookback_days: int = 90, reason: str = "") -> dict:
    """
    Calibrate all 20 PAIR_PARAM_SPECS knobs for one pair from lookback_days
    (default 90) of 1h Binance futures candles + funding-rate history
    (public endpoints, no key needed): volatility/ATR relative to BTC, a
    rolling-peak drawdown-cascade study (same methodology as the original
    grey-zone calibration), funding-rate and volume-spike distributions, and
    correlation to BTC. Writes every derived value into
    pair_strategy_params.json (source="calibrated") and a profile snapshot
    into pair_calibration_profiles.json for later detect_pair_drift calls to
    compare against. Takes effect on the bot's next bot_loop_start poll
    (within ~20-55s) - no reload, no restart.

    This is a first calibration pass per pair, not a hand-tuned final answer
    - see pair_param_calibration.py's module docstring for the exact formula
    behind each knob. Safe to re-run any time; each call fully replaces the
    pair's previous entries and re-baselines its drift profile.
    """
    try:
        return pair_param_calibration.compute_and_cache(pair, lookback_days=lookback_days, reason=reason)
    except CalibrationError as exc:
        return {"pair": pair, "error": str(exc)}


@mcp.tool()
def calibrate_all_pair_params(bot: str = DEFAULT_BOT, lookback_days: int = 90, reason: str = "") -> dict:
    """
    Bulk calibrate_pair_params over the bot's live whitelist. Sequential
    (small delay between pairs, courtesy to Binance's public endpoints, not
    an auth requirement) - for ~90-100 pairs this takes a few minutes.
    Returns counts and any per-pair errors rather than aborting the whole
    batch on one bad symbol (e.g. a pair with too little Binance futures
    history). Re-running is the intended way to keep the whole whitelist
    fresh, same philosophy as compute_green_streak_n.
    """
    whitelist = _api(bot).get("whitelist")
    pairs = whitelist.get("whitelist", []) if isinstance(whitelist, dict) else []
    if not pairs:
        return {"error": "empty or unreadable whitelist from the bot API"}
    return pair_param_calibration.compute_and_cache_all(pairs, lookback_days=lookback_days, reason=reason)


# --- Concept-drift detection / flagging (adaptive-control fail-safe) ---------

@mcp.tool()
def get_drift_flags() -> dict:
    """List every currently-tracked concept-drift flag, bucketed into active
    (currently overriding that pair's calibration to global defaults, and
    blocking new entries if drift_block_entries_enabled) vs expired."""
    return pair_param_calibration.get_drift_flags()


@mcp.tool()
def detect_pair_drift(pair: str, lookback_days: int = 10) -> dict:
    """
    Read-only: re-measure a short recent window (default 10d, vs the 90d
    calibration window) and compare it against this pair's calibration
    profile snapshot - per-metric relative deviation (volatility, cascade
    rate, BTC correlation, funding, volume-spike distribution), weighted
    into one drift_score. drifted=True once the score crosses 0.5 (the
    weighted-average metric has moved by half its calibrated value).
    Returns has_baseline=False if the pair was never calibrated - nothing to
    compare against yet, call calibrate_pair_params first. Does not write
    anything; use flag_pair_drift or auto_flag_if_drifted to act on it."""
    try:
        return pair_param_calibration.detect_pair_drift(pair, lookback_days=lookback_days)
    except CalibrationError as exc:
        return {"pair": pair, "error": str(exc)}


@mcp.tool()
def auto_flag_if_drifted(pair: str, lookback_days: int = 10, ttl_hours: float = 72.0) -> dict:
    """detect_pair_drift + flag_pair_drift in one call, only writing a flag
    when the score actually crosses the threshold. This is the intended
    entry point for periodic/automated drift checking - there is no
    scheduler inside this server (same design as request_risk_analysis: a
    human or an LLM agent decides how often 'periodic' is and calls this).
    A written flag makes the strategy fall back to global defaults for that
    pair immediately and, if drift_block_entries_enabled is on, block new
    entries until the flag clears or self-expires (ttl_hours, default 72h)."""
    try:
        return pair_param_calibration.auto_flag_if_drifted(pair, lookback_days=lookback_days, ttl_hours=ttl_hours)
    except CalibrationError as exc:
        return {"pair": pair, "error": str(exc)}


@mcp.tool()
def flag_pair_drift(pair: str, reason: str, score: float = 0.0, ttl_hours: float = 72.0) -> dict:
    """Manually flag a pair as concept-drifted (skip the automatic
    threshold check) - use when you have a specific reason to distrust its
    current calibration (a listing event, an exchange incident, a sudden
    liquidity change) that the statistical check might not catch yet.
    Always pass a real reason - it is written to the audit journal. Same
    effect as auto_flag_if_drifted's automatic flag: global-default
    fallback immediately, entry block if drift_block_entries_enabled."""
    try:
        return pair_param_calibration.flag_pair_drift(pair, reason, score=score or None, ttl_hours=ttl_hours)
    except CalibrationError as exc:
        return {"pair": pair, "error": str(exc)}


@mcp.tool()
def clear_pair_drift_flag(pair: str, reason: str) -> dict:
    """Remove a drift flag before/regardless of its own TTL - call this
    after calibrate_pair_params has re-baselined the pair and its numbers
    can be trusted again. Clearing does NOT re-run a calibration by itself."""
    return pair_param_calibration.clear_pair_drift_flag(pair, reason)


@mcp.tool()
def request_drift_analysis(pair: str, lookback_days: int = 10) -> dict:
    """
    Read-only briefing for deciding whether a pair's apparent concept drift
    is worth acting on, mirroring request_risk_analysis exactly (assembles
    context, does not call any LLM itself, does not read news - YOU are
    expected to bring judgment). Use this instead of auto_flag_if_drifted
    when a statistical threshold crossing alone doesn't feel like enough
    evidence - e.g. the deviation is borderline, or you want to weigh
    corroborating context (news, orderbook, a known event) before deciding
    between three outcomes: do nothing, flag_pair_drift now (if you're
    convinced the regime genuinely changed), or calibrate_pair_params now
    (if you think this is just the calibration going stale, not a real
    shift, so simply recalibrating naturally resolves it).
    """
    detection = pair_param_calibration.detect_pair_drift(pair, lookback_days=lookback_days)
    if not detection.get("has_baseline"):
        return {
            "pair": pair,
            "detection": detection,
            "prompt": (
                f"{pair} has never been calibrated (no pair_calibration_profiles.json "
                "entry) - there is nothing to compare a recent window against. Call "
                "calibrate_pair_params first if you want this pair's knobs to be "
                "pair-specific at all; there is no drift decision to make yet."
            ),
        }
    if detection.get("error"):
        return {"pair": pair, "detection": detection, "prompt": f"Could not complete drift check: {detection['error']}"}

    lines = []
    for metric, row in detection["deviations"].items():
        lines.append(
            f"  {metric}: baseline {row['baseline']:.6g} -> current {row['current']:.6g} "
            f"({row['relative_deviation']:.1%} relative deviation)"
        )
    prompt = f"""You are assessing whether {pair}'s per-pair calibration (see
pair_strategy_params.json / calibrate_pair_params) still reflects how this
pair actually behaves, or whether the market regime has moved enough that
the calibration should be distrusted.

BASELINE vs RECENT ({detection['recheck_lookback_days']}d window, calibrated
{detection['baseline_lookback_days']}d ago on {detection['baseline_computed_at']}):
{chr(10).join(lines) if lines else "  no comparable metrics"}

Weighted drift_score: {detection['drift_score']} (threshold {detection['threshold']} -
{"ALREADY OVER threshold" if detection['drifted'] else "still under threshold"}).

YOUR TASK: using this evidence (and any external context you have - news,
market structure, a known event on this pair), decide ONE of:
  1. Do nothing. A single metric moving is often just noise or a temporary
     regime (e.g. a news-driven volatility spike) that will revert - don't
     react to every fluctuation.
  2. flag_pair_drift(pair, reason, score, ttl_hours) - if you're convinced
     this is a genuine, likely-durable shift (the pair's fundamental
     behavior changed - a listing tier change, a liquidity regime shift,
     a structural event) that should distrust the calibration until it's
     redone. This immediately reverts the pair to plain global defaults and
     (if drift_block_entries_enabled) blocks new entries.
  3. calibrate_pair_params(pair) - if you think the calibration is simply
     stale (normal drift from an aging 90d window, nothing alarming) and a
     fresh calibration would naturally resolve the deviation without
     needing a distrust flag at all.

There is no autonomous default here - inaction is a legitimate answer if
the evidence doesn't convince you either way."""

    return {"pair": pair, "detection": detection, "prompt": prompt}


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
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
