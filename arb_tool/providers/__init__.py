"""Odds providers for the supported bookmakers."""

from __future__ import annotations

from ..config import Config
from ..models import Competition
from .base import OddsProvider
from .file import FileProvider
from .kambi import KambiUnibetProvider
from .mock import MockBet365Provider, MockUnibetProvider


def build_providers(config: Config) -> tuple[OddsProvider, OddsProvider]:
    """Instantiate the (bet365, unibet) providers selected in the config."""
    bet365: OddsProvider
    unibet: OddsProvider

    if config.bet365_provider == "mock":
        bet365 = MockBet365Provider()
    elif config.bet365_provider == "file":
        bet365 = FileProvider("Bet365", config.bet365_data_file)
    elif config.bet365_provider == "selenium":
        from .bet365_selenium import SeleniumBet365Provider

        bet365 = SeleniumBet365Provider()
    else:
        raise ValueError(f"Unknown BET365_PROVIDER: {config.bet365_provider!r}")

    if config.unibet_provider == "mock":
        unibet = MockUnibetProvider()
    elif config.unibet_provider == "kambi":
        unibet = KambiUnibetProvider()
    elif config.unibet_provider == "file":
        unibet = FileProvider("Unibet", config.unibet_data_file)
    else:
        raise ValueError(f"Unknown UNIBET_PROVIDER: {config.unibet_provider!r}")

    return bet365, unibet


__all__ = [
    "OddsProvider",
    "FileProvider",
    "KambiUnibetProvider",
    "MockBet365Provider",
    "MockUnibetProvider",
    "build_providers",
    "Competition",
]
