"""Configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .models import Competition

DEFAULT_COMPETITIONS = (
    Competition.PREMIER_LEAGUE,
    Competition.LA_LIGA,
    Competition.WORLD_CUP,
)


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader; existing environment variables win.

    Reads with ``utf-8-sig`` so a UTF-8 BOM (which Windows PowerShell 5.1
    writes by default via ``Out-File``/``>``) doesn't get glued onto the
    first key name.
    """
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        # Strip a stray BOM defensively in case one slipped through.
        line = line.lstrip("﻿").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


@dataclass
class Config:
    discord_webhook_url: str = ""
    total_stake: float = 100.0
    min_profit_pct: float = 0.5
    poll_interval_seconds: int = 300
    competitions: tuple[Competition, ...] = field(default=DEFAULT_COMPETITIONS)
    bet365_provider: str = "mock"  # mock | file | selenium
    unibet_provider: str = "mock"  # mock | kambi | file
    bet365_data_file: str = ""
    unibet_data_file: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        return cls(
            discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL", ""),
            total_stake=float(os.environ.get("TOTAL_STAKE", "100")),
            min_profit_pct=float(os.environ.get("MIN_PROFIT_PCT", "0.5")),
            poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", "300")),
            competitions=_parse_competitions(os.environ.get("COMPETITIONS", "")),
            bet365_provider=os.environ.get("BET365_PROVIDER", "mock"),
            unibet_provider=os.environ.get("UNIBET_PROVIDER", "mock"),
            bet365_data_file=os.environ.get("BET365_DATA_FILE", ""),
            unibet_data_file=os.environ.get("UNIBET_DATA_FILE", ""),
        )


def _parse_competitions(raw: str) -> tuple[Competition, ...]:
    """Parse COMPETITIONS, e.g. "world_cup" or "premier_league, la_liga"."""
    if not raw.strip():
        return DEFAULT_COMPETITIONS
    result = []
    for part in raw.split(","):
        part = part.strip().lower().replace(" ", "_")
        if part == "laliga":
            part = "la_liga"
        result.append(Competition(part))
    return tuple(result)
