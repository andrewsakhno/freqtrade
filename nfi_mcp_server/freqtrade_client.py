"""
Native client for the freqtrade REST API — runs on the same host as the bots,
so this talks to http://127.0.0.1:<port> directly. No ssh hop.

Credentials: tries this process's own environment first (this container's
docker-compose loads /opt/nfi/.env via env_file, giving it the `nfi` bot's
FREQTRADE__API_SERVER__USERNAME/PASSWORD directly - no docker.sock, no
exec-into-another-container needed), falling back to the api_server block in
the bot's own config.json (how the `sample` bot is configured, and would also
cover an `nfi` env-var mismatch). Nothing is hardcoded or passed by the
caller.
"""

import os
from typing import Any, Optional

import requests

from .bots import BotConfig
from .json_utils import load_json_with_comments


class FreqtradeApiError(RuntimeError):
    pass


def _credentials(bot: BotConfig) -> tuple[str, str]:
    username = os.environ.get("FREQTRADE__API_SERVER__USERNAME")
    password = os.environ.get("FREQTRADE__API_SERVER__PASSWORD")
    if username and password:
        return username, password

    cfg = load_json_with_comments(bot.config_path)
    api = cfg["api_server"]
    return api["username"], api["password"]


class FreqtradeApi:
    def __init__(self, bot: BotConfig):
        self._bot = bot
        self._base = f"http://127.0.0.1:{bot.port}/api/v1"
        self._auth = _credentials(bot)

    def request(
        self,
        method: str,
        path: str,
        query: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
    ) -> Any:
        url = f"{self._base}/{path.lstrip('/')}"
        try:
            resp = requests.request(
                method, url, params=query, json=body, auth=self._auth, timeout=30
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise FreqtradeApiError(f"[{self._bot.name}] {path}: {exc}") from exc
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise FreqtradeApiError(
                f"[{self._bot.name}] non-JSON response from {path}: {resp.text[:300]}"
            ) from exc

    def get(self, path: str, query: Optional[dict[str, Any]] = None) -> Any:
        return self.request("GET", path, query=query)

    def post(self, path: str, body: Optional[dict[str, Any]] = None) -> Any:
        return self.request("POST", path, body=body)

    def delete(self, path: str, query: Optional[dict[str, Any]] = None) -> Any:
        return self.request("DELETE", path, query=query)

    def closed_trades_since(self, cutoff_ts_ms: int, max_pages: int = 10) -> list[dict]:
        """
        Fetch closed trades with close_timestamp >= cutoff, newest first.

        Pages through /trades (sorted by close_date desc) until the cutoff or
        max_pages is reached.
        """
        trades: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            page = self.get(
                "trades", {"limit": 500, "offset": offset, "order_by_id": "false"}
            )
            batch = page.get("trades", [])
            if not batch:
                break
            for trade in batch:
                if (trade.get("close_timestamp") or 0) >= cutoff_ts_ms:
                    trades.append(trade)
                else:
                    return trades
            offset += len(batch)
            if offset >= page.get("total_trades", 0):
                break
        return trades
