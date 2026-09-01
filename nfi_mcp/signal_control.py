"""
Hot signal control for the NFI bot via strategy_control.json on the server.

All JSON read-modify-write happens in a single server-side python process
(atomic os.replace, journal append), so there is no read/write race across the
ssh round-trip. The op payload travels base64-encoded to survive any quoting.
"""

import base64
import json
from typing import Any

from .remote_snippets import STRIP_JSON_COMMENTS_PY
from .ssh_link import SshRunner

CONTROL_PATH = "/opt/nfi/user_data/strategy_control.json"
JOURNAL_PATH = "/opt/nfi/user_data/strategy_control_log.jsonl"
CONFIG_PATH = "/opt/nfi/user_data/config.json"
STRATEGY_PATH = "/opt/nfi/user_data/strategies/NostalgiaForInfinityX7.py"
EMA200_STRATEGY_PATH = "/opt/nfi/user_data/strategies/NostalgiaForInfinityX7EMA200.py"

# Runs on the server. Reads one base64(JSON) op from argv, prints JSON result.
# Placeholders are substituted with str.replace below — do not use %-formatting
# here, the program body itself contains % characters. Every constant and
# validation rule here must stay a literal duplicate of
# nfi_mcp_server/signal_control.py (the canonical, native implementation) —
# this flat script has no import to share code with it.
_REMOTE_TEMPLATE = r'''
import ast, base64, datetime, json, math, os, re, sys

CONTROL_PATH = "@CONTROL_PATH@"
JOURNAL_PATH = "@JOURNAL_PATH@"
CONFIG_PATH = "@CONFIG_PATH@"
STRATEGY_PATH = "@STRATEGY_PATH@"
EMA200_STRATEGY_PATH = "@EMA200_STRATEGY_PATH@"

RISK_ADJUSTMENT_MULTIPLIER_MIN = 0.25
RISK_ADJUSTMENT_MULTIPLIER_MAX = 4.0
RISK_ADJUSTMENT_TTL_MIN_HOURS = 0.25
RISK_ADJUSTMENT_TTL_MAX_HOURS = 24.0
RISK_ADJUSTMENT_MAX_ENTRIES = 20
RISK_ADJUSTMENT_REASON_MAX_LEN = 500
PAIR_RE = re.compile(r"^[A-Z0-9]+/[A-Z0-9]+(:[A-Z0-9]+)?$")

FALLBACK_CASCADE_PCT_72H = {
    "BTC": 4.9, "ETH": 15.1, "SOL": 22.5, "XRP": 22.8,
    "DOGE": 27.0, "ADA": 30.4, "LINK": 23.7, "AVAX": 20.1,
    "*": 20.7,
}
FALLBACK_GLOBAL_SHAPE = ((6.0, 0.8), (24.0, 8.4), (72.0, 20.7))
FALLBACK_DEFAULTS = {
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

@STRIP_JSON_COMMENTS_PY@

DEFAULT_CONTROL = {
    "long_signals_override": {},
    "short_signals_override": {},
    "ema200_guard_enabled": True,
    "risk_adjustments": {},
}


def load_control():
    if not os.path.exists(CONTROL_PATH):
        return dict(DEFAULT_CONTROL)
    with open(CONTROL_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    for key, value in DEFAULT_CONTROL.items():
        data.setdefault(key, value if not isinstance(value, dict) else dict(value))
    return data


def save_control(data):
    tmp = CONTROL_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, CONTROL_PATH)


def journal_append(entry):
    with open(JOURNAL_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def strategy_defaults():
    """Extract signal-enable dicts from the NFI class body via ast (no import)."""
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


def parse_iso(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def prune_expired(control, now):
    adjustments = control.get("risk_adjustments")
    if not isinstance(adjustments, dict):
        return []
    dropped = []
    for key in list(adjustments):
        entry = adjustments.get(key)
        if not isinstance(entry, dict):
            continue
        expires_at = parse_iso(entry.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            del adjustments[key]
            dropped.append(key)
    return dropped


def risk_adjustments_state(control):
    now = datetime.datetime.now(datetime.timezone.utc)
    active, expired = {}, {}
    for key, entry in (control.get("risk_adjustments", {}) or {}).items():
        if not isinstance(entry, dict):
            continue
        expires_at = parse_iso(entry.get("expires_at"))
        bucket = expired if (expires_at is not None and expires_at <= now) else active
        row = dict(entry)
        if expires_at is not None:
            row["hours_remaining"] = round((expires_at - now).total_seconds() / 3600.0, 2)
        bucket[key] = row
    return {"active": active, "expired": expired}


def ema200_strategy_defaults():
    """Extract grey-zone calibration constants from NostalgiaForInfinityX7EMA200's
    class body via ast (no import). Returns {} if the file is missing or
    predates the feature, so callers degrade gracefully instead of raising."""
    if not os.path.exists(EMA200_STRATEGY_PATH):
        return {}
    with open(EMA200_STRATEGY_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    wanted = {
        "GREY_ZONE_CASCADE_PCT_72H", "GREY_ZONE_CASCADE_GLOBAL_SHAPE",
        "grey_zone_exit_ref_hours", "grey_zone_exit_ref_cascade_pct",
        "grey_zone_exit_pair_sensitivity", "grey_zone_exit_min_full_hours",
        "grey_zone_exit_max_full_hours", "grey_zone_exit_curve_exponent",
        "grey_zone_exit_start_hours", "grey_zone_exit_floor_ratio",
        "stale_exit_hours", "stale_exit_max_loss", "catastrophic_exit_loss_ratio",
    }
    result = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "NostalgiaForInfinityX7EMA200":
            for stmt in node.body:
                target_name, value_node = None, None
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                    target_name, value_node = stmt.targets[0].id, stmt.value
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
                    target_name, value_node = stmt.target.id, stmt.value
                if target_name in wanted:
                    try:
                        result[target_name] = ast.literal_eval(value_node)
                    except (ValueError, SyntaxError):
                        pass
            break
    return result


def grey_zone_calibration(pair):
    strategy_defaults = ema200_strategy_defaults()
    warning = None
    if not strategy_defaults:
        warning = (
            "strategy file on the server has no grey-zone calibration yet "
            "(or NostalgiaForInfinityX7EMA200.py is missing) — using MCP-side "
            "fallback defaults; the bot is not necessarily running this feature"
        )

    cascade_table = strategy_defaults.get("GREY_ZONE_CASCADE_PCT_72H") or FALLBACK_CASCADE_PCT_72H
    global_shape = strategy_defaults.get("GREY_ZONE_CASCADE_GLOBAL_SHAPE") or FALLBACK_GLOBAL_SHAPE

    def resolved(key):
        return strategy_defaults[key] if key in strategy_defaults else FALLBACK_DEFAULTS[key]

    control = load_control()
    now = datetime.datetime.now(datetime.timezone.utc)
    prune_expired(control, now)

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

    def full_hours(multiplier):
        c = max(cascade_pct_72h * multiplier, 0.01)
        h = ref_hours * (ref_cascade_pct / c) ** sensitivity
        return min(max(h, min_full_hours), max_full_hours)

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
        expires_at = parse_iso(entry.get("expires_at"))
        if expires_at is None or expires_at > now:
            hours_remaining = None if expires_at is None else round((expires_at - now).total_seconds() / 3600.0, 2)
            active_adj = dict(entry, pair_or_wildcard=key, hours_remaining=hours_remaining)
            break

    return {
        "pair": pair,
        "warning": warning,
        "cascade_pct_72h": cascade_pct_72h,
        "calibration_source": calibration_source,
        "cascade_curve": cascade_curve,
        "resolved_params": {
            "start_hours": start_hours, "floor_ratio": floor_ratio, "ceiling_ratio": ceiling,
            "ref_hours": ref_hours, "ref_cascade_pct": ref_cascade_pct,
            "pair_sensitivity": sensitivity, "curve_exponent": curve_exponent,
            "min_full_hours": min_full_hours, "max_full_hours": max_full_hours,
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


def config_signal_params():
    cfg = load_json_with_comments(CONFIG_PATH)
    nfi = cfg.get("nfi_parameters", {}) or {}
    return (
        nfi.get("long_entry_signal_params", {}) or {},
        nfi.get("short_entry_signal_params", {}) or {},
    )


def effective_state():
    defaults = strategy_defaults()
    cfg_long, cfg_short = config_signal_params()
    control = load_control()

    def side_state(side, default_params, cfg_params, overrides):
        template = side + "_entry_condition_{}_enable"
        merged = dict(default_params)
        merged.update({k: v for k, v in cfg_params.items() if k in merged})
        for sid, val in overrides.items():
            key = template.format(sid)
            if key in merged:
                merged[key] = bool(val)
        enabled = sorted(
            int(k.rsplit("_", 2)[1]) for k, v in merged.items() if v
        )
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

    return {
        "long": side_state(
            "long", defaults.get("long_entry_signal_params", {}), cfg_long,
            control.get("long_signals_override", {}),
        ),
        "short": side_state(
            "short", defaults.get("short_entry_signal_params", {}), cfg_short,
            control.get("short_signals_override", {}),
        ),
        "ema200_guard_enabled": control.get("ema200_guard_enabled", True),
        "risk_adjustments": risk_adjustments_state(control),
        "control_file": control,
    }


def run_op(op):
    kind = op["op"]
    if kind == "get_state":
        return effective_state()

    if kind == "journal_tail":
        limit = int(op.get("limit", 20))
        if not os.path.exists(JOURNAL_PATH):
            return {"entries": []}
        with open(JOURNAL_PATH, encoding="utf-8") as fh:
            lines = fh.readlines()
        return {"entries": [json.loads(line) for line in lines[-limit:]]}

    if kind == "grey_zone_calibration":
        return grey_zone_calibration(op["pair"])

    # Mutating ops below.
    control = load_control()
    before = json.loads(json.dumps(control))

    if kind in ("set_override", "clear_override"):
        side = op["side"]
        if side not in ("long", "short"):
            raise ValueError("side must be 'long' or 'short'")
        signal_id = str(op["signal_id"])
        key = side + "_entry_condition_" + signal_id + "_enable"
        defaults = strategy_defaults().get(side + "_entry_signal_params", {})
        if key not in defaults:
            raise ValueError(f"unknown signal id {signal_id!r} for side {side}")
        overrides = control.setdefault(side + "_signals_override", {})
        if kind == "set_override":
            overrides[signal_id] = bool(op["enabled"])
        else:
            overrides.pop(signal_id, None)
    elif kind == "set_ema200_guard":
        control["ema200_guard_enabled"] = bool(op["enabled"])
    elif kind == "set_risk_adjustment":
        pair = op["pair"]
        if not isinstance(pair, str) or not pair.strip():
            raise ValueError("pair must be a non-empty string")
        pair = pair.strip()
        if pair != "*" and not PAIR_RE.match(pair):
            raise ValueError(f"pair must be '*' or a freqtrade pair like 'ADA/USDT:USDT', got {pair!r}")

        multiplier = op["multiplier"]
        if not isinstance(multiplier, (int, float)) or isinstance(multiplier, bool):
            raise ValueError("multiplier must be a number")
        if not math.isfinite(multiplier):
            raise ValueError("multiplier must be finite")
        if multiplier <= 0:
            raise ValueError("multiplier must be positive")
        multiplier_applied = min(max(multiplier, RISK_ADJUSTMENT_MULTIPLIER_MIN), RISK_ADJUSTMENT_MULTIPLIER_MAX)

        ttl_hours = op["ttl_hours"]
        if not isinstance(ttl_hours, (int, float)) or isinstance(ttl_hours, bool):
            raise ValueError("ttl_hours must be a number")
        if not math.isfinite(ttl_hours):
            raise ValueError("ttl_hours must be finite")
        if ttl_hours <= 0:
            raise ValueError("ttl_hours must be positive")
        ttl_applied = min(max(ttl_hours, RISK_ADJUSTMENT_TTL_MIN_HOURS), RISK_ADJUSTMENT_TTL_MAX_HOURS)

        reason = op["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason is required — it is the audit trail")
        reason = reason.strip()
        if len(reason) > RISK_ADJUSTMENT_REASON_MAX_LEN:
            reason = reason[:RISK_ADJUSTMENT_REASON_MAX_LEN] + "…"

        now = datetime.datetime.now(datetime.timezone.utc)
        prune_expired(control, now)
        adjustments = control.setdefault("risk_adjustments", {})
        if pair not in adjustments and len(adjustments) >= RISK_ADJUSTMENT_MAX_ENTRIES:
            raise ValueError(f"too many risk adjustments ({RISK_ADJUSTMENT_MAX_ENTRIES}); clear one first")
        adjustments[pair] = {
            "multiplier": multiplier_applied,
            "expires_at": (now + datetime.timedelta(hours=ttl_applied)).isoformat(timespec="seconds"),
            "set_at": now.isoformat(timespec="seconds"),
            "ttl_hours": ttl_applied,
            "reason": reason,
        }
        op = dict(
            op,
            multiplier_requested=multiplier, multiplier_applied=multiplier_applied,
            ttl_hours_requested=ttl_hours, ttl_hours_applied=ttl_applied,
        )
    elif kind == "clear_risk_adjustment":
        now = datetime.datetime.now(datetime.timezone.utc)
        prune_expired(control, now)
        control.setdefault("risk_adjustments", {}).pop(op["pair"], None)
    else:
        raise ValueError(f"unknown op {kind!r}")

    save_control(control)
    journal_append({
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "action": kind,
        "params": {k: v for k, v in op.items() if k not in ("op", "reason")},
        "reason": op.get("reason", ""),
        "before": before,
        "after": control,
    })
    return {"control_file": control}


def main():
    op = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
    try:
        print(json.dumps({"ok": True, "result": run_op(op)}))
    except Exception as exc:  # surfaced to the MCP tool caller
        print(json.dumps({"ok": False, "error": str(exc)}))


main()
'''

