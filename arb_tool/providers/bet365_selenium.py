"""Bet365 provider that scrapes player shots odds with Selenium.

Personal-use scraper (BET365_PROVIDER=selenium). It drives a real Chromium
via Selenium: opens the competition page, walks the listed matches, opens
each match page and reads the "Player Shots" / "Player Shots on Target"
market groups.

How bet365 hides its DOM
------------------------
bet365 renders the entire sports UI inside *closed* shadow DOM roots: the
top document contains little more than the header and betslip, while the
content is invisible to ``page_source``, plain CSS selection and
``element.shadowRoot``. Two counters are used here:

* a script injected before any page JS runs rewrites
  ``Element.attachShadow`` so every shadow root is created *open*;
* all element lookups go through a JavaScript deep query that descends
  into (now open) shadow roots, instead of ``find_elements``.

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
live in the SELECTORS dict below so they can be fixed in one place. Use
``python -m arb_tool --dump-bet365`` to see what the current DOM offers.

Environment variables:

    BET365_BASE_URL          default https://www.bet365.nl
    BET365_HEADLESS          default 1 (set 0 to show the browser window)
    BET365_MAX_EVENTS        default 10 matches per competition
    BET365_CHROME_BINARY     optional path to a Chrome/Chromium binary
    BET365_CHROMEDRIVER      optional path to a matching chromedriver
    BET365_CHROME_VERSION    optional Chrome major version to pin the
                             undetected-chromedriver download to (normally
                             auto-detected on mismatch)
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

# Injected before any page script runs: bet365 creates its shadow roots
# closed, which hides them from automation; force them open. The flag lets
# diagnostics confirm the pre-load injection actually ran.
_FORCE_OPEN_SHADOW_JS = """
(function () {
  window.__arbShadowPatched = true;
  const original = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function (init) {
    const opened = Object.assign({}, init || {}, { mode: 'open' });
    return original.call(this, opened);
  };
})();
"""

# Deep querySelectorAll that descends into open shadow roots. arguments[0]
# is the CSS selector, arguments[1] an optional root element/document.
_DEEP_QUERY_JS = """
const selector = arguments[0];
const start = arguments[1] || document;
const out = [];
function collect(root) {
  if (!root.querySelectorAll) return;
  for (const el of root.querySelectorAll(selector)) out.push(el);
  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot) collect(el.shadowRoot);
  }
}
collect(start);
if (start.shadowRoot) collect(start.shadowRoot);
return out;
"""

# One-shot page probe for --dump-bet365. Walks the light DOM and every
# open shadow root, and reports: whether our attachShadow patch ran, total
# element and shadow-host counts, SELECTORS hit counts, the most frequent
# class names (UNFILTERED, minus obvious betslip noise), and — most useful
# for rebuilding selectors — the class-chain of whichever element contains
# each requested anchor text (e.g. "WK 2026").
_DUMP_STATS_JS = """
const selectors = arguments[0];
const anchors = arguments[1];

const roots = [];
let elementCount = 0;
function collectRoots(root) {
  if (!root.querySelectorAll) return;
  roots.push(root);
  const all = root.querySelectorAll('*');
  elementCount += all.length;
  for (const el of all) if (el.shadowRoot) collectRoots(el.shadowRoot);
}
collectRoots(document);

const hits = {};
for (const [name, css] of Object.entries(selectors)) {
  let n = 0;
  for (const root of roots) n += root.querySelectorAll(css).length;
  hits[name] = n;
}

const classes = {};
for (const root of roots) {
  for (const el of root.querySelectorAll('*')) {
    if (!el.classList) continue;
    for (const token of el.classList) {
      if (/^(bs|bss|bsf|bsk|bf)-/.test(token)) continue;  // betslip noise
      classes[token] = (classes[token] || 0) + 1;
    }
  }
}

function classChain(el) {
  const chain = [];
  let node = el;
  for (let i = 0; node && i < 8; i++) {
    const cls = (node.className && node.className.baseVal !== undefined)
      ? node.className.baseVal : (node.className || '');
    chain.push(node.tagName.toLowerCase() + (cls ? '.' + String(cls).trim().split(/\\s+/).join('.') : ''));
    node = node.parentElement || (node.getRootNode() && node.getRootNode().host);
  }
  return chain;
}

