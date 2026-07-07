"""Provider that reads odds from a JSON file or JSON HTTP endpoint.

This is the intended integration point for Bet365. Bet365 offers no public
API and actively blocks scrapers, so this tool does not ship a Bet365
scraper. Instead, export odds from whatever licensed data feed or in-house
collector you have into the JSON shape below and point BET365_DATA_FILE at
it (a local path or an https URL):

    [
      {
        "competition": "premier_league",        // premier_league | la_liga | world_cup
        "event": "Liverpool vs Arsenal",
        "kickoff": "2026-07-11 17:30",           // optional
        "player": "Mohamed Salah",
        "market": "player_shots",               // player_shots | player_shots_on_target
        "line": 2.5,
        "over": 2.10,                            // decimal odds, optional
        "under": 1.72                            // decimal odds, optional
      },
      ...
    ]
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import requests

from ..models import Competition, Market, PropOdds
from .base import OddsProvider

log = logging.getLogger(__name__)


class FileProvider(OddsProvider):
    def __init__(self, name: str, source: str, timeout: float = 15.0):
        if not source:
            raise ValueError(
                f"{name}: no data source configured. Set the *_DATA_FILE "
                "environment variable to a JSON file path or https URL."
            )
        self.name = name
        self.source = source
        self.timeout = timeout

    def _load(self) -> list[dict]:
        if self.source.startswith(("http://", "https://")):
            response = requests.get(self.source, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        return json.loads(Path(self.source).read_text())

    def fetch_props(self, competitions: tuple[Competition, ...]) -> list[PropOdds]:
        props: list[PropOdds] = []
        for row in self._load():
            try:
                competition = Competition(row["competition"])
                if competition not in competitions:
                    continue
                props.append(
                    PropOdds(
                        bookmaker=self.name,
                        competition=competition,
                        event=row["event"],
                        kickoff=row.get("kickoff"),
                        player=row["player"],
                        market=Market(row["market"]),
                        line=float(row["line"]),
                        over=float(row["over"]) if row.get("over") else None,
                        under=float(row["under"]) if row.get("under") else None,
                    )
                )
            except (KeyError, ValueError) as exc:
                log.warning("%s: skipping malformed row %r: %s", self.name, row, exc)
        return props
