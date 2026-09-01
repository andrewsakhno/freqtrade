"""
Hot NFI signal control — runs natively on the same host as the bot, so this
reads/writes strategy_control.json and the strategy file directly (no ssh).

The NostalgiaForInfinityX7EMA200 subclass re-reads strategy_control.json every
bot loop and mutates its signal-enable dicts in place; populate_entry_trend
reads those dicts on every call, so a write here takes effect on the bot's
next 5m candle. No /reload_config, no container restart.
"""

import ast
import datetime
import json
import math
import os
import re
import threading
from typing import Any, Optional

from .json_utils import load_json_with_comments

CONTROL_PATH = "/opt/nfi/user_data/strategy_control.json"
JOURNAL_PATH = "/opt/nfi/user_data/strategy_control_log.jsonl"
CONFIG_PATH = "/opt/nfi/user_data/config.json"
STRATEGY_PATH = "/opt/nfi/user_data/strategies/NostalgiaForInfinityX7.py"
EMA200_STRATEGY_PATH = "/opt/nfi/user_data/strategies/NostalgiaForInfinityX7EMA200.py"

DEFAULT_CONTROL = {
    "long_signals_override": {},
    "short_signals_override": {},
    "ema200_guard_enabled": True,
    "risk_adjustments": {},
    "pair_blocks": {},
    "unbanned_pairs": {},
}

# Bounds for set_risk_adjustment. The clamp here is belt-and-braces — the
# REAL safety limit is the strategy's own grey_zone_exit_min_full_hours/
# max_full_hours clamp, which no multiplier value can escape. See
# NostalgiaForInfinityX7EMA200's module docstring.
RISK_ADJUSTMENT_MULTIPLIER_MIN = 0.25
RISK_ADJUSTMENT_MULTIPLIER_MAX = 4.0
RISK_ADJUSTMENT_TTL_MIN_HOURS = 0.25
RISK_ADJUSTMENT_TTL_MAX_HOURS = 24.0
RISK_ADJUSTMENT_MAX_ENTRIES = 20
RISK_ADJUSTMENT_REASON_MAX_LEN = 500
_PAIR_RE = re.compile(r"^[A-Z0-9]+/[A-Z0-9]+(:[A-Z0-9]+)?$")

# Bounds for schedule_pair_block. A block can be pre-staged to start in the
# future (e.g. ahead of a known token-unlock/listing event) and can be left
# open-ended (expires_at=None) for a structural ban, or bounded like a
# risk_adjustment for a temporary one. Three timestamps distinguish "when
# was this rule written" from "when does it start applying" from "when does
# it stop" — created_at is always now; effective_from and expires_at are
# both caller-controlled.
PAIR_BLOCK_MAX_LEAD_DAYS = 90
PAIR_BLOCK_MAX_TTL_DAYS = 365
PAIR_BLOCK_MAX_ENTRIES = 100
PAIR_BLOCK_REASON_MAX_LEN = 500

# Bounds for unbanned_pairs / set_unbanned_pair_risk_budget. A pair enters
# shadow mode (both budgets 0) automatically via mark_pair_unbanned - see the
# blacklist diff workflow. It only leaves shadow mode when a human sets a
# nonzero budget here; there is no automatic graduation.
UNBANNED_PAIR_MAX_ENTRIES = 50
UNBANNED_PAIR_REASON_MAX_LEN = 500
UNBANNED_PAIR_MAX_RISK_BUDGET_PCT = 5.0  # of account equity, per pair
UNBANNED_PAIR_MAX_RISK_BUDGET_ABS = 1000.0  # stake currency units, per pair

