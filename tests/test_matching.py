from arb_tool.matching import events_match, match_props, players_match
from arb_tool.models import Competition, Market, PropOdds


def prop(book, event, player, market=Market.SHOTS, line=2.5,
         comp=Competition.PREMIER_LEAGUE):
    return PropOdds(
        bookmaker=book, competition=comp, event=event, kickoff=None,
        player=player, market=market, line=line, over=2.0, under=1.8,
    )


def test_events_match_across_separators_and_spellings():
    assert events_match("Liverpool vs Arsenal", "Liverpool - Arsenal")
    assert events_match("Man City vs Chelsea", "Manchester City - Chelsea")
    assert events_match("Real Madrid vs Barcelona", "Real Madrid - FC Barcelona")
    assert not events_match("Liverpool vs Arsenal", "Everton - Arsenal")


def test_players_match_handles_name_order_and_accents():
    assert players_match("Mohamed Salah", "Salah, Mohamed")
    assert players_match("Kylian Mbappé", "Mbappe, Kylian")
    assert players_match("Haaland", "Erling Haaland")
    assert not players_match("Mohamed Salah", "Bukayo Saka")


def test_match_props_requires_same_market_and_line():
    a = [
        prop("Bet365", "Liverpool vs Arsenal", "Mohamed Salah", line=2.5),
        prop("Bet365", "Liverpool vs Arsenal", "Mohamed Salah",
             market=Market.SHOTS_ON_TARGET, line=1.5),
    ]
    b = [
        prop("Unibet", "Liverpool - Arsenal", "Salah, Mohamed", line=2.5),
        prop("Unibet", "Liverpool - Arsenal", "Salah, Mohamed", line=3.5),  # other line
        prop("Unibet", "Liverpool - Arsenal", "Salah, Mohamed",
             market=Market.SHOTS_ON_TARGET, line=1.5),
    ]
    pairs = match_props(a, b)
    assert len(pairs) == 2
    for prop_a, prop_b in pairs:
        assert prop_a.market == prop_b.market
        assert prop_a.line == prop_b.line


def test_match_props_ignores_other_competitions_and_players():
    a = [prop("Bet365", "Liverpool vs Arsenal", "Mohamed Salah")]
    b = [
        prop("Unibet", "Liverpool - Arsenal", "Mohamed Salah", comp=Competition.LA_LIGA),
        prop("Unibet", "Liverpool - Arsenal", "Bukayo Saka"),
    ]
    assert match_props(a, b) == []
