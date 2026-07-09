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


def test_parse_match_page_from_fixture(driver):
    provider = SeleniumBet365Provider()
    driver.get(FIXTURE.as_uri())
    props = provider._parse_match_page(driver, Competition.PREMIER_LEAGUE)

    assert len(props) == 3
    by_key = {(p.player, p.market): p for p in props}

    salah_shots = by_key[("Mohamed Salah", Market.SHOTS)]
    assert salah_shots.event == "Liverpool vs Arsenal"
    assert salah_shots.line == 2.5
    assert salah_shots.over == pytest.approx(2.10)
    assert salah_shots.under == pytest.approx(1.72)

    saka_shots = by_key[("Bukayo Saka", Market.SHOTS)]
    assert saka_shots.line == 1.5
    assert saka_shots.over == pytest.approx(2.60)  # 8/5 fractional

    salah_sot = by_key[("Mohamed Salah", Market.SHOTS_ON_TARGET)]
    assert salah_sot.market is Market.SHOTS_ON_TARGET
    assert salah_sot.line == 1.5

    # The "Goals Over/Under" group must not leak in.
    assert all(p.market in (Market.SHOTS, Market.SHOTS_ON_TARGET) for p in props)
