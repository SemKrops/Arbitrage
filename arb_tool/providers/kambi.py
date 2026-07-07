"""Unibet Netherlands provider using the Kambi offering API.

Unibet.nl runs on the Kambi sportsbook platform, whose offering API serves
the same JSON the website itself consumes. Endpoints and market labels are
not officially documented and can change, so everything (base URL, locale,
competition paths) is overridable via environment variables:

    KAMBI_BASE_URL           default https://eu-offering-api.kambicdn.com/offering/v2018/ubnl
    KAMBI_LOCALE             default nl_NL
    KAMBI_MARKET             default NL
    KAMBI_PATH_PREMIER_LEAGUE  default football/england/premier_league
    KAMBI_PATH_LA_LIGA         default football/spain/la_liga
    KAMBI_PATH_WORLD_CUP       default football/world_cup_2026

If Unibet moves off Kambi or the shape changes, switch to the file provider
(UNIBET_PROVIDER=file) and feed odds from another source.
"""

from __future__ import annotations

import logging
import os
import re

import requests

from ..models import Competition, Market, PropOdds
from .base import OddsProvider

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://eu-offering-api.kambicdn.com/offering/v2018/ubnl"
DEFAULT_PATHS = {
    Competition.PREMIER_LEAGUE: "football/england/premier_league",
    Competition.LA_LIGA: "football/spain/la_liga",
    Competition.WORLD_CUP: "football/world_cup_2026",
}

# Criterion labels for player shots markets, English and Dutch variants.
_SHOTS_ON_TARGET_RE = re.compile(r"shots?\s+on\s+target|schoten\s+op\s+doel", re.IGNORECASE)
_SHOTS_RE = re.compile(r"\bshots?\b|\bschoten\b", re.IGNORECASE)
_PLAYER_FROM_LABEL_RE = re.compile(r"\b(?:by|door)\s+(.+)$", re.IGNORECASE)


class KambiUnibetProvider(OddsProvider):
    name = "Unibet"

    def __init__(self, timeout: float = 15.0, max_events_per_competition: int = 25):
        self.base_url = os.environ.get("KAMBI_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        self.locale = os.environ.get("KAMBI_LOCALE", "nl_NL")
        self.market = os.environ.get("KAMBI_MARKET", "NL")
        self.timeout = timeout
        self.max_events = max_events_per_competition
        self.paths = {
            comp: os.environ.get(f"KAMBI_PATH_{comp.name}", default)
            for comp, default in DEFAULT_PATHS.items()
        }
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64)"

    def _get(self, path: str) -> dict:
        url = f"{self.base_url}/{path}"
        response = self.session.get(
            url,
            params={"lang": self.locale, "market": self.market},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def _list_events(self, competition: Competition) -> list[dict]:
        data = self._get(f"listView/{self.paths[competition]}/all/matches.json")
        events = [item["event"] for item in data.get("events", []) if "event" in item]
        return events[: self.max_events]

    def fetch_props(self, competitions: tuple[Competition, ...]) -> list[PropOdds]:
        props: list[PropOdds] = []
        for competition in competitions:
            try:
                events = self._list_events(competition)
            except (requests.RequestException, ValueError, KeyError) as exc:
                log.warning("Unibet/Kambi: could not list %s events: %s", competition.value, exc)
                continue
            for event in events:
                try:
                    props.extend(self._fetch_event_props(competition, event))
                except (requests.RequestException, ValueError, KeyError) as exc:
                    log.warning(
                        "Unibet/Kambi: could not fetch offers for event %s: %s",
                        event.get("id"), exc,
                    )
        return props

    def _fetch_event_props(self, competition: Competition, event: dict) -> list[PropOdds]:
        data = self._get(f"betoffer/event/{event['id']}.json")
        event_name = event.get("name", "").replace(" - ", " vs ")
        kickoff = event.get("start")
        props: list[PropOdds] = []
        for offer in data.get("betOffers", []):
            prop = self._parse_offer(competition, event_name, kickoff, offer)
            if prop is not None:
                props.append(prop)
        return props

    def _parse_offer(
        self, competition: Competition, event_name: str, kickoff: str | None, offer: dict
    ) -> PropOdds | None:
        label = offer.get("criterion", {}).get("label", "")
        market = _classify_market(label)
        if market is None:
            return None

        over_odds = under_odds = line = None
        player = None
        for outcome in offer.get("outcomes", []):
            if outcome.get("odds") is None or outcome.get("line") is None:
                continue  # suspended or non-priced outcome
            player = outcome.get("participant") or player
            line = outcome["line"] / 1000.0
            if outcome.get("type") == "OT_OVER":
                over_odds = outcome["odds"] / 1000.0
            elif outcome.get("type") == "OT_UNDER":
                under_odds = outcome["odds"] / 1000.0

        if player is None:
            match = _PLAYER_FROM_LABEL_RE.search(label)
            player = match.group(1).strip() if match else None

        if player is None or line is None or (over_odds is None and under_odds is None):
            return None

        return PropOdds(
            bookmaker=self.name,
            competition=competition,
            event=event_name,
            kickoff=kickoff,
            player=player,
            market=market,
            line=line,
            over=over_odds,
            under=under_odds,
        )


def _classify_market(criterion_label: str) -> Market | None:
    """Map a Kambi criterion label to one of our markets, or None."""
    if _SHOTS_ON_TARGET_RE.search(criterion_label):
        return Market.SHOTS_ON_TARGET
    if _SHOTS_RE.search(criterion_label):
        return Market.SHOTS
    return None
