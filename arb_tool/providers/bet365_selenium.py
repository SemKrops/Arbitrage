"""Bet365 provider that scrapes player shots odds with Selenium.

Personal-use scraper (BET365_PROVIDER=selenium). It drives a real Chromium
via Selenium: opens the competition page, walks the listed matches, opens
each match page and reads the "Player Shots" / "Player Shots on Target"
market groups.

Realistic expectations
----------------------
Bet365 employs aggressive bot detection (fingerprinting, IP reputation,
geo-blocking). This scraper works best:

* from a residential IP (datacenter/VPN IPs are usually blocked outright),
* non-headless (set BET365_HEADLESS=0; headless browsers are easier to
  detect), and
* with `undetected-chromedriver` installed (`pip install
  undetected-chromedriver`) — when importable it is used automatically
  instead of plain Selenium.

Bet365 also changes its CSS class names from time to time; the selectors
live in the SELECTORS dict below so they can be fixed in one place. When a
page fails to parse, the provider logs a warning and (with -v) dumps the
page title to help diagnose whether you were blocked or the layout changed.

Environment variables:

    BET365_BASE_URL          default https://www.bet365.nl
    BET365_HEADLESS          default 1 (set 0 to show the browser window)
    BET365_MAX_EVENTS        default 10 matches per competition
    BET365_CHROME_BINARY     optional path to a Chrome/Chromium binary
    BET365_CHROMEDRIVER      optional path to a matching chromedriver
    BET365_URL_PREMIER_LEAGUE / BET365_URL_LA_LIGA / BET365_URL_WORLD_CUP
        optional deep links to each competition page; when unset the
        scraper navigates from the football section by link text.
"""

from __future__ import annotations

import logging
import os
import re
import time

from ..models import Competition, Market, PropOdds
from .base import OddsProvider

log = logging.getLogger(__name__)

# All bet365 CSS class names in one place — first thing to check when the
# scraper stops finding markets.
SELECTORS = {
    "cookie_accept": ".ccm-CookieConsentPopup_Accept",
    "competition_link": ".sm-CouponLink_Label, .sm-SplashMarketGroupButton",
    "event_row": ".sl-CouponParticipantWithBookCloses_Name, .rcl-ParticipantFixtureDetails_TeamNames",
    "market_group": ".gl-MarketGroup",
    "market_group_title": ".gl-MarketGroupButton_Text, .sip-MarketGroupButton_Text",
    "market_group_open": ".gl-MarketGroup_Open",
    "show_more": ".gl-MarketGroupShowMore_Link",
    "player_name": ".srb-ParticipantLabelWithTeam_Name, .gl-ParticipantLabel_Name",
    "column": ".gl-Market",
    "column_header": ".gl-MarketColumnHeader",
    "cell_stacked": ".gl-ParticipantCenteredStacked",
    "cell_handicap": ".gl-ParticipantCenteredStacked_Handicap",
    "cell_odds": ".gl-ParticipantCenteredStacked_Odds",
}

# Market group titles as bet365 names them, English and Dutch.
MARKET_TITLES = {
    Market.SHOTS: ("player shots", "spelersschoten", "schoten van speler"),
    Market.SHOTS_ON_TARGET: (
        "player shots on target",
        "spelersschoten op doel",
        "schoten op doel van speler",
    ),
}

# Link texts used to find each competition from the football section.
COMPETITION_LINKS = {
    Competition.PREMIER_LEAGUE: ("premier league",),
    Competition.LA_LIGA: ("laliga", "la liga"),
    Competition.WORLD_CUP: ("world cup", "wk", "wereldkampioenschap"),
}


def fractional_to_decimal(odds_text: str) -> float | None:
    """Parse bet365 odds shown as decimal ("2.10") or fractional ("11/10")."""
    odds_text = odds_text.strip().replace(",", ".")
    if not odds_text:
        return None
    if "/" in odds_text:
        num, _, den = odds_text.partition("/")
        try:
            return 1.0 + float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(odds_text)
    except ValueError:
        return None


