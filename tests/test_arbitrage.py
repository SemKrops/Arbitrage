import pytest

from arb_tool.arbitrage import arbitrage_margin, find_arbitrages, split_stakes
from arb_tool.models import Competition, Market, PropOdds


def prop(book, over=None, under=None, line=2.5, player="Mohamed Salah",
         event="Liverpool vs Arsenal", market=Market.SHOTS):
    return PropOdds(
        bookmaker=book, competition=Competition.PREMIER_LEAGUE, event=event,
        kickoff=None, player=player, market=market, line=line, over=over, under=under,
    )


def test_margin_below_one_is_arb():
    assert arbitrage_margin(2.10, 2.10) == pytest.approx(0.95238, abs=1e-4)
    assert arbitrage_margin(1.90, 1.90) > 1.0


def test_split_stakes_equalises_payout():
    stake_over, stake_under = split_stakes(2.10, 2.15, 100.0)
    assert stake_over + stake_under == pytest.approx(100.0)
    # Payouts equal up to cent rounding.
    assert stake_over * 2.10 == pytest.approx(stake_under * 2.15, abs=0.05)


def test_finds_arb_in_both_directions():
    a = prop("Bet365", over=2.10, under=1.72)
    b = prop("Unibet", over=1.75, under=2.15)
    arbs = find_arbitrages([(a, b)], total_stake=100.0)
    assert len(arbs) == 1
    arb = arbs[0]
    assert {leg.bookmaker for leg in arb.legs} == {"Bet365", "Unibet"}
    over_leg, under_leg = arb.legs
    assert over_leg.side == "Over" and over_leg.bookmaker == "Bet365"
    assert under_leg.side == "Under" and under_leg.bookmaker == "Unibet"
    # 1/2.10 + 1/2.15 = 0.94131 → ~6.23% guaranteed profit.
    assert arb.profit_pct == pytest.approx(6.23, abs=0.05)
    assert arb.guaranteed_return > arb.total_stake

    # Swapped argument order finds the same arb via the reverse direction.
    arbs_swapped = find_arbitrages([(b, a)], total_stake=100.0)
    assert len(arbs_swapped) == 1
    assert arbs_swapped[0].profit == pytest.approx(arb.profit, abs=0.05)


def test_no_arb_when_margin_above_one():
    a = prop("Bet365", over=1.90, under=1.90)
    b = prop("Unibet", over=1.92, under=1.88)
    assert find_arbitrages([(a, b)], total_stake=100.0) == []


def test_min_profit_threshold_filters_small_arbs():
    a = prop("Bet365", over=2.02, under=1.90)
    b = prop("Unibet", over=1.90, under=2.02)  # ~0.5% arb
    assert find_arbitrages([(a, b)], 100.0, min_profit_pct=2.0) == []
    assert len(find_arbitrages([(a, b)], 100.0, min_profit_pct=0.1)) > 0


def test_missing_side_is_skipped():
    a = prop("Bet365", over=2.50, under=None)
    b = prop("Unibet", over=2.50, under=None)
    assert find_arbitrages([(a, b)], total_stake=100.0) == []


def test_guaranteed_profit_regardless_of_outcome():
    a = prop("Bet365", over=2.20, under=1.68)
    b = prop("Unibet", over=1.72, under=2.12)
    (arb,) = find_arbitrages([(a, b)], total_stake=250.0)
    over_leg, under_leg = arb.legs
    payout_if_over = over_leg.stake * over_leg.odds
    payout_if_under = under_leg.stake * under_leg.odds
    assert payout_if_over > arb.total_stake
    assert payout_if_under > arb.total_stake