const anchorHits = {};
for (const anchor of anchors) {
  for (const root of roots) {
    let found = null;
    for (const el of root.querySelectorAll('*')) {
      if (el.children.length === 0 && (el.textContent || '').trim() === anchor) {
        found = el; break;
      }
    }
    if (!found) {  // fall back to a contains match on a leaf-ish node
      for (const el of root.querySelectorAll('*')) {
        if (el.children.length <= 1 && (el.textContent || '').includes(anchor)) {
          found = el; break;
        }
      }
    }
    if (found) { anchorHits[anchor] = classChain(found); break; }
  }
}

return {
  patched: !!window.__arbShadowPatched,
  elementCount: elementCount,
  shadowRoots: roots.length - 1,
  hits: hits,
  classes: classes,
  anchors: anchorHits,
};
"""

# On-screen texts used to locate the sports-content DOM by anchor.
_DUMP_ANCHORS = ["WK 2026", "Frankrijk v Marokko", "Alle sporten", "Aankomend", "Live"]

# Container selectors whose subtree we dump on a match page, so the
# player/line/odds grid can be reverse-engineered. bet365's odds grid uses
# stable gl-/srb- classes (only the left-nav sidebar is obfuscated).
_MARKET_SUBTREE_SELECTORS = [".gl-MarketGroupPod", ".gl-MarketGroup", ".gl-Market_General"]

# Dump the subtree of the first few market-pod containers: depth, tag,
# class list and short text for each descendant, revealing the grid layout
# (pod title, column headers, player rows, odds cells).
_MARKET_SUBTREE_JS = """
const selectors = arguments[0];
const maxPods = 3;
const maxNodes = 90;

const roots = [];
function collectRoots(root) {
  if (!root.querySelectorAll) return;
  roots.push(root);
  for (const el of root.querySelectorAll('*')) if (el.shadowRoot) collectRoots(el.shadowRoot);
}
collectRoots(document);

function clsOf(node) {
  const c = (node.className && node.className.baseVal !== undefined)
    ? node.className.baseVal : (node.className || '');
  return String(c).trim();
}

let pods = [];
for (const selector of selectors) {
  for (const root of roots) {
    for (const el of root.querySelectorAll(selector)) pods.push(el);
  }
  if (pods.length) break;  // use the first selector that matches anything
}
pods = pods.slice(0, maxPods);