class SeleniumBet365Provider(OddsProvider):
    name = "Bet365"

    def __init__(self):
        self.base_url = os.environ.get("BET365_BASE_URL", "https://www.bet365.nl").rstrip("/")
        self.headless = os.environ.get("BET365_HEADLESS", "1") != "0"
        self.max_events = int(os.environ.get("BET365_MAX_EVENTS", "10"))
        self.chrome_binary = os.environ.get("BET365_CHROME_BINARY", "")
        self.page_timeout = float(os.environ.get("BET365_PAGE_TIMEOUT", "20"))
        self.competition_urls = {
            comp: os.environ.get(f"BET365_URL_{comp.name}", "") for comp in Competition
        }

    # ------------------------------------------------------------------ #
    # Browser setup
    # ------------------------------------------------------------------ #

    def _build_driver(self):
        """Start Chromium, preferring undetected-chromedriver when installed."""
        try:
            import undetected_chromedriver as uc

            options = uc.ChromeOptions()
            self._apply_common_options(options)
            log.info("Bet365: using undetected-chromedriver")
            return uc.Chrome(options=options, headless=self.headless)
        except ImportError:
            pass

        from selenium import webdriver

        options = webdriver.ChromeOptions()
        self._apply_common_options(options)
        if self.headless:
            options.add_argument("--headless=new")
        # Hide the most obvious webdriver fingerprints.
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        service = None
        driver_path = os.environ.get("BET365_CHROMEDRIVER", "")
        if driver_path:
            from selenium.webdriver.chrome.service import Service

            service = Service(executable_path=driver_path)
        driver = webdriver.Chrome(options=options, service=service)
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
        )
        return driver

    def _apply_common_options(self, options) -> None:
        options.add_argument("--window-size=1600,1000")
        options.add_argument("--lang=nl-NL")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        if self.chrome_binary:
            options.binary_location = self.chrome_binary
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy:
            options.add_argument(f"--proxy-server={proxy}")

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #

    def fetch_props(self, competitions: tuple[Competition, ...]) -> list[PropOdds]:
        driver = self._build_driver()
        try:
            return self._scrape(driver, competitions)
        finally:
            driver.quit()

    def _scrape(self, driver, competitions: tuple[Competition, ...]) -> list[PropOdds]:
        from selenium.webdriver.common.by import By

        props: list[PropOdds] = []
        for competition in competitions:
            try:
                if not self._open_competition(driver, competition):
                    continue
                event_urls = self._collect_event_urls(driver)
                log.info(
                    "Bet365: %s — found %d match pages", competition.value, len(event_urls)
                )
                for url in event_urls[: self.max_events]:
                    driver.get(url)
                    self._wait_for(driver, By.CSS_SELECTOR, SELECTORS["market_group"])
                    props.extend(self._parse_match_page(driver, competition))
            except Exception as exc:
                log.warning(
                    "Bet365: scraping %s failed (%s). Page title was %r — a "
                    "block/challenge page usually means your IP or headless "
                    "browser was detected.",
                    competition.value, exc, _safe_title(driver),
                )
        return props

    def _open_competition(self, driver, competition: Competition) -> bool:
        """Land on the competition's fixtures page; True on success."""
        from selenium.webdriver.common.by import By

        deep_link = self.competition_urls[competition]
        if deep_link:
            driver.get(deep_link)
            return self._wait_for(driver, By.CSS_SELECTOR, SELECTORS["event_row"])

        driver.get(f"{self.base_url}/#/AS/B1/")  # football section
        self._dismiss_cookies(driver)
        if not self._wait_for(driver, By.CSS_SELECTOR, SELECTORS["competition_link"]):
            log.warning(
                "Bet365: football section did not load (title %r) — likely "
                "blocked; try BET365_HEADLESS=0 and a residential IP, or set "
                "BET365_URL_%s to a direct competition link.",
                _safe_title(driver), competition.name,
            )
            return False

        wanted = COMPETITION_LINKS[competition]
        for link in driver.find_elements(By.CSS_SELECTOR, SELECTORS["competition_link"]):
            text = link.text.strip().lower()
            if any(w in text for w in wanted):
                driver.execute_script("arguments[0].click()", link)
                return self._wait_for(driver, By.CSS_SELECTOR, SELECTORS["event_row"])
        log.warning("Bet365: no %s link found in football section", competition.value)
        return False

    def _collect_event_urls(self, driver) -> list[str]:
        """Open each listed match once to record its (session-bound) URL."""
        from selenium.webdriver.common.by import By

        urls: list[str] = []
        count = len(driver.find_elements(By.CSS_SELECTOR, SELECTORS["event_row"]))
        list_url = driver.current_url
        for index in range(min(count, self.max_events)):
            rows = driver.find_elements(By.CSS_SELECTOR, SELECTORS["event_row"])
            if index >= len(rows):
                break
            driver.execute_script("arguments[0].click()", rows[index])
            if self._wait_for(driver, By.CSS_SELECTOR, SELECTORS["market_group"]):
                urls.append(driver.current_url)
            driver.get(list_url)
            self._wait_for(driver, By.CSS_SELECTOR, SELECTORS["event_row"])
        return urls

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #

    def _parse_match_page(self, driver, competition: Competition) -> list[PropOdds]:
        from selenium.webdriver.common.by import By

        event_name = self._read_event_name(driver)
        props: list[PropOdds] = []
        for group in driver.find_elements(By.CSS_SELECTOR, SELECTORS["market_group"]):
            market = self._classify_group(group)
            if market is None:
                continue
            self._expand_group(driver, group)
            props.extend(self._parse_market_group(group, competition, event_name, market))
        if not props:
            log.debug("Bet365: no player shots markets on %r", event_name)
        return props

    def _classify_group(self, group) -> Market | None:
        from selenium.webdriver.common.by import By

        titles = group.find_elements(By.CSS_SELECTOR, SELECTORS["market_group_title"])
        title = titles[0].text.strip().lower() if titles else ""
        for market, names in MARKET_TITLES.items():
            if title in names:
                return market
        return None

    def _expand_group(self, driver, group) -> None:
        """Open a collapsed market group and click 'Show more' if present."""
        from selenium.webdriver.common.by import By

        if not group.find_elements(By.CSS_SELECTOR, SELECTORS["market_group_open"]):
            headers = group.find_elements(By.CSS_SELECTOR, SELECTORS["market_group_title"])
            if headers:
                driver.execute_script("arguments[0].click()", headers[0])
                time.sleep(0.5)
        for more in group.find_elements(By.CSS_SELECTOR, SELECTORS["show_more"]):
            driver.execute_script("arguments[0].click()", more)
            time.sleep(0.3)

    def _parse_market_group(
        self, group, competition: Competition, event_name: str, market: Market
    ) -> list[PropOdds]:
        """Parse bet365's player-props grid.

        Layout: a label column with player names, then one column per side
        ("Over"/"Under"), each cell stacking the line above the odds.
        """
        from selenium.webdriver.common.by import By

        players = [
            el.text.strip()
            for el in group.find_elements(By.CSS_SELECTOR, SELECTORS["player_name"])
            if el.text.strip()
        ]
        if not players:
            return []

        sides: dict[str, list[tuple[float, float]]] = {}
        for column in group.find_elements(By.CSS_SELECTOR, SELECTORS["column"]):
            headers = column.find_elements(By.CSS_SELECTOR, SELECTORS["column_header"])
            header = headers[0].text.strip().lower() if headers else ""
            if header in ("over", "meer dan"):
                side = "over"
            elif header in ("under", "minder dan"):
                side = "under"
            else:
                continue
            cells = []
            for cell in column.find_elements(By.CSS_SELECTOR, SELECTORS["cell_stacked"]):
                line = _read_text(cell, SELECTORS["cell_handicap"])
                odds = _read_text(cell, SELECTORS["cell_odds"])
                parsed_line = _parse_line(line)
                parsed_odds = fractional_to_decimal(odds)
                cells.append((parsed_line, parsed_odds))
            sides[side] = cells

        props: list[PropOdds] = []
        for index, player in enumerate(players):
            over_line, over_odds = _cell_at(sides.get("over"), index)
            under_line, under_odds = _cell_at(sides.get("under"), index)
            line = over_line if over_line is not None else under_line
            if line is None or (over_odds is None and under_odds is None):
                continue
            if under_line is not None and over_line is not None and under_line != over_line:
                continue  # asymmetric lines: not a two-way arb candidate
            props.append(
                PropOdds(
                    bookmaker=self.name,
                    competition=competition,
                    event=event_name,
                    kickoff=None,
                    player=player,
                    market=market,
                    line=line,
                    over=over_odds,
                    under=under_odds,
                )
            )
        return props

    def _read_event_name(self, driver) -> str:
        from selenium.webdriver.common.by import By

        for selector in (".sph-EventHeader_Label", ".sip-EventHeader_Label", "h1"):
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if elements and elements[0].text.strip():
                return elements[0].text.strip().replace(" v ", " vs ")
        return driver.title

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _dismiss_cookies(self, driver) -> None:
        from selenium.webdriver.common.by import By

        for button in driver.find_elements(By.CSS_SELECTOR, SELECTORS["cookie_accept"]):
            try:
                button.click()
                time.sleep(0.3)
            except Exception:  # pragma: no cover - best effort
                pass

    def _wait_for(self, driver, by, selector: str) -> bool:
        from selenium.webdriver.support.ui import WebDriverWait

        try:
            WebDriverWait(driver, self.page_timeout).until(
                lambda d: d.find_elements(by, selector)
            )
            return True
        except Exception:
            return False


def _read_text(element, selector: str) -> str:
    from selenium.webdriver.common.by import By

    found = element.find_elements(By.CSS_SELECTOR, selector)
    return found[0].text if found else ""


def _parse_line(text: str) -> float | None:
    match = re.search(r"\d+(?:[.,]\d+)?", text)
    return float(match.group().replace(",", ".")) if match else None


def _cell_at(cells, index: int) -> tuple[float | None, float | None]:
    if cells and index < len(cells):
        return cells[index]
    return None, None


def _safe_title(driver) -> str:
    try:
        return driver.title
    except Exception:  # pragma: no cover
        return "<no title>"
