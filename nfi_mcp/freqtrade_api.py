"""
Client for the freqtrade REST API, tunneled through ssh.

The bots listen on localhost ports on the server, so every request is a curl
executed server-side. Credentials are read server-side from the bot's own
config.json and never leave the server.
"""

import json
import shlex
from typing import Any, Optional

from .bots import BotConfig
from .remote_snippets import STRIP_JSON_COMMENTS_PY
from .ssh_link import SshError, SshRunner


class FreqtradeApiError(RuntimeError):
    pass


class FreqtradeApi:
    def __init__(self, runner: SshRunner, bot: BotConfig):
        self._runner = runner
        self._bot = bot

    def request(
        self,
        method: str,
        path: str,
        query: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Perform an authenticated request against /api/v1/<path>."""
        url = f"http://localhost:{self._bot.port}/api/v1/{path.lstrip('/')}"
        if query:
            pairs = "&".join(f"{k}={v}" for k, v in query.items())
            url = f"{url}?{pairs}"
        curl = (
            'curl -sS -m 30 -u "$FT_CRED" '
            f"-X {shlex.quote(method.upper())} "
            '-H "Content-Type: application/json" '
        )
        if body is not None:
            curl += f"-d {shlex.quote(json.dumps(body))} "
        curl += shlex.quote(url)

        # Credentials: prefer the container's actual process environment (how
        # the NFI bot is configured, via .env/docker-compose), falling back to
        # config.json's api_server block (how the sample bot is configured).
        # Trying env first and falling back covers both without needing to
        # know in advance which mechanism a given bot uses.
        script = f"""
set -eu
FT_CRED=$(python3 - <<'PY'
import json
import subprocess
{STRIP_JSON_COMMENTS_PY}

def from_container_env():
    try:
        out = subprocess.run(
            ["docker", "exec", {self._bot.container!r}, "printenv",
             "FREQTRADE__API_SERVER__USERNAME", "FREQTRADE__API_SERVER__PASSWORD"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip().splitlines()
        if len(out) == 2 and out[0] and out[1]:
            return out[0], out[1]
    except subprocess.CalledProcessError:
        pass
    return None

def from_config_file():
    with open({self._bot.config_path!r}, encoding="utf-8") as fh:
        text = strip_json_comments(fh.read())
    c = json.loads(text)["api_server"]
    return c["username"], c["password"]

creds = from_container_env() or from_config_file()
print(creds[0] + ":" + creds[1])
PY
)
{curl}
"""
        try:
            result = self._runner.run(script)
        except SshError as exc:
            raise FreqtradeApiError(f"[{self._bot.name}] {exc}") from exc
        text = result.stdout.strip()
        if not text:
            raise FreqtradeApiError(f"[{self._bot.name}] empty response from {path}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise FreqtradeApiError(
                f"[{self._bot.name}] non-JSON response from {path}: {text[:300]}"
            ) from exc

    # Convenience wrappers -----------------------------------------------------
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