_REMOTE_PROGRAM = (
    _REMOTE_TEMPLATE
    .replace("@CONTROL_PATH@", CONTROL_PATH)
    .replace("@JOURNAL_PATH@", JOURNAL_PATH)
    .replace("@CONFIG_PATH@", CONFIG_PATH)
    .replace("@STRATEGY_PATH@", STRATEGY_PATH)
    .replace("@EMA200_STRATEGY_PATH@", EMA200_STRATEGY_PATH)
    .replace("@STRIP_JSON_COMMENTS_PY@", STRIP_JSON_COMMENTS_PY)
)


class SignalControlError(RuntimeError):
    pass


class SignalControl:
    """Client for the server-side control-file operations."""

    def __init__(self, runner: SshRunner):
        self._runner = runner

    def _op(self, payload: dict[str, Any]) -> Any:
        encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        script = (
            "set -eu\n"
            f"python3 - {encoded} <<'NFI_MCP_REMOTE_EOF'\n"
            f"{_REMOTE_PROGRAM}\n"
            "NFI_MCP_REMOTE_EOF\n"
        )
        result = self._runner.run(script)
        response = json.loads(result.stdout.strip())
        if not response.get("ok"):
            raise SignalControlError(response.get("error", "unknown remote error"))
        return response["result"]

    def get_state(self) -> dict:
        return self._op({"op": "get_state"})

    def set_override(self, side: str, signal_id: str, enabled: bool, reason: str) -> dict:
        return self._op({
            "op": "set_override", "side": side, "signal_id": signal_id,
            "enabled": enabled, "reason": reason,
        })

    def clear_override(self, side: str, signal_id: str, reason: str) -> dict:
        return self._op({
            "op": "clear_override", "side": side, "signal_id": signal_id, "reason": reason,
        })

    def set_ema200_guard(self, enabled: bool, reason: str) -> dict:
        return self._op({"op": "set_ema200_guard", "enabled": enabled, "reason": reason})

    def journal_tail(self, limit: int = 20) -> dict:
        return self._op({"op": "journal_tail", "limit": limit})

    def grey_zone_calibration(self, pair: str) -> dict:
        return self._op({"op": "grey_zone_calibration", "pair": pair})

    def set_risk_adjustment(self, pair: str, multiplier: float, ttl_hours: float, reason: str) -> dict:
        return self._op({
            "op": "set_risk_adjustment", "pair": pair, "multiplier": multiplier,
            "ttl_hours": ttl_hours, "reason": reason,
        })

    def clear_risk_adjustment(self, pair: str, reason: str) -> dict:
        return self._op({"op": "clear_risk_adjustment", "pair": pair, "reason": reason})
