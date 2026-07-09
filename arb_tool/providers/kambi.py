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

When a competition path 404s (Kambi renames term keys, e.g. per World Cup
edition), the provider automatically looks up the real path in Kambi's
group tree (group.json) and retries with it.

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

# Groups that look like a World Cup but aren't the men's main tournament.
_WORLD_CUP_EXCLUDE = (
    "qualif", "kwalificatie", "women", "vrouwen", "u17", "u19", "u20", "u21",
    "youth", "jeugd", "futsal", "beach", "club", "esoccer", "e-soccer",
)

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

    def _list_view_candidates(self, path: str) -> list[str]:
        """listView URL variants for a competition path, most likely first.

        Kambi's listView expects the group path padded with "all" to four
        segments: England's Premier League lives at depth 3
        (football/england/premier_league → .../all/matches.json) but a
        World Cup sits directly under football at depth 2 and needs two
        fillers (football/world_cup_2026/all/all/matches.json).
        """
        segments = path.split("/")
        padded = segments + ["all"] * max(0, 4 - len(segments))
        candidates = [f"listView/{'/'.join(padded)}/matches.json"]
        if padded != segments:
            candidates.append(f"listView/{path}/matches.json")
        candidates.append(f"listView/{path}.json")
        return candidates

    def _fetch_listing(self, path: str) -> dict:
        last_404: requests.HTTPError | None = None
        for url in self._list_view_candidates(path):
            try:
                return self._get(url)
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 404:
                    raise
                last_404 = exc
        assert last_404 is not None
        raise last_404

    def _list_events(self, competition: Competition) -> list[dict]:
        try:
            data = self._fetch_listing(self.paths[competition])
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise
            # Path is stale (Kambi renames competition term keys, e.g. for
            # each World Cup edition) — look up the real one in the group
            # tree and retry.
            discovered = self._discover_path(competition)
            if not discovered or discovered == self.paths[competition]:
                raise
            log.info(
                "Unibet/Kambi: resolved %s to path %r", competition.value, discovered
            )
            self.paths[competition] = discovered
            data = self._fetch_listing(discovered)
        events = [item["event"] for item in data.get("events", []) if "event" in item]
        if not events:
            log.info(
                "Unibet/Kambi: no upcoming %s matches listed (off-season or "
                "markets not open yet)",
                competition.value,
            )
        return events[: self.max_events]

    def search_groups(self, query: str) -> list[tuple[str, str]]:
        """Search Kambi's football group tree by name or term key.

        Returns (listView path, display name) tuples — a diagnostic helper
        exposed via ``python -m arb_tool --find-competition <query>``.
        """
        query = query.lower()
        return [
            (path, group.get("name") or "")
            for group, path in self._football_groups()
            if query in (group.get("name") or "").lower()
            or query in (group.get("termKey") or "").lower()
        ]

    def _discover_path(self, competition: Competition) -> str | None:
        """Find the competition's listView path in Kambi's group tree."""
        best: tuple[tuple, str] | None = None
        for group, path in self._football_groups():
            score = _match_competition(competition, group, path)
            if score is not None and (best is None or score > best[0]):
                best = (score, path)
        return best[1] if best else None

    def _football_groups(self) -> list[tuple[dict, str]]:
        """Flatten the football subtree of group.json to (group, path) pairs."""
        data = self._get("group.json")
        football = next(
            (
                group
                for group in data.get("group", {}).get("groups", [])
                if group.get("termKey") == "football"
                or (group.get("name") or "").lower() in ("football", "voetbal")
            ),
            None,
        )
        if football is None:
            return []

        candidates: list[tuple[dict, str]] = []

        def walk(group: dict, prefix: str) -> None:
            for child in group.get("groups") or []:
                term = child.get("termKey") or ""
                path = f"{prefix}/{term}" if term else prefix
                candidates.append((child, path))
                walk(child, path)

        walk(football, football.get("termKey", "football"))
        return candidates

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


def _match_competition(
    competition: Competition, group: dict, path: str
) -> tuple | None:
    """Score a group-tree node as a candidate for the competition.

    Returns None for non-matches; otherwise a sortable score tuple where a
    higher tuple means a better match.
    """
    name = (group.get("name") or "").lower()
    term = (group.get("termKey") or "").lower()
    path_l = path.lower()

    if competition is Competition.PREMIER_LEAGUE:
        if term == "premier_league" or name == "premier league":
            # Many countries have a "Premier League"; England's is the one.
            return ("england" in path_l or "engeland" in path_l, term == "premier_league")
    elif competition is Competition.LA_LIGA:
        if term in ("la_liga", "laliga") or name in ("la liga", "laliga"):
            return ("spain" in path_l or "spanje" in path_l, term.startswith("la"))
    elif competition is Competition.WORLD_CUP:
        text = f"{term} {name}"
        is_wc = (
            "world_cup" in term
            or "world cup" in name
            or name == "wk"
            or name.startswith("wk ")
            or "wereldkampioenschap" in name
        )
        if is_wc and not any(word in text for word in _WORLD_CUP_EXCLUDE):
            # Prefer the current edition and shallower (top-level) groups.
            return ("2026" in text, -path.count("/"))
    return None


def _classify_market(criterion_label: str) -> Market | None:
    """Map a Kambi criterion label to one of our markets, or None."""
    if _SHOTS_ON_TARGET_RE.search(criterion_label):
        return Market.SHOTS_ON_TARGET
    if _SHOTS_RE.search(criterion_label):
        return Market.SHOTS
    return None
