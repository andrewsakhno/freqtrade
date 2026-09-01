"""Per-bot Telegram message formatting. Pure functions - no I/O, no Telegram
objects - so each can be unit-tested against a raw freqtrade REST payload."""

from typing import Any


def format_balance(bot_name: str, data: dict[str, Any]) -> str:
    total = data.get("total", 0)
    symbol = data.get("symbol", "")
    starting = data.get("starting_capital")
    lines = [f"*{bot_name}* balance", f"Total: {total:.4f} {symbol}"]
    if starting:
        ratio = data.get("starting_capital_ratio", 0) * 100
        lines.append(f"Starting: {starting:.4f} {symbol} ({ratio:+.2f}%)")
    for cur in data.get("currencies", []):
        if cur.get("est_stake", 0) == 0:
            continue
        lines.append(f"  {cur['currency']}: {cur['balance']:.4f} (~{cur['est_stake']:.2f} {symbol})")
    return "\n".join(lines)


def format_profit(bot_name: str, data: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"*{bot_name}* profit",
            f"Closed trades: {data.get('trade_count', 0)}"
            f" (winrate {data.get('winrate', 0) * 100:.1f}%)",
            f"Closed profit: {data.get('profit_closed_coin', 0):.4f}"
            f" ({data.get('profit_closed_percent', 0):+.2f}%)",
            f"All profit: {data.get('profit_all_coin', 0):.4f}"
            f" ({data.get('profit_all_percent', 0):+.2f}%)",
            f"Best pair: {data.get('best_pair', 'n/a')}",
        ]
    )


def format_status(bot_name: str, trades: list[dict[str, Any]]) -> str:
    if not trades:
        return f"*{bot_name}* status\nNo open trades."
    lines = [f"*{bot_name}* status ({len(trades)} open)"]
    for t in trades:
        side = "short" if t.get("is_short") else "long"
        lines.append(
            f"  #{t.get('trade_id')} {t.get('pair')} [{side}] "
            f"{t.get('profit_ratio', 0) * 100:+.2f}% ({t.get('profit_abs', 0):+.2f}) "
            f"tag={t.get('enter_tag') or '-'}"
        )
    return "\n".join(lines)


def format_error(bot_name: str, exc: Exception) -> str:
    return f"*{bot_name}* error: {exc}"
