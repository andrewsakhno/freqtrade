# nfi-telegram-bot — one Telegram bot, both freqtrade instances

Fronts NFI X7 (:8087) and SampleStrategy (:8086) with a single Telegram bot
token. Each command (`/balance`, `/profit`, `/status`) queries both bots'
REST APIs and replies once per bot, sequentially.

## Why a separate service

freqtrade's own Telegram RPC (`freqtrade/rpc/telegram.py`) always starts
`getUpdates` long-polling when `telegram.enabled: true`. Two freqtrade
processes polling the *same* bot token collide (`telegram.error.Conflict`) —
the same failure mode already seen running the local dev bot and the server
bot on the same token. So only **one** process may poll a given token.

This service is that one process. It does not run inside the freqtrade
containers at all — it's a thin sibling to `nfi_mcp_server` that talks to
both bots purely over their REST APIs (`FreqtradeApi`, copied from
`nfi_mcp_server/freqtrade_client.py` by the same self-contained-package
convention).

**Precondition:** `telegram.enabled` must be set to `false` in both
`/opt/freqtrade-bot/user_data/config.json` and the `nfi` bot's config,
followed by `reload_config` (or a container restart) on each — otherwise
freqtrade's built-in Telegram RPC keeps polling the same token alongside
this bot and you're back to the Conflict error.

## Commands

- `/balance` - wallet balance per bot
- `/profit` - closed-trade profit summary per bot
- `/status` - open trades per bot
- `/help` - list commands

All handlers are restricted to the configured `chat_id` (message from any
other chat is logged and ignored) — same-chat-only, no external attack
surface beyond the Telegram bot token itself.

## Deployment (mirrors nfi_mcp_server)

```
/opt/nfi-telegram-bot/
  Dockerfile
  docker-compose.yml
  requirements.txt
  *.py
```

```bash
ssh freqtrade-ui
cd /opt/nfi-telegram-bot
docker compose build
docker compose up -d
docker logs nfi-telegram-bot --tail 30
```

Credentials, in order of precedence:
- `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` env vars (set in `docker-compose.yml`
  if you want a dedicated token instead of reusing SampleStrategy's).
- Fallback: the `telegram` block of `/opt/freqtrade-bot/user_data/config.json`
  (`token`, `chat_id`) — this is where the token lives today, so the default
  deploy needs no new secret.
- REST API creds: `FREQTRADE__API_SERVER__USERNAME/PASSWORD` from
  `/opt/nfi/.env` (`env_file`), same as `nfi_mcp_server`; falls back to each
  bot's own `config.json` `api_server` block.
