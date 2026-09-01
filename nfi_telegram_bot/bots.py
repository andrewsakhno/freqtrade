"""Registry of freqtrade bots reachable on this host (localhost ports).

Kept identical to nfi_mcp_server/bots.py by convention - each deployable
package under this repo is self-contained (see nfi_mcp_server/README.md),
so this is a deliberate copy, not an import across packages.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class BotConfig:
    name: str
    port: int
    config_path: str  # config.json fallback for credentials (see freqtrade_client._credentials)


BOTS: dict[str, BotConfig] = {
    "nfi": BotConfig(
        name="nfi",
        port=8087,
        config_path="/opt/nfi/user_data/config.json",
    ),
    "sample": BotConfig(
        name="sample",
        port=8086,
        config_path="/opt/freqtrade-bot/user_data/config.json",
    ),
}

DEFAULT_BOT = "nfi"


def get_bot(name: str) -> BotConfig:
    try:
        return BOTS[name]
    except KeyError:
        raise ValueError(f"Unknown bot {name!r}; known bots: {', '.join(BOTS)}")
