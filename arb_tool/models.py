"""Core data models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Market(str, Enum):
    SHOTS = "player_shots"
    SHOTS_ON_TARGET = "player_shots_on_target"

    @property
    def display_name(self) -> str:
        return {
            Market.SHOTS: "Player Shots",
            Market.SHOTS_ON_TARGET: "Player Shots on Target",
        }[self]


class Competition(str, Enum):
    PREMIER_LEAGUE = "premier_league"
    LA_LIGA = "la_liga"
    WORLD_CUP = "world_cup"

    @property
    def display_name(self) -> str:
        return {
            Competition.PREMIER_LEAGUE: "Premier League",
            Competition.LA_LIGA: "La Liga",
            Competition.WORLD_CUP: "World Cup",
        }[self]


@dataclass(frozen=True)
class PropOdds:
    """Over/under odds for a single player prop line at one bookmaker.

    Odds are decimal (European) odds. ``line`` is the over/under threshold,
    e.g. 2.5 shots. Either side may be missing if the bookmaker only
    publishes one side of the market.
    """

    bookmaker: str
    competition: Competition
    event: str  # e.g. "Arsenal vs Chelsea"
    kickoff: str | None
    player: str
    market: Market
    line: float
    over: float | None = None
    under: float | None = None


@dataclass(frozen=True)
class Leg:
    """One side of an arbitrage bet."""

    bookmaker: str
    side: str  # "Over" or "Under"
    odds: float
    stake: float


@dataclass(frozen=True)
class ArbitrageOpportunity:
    competition: Competition
    event: str
    kickoff: str | None
    player: str
    market: Market
    line: float
    legs: tuple[Leg, Leg]
    total_stake: float
    guaranteed_return: float

    @property
    def profit(self) -> float:
        return self.guaranteed_return - self.total_stake

    @property
    def profit_pct(self) -> float:
        return self.profit / self.total_stake * 100

    def key(self) -> tuple:
        """Stable identity used to avoid alerting the same arb twice."""
        return (
            self.event,
            self.player,
            self.market,
            self.line,
            tuple((l.bookmaker, l.side, round(l.odds, 3)) for l in self.legs),
        )
