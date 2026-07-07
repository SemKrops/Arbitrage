"""Mock providers with realistic sample data for end-to-end testing.

The data deliberately contains a few arbitrage opportunities (including one
where the books disagree enough that Over at one and Under at the other is
profitable) plus plenty of non-arb lines, name-spelling differences and a
line that exists at only one book — so the whole pipeline (matching, arb
detection, Discord alert) can be exercised without any network access.
"""

from __future__ import annotations

from ..models import Competition, Market, PropOdds
from .base import OddsProvider


class MockBet365Provider(OddsProvider):
    name = "Bet365"

    def fetch_props(self, competitions: tuple[Competition, ...]) -> list[PropOdds]:
        return [p for p in _BET365_PROPS if p.competition in competitions]


class MockUnibetProvider(OddsProvider):
    name = "Unibet"

    def fetch_props(self, competitions: tuple[Competition, ...]) -> list[PropOdds]:
        return [p for p in _UNIBET_PROPS if p.competition in competitions]


def _prop(book, comp, event, kickoff, player, market, line, over, under):
    return PropOdds(
        bookmaker=book,
        competition=comp,
        event=event,
        kickoff=kickoff,
        player=player,
        market=market,
        line=line,
        over=over,
        under=under,
    )


_BET365_PROPS = [
    # Premier League — Salah shots o/u 2.5: Bet365 Over 2.10 vs Unibet Under 2.15 is an arb.
    _prop("Bet365", Competition.PREMIER_LEAGUE, "Liverpool vs Arsenal", "2026-07-11 17:30",
          "Mohamed Salah", Market.SHOTS, 2.5, 2.10, 1.72),
    _prop("Bet365", Competition.PREMIER_LEAGUE, "Liverpool vs Arsenal", "2026-07-11 17:30",
          "Mohamed Salah", Market.SHOTS_ON_TARGET, 1.5, 1.85, 1.95),
    _prop("Bet365", Competition.PREMIER_LEAGUE, "Liverpool vs Arsenal", "2026-07-11 17:30",
          "Bukayo Saka", Market.SHOTS, 1.5, 1.62, 2.25),
    # No arb here — both books price Haaland tightly.
    _prop("Bet365", Competition.PREMIER_LEAGUE, "Man City vs Chelsea", "2026-07-12 15:00",
          "Erling Haaland", Market.SHOTS, 3.5, 1.90, 1.90),
    # La Liga — shots on target arb: Bet365 Under 2.30 vs Unibet Over 1.95.
    _prop("Bet365", Competition.LA_LIGA, "Real Madrid vs Barcelona", "2026-07-12 21:00",
          "Vinicius Junior", Market.SHOTS_ON_TARGET, 1.5, 1.70, 2.30),
    _prop("Bet365", Competition.LA_LIGA, "Real Madrid vs Barcelona", "2026-07-12 21:00",
          "Robert Lewandowski", Market.SHOTS, 2.5, 1.80, 2.00),
    # World Cup — Mbappé shots o/u 3.5 arb in the Over@Bet365 direction.
    _prop("Bet365", Competition.WORLD_CUP, "France vs Netherlands", "2026-07-14 20:00",
          "Kylian Mbappe", Market.SHOTS, 3.5, 2.20, 1.68),
    _prop("Bet365", Competition.WORLD_CUP, "France vs Netherlands", "2026-07-14 20:00",
          "Memphis Depay", Market.SHOTS_ON_TARGET, 1.5, 2.05, 1.75),
    # Only offered at Bet365 — must not produce a match.
    _prop("Bet365", Competition.WORLD_CUP, "France vs Netherlands", "2026-07-14 20:00",
          "Antoine Griezmann", Market.SHOTS, 1.5, 1.95, 1.85),
]

_UNIBET_PROPS = [
    # Different event/player spellings on purpose.
    _prop("Unibet", Competition.PREMIER_LEAGUE, "Liverpool - Arsenal", "2026-07-11 17:30",
          "Salah, Mohamed", Market.SHOTS, 2.5, 1.75, 2.15),
    _prop("Unibet", Competition.PREMIER_LEAGUE, "Liverpool - Arsenal", "2026-07-11 17:30",
          "Salah, Mohamed", Market.SHOTS_ON_TARGET, 1.5, 1.90, 1.90),
    _prop("Unibet", Competition.PREMIER_LEAGUE, "Liverpool - Arsenal", "2026-07-11 17:30",
          "Saka, Bukayo", Market.SHOTS, 1.5, 1.65, 2.20),
    _prop("Unibet", Competition.PREMIER_LEAGUE, "Manchester City - Chelsea", "2026-07-12 15:00",
          "Haaland, Erling", Market.SHOTS, 3.5, 1.92, 1.88),
    _prop("Unibet", Competition.LA_LIGA, "Real Madrid - FC Barcelona", "2026-07-12 21:00",
          "Vinicius Junior", Market.SHOTS_ON_TARGET, 1.5, 1.95, 1.85),
    _prop("Unibet", Competition.LA_LIGA, "Real Madrid - FC Barcelona", "2026-07-12 21:00",
          "Lewandowski, Robert", Market.SHOTS, 2.5, 1.85, 1.95),
    _prop("Unibet", Competition.WORLD_CUP, "Frankrijk - Nederland", "2026-07-14 20:00",
          "Mbappe, Kylian", Market.SHOTS, 3.5, 1.72, 2.12),
    _prop("Unibet", Competition.WORLD_CUP, "Frankrijk - Nederland", "2026-07-14 20:00",
          "Depay, Memphis", Market.SHOTS_ON_TARGET, 1.5, 1.80, 2.00),
]
