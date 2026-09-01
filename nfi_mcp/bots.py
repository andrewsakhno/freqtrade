"""Registry of freqtrade bots reachable on the server (localhost ports)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class BotConfig:
    name: str
    port: int
    container: str  # docker container name — env vars are the primary credential source
    config_path: str  # config.json fallback, for bots that set creds there instead of env


BOTS: dict[str, BotConfig] = {
    "nfi": BotConfig(
        name="nfi",
        port=8087,
        container="NFI_Dry_binance_futures-NostalgiaForInfinityX7",
        config_path="/opt/nfi/user_data/config.json",
    ),
    "sample": BotConfig(
        name="sample",
        port=8086,
        container="freqtrade",
        config_path="/opt/freqtrade-bot/user_data/config.json",
    ),
}

DEFAULT_BOT = "nfi"


def get_bot(name: str) -> BotConfig:
    try:
        return BOTS[name]
    except KeyError:
        raise ValueError(f"Unknown bot {name!r}; known bots: {', '.join(BOTS)}")