const results = [];
for (const pod of pods) {
  const nodes = [];
  (function walk(node, depth) {
    if (nodes.length >= maxNodes) return;
    const tag = node.tagName.toLowerCase();
    if (tag === 'script' || tag === 'style' || tag === 'svg') return;
    const text = Array.from(node.childNodes)
      .filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').trim().slice(0, 40);
    nodes.push({ depth: depth, tag: tag, cls: clsOf(node), text: text });
    for (const child of node.children) walk(child, depth + 1);
  })(pod, 0);
  results.push({ selector: clsOf(pod), nodeCount: nodes.length, nodes: nodes });
}
return results;
"""


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
        except ImportError:
            uc = None

        if uc is not None:
            from selenium.common.exceptions import SessionNotCreatedException

            log.info("Bet365: using undetected-chromedriver")
            pinned = os.environ.get("BET365_CHROME_VERSION", "")
            version_main = int(pinned) if pinned else None

            def launch(version):
                # A ChromeOptions object cannot be reused between attempts.
                options = uc.ChromeOptions()
                self._apply_common_options(options)
                driver = uc.Chrome(
                    options=options, headless=self.headless, version_main=version
                )
                self._install_page_scripts(driver)
                return driver

            try:
                return launch(version_main)
            except SessionNotCreatedException as exc:
                # uc fetched a driver for the newest Chrome, but the locally
                # installed browser lags behind ("This version of ChromeDriver
                # only supports Chrome version 150. Current browser version is
                # 149..."). Retry pinned to the browser's actual version.
                detected = re.search(r"[Cc]urrent browser version is (\d+)", str(exc))
                if detected is None:
                    raise
                version = int(detected.group(1))
                log.info(
                    "Bet365: chromedriver/browser mismatch, retrying with "
                    "driver for Chrome %d", version,
                )
                return launch(version)

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
        self._install_page_scripts(driver)
        return driver

    def _install_page_scripts(self, driver) -> None:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument", {"source": _FORCE_OPEN_SHADOW_JS}
        )

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
    # Shadow-DOM aware element access
    # ------------------------------------------------------------------ #

    def _query(self, driver, selector: str, root=None) -> list:
        """querySelectorAll that also searches inside open shadow roots."""
        return driver.execute_script(_DEEP_QUERY_JS, selector, root)

    def _text(self, driver, element) -> str:
        return (driver.execute_script("return arguments[0].innerText", element) or "").strip()

    def _click(self, driver, element) -> None:
        driver.execute_script("arguments[0].click()", element)

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
                    self._wait_for(driver, SELECTORS["market_group"])
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
        deep_link = self.competition_urls[competition]
        if deep_link:
            driver.get(deep_link)
            return self._wait_for(driver, SELECTORS["event_row"])

        driver.get(f"{self.base_url}/#/AS/B1/")  # football section
        self._dismiss_cookies(driver)
        if not self._wait_for(driver, SELECTORS["competition_link"]):
            log.warning(
                "Bet365: football section did not load (title %r) — the DOM "
                "may have changed (run --dump-bet365) or you may be blocked; "
                "try BET365_HEADLESS=0 or set BET365_URL_%s to a direct link.",
                _safe_title(driver), competition.name,
            )
            return False

        wanted = COMPETITION_LINKS[competition]
        for link in self._query(driver, SELECTORS["competition_link"]):
            text = self._text(driver, link).lower()
            if any(w in text for w in wanted):
                self._click(driver, link)
                return self._wait_for(driver, SELECTORS["event_row"])
        log.warning("Bet365: no %s link found in football section", competition.value)
        return False

    def _collect_event_urls(self, driver) -> list[str]:
        """Open each listed match once to record its (session-bound) URL."""
        urls: list[str] = []
        count = len(self._query(driver, SELECTORS["event_row"]))
        list_url = driver.current_url
        for index in range(min(count, self.max_events)):
            rows = self._query(driver, SELECTORS["event_row"])
            if index >= len(rows):
                break
            self._click(driver, rows[index])
            if self._wait_for(driver, SELECTORS["market_group"]):
                urls.append(driver.current_url)
            driver.get(list_url)
            self._wait_for(driver, SELECTORS["event_row"])
        return urls

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #

    def _parse_match_page(self, driver, competition: Competition) -> list[PropOdds]:
        event_name = self._read_event_name(driver)
        props: list[PropOdds] = []
        for group in self._query(driver, SELECTORS["market_group"]):
            market = self._classify_group(driver, group)
            if market is None:
                continue
            self._expand_group(driver, group)
            props.extend(
                self._parse_market_group(driver, group, competition, event_name, market)
            )
        if not props:
            log.debug("Bet365: no player shots markets on %r", event_name)
        return props

    def _classify_group(self, driver, group) -> Market | None:
        titles = self._query(driver, SELECTORS["market_group_title"], group)
        title = self._text(driver, titles[0]).lower() if titles else ""
        for market, names in MARKET_TITLES.items():
            if title in names:
                return market
        return None

    def _expand_group(self, driver, group) -> None:
        """Open a collapsed market group and click 'Show more' if present."""
        is_open = driver.execute_script(
            "return arguments[0].matches(arguments[1])", group, SELECTORS["market_group_open"]
        )
        if not is_open:
            headers = self._query(driver, SELECTORS["market_group_title"], group)
            if headers:
                self._click(driver, headers[0])
                time.sleep(0.5)
        for more in self._query(driver, SELECTORS["show_more"], group):
            self._click(driver, more)
            time.sleep(0.3)

    def _parse_market_group(
        self, driver, group, competition: Competition, event_name: str, market: Market
    ) -> list[PropOdds]:
        """Parse bet365's player-props grid.

        Layout: a label column with player names, then one column per side
        ("Over"/"Under"), each cell stacking the line above the odds.
        """
        players = [
            text
            for el in self._query(driver, SELECTORS["player_name"], group)
            if (text := self._text(driver, el))
        ]
        if not players:
            return []

        sides: dict[str, list[tuple[float | None, float | None]]] = {}
        for column in self._query(driver, SELECTORS["column"], group):
            headers = self._query(driver, SELECTORS["column_header"], column)
            header = self._text(driver, headers[0]).lower() if headers else ""
            if header in ("over", "meer dan"):
                side = "over"
            elif header in ("under", "minder dan"):
                side = "under"
            else:
                continue
            cells = []
            for cell in self._query(driver, SELECTORS["cell_stacked"], column):
                line = self._read_text(driver, cell, SELECTORS["cell_handicap"])
                odds = self._read_text(driver, cell, SELECTORS["cell_odds"])
                cells.append((_parse_line(line), fractional_to_decimal(odds)))
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
        for selector in (".sph-EventHeader_Label", ".sip-EventHeader_Label", "h1"):
            elements = self._query(driver, selector)
            if elements:
                text = self._text(driver, elements[0])
                if text:
                    return text.replace(" v ", " vs ")
        return driver.title

    # ------------------------------------------------------------------ #
    # Diagnostics
    # ------------------------------------------------------------------ #

    def dump_page(
        self, url: str | None = None, out_prefix: str = "bet365_dump", wait: bool = False
    ) -> None:
        """Open a bet365 page and report what the scraper can see.

        Saves rendered HTML and a screenshot, then prints — shadow DOM and
        iframes included — the attachShadow-patch status, element/shadow
        counts, SELECTORS hit counts, anchor-text class chains, unfiltered
        frequent classes and (on a match page) the market subtree structure.

        With ``wait=True`` (``--dump-bet365 --wait``, best with
        BET365_HEADLESS=0) it pauses after loading so you can navigate the
        browser to a match page that has player shots markets open, then
        dumps whatever is on screen — the reliable way to capture the
        odds-grid DOM despite bet365's obfuscated class names.
        Run via: python -m arb_tool --dump-bet365 [URL] [--wait]
        """
        from pathlib import Path

        driver = self._build_driver()
        try:
            driver.get(url or f"{self.base_url}/#/AS/B1/")
            time.sleep(12)  # let the SPA render
            self._dismiss_cookies(driver)
            time.sleep(2)

            if wait:
                print(
                    "\n>>> Browser is open. In the bet365 window, navigate to a "
                    "match that\n    has Player Shots / Spelersschoten markets "
                    "open, then press Enter\n    here to dump that page..."
                )
                try:
                    input()
                except EOFError:
                    print("(no interactive stdin; dumping current page)")
                time.sleep(1)

            Path(f"{out_prefix}.html").write_text(driver.page_source, encoding="utf-8")
            driver.save_screenshot(f"{out_prefix}.png")
            print(f"Title : {driver.title}")
            print(f"URL   : {driver.current_url}")
            print(f"Saved : {out_prefix}.html, {out_prefix}.png")

            self._dump_context(driver, "top document")
            self._dump_market_subtree(driver)
            self._dump_frames(driver, depth=2)
        finally:
            driver.quit()

    def _dump_market_subtree(self, driver) -> None:
        """Print the DOM subtree of the market pods (the player-props grid)."""
        results = driver.execute_script(_MARKET_SUBTREE_JS, _MARKET_SUBTREE_SELECTORS)
        if not results:
            print(
                "\nmarket subtree: no market-pod containers found on this page "
                "(navigate to a match's Shots tab and use --wait)"
            )
            return
        for index, result in enumerate(results):
            print(f"\nmarket pod #{index} ({result['selector']!r}, "
                  f"{result['nodeCount']} nodes):")
            for node in result["nodes"]:
                indent = "  " * node["depth"]
                cls = f".{node['cls'].replace(' ', '.')}" if node["cls"] else ""
                text = f"  {node['text']!r}" if node["text"] else ""
                print(f"  {indent}{node['tag']}{cls}{text}")

    def _dump_frames(self, driver, depth: int, label: str = "") -> None:
        """Recursively dump every iframe's content."""
        from selenium.webdriver.common.by import By

        frames = driver.find_elements(By.TAG_NAME, "iframe")
        if frames:
            print(f"\n*** {len(frames)} iframe(s) under {label or 'top document'} ***")
        for index, frame in enumerate(frames):
            src = (frame.get_attribute("src") or "<no src>")[:100]
            frame_label = f"{label}iframe[{index}] src={src}"
            try:
                driver.switch_to.frame(frame)
            except Exception as exc:
                print(f"\n=== {frame_label} === (cannot enter: {exc})")
                continue
            try:
                self._dump_context(driver, frame_label)
                if depth > 1:
                    self._dump_frames(driver, depth - 1, f"{frame_label} > ")
            finally:
                driver.switch_to.parent_frame()

    def _dump_context(self, driver, label: str) -> None:
        """Print scraper-relevant facts about the current document/frame."""
        print(f"\n=== {label} ===")
        body_text = driver.execute_script(
            "return document.body ? document.body.innerText.slice(0, 300) : ''"
        )
        print(f"body text: {body_text!r}")

        stats = driver.execute_script(_DUMP_STATS_JS, SELECTORS, _DUMP_ANCHORS)
        print(f"attachShadow patch ran: {stats['patched']}")
        print(f"elements reachable: {stats['elementCount']}")
        print(f"open shadow roots: {stats['shadowRoots']}")
        matched = {name: count for name, count in stats["hits"].items() if count}
        print(f"SELECTORS hits: {matched if matched else 'none'}")

        print("anchor text -> class chain (element containing known on-screen text):")
        if stats["anchors"]:
            for anchor, chain in stats["anchors"].items():
                print(f"  [{anchor}]")
                for step in chain:
                    print(f"      {step}")
        else:
            print("  (none of the anchor texts were found in the queried DOM)")

        print("frequent classes (unfiltered, betslip noise removed):")
        ranked = sorted(stats["classes"].items(), key=lambda kv: -kv[1])
        for token, count in ranked[:60]:
            print(f"{count:>5}  {token}")

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _dismiss_cookies(self, driver) -> None:
        for button in self._query(driver, SELECTORS["cookie_accept"]):
            try:
                self._click(driver, button)
                time.sleep(0.3)
            except Exception:  # pragma: no cover - best effort
                pass

    def _wait_for(self, driver, selector: str) -> bool:
        """Wait until ``selector`` exists, searching iframes and shadow DOM.

        On success the driver context is left switched to whichever
        document contains the selector.
        """
        from selenium.webdriver.support.ui import WebDriverWait

        try:
            WebDriverWait(driver, self.page_timeout).until(
                lambda d: self._enter_context_with(d, selector)
            )
            return True
        except Exception:
            return False

    def _enter_context_with(self, driver, selector: str, depth: int = 2) -> bool:
        """Switch to the (i)frame containing ``selector``; False if absent."""
        driver.switch_to.default_content()
        if self._descend_to(driver, selector, depth):
            return True
        driver.switch_to.default_content()
        return False

    def _descend_to(self, driver, selector: str, depth: int) -> bool:
        from selenium.webdriver.common.by import By

        if self._query(driver, selector):
            return True
        if depth <= 0:
            return False
        for frame in driver.find_elements(By.TAG_NAME, "iframe"):
            try:
                driver.switch_to.frame(frame)
            except Exception:
                continue
            if self._descend_to(driver, selector, depth - 1):
                return True
            driver.switch_to.parent_frame()
        return False

    def _read_text(self, driver, element, selector: str) -> str:
        found = self._query(driver, selector, element)
        return self._text(driver, found[0]) if found else ""


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