# Fallback calibration used only if the deployed EMA200 strategy file
# predates the grey-zone feature (grey_zone_calibration must degrade
# gracefully, not raise, during the gap between an MCP deploy and a
# strategy deploy). Keep in sync with NostalgiaForInfinityX7EMA200's
# GREY_ZONE_CASCADE_PCT_72H / GREY_ZONE_CASCADE_GLOBAL_SHAPE.
_FALLBACK_CASCADE_PCT_72H = {
    "BTC": 4.9, "ETH": 15.1, "SOL": 22.5, "XRP": 22.8,
    "DOGE": 27.0, "ADA": 30.4, "LINK": 23.7, "AVAX": 20.1,
    "*": 20.7,
}
_FALLBACK_GLOBAL_SHAPE = ((6.0, 0.8), (24.0, 8.4), (72.0, 20.7))
_FALLBACK_DEFAULTS = {
    "grey_zone_exit_ref_hours": 72.0,
    "grey_zone_exit_ref_cascade_pct": 20.7,
    "grey_zone_exit_pair_sensitivity": 3.0,
    "grey_zone_exit_min_full_hours": 24.0,
    "grey_zone_exit_max_full_hours": 168.0,
    "grey_zone_exit_curve_exponent": 5.0,
    "grey_zone_exit_start_hours": None,
    "grey_zone_exit_floor_ratio": None,
    "stale_exit_hours": 6.0,
    "stale_exit_max_loss": 0.015,
    "catastrophic_exit_loss_ratio": 0.20,
}

# All file operations below are quick and local; a plain lock is enough to
# serialize concurrent tool calls from the same MCP server process (the
# bot's own read of the file is a separate process and doesn't need it —
# save_control()'s os.replace is atomic against a concurrent reader).
_lock = threading.Lock()


class SignalControlError(RuntimeError):
    pass


def _load_control() -> dict:
    if not os.path.exists(CONTROL_PATH):
        return dict(DEFAULT_CONTROL)
    with open(CONTROL_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    for key, value in DEFAULT_CONTROL.items():
        data.setdefault(key, value if not isinstance(value, dict) else dict(value))
    return data


def _save_control(data: dict) -> None:
    tmp = CONTROL_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, CONTROL_PATH)


