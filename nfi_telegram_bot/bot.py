"""
Single Telegram bot fronting both freqtrade instances (NFI X7 :8087 and
SampleStrategy :8086). Each command queries both bots' REST APIs in turn and
replies with one message per bot, sequentially - so only this process ever
polls this bot token (freqtrade's own built-in Telegram RPC must stay
disabled on both bots, or `getUpdates` collides and Telegram returns 409
Conflict; see freqtrade/rpc/telegram.py's start_polling()).

Run: python -m nfi_telegram_bot.bot
Env:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID  - override the token/chat_id pulled from
      the sample bot's config.json `telegram` block (where they already
      live today).
  TELEGRAM_EXTRA_CHAT_IDS  - comma-separated extra chat ids to allow
      alongside the primary one (e.g. a group chat) without touching the
      token.
  FREQTRADE__API_SERVER__USERNAME/PASSWORD - same convention as
      nfi_mcp_server: loaded from /opt/nfi/.env via env_file.
"""

import logging
import os

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from .bots import BOTS, get_bot
from .formatting import format_balance, format_error, format_profit, format_status
from .freqtrade_client import FreqtradeApi, FreqtradeApiError
from .json_utils import load_json_with_comments

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nfi_telegram_bot")

_api_cache: dict[str, FreqtradeApi] = {}


def _api(bot: str) -> FreqtradeApi:
    if bot not in _api_cache:
        _api_cache[bot] = FreqtradeApi(get_bot(bot))
    return _api_cache[bot]


def _telegram_credentials() -> tuple[str, set[int]]:
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        cfg = load_json_with_comments(get_bot("sample").config_path)
        tg = cfg["telegram"]
        token, chat_id = tg["token"], tg["chat_id"]

    chat_ids = {int(chat_id)}
    extra = os.environ.get("TELEGRAM_EXTRA_CHAT_IDS", "")
    chat_ids.update(int(c) for c in extra.split(",") if c.strip())
    return token, chat_ids


async def _reply_per_bot(update: Update, fetch, formatter) -> None:
    for name in BOTS:
        try:
            data = fetch(_api(name))
            text = formatter(name, data)
        except FreqtradeApiError as exc:
            text = format_error(name, exc)
        await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_per_bot(update, lambda api: api.get("balance"), format_balance)


async def cmd_profit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_per_bot(update, lambda api: api.get("profit"), format_profit)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_per_bot(update, lambda api: api.get("status"), format_status)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Commands (each replies once per bot: "
        + ", ".join(BOTS)
        + "):\n"
        "/balance - wallet balance\n"
        "/profit - closed-trade profit summary\n"
        "/status - open trades"
    )


def build_app() -> Application:
    token, chat_ids = _telegram_credentials()
    app = Application.builder().token(token).build()

    async def _restrict(update: Update, context: ContextTypes.DEFAULT_TYPE, handler):
        if update.effective_chat is None or update.effective_chat.id not in chat_ids:
            logger.warning("Ignoring message from unauthorized chat %s", update.effective_chat)
            return
        await handler(update, context)

    def guarded(handler):
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            await _restrict(update, context, handler)

        return wrapped

    app.add_handler(CommandHandler("balance", guarded(cmd_balance)))
    app.add_handler(CommandHandler("profit", guarded(cmd_profit)))
    app.add_handler(CommandHandler("status", guarded(cmd_status)))
    app.add_handler(CommandHandler(["help", "start"], guarded(cmd_help)))
    return app


def main() -> None:
    app = build_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
