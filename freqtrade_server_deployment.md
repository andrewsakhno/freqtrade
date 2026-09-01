---
name: freqtrade-server-deployment
description: "Freqtrade bots run on remote server 85.122.114.77 (ssh alias freqtrade-ui from WSL): main SampleStrategy bot on :8086 and NFI X7 dry-run on :8087 with auto-updater; local repo bot is stopped"
metadata: 
  node_type: memory
  type: project
  originSessionId: 093709c5-9ff0-49f3-b914-0fbf6a12d344
  modified: 2026-08-25T20:01:08.172Z
---

The user's freqtrade bots run on a remote server **85.122.114.77** (root), hosted at **AlexHost** (AlexHost SRL, Chisinau, Moldova — alexhost.com), reachable as ssh host alias **`freqtrade-ui`** from WSL Debian (`~/.ssh/config` there; key `~/.ssh/id_ed25519`). Access pattern: [[docker-wsl-access]].

**Local port 8086 on Windows is an SSH tunnel** (`ssh freqtrade-ui` keeps `LocalForward 8086`) to the server's FreqUI/API — it is NOT a local bot. The bot instance inside the local repo (`E:\projects\freqtrade\user_data`) was stopped 2026-08-24 and stays stopped; running it again with the same Telegram token as the server bot causes `telegram.error.Conflict`.

Server layout (as of 2026-08-25):
- `/opt/freqtrade-bot/` — main bot, container `freqtrade`, image `freqtradeorg/freqtrade:stable`, SampleStrategy, binance **futures isolated**, dry-run, `max_open_trades: 3`, API on `127.0.0.1:8086`, Telegram enabled. API creds live in `/opt/freqtrade-bot/user_data/config.json`.
- `/opt/nfi/` — clone of iterativv/NostalgiaForInfinity, container `NFI_Dry_binance_futures-NostalgiaForInfinityX7`: NFI X7 dry-run (wallet 10000 USDT, 6 trades, 100-pair VolumePairList), API on `127.0.0.1:8087` (same creds as main bot, CORS allows origin :8086), Telegram OFF. Config via `.env` (`FREQTRADE__*` vars) + `configs/recommended_config.json` chain. Deployed 2026-08-25 for comparison with SampleStrategy.
- `/opt/nfi/docker-compose.override.yml` — `nfi-updater` sidecar (their official updater image): daily cron 10:00 Europe/Kyiv + 60s ETag watch on the blacklist; auto-restarts the NFI container on changes; needs `COMPOSE_PROJECT_NAME=nfi` in `.env`. Stop it before going live to pin the strategy version.
- Server also hosts unrelated polymarket stack and a GitHub actions runner; ports 8086/8087 are localhost-only on the server.

Gotcha: `/opt/nfi` was cloned as root but the freqtrade container runs as uid 1000 — after touching `user_data` as root, re-run `chown -R 1000:1000 /opt/nfi/user_data`.
