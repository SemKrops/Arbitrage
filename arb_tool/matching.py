"""Match events and players between two bookmakers.

Bookmakers spell things differently ("Man City" vs "Manchester City",
"Haaland, Erling" vs "Erling Haaland"), so props are matched with normalised
fuzzy comparison rather than exact string equality. Market and line must
match exactly — an Over 2.5 at one book is only comparable with an
Under 2.5 elsewhere.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from .models import PropOdds

EVENT_MATCH_THRESHOLD = 0.75
PLAYER_MATCH_THRESHOLD = 0.80

# Common noise words in team/event names that differ between bookmakers.
_NOISE_WORDS = {"fc", "cf", "afc", "the", "de", "los", "las", "el", "real"}

_TEAM_ALIASES = {
    "man city": "manchester city",
    "man utd": "manchester united",
    "man united": "manchester united",
    "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "wolves": "wolverhampton wanderers",
    "atletico": "atletico madrid",
    "atl madrid": "atletico madrid",
    "betis": "real betis",
    "sociedad": "real sociedad",
    # Dutch country names as shown on Unibet NL (accents already stripped).
    "nederland": "netherlands",
    "frankrijk": "france",
    "duitsland": "germany",
    "spanje": "spain",
    "engeland": "england",
    "belgie": "belgium",
    "italie": "italy",
    "kroatie": "croatia",
    "argentinie": "argentina",
    "brazilie": "brazil",
    "marokko": "morocco",
    "zwitserland": "switzerland",
    "polen": "poland",
    "denemarken": "denmark",
    "noorwegen": "norway",
    "zweden": "sweden",
    "turkije": "turkey",
    "verenigde staten": "united states",
}


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def normalize(text: str) -> str:
    """Lowercase, strip accents/punctuation and drop noise words."""
    text = _strip_accents(text.lower())
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    words = [w for w in text.split() if w not in _NOISE_WORDS]
    return " ".join(words)


def normalize_player(name: str) -> str:
    """Normalise a player name, handling the "Last, First" convention."""
    if "," in name:
        last, _, first = name.partition(",")
        name = f"{first.strip()} {last.strip()}"
    return normalize(name)


def _normalize_team(team: str) -> str:
    team = normalize(team)
    return _TEAM_ALIASES.get(team, team)


def _event_teams(event: str) -> tuple[str, str] | None:
    """Split an event name like "Arsenal vs Chelsea" into its two teams."""
    parts = re.split(r"\s+(?:vs\.?|v|-|–)\s+", event, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None
    return _normalize_team(parts[0]), _normalize_team(parts[1])


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Containment covers short forms like "haaland" vs "erling haaland".
    if a in b or b in a:
        return 0.95
    return SequenceMatcher(None, a, b).ratio()


def events_match(event_a: str, event_b: str) -> bool:
    teams_a = _event_teams(event_a)
    teams_b = _event_teams(event_b)
    if teams_a and teams_b:
        home = _similar(teams_a[0], teams_b[0])
        away = _similar(teams_a[1], teams_b[1])
        return home >= EVENT_MATCH_THRESHOLD and away >= EVENT_MATCH_THRESHOLD
    return _similar(normalize(event_a), normalize(event_b)) >= EVENT_MATCH_THRESHOLD


def players_match(player_a: str, player_b: str) -> bool:
    return _similar(normalize_player(player_a), normalize_player(player_b)) >= PLAYER_MATCH_THRESHOLD


def match_props(
    props_a: list[PropOdds], props_b: list[PropOdds]
) -> list[tuple[PropOdds, PropOdds]]:
    """Pair up props that refer to the same player/market/line at both books."""
    pairs: list[tuple[PropOdds, PropOdds]] = []
    # Bucket by (competition, market, line) first so fuzzy matching only runs
    # on plausible candidates.
    buckets: dict[tuple, list[PropOdds]] = {}
    for prop in props_b:
        buckets.setdefault((prop.competition, prop.market, prop.line), []).append(prop)

    for prop_a in props_a:
        for prop_b in buckets.get((prop_a.competition, prop_a.market, prop_a.line), []):
            if events_match(prop_a.event, prop_b.event) and players_match(
                prop_a.player, prop_b.player
            ):
                pairs.append((prop_a, prop_b))
    return pairs
