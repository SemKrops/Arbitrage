"""Tests for the Selenium Bet365 provider.

Odds parsing is plain unit-tested; the DOM parser is tested by loading a
static replica of bet365's market markup (tests/fixtures/bet365_match.html)
in a real headless Chromium via file:// — skipped when no browser can start.
"""

from pathlib import Path

import pytest

from arb_tool.models import Competition, Market
from arb_tool.providers.bet365_selenium import (
    SeleniumBet365Provider,
    fractional_to_decimal,
)

FIXTURE = Path(__file__).parent / "fixtures" / "bet365_match.html"


def test_fractional_to_decimal():
    assert fractional_to_decimal("11/10") == pytest.approx(2.10)
    assert fractional_to_decimal("8/5") == pytest.approx(2.60)
    assert fractional_to_decimal("2.10") == pytest.approx(2.10)
    assert fractional_to_decimal("2,10") == pytest.approx(2.10)
    assert fractional_to_decimal("") is None
    assert fractional_to_decimal("evs/x") is None


@pytest.fixture(scope="module")
def driver():
    provider = SeleniumBet365Provider()
    try:
        drv = provider._build_driver()
    except Exception as exc:  # no chrome/chromedriver available
        pytest.skip(f"cannot start Chromium: {exc}")
    yield drv
    drv.quit()


def test_deep_query_pierces_closed_shadow_dom(driver):
    """bet365 hides content in closed shadow roots; the injected
    attachShadow override plus the deep query must still find it."""
    provider = SeleniumBet365Provider()
    driver.get("about:blank")
    driver.execute_script(
        """
        const host = document.createElement('div');
        document.body.appendChild(host);
        const root = host.attachShadow({mode: 'closed'});
        const inner = document.createElement('div');
        inner.className = 'gl-MarketGroup';
        inner.textContent = 'hidden market';
        root.appendChild(inner);
        """
    )
    found = provider._query(driver, ".gl-MarketGroup")
    assert len(found) == 1
    assert provider._text(driver, found[0]) == "hidden market"


def test_milestone_to_int_and_match_urls():
    from arb_tool.providers.bet365_selenium import _milestone_to_int, _parse_match_urls

    assert _milestone_to_int("3+") == 3
    assert _milestone_to_int("10+") == 10
    assert _milestone_to_int("Over") is None

    urls = _parse_match_urls("world_cup=https://x/1 , la_liga=https://x/2")
    assert urls == [
        (Competition.WORLD_CUP, "https://x/1"),
        (Competition.LA_LIGA, "https://x/2"),
    ]


def test_parse_match_page_from_fixture(driver):
    provider = SeleniumBet365Provider()
    driver.get(FIXTURE.as_uri())
    event_name = provider._read_event_name(driver)
    props = provider._parse_match_page(driver, Competition.PREMIER_LEAGUE, event_name)

    assert event_name == "Liverpool vs Arsenal"

    # Player Shots: Salah at 1+/2+/3+ (Over 0.5/1.5/2.5), Saka at 1+/2+
    # (3+ suspended). Plus Salah Shots on Target 1+ (Over 0.5). = 6 props.
    # The "Player Shots Over/Under" pod must NOT contribute (9.99 excluded).
    assert all(p.over != pytest.approx(9.99) for p in props)
    assert all(p.under is None for p in props)  # milestone grid is Over-only

    by_key = {(p.player, p.market, p.line): p for p in props}

    # "2+" shots == Over 1.5, Salah @ 2.10
    salah_o15 = by_key[("Mohamed Salah", Market.SHOTS, 1.5)]
    assert salah_o15.over == pytest.approx(2.10)
    assert salah_o15.event == "Liverpool vs Arsenal"

    # "1+" shots == Over 0.5, Saka @ 1.30
    assert by_key[("Bukayo Saka", Market.SHOTS, 0.5)].over == pytest.approx(1.30)
    # "3+" shots == Over 2.5, Salah @ 4.00; Saka suspended -> absent
    assert by_key[("Mohamed Salah", Market.SHOTS, 2.5)].over == pytest.approx(4.00)
    assert ("Bukayo Saka", Market.SHOTS, 2.5) not in by_key

    # Shots on target "1+" == Over 0.5, Salah @ 1.85
    salah_sot = by_key[("Mohamed Salah", Market.SHOTS_ON_TARGET, 0.5)]
    assert salah_sot.over == pytest.approx(1.85)

    assert len(props) == 6