def _journal_append(entry: dict) -> None:
    with open(JOURNAL_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _strategy_defaults() -> dict:
    """Extract signal-enable dicts from the NFI class body via ast (no import
    — avoids pulling in talib/pandas/the whole strategy just to read two
    literal dict assignments)."""
    with open(STRATEGY_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    result = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "NostalgiaForInfinityX7":
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    target = stmt.targets[0]
                    if isinstance(target, ast.Name) and target.id in (
                        "long_entry_signal_params",
                        "short_entry_signal_params",
                    ):
                        result[target.id] = ast.literal_eval(stmt.value)
            break
    return result


def _parse_iso(value: Any) -> Optional[datetime.datetime]:
    """Tolerant ISO-8601 parse: returns None on anything unparseable, and
    normalizes a naive datetime to UTC (freqtrade/the strategy always
    compares tz-aware UTC datetimes)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _prune_expired(control: dict, now: datetime.datetime, key: str = "risk_adjustments") -> list[str]:
    """Drop entries of the given dict (risk_adjustments or pair_blocks) whose
    expires_at is in the past. Cosmetic (the strategy already ignores
    expired entries on its own) but keeps get_state readable and bounds
    file growth. Returns the dropped keys for logging."""
    entries = control.get(key)
    if not isinstance(entries, dict):
        return []
    dropped = []
    for entry_key in list(entries):
        entry = entries.get(entry_key)
        if not isinstance(entry, dict):
            continue
        expires_at = _parse_iso(entry.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            del entries[entry_key]
            dropped.append(entry_key)
    return dropped


def _ema200_strategy_defaults() -> dict:
    """Extract the grey-zone calibration constants + tunable defaults from
    NostalgiaForInfinityX7EMA200's class body via ast (no import — same
    reason as _strategy_defaults: avoids pulling in talib/pandas/the whole
    base strategy). Returns {} if the file doesn't exist or predates the
    feature, so callers can degrade gracefully instead of raising."""
    if not os.path.exists(EMA200_STRATEGY_PATH):
        return {}
    with open(EMA200_STRATEGY_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    wanted = {
        "GREY_ZONE_CASCADE_PCT_72H",
        "GREY_ZONE_CASCADE_GLOBAL_SHAPE",
        "grey_zone_exit_ref_hours",
        "grey_zone_exit_ref_cascade_pct",
        "grey_zone_exit_pair_sensitivity",
        "grey_zone_exit_min_full_hours",
        "grey_zone_exit_max_full_hours",
        "grey_zone_exit_curve_exponent",
        "grey_zone_exit_start_hours",
        "grey_zone_exit_floor_ratio",
        "stale_exit_hours",
        "stale_exit_max_loss",
        "catastrophic_exit_loss_ratio",
    }
    result: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "NostalgiaForInfinityX7EMA200":
            for stmt in node.body:
                targets = None
                value_node = None
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                    targets, value_node = stmt.targets[0].id, stmt.value
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
                    targets, value_node = stmt.target.id, stmt.value
                if targets in wanted:
                    try:
                        result[targets] = ast.literal_eval(value_node)
                    except (ValueError, SyntaxError):
                        pass
            break
    return result


def grey_zone_calibration(pair: str) -> dict:
    """
    Read-only: the grey-zone exit's baseline calibration for one pair,
    overlaid with any live strategy_control.json overrides, plus the
    currently-resolved cascade curve and the active/expired risk adjustment
    for that pair. Used by request_risk_analysis to brief a caller before it
    decides whether to call set_risk_adjustment. Degrades gracefully (a
    "warning" key, not an exception) if the deployed strategy predates the
    grey-zone feature.
    """
    strategy_defaults = _ema200_strategy_defaults()
    warning = None
    if not strategy_defaults:
        warning = (
            "strategy file on the server has no grey-zone calibration yet "
            "(or NostalgiaForInfinityX7EMA200.py is missing) — using MCP-side "
            "fallback defaults; the bot is not necessarily running this feature"
        )

    cascade_table = strategy_defaults.get("GREY_ZONE_CASCADE_PCT_72H") or _FALLBACK_CASCADE_PCT_72H
    global_shape = strategy_defaults.get("GREY_ZONE_CASCADE_GLOBAL_SHAPE") or _FALLBACK_GLOBAL_SHAPE

    def resolved(key):
        if key in strategy_defaults:
            return strategy_defaults[key]
        return _FALLBACK_DEFAULTS[key]

    control = _load_control()
    now = datetime.datetime.now(datetime.timezone.utc)
    _prune_expired(control, now)

    # Live control-file overrides win over strategy-file defaults for the
    # scalar tunables (mirrors _apply_grey_zone_exit_control's precedence).
    ref_hours = control.get("grey_zone_exit_ref_hours", resolved("grey_zone_exit_ref_hours"))
    ref_cascade_pct = control.get("grey_zone_exit_ref_cascade_pct", resolved("grey_zone_exit_ref_cascade_pct"))
    sensitivity = control.get("grey_zone_exit_pair_sensitivity", resolved("grey_zone_exit_pair_sensitivity"))
    min_full_hours = control.get("grey_zone_exit_min_full_hours", resolved("grey_zone_exit_min_full_hours"))
    max_full_hours = control.get("grey_zone_exit_max_full_hours", resolved("grey_zone_exit_max_full_hours"))
    curve_exponent = control.get("grey_zone_exit_curve_exponent", resolved("grey_zone_exit_curve_exponent"))
    start_hours = control.get("grey_zone_exit_start_hours", resolved("grey_zone_exit_start_hours"))
    floor_ratio = control.get("grey_zone_exit_floor_ratio", resolved("grey_zone_exit_floor_ratio"))
    if start_hours is None:
        start_hours = control.get("stale_exit_hours", resolved("stale_exit_hours"))
    if floor_ratio is None:
        floor_ratio = control.get("stale_exit_max_loss", resolved("stale_exit_max_loss"))
    ceiling = control.get("catastrophic_exit_loss_ratio", resolved("catastrophic_exit_loss_ratio"))

    base = pair.split("/", 1)[0].strip().upper()
    cascade_pct_72h = cascade_table.get(base, cascade_table["*"])
    calibration_source = "measured" if base in cascade_table else "fallback (*)"

    def full_hours(multiplier: float) -> float:
        c = max(cascade_pct_72h * multiplier, 0.01)
        h = ref_hours * (ref_cascade_pct / c) ** sensitivity
        return min(max(h, min_full_hours), max_full_hours)

    # Model the 6h/24h points per-pair by scaling the measured global shape
    # by this pair's ratio to the reference cascade rate (see
    # NostalgiaForInfinityX7EMA200's calibration comment) — only the 72h
    # column is directly measured per-pair, so label honestly.
    cascade_curve = []
    for hours, global_pct in global_shape:
        if abs(hours - 72.0) < 1e-9:
            cascade_curve.append({"hours": hours, "cascade_pct": round(cascade_pct_72h, 1), "source": "measured"})
        else:
            modelled = global_pct * (cascade_pct_72h / 20.7)
            cascade_curve.append({"hours": hours, "cascade_pct": round(modelled, 1), "source": "modelled"})

    adjustments = control.get("risk_adjustments", {}) or {}
    active_adj = None
    for key in (pair, "*"):
        entry = adjustments.get(key)
        if not isinstance(entry, dict):
            continue
        expires_at = _parse_iso(entry.get("expires_at"))
        if expires_at is None or expires_at > now:
            active_adj = dict(entry, pair_or_wildcard=key, hours_remaining=(
                None if expires_at is None else round((expires_at - now).total_seconds() / 3600.0, 2)
            ))
            break

    return {
        "pair": pair,
        "warning": warning,
        "cascade_pct_72h": cascade_pct_72h,
        "calibration_source": calibration_source,
        "cascade_curve": cascade_curve,
        "resolved_params": {
            "start_hours": start_hours,
            "floor_ratio": floor_ratio,
            "ceiling_ratio": ceiling,
            "ref_hours": ref_hours,
            "ref_cascade_pct": ref_cascade_pct,
            "pair_sensitivity": sensitivity,
            "curve_exponent": curve_exponent,
            "min_full_hours": min_full_hours,
            "max_full_hours": max_full_hours,
        },
        "full_hours_baseline": round(full_hours(1.0), 1),
        "active_risk_adjustment": active_adj,
        "full_hours_with_active_adjustment": (
            round(full_hours(active_adj["multiplier"]), 1) if active_adj else None
        ),
        "bounds": {
            "multiplier": [RISK_ADJUSTMENT_MULTIPLIER_MIN, RISK_ADJUSTMENT_MULTIPLIER_MAX],
            "ttl_hours": [RISK_ADJUSTMENT_TTL_MIN_HOURS, RISK_ADJUSTMENT_TTL_MAX_HOURS],
        },
    }


def _config_signal_params() -> tuple[dict, dict]:
    cfg = load_json_with_comments(CONFIG_PATH)
    nfi = cfg.get("nfi_parameters", {}) or {}
    return (
        nfi.get("long_entry_signal_params", {}) or {},
        nfi.get("short_entry_signal_params", {}) or {},
    )


def _side_state(side: str, default_params: dict, cfg_params: dict, overrides: dict) -> dict:
    template = side + "_entry_condition_{}_enable"
    merged = dict(default_params)
    merged.update({k: v for k, v in cfg_params.items() if k in merged})
    for sid, val in overrides.items():
        key = template.format(sid)
        if key in merged:
            merged[key] = bool(val)
    enabled = sorted(int(k.rsplit("_", 2)[1]) for k, v in merged.items() if v)
    diffs = {}
    for key, default_val in default_params.items():
        sid = key.rsplit("_", 2)[1]
        cfg_val = cfg_params.get(key)
        override_val = overrides.get(sid)
        if (cfg_val is not None and cfg_val != default_val) or override_val is not None:
            diffs[sid] = {
                "default": default_val,
                "config": cfg_val,
                "override": override_val,
                "effective": merged[key],
            }
    return {"enabled": enabled, "non_default": diffs}


def get_state() -> dict:
    defaults = _strategy_defaults()
    cfg_long, cfg_short = _config_signal_params()
    control = _load_control()
    return {
        "long": _side_state(
            "long", defaults.get("long_entry_signal_params", {}), cfg_long,
            control.get("long_signals_override", {}),
        ),
        "short": _side_state(
            "short", defaults.get("short_entry_signal_params", {}), cfg_short,
            control.get("short_signals_override", {}),
        ),
        "ema200_guard_enabled": control.get("ema200_guard_enabled", True),
        "risk_adjustments": _risk_adjustments_state(control),
        "pair_blocks": _pair_blocks_state(control),
        "control_file": control,
    }


def _risk_adjustments_state(control: dict) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc)
    active, expired = {}, {}
    for key, entry in (control.get("risk_adjustments", {}) or {}).items():
        if not isinstance(entry, dict):
            continue
        expires_at = _parse_iso(entry.get("expires_at"))
        bucket = expired if (expires_at is not None and expires_at <= now) else active
        row = dict(entry)
        if expires_at is not None:
            row["hours_remaining"] = round((expires_at - now).total_seconds() / 3600.0, 2)
        bucket[key] = row
    return {"active": active, "expired": expired}


def _pair_blocks_state(control: dict) -> dict:
    """Buckets each pair_blocks entry into scheduled (effective_from still in
    the future), active (currently blocking entries) or expired, mirroring
    how the strategy itself will treat it on the next candle."""
    now = datetime.datetime.now(datetime.timezone.utc)
    scheduled, active, expired = {}, {}, {}
    for pair, entry in (control.get("pair_blocks", {}) or {}).items():
        if not isinstance(entry, dict):
            continue
        effective_from = _parse_iso(entry.get("effective_from"))
        expires_at = _parse_iso(entry.get("expires_at"))
        row = dict(entry)
        if expires_at is not None:
            row["hours_remaining"] = round((expires_at - now).total_seconds() / 3600.0, 2)
        if expires_at is not None and expires_at <= now:
            expired[pair] = row
        elif effective_from is not None and effective_from > now:
            row["hours_until_effective"] = round((effective_from - now).total_seconds() / 3600.0, 2)
            scheduled[pair] = row
        else:
            active[pair] = row
    return {"active": active, "scheduled": scheduled, "expired": expired}


def journal_tail(limit: int = 20) -> dict:
    if not os.path.exists(JOURNAL_PATH):
        return {"entries": []}
    with open(JOURNAL_PATH, encoding="utf-8") as fh:
        lines = fh.readlines()
    return {"entries": [json.loads(line) for line in lines[-limit:]]}


def _mutate(kind: str, **params: Any) -> dict:
    with _lock:
        control = _load_control()
        before = json.loads(json.dumps(control))

        if kind in ("set_override", "clear_override"):
            side = params["side"]
            if side not in ("long", "short"):
                raise SignalControlError("side must be 'long' or 'short'")
            signal_id = str(params["signal_id"])
            key = f"{side}_entry_condition_{signal_id}_enable"
            defaults = _strategy_defaults().get(f"{side}_entry_signal_params", {})
            if key not in defaults:
                raise SignalControlError(f"unknown signal id {signal_id!r} for side {side}")
            overrides = control.setdefault(f"{side}_signals_override", {})
            if kind == "set_override":
                overrides[signal_id] = bool(params["enabled"])
            else:
                overrides.pop(signal_id, None)
        elif kind == "set_ema200_guard":
            control["ema200_guard_enabled"] = bool(params["enabled"])
        elif kind == "set_risk_adjustment":
            pair = params["pair"]
            if not isinstance(pair, str) or not pair.strip():
                raise SignalControlError("pair must be a non-empty string")
            pair = pair.strip()
            if pair != "*" and not _PAIR_RE.match(pair):
                raise SignalControlError(
                    f"pair must be '*' or a freqtrade pair like 'ADA/USDT:USDT', got {pair!r}"
                )

            multiplier = params["multiplier"]
            if not isinstance(multiplier, (int, float)) or isinstance(multiplier, bool):
                raise SignalControlError("multiplier must be a number")
            if not math.isfinite(multiplier):
                raise SignalControlError("multiplier must be finite")
            if multiplier <= 0:
                raise SignalControlError("multiplier must be positive")
            multiplier_applied = min(max(multiplier, RISK_ADJUSTMENT_MULTIPLIER_MIN), RISK_ADJUSTMENT_MULTIPLIER_MAX)

            ttl_hours = params["ttl_hours"]
            if not isinstance(ttl_hours, (int, float)) or isinstance(ttl_hours, bool):
                raise SignalControlError("ttl_hours must be a number")
            if not math.isfinite(ttl_hours):
                raise SignalControlError("ttl_hours must be finite")
            if ttl_hours <= 0:
                raise SignalControlError("ttl_hours must be positive")
            ttl_applied = min(max(ttl_hours, RISK_ADJUSTMENT_TTL_MIN_HOURS), RISK_ADJUSTMENT_TTL_MAX_HOURS)

            reason = params["reason"]
            if not isinstance(reason, str) or not reason.strip():
                raise SignalControlError("reason is required — it is the audit trail")
            reason = reason.strip()
            if len(reason) > RISK_ADJUSTMENT_REASON_MAX_LEN:
                reason = reason[:RISK_ADJUSTMENT_REASON_MAX_LEN] + "…"

            now = datetime.datetime.now(datetime.timezone.utc)
            _prune_expired(control, now)
            adjustments = control.setdefault("risk_adjustments", {})
            if pair not in adjustments and len(adjustments) >= RISK_ADJUSTMENT_MAX_ENTRIES:
                raise SignalControlError(
                    f"too many risk adjustments ({RISK_ADJUSTMENT_MAX_ENTRIES}); clear one first"
                )
            adjustments[pair] = {
                "multiplier": multiplier_applied,
                "expires_at": (now + datetime.timedelta(hours=ttl_applied)).isoformat(timespec="seconds"),
                "set_at": now.isoformat(timespec="seconds"),
                "ttl_hours": ttl_applied,
                "reason": reason,
            }
            params = dict(
                params,
                multiplier_requested=multiplier,
                multiplier_applied=multiplier_applied,
                ttl_hours_requested=ttl_hours,
                ttl_hours_applied=ttl_applied,
            )
        elif kind == "clear_risk_adjustment":
            now = datetime.datetime.now(datetime.timezone.utc)
            _prune_expired(control, now)
            control.setdefault("risk_adjustments", {}).pop(params["pair"], None)
        elif kind == "schedule_pair_block":
            pair = params["pair"]
            if not isinstance(pair, str) or not pair.strip():
                raise SignalControlError("pair must be a non-empty string")
            pair = pair.strip()
            if not _PAIR_RE.match(pair):
                raise SignalControlError(f"pair must look like 'ADA/USDT:USDT', got {pair!r}")

            reason = params["reason"]
            if not isinstance(reason, str) or not reason.strip():
                raise SignalControlError("reason is required — it is the audit trail")
            reason = reason.strip()
            if len(reason) > PAIR_BLOCK_REASON_MAX_LEN:
                reason = reason[:PAIR_BLOCK_REASON_MAX_LEN] + "…"

            now = datetime.datetime.now(datetime.timezone.utc)

            effective_from_raw = params.get("effective_from")
            if effective_from_raw is None:
                effective_from = now
            else:
                effective_from = _parse_iso(effective_from_raw)
                if effective_from is None:
                    raise SignalControlError(
                        f"effective_from must be an ISO-8601 timestamp, got {effective_from_raw!r}"
                    )
                lead_limit = now + datetime.timedelta(days=PAIR_BLOCK_MAX_LEAD_DAYS)
                if effective_from > lead_limit:
                    raise SignalControlError(
                        f"effective_from is more than {PAIR_BLOCK_MAX_LEAD_DAYS} days out "
                        f"({effective_from_raw!r}) — too far ahead to pre-stage usefully"
                    )

            ttl_days = params.get("ttl_days")
            if ttl_days is None:
                expires_at = None
            else:
                if not isinstance(ttl_days, (int, float)) or isinstance(ttl_days, bool):
                    raise SignalControlError("ttl_days must be a number or omitted for an open-ended block")
                if not math.isfinite(ttl_days) or ttl_days <= 0:
                    raise SignalControlError("ttl_days must be positive")
                ttl_days_applied = min(ttl_days, PAIR_BLOCK_MAX_TTL_DAYS)
                expires_at = effective_from + datetime.timedelta(days=ttl_days_applied)

            _prune_expired(control, now, key="pair_blocks")
            blocks = control.setdefault("pair_blocks", {})
            if pair not in blocks and len(blocks) >= PAIR_BLOCK_MAX_ENTRIES:
                raise SignalControlError(f"too many pair_blocks ({PAIR_BLOCK_MAX_ENTRIES}); clear one first")
            blocks[pair] = {
                "created_at": now.isoformat(timespec="seconds"),
                "effective_from": effective_from.isoformat(timespec="seconds"),
                "expires_at": expires_at.isoformat(timespec="seconds") if expires_at else None,
                "reason": reason,
            }
            params = dict(params, effective_from_applied=blocks[pair]["effective_from"],
                          expires_at_applied=blocks[pair]["expires_at"])
        elif kind == "clear_pair_block":
            now = datetime.datetime.now(datetime.timezone.utc)
            _prune_expired(control, now, key="pair_blocks")
            control.setdefault("pair_blocks", {}).pop(params["pair"], None)
        elif kind == "mark_pair_unbanned":
            pair = params["pair"]
            if not isinstance(pair, str) or not _PAIR_RE.match(pair.strip()):
                raise SignalControlError(f"pair must look like 'ADA/USDT:USDT', got {pair!r}")
            pair = pair.strip()
            reason = params["reason"]
            if not isinstance(reason, str) or not reason.strip():
                raise SignalControlError("reason is required — it is the audit trail")
            reason = reason.strip()[:UNBANNED_PAIR_REASON_MAX_LEN]

            unbanned = control.setdefault("unbanned_pairs", {})
            if pair in unbanned:
                raise SignalControlError(f"{pair} is already tracked in unbanned_pairs")
            if len(unbanned) >= UNBANNED_PAIR_MAX_ENTRIES:
                raise SignalControlError(f"too many unbanned_pairs ({UNBANNED_PAIR_MAX_ENTRIES}); clear one first")
            now = datetime.datetime.now(datetime.timezone.utc)
            unbanned[pair] = {
                "unbanned_at": now.isoformat(timespec="seconds"),
                "risk_budget_pct": 0.0,
                "risk_budget_abs": 0.0,
                "reason": reason,
            }
        elif kind == "set_unbanned_pair_risk_budget":
            pair = params["pair"]
            unbanned = control.setdefault("unbanned_pairs", {})
            entry = unbanned.get(pair)
            if entry is None:
                raise SignalControlError(
                    f"{pair} is not tracked in unbanned_pairs — call mark_pair_unbanned first"
                )

            def _num(field: str, cap: float) -> float:
                value = params[field]
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise SignalControlError(f"{field} must be a number")
                if not math.isfinite(value) or value < 0:
                    raise SignalControlError(f"{field} must be zero or a positive finite number")
                return min(value, cap)

            risk_budget_pct = _num("risk_budget_pct", UNBANNED_PAIR_MAX_RISK_BUDGET_PCT)
            risk_budget_abs = _num("risk_budget_abs", UNBANNED_PAIR_MAX_RISK_BUDGET_ABS)

            reason = params["reason"]
            if not isinstance(reason, str) or not reason.strip():
                raise SignalControlError("reason is required — it is the audit trail")
            reason = reason.strip()[:UNBANNED_PAIR_REASON_MAX_LEN]

            entry["risk_budget_pct"] = risk_budget_pct
            entry["risk_budget_abs"] = risk_budget_abs
            entry["reason"] = reason
            params = dict(params, risk_budget_pct_applied=risk_budget_pct, risk_budget_abs_applied=risk_budget_abs)
        elif kind == "clear_unbanned_pair":
            control.setdefault("unbanned_pairs", {}).pop(params["pair"], None)
        else:
            raise SignalControlError(f"unknown op {kind!r}")

        _save_control(control)
        _journal_append({
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "action": kind,
            "params": {k: v for k, v in params.items() if k != "reason"},
            "reason": params.get("reason", ""),
            "before": before,
            "after": control,
        })
        return {"control_file": control}


def set_override(side: str, signal_id: str, enabled: bool, reason: str) -> dict:
    return _mutate("set_override", side=side, signal_id=signal_id, enabled=enabled, reason=reason)


def clear_override(side: str, signal_id: str, reason: str) -> dict:
    return _mutate("clear_override", side=side, signal_id=signal_id, reason=reason)


def set_ema200_guard(enabled: bool, reason: str) -> dict:
    return _mutate("set_ema200_guard", enabled=enabled, reason=reason)


def set_risk_adjustment(pair: str, multiplier: float, ttl_hours: float, reason: str) -> dict:
    return _mutate("set_risk_adjustment", pair=pair, multiplier=multiplier, ttl_hours=ttl_hours, reason=reason)


def clear_risk_adjustment(pair: str, reason: str) -> dict:
    return _mutate("clear_risk_adjustment", pair=pair, reason=reason)


def schedule_pair_block(
    pair: str, reason: str, effective_from: Optional[str] = None, ttl_days: Optional[float] = None
) -> dict:
    return _mutate("schedule_pair_block", pair=pair, reason=reason, effective_from=effective_from, ttl_days=ttl_days)


def clear_pair_block(pair: str, reason: str) -> dict:
    return _mutate("clear_pair_block", pair=pair, reason=reason)


def get_pair_blocks() -> dict:
    control = _load_control()
    return _pair_blocks_state(control)


def mark_pair_unbanned(pair: str, reason: str) -> dict:
    return _mutate("mark_pair_unbanned", pair=pair, reason=reason)


def set_unbanned_pair_risk_budget(
    pair: str, risk_budget_pct: float, risk_budget_abs: float, reason: str
) -> dict:
    return _mutate(
        "set_unbanned_pair_risk_budget",
        pair=pair, risk_budget_pct=risk_budget_pct, risk_budget_abs=risk_budget_abs, reason=reason,
    )


def clear_unbanned_pair(pair: str, reason: str) -> dict:
    return _mutate("clear_unbanned_pair", pair=pair, reason=reason)


def get_unbanned_pairs() -> dict:
    control = _load_control()
    now = datetime.datetime.now(datetime.timezone.utc)
    result = {}
    for pair, entry in (control.get("unbanned_pairs", {}) or {}).items():
        if not isinstance(entry, dict):
            continue
        row = dict(entry)
        row["shadow_mode"] = entry.get("risk_budget_pct", 0) <= 0 and entry.get("risk_budget_abs", 0) <= 0
        unbanned_at = _parse_iso(entry.get("unbanned_at"))
        if unbanned_at is not None:
            row["hours_since_unbanned"] = round((now - unbanned_at).total_seconds() / 3600.0, 1)
        result[pair] = row
    return result
