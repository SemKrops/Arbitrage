"""Tests for the Kambi provider's competition path discovery and parsing."""

import requests

from arb_tool.models import Competition, Market
from arb_tool.providers.kambi import KambiUnibetProvider, _classify_market

# A trimmed-down replica of Kambi's group.json tree, including the traps:
# a non-English "Premier League", World Cup qualifiers, and a women's
# World Cup.
GROUP_TREE = {
    "group": {
        "name": "All sports",
        "groups": [
            {
                "name": "Voetbal",
                "termKey": "football",
                "groups": [
                    {
                        "name": "Engeland",
                        "termKey": "england",
                        "groups": [
                            {"name": "Premier League", "termKey": "premier_league"},
                            {"name": "Championship", "termKey": "the_championship"},
                        ],
                    },
                    {
                        "name": "Rusland",
                        "termKey": "russia",
                        "groups": [
                            {"name": "Premier League", "termKey": "premier_liga"},
                        ],
                    },
                    {
                        "name": "Spanje",
                        "termKey": "spain",
                        "groups": [
                            {"name": "LaLiga", "termKey": "la_liga"},
                            {"name": "LaLiga 2", "termKey": "la_liga_2"},
                        ],
                    },
                    {
                        "name": "WK 2026",
                        "termKey": "fifa_world_cup_2026",
                        "groups": [],
                    },
                    {
                        "name": "World Cup Qualifying - Europe",
                        "termKey": "world_cup_qualifying_europe",
                        "groups": [],
                    },
                    {
                        "name": "Women's World Cup",
                        "termKey": "world_cup_women",
                        "groups": [],
                    },
                ],
            },
            {"name": "Tennis", "termKey": "tennis", "groups": []},
        ],
    }
}


def make_provider(monkeypatch, responses):
    """Provider whose _get returns canned responses keyed by path prefix."""
    provider = KambiUnibetProvider()

    def fake_get(path):
        for prefix, response in responses.items():
            if path.startswith(prefix):
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(provider, "_get", fake_get)
    return provider


def http_404():
    response = requests.Response()
    response.status_code = 404
    return requests.HTTPError("404 Client Error", response=response)


def test_discover_path_world_cup(monkeypatch):
    provider = make_provider(monkeypatch, {"group.json": GROUP_TREE})
    assert provider._discover_path(Competition.WORLD_CUP) == "football/fifa_world_cup_2026"


def test_discover_path_premier_league_prefers_england(monkeypatch):
    provider = make_provider(monkeypatch, {"group.json": GROUP_TREE})
    assert (
        provider._discover_path(Competition.PREMIER_LEAGUE)
        == "football/england/premier_league"
    )


def test_discover_path_la_liga(monkeypatch):
    provider = make_provider(monkeypatch, {"group.json": GROUP_TREE})
    assert provider._discover_path(Competition.LA_LIGA) == "football/spain/la_liga"


def test_list_view_candidates_pads_to_four_segments():
    provider = KambiUnibetProvider()
    assert provider._list_view_candidates("football/world_cup_2026") == [
        "listView/football/world_cup_2026/all/all/matches.json",
        "listView/football/world_cup_2026/matches.json",
        "listView/football/world_cup_2026.json",
    ]
    # Depth-3 paths (the common case) keep their proven single-"all" form.
    assert (
        provider._list_view_candidates("football/england/premier_league")[0]
        == "listView/football/england/premier_league/all/matches.json"
    )


def test_list_events_uses_padded_url_for_top_level_competition(monkeypatch):
    """The real-world WK 2026 case: right term key, wrong URL depth."""
    listing = {
        "events": [
            {"event": {"id": 7, "name": "Frankrijk - Nederland", "start": "2026-07-14"}}
        ]
    }
    provider = KambiUnibetProvider()

    def fake_get(path):
        if path == "listView/football/world_cup_2026/all/all/matches.json":
            return listing
        raise http_404()

    monkeypatch.setattr(provider, "_get", fake_get)
    events = provider._list_events(Competition.WORLD_CUP)
    assert [e["id"] for e in events] == [7]


def test_list_events_falls_back_to_discovery_on_404(monkeypatch):
    listing = {
        "events": [
            {"event": {"id": 1, "name": "Frankrijk - Nederland", "start": "2026-07-14"}}
        ]
    }
    provider = make_provider(
        monkeypatch,
        {
            "listView/football/world_cup_2026": http_404(),  # stale default
            "listView/football/fifa_world_cup_2026": listing,
            "group.json": GROUP_TREE,
        },
    )
    events = provider._list_events(Competition.WORLD_CUP)
    assert [e["id"] for e in events] == [1]
    # The discovered path is cached for subsequent calls.
    assert provider.paths[Competition.WORLD_CUP] == "football/fifa_world_cup_2026"


def test_search_groups(monkeypatch):
    provider = make_provider(monkeypatch, {"group.json": GROUP_TREE})
    results = provider.search_groups("world")
    paths = [path for path, _ in results]
    assert "football/fifa_world_cup_2026" in paths
    assert "football/world_cup_qualifying_europe" in paths
    # Dutch query matches the WK group by name.
    assert provider.search_groups("wk") == [("football/fifa_world_cup_2026", "WK 2026")]
    assert provider.search_groups("tennis") == []


def test_classify_market_labels():
    assert _classify_market("Total Shots on Target by Mohamed Salah") is Market.SHOTS_ON_TARGET
    assert _classify_market("Schoten op doel van speler") is Market.SHOTS_ON_TARGET
    assert _classify_market("Total Shots by Mohamed Salah") is Market.SHOTS
    assert _classify_market("Aantal schoten van speler") is Market.SHOTS
    assert _classify_market("Total Goals") is None


def test_parse_offer_lines_and_odds_are_scaled():
    provider = KambiUnibetProvider()
    offer = {
        "criterion": {"label": "Total Shots by Mohamed Salah"},
        "outcomes": [
            {"type": "OT_OVER", "line": 2500, "odds": 2100, "participant": "Mohamed Salah"},
            {"type": "OT_UNDER", "line": 2500, "odds": 1720, "participant": "Mohamed Salah"},
        ],
    }
    prop = provider._parse_offer(
        Competition.PREMIER_LEAGUE, "Liverpool vs Arsenal", None, offer
    )
    assert prop is not None
    assert prop.player == "Mohamed Salah"
    assert prop.line == 2.5
    assert prop.over == 2.1
    assert prop.under == 1.72
