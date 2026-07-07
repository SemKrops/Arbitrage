"""Provider interface every odds source must implement."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Competition, PropOdds


class OddsProvider(ABC):
    """A source of player prop odds for one bookmaker.

    Implementations fetch odds however they can (public JSON API, licensed
    data feed, exported file) and return them in the common ``PropOdds``
    shape so the arbitrage engine never needs to know where odds came from.
    """

    #: Bookmaker name shown in alerts, e.g. "Bet365".
    name: str

    @abstractmethod
    def fetch_props(self, competitions: tuple[Competition, ...]) -> list[PropOdds]:
        """Return current player shots / shots-on-target odds.

        Must only return props for the requested competitions. Should raise
        on hard failures (network down, auth rejected) and return an empty
        list when there are simply no markets available right now.
        """
