"""Two-way arbitrage detection and stake splitting for over/under markets.

An arbitrage exists on an over/under line when the implied probabilities of
backing Over at one bookmaker and Under at another sum to less than 1:

    1/odds_over + 1/odds_under < 1

Stakes are then split so that the payout is identical whichever side wins,
which makes the profit guaranteed.
"""

from __future__ import annotations

from .models import ArbitrageOpportunity, Leg, PropOdds


def arbitrage_margin(over_odds: float, under_odds: float) -> float:
    """Sum of implied probabilities. Below 1.0 means guaranteed profit."""
    return 1.0 / over_odds + 1.0 / under_odds


def _best_event(*props: PropOdds) -> str:
    """Pick the most trustworthy event label for display.

    bet365's event name is scraped from the page and can be unreliable, so
    prefer a non-bet365 book's label (e.g. Unibet's, from its API) when
    available; fall back to the first prop's event otherwise.
    """
    for prop in props:
        if prop.bookmaker.lower() != "bet365" and prop.event:
            return prop.event
    return props[0].event


def split_stakes(over_odds: float, under_odds: float, total_stake: float) -> tuple[float, float]:
    """Split ``total_stake`` so both outcomes return the same payout.

    Returns (stake_on_over, stake_on_under), rounded to cents. The rounding
    difference is absorbed into the over stake so the total is exact.
    """
    margin = arbitrage_margin(over_odds, under_odds)
    stake_under = round(total_stake * (1.0 / under_odds) / margin, 2)
    stake_over = round(total_stake - stake_under, 2)
    return stake_over, stake_under


def check_pair(
    over_prop: PropOdds,
    under_prop: PropOdds,
    total_stake: float,
    min_profit_pct: float,
) -> ArbitrageOpportunity | None:
    """Check Over at ``over_prop.bookmaker`` vs Under at ``under_prop.bookmaker``."""
    if over_prop.over is None or under_prop.under is None:
        return None

    margin = arbitrage_margin(over_prop.over, under_prop.under)
    if margin >= 1.0:
        return None

    stake_over, stake_under = split_stakes(over_prop.over, under_prop.under, total_stake)
    # Guaranteed return is the smaller of the two payouts (they differ only
    # by cent rounding).
    guaranteed_return = min(stake_over * over_prop.over, stake_under * under_prop.under)
    profit_pct = (guaranteed_return - total_stake) / total_stake * 100
    if profit_pct < min_profit_pct:
        return None

    return ArbitrageOpportunity(
        competition=over_prop.competition,
        event=_best_event(over_prop, under_prop),
        kickoff=over_prop.kickoff or under_prop.kickoff,
        player=over_prop.player,
        market=over_prop.market,
        line=over_prop.line,
        legs=(
            Leg(over_prop.bookmaker, "Over", over_prop.over, stake_over),
            Leg(under_prop.bookmaker, "Under", under_prop.under, stake_under),
        ),
        total_stake=total_stake,
        guaranteed_return=round(guaranteed_return, 2),
    )


def find_arbitrages(
    matched_pairs: list[tuple[PropOdds, PropOdds]],
    total_stake: float,
    min_profit_pct: float = 0.0,
) -> list[ArbitrageOpportunity]:
    """Find arbitrage opportunities in matched prop pairs.

    Each pair holds the same player/market/line at two different bookmakers.
    Both directions are checked: Over@A + Under@B and Over@B + Under@A.
    Results are sorted by profit, highest first.
    """
    opportunities: list[ArbitrageOpportunity] = []
    for prop_a, prop_b in matched_pairs:
        for over_prop, under_prop in ((prop_a, prop_b), (prop_b, prop_a)):
            arb = check_pair(over_prop, under_prop, total_stake, min_profit_pct)
            if arb is not None:
                opportunities.append(arb)
    opportunities.sort(key=lambda o: o.profit, reverse=True)
    return opportunities
