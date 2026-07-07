"""End-to-end test: mock providers → matching → arb detection → Discord payload."""

from arb_tool.arbitrage import find_arbitrages
from arb_tool.config import DEFAULT_COMPETITIONS
from arb_tool.discord_notifier import build_embed
from arb_tool.matching import match_props
from arb_tool.models import Competition, Market
from arb_tool.providers.mock import MockBet365Provider, MockUnibetProvider


def test_mock_pipeline_finds_expected_arbs():
    bet365 = MockBet365Provider().fetch_props(DEFAULT_COMPETITIONS)
    unibet = MockUnibetProvider().fetch_props(DEFAULT_COMPETITIONS)

    pairs = match_props(bet365, unibet)
    assert pairs, "mock data should produce matched pairs"

    arbs = find_arbitrages(pairs, total_stake=100.0, min_profit_pct=0.5)
    found = {(arb.player, arb.market, arb.competition) for arb in arbs}
    assert ("Mohamed Salah", Market.SHOTS, Competition.PREMIER_LEAGUE) in found
    assert ("Vinicius Junior", Market.SHOTS_ON_TARGET, Competition.LA_LIGA) in found
    assert ("Kylian Mbappe", Market.SHOTS, Competition.WORLD_CUP) in found

    for arb in arbs:
        assert arb.profit > 0
        assert arb.guaranteed_return > arb.total_stake


def test_competition_filter_is_respected():
    only_pl = (Competition.PREMIER_LEAGUE,)
    props = MockBet365Provider().fetch_props(only_pl)
    assert props
    assert all(p.competition is Competition.PREMIER_LEAGUE for p in props)


def test_discord_embed_contains_bet_details():
    bet365 = MockBet365Provider().fetch_props(DEFAULT_COMPETITIONS)
    unibet = MockUnibetProvider().fetch_props(DEFAULT_COMPETITIONS)
    arbs = find_arbitrages(match_props(bet365, unibet), total_stake=100.0)
    embed = build_embed(arbs[0])

    assert embed["title"] == "💰 Arbitrage found"
    field_names = " ".join(f["name"] for f in embed["fields"])
    assert "Bet365" in field_names and "Unibet" in field_names
    all_values = " ".join(f["value"] for f in embed["fields"])
    assert "€" in all_values
