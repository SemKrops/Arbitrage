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

# Milestone-grid market titles (English + Dutch) mapped to our markets. The
# match is exact on the pod title so nearby variants ("... Over/Under",
# "Headed ...", "... Outside Box") are excluded.
MILESTONE_MARKET_TITLES = {
    "player shots": Market.SHOTS,
    "speler schoten": Market.SHOTS,
    "spelersschoten": Market.SHOTS,
    "player shots on target": Market.SHOTS_ON_TARGET,
    "speler schoten op doel": Market.SHOTS_ON_TARGET,
    "spelersschoten op doel": Market.SHOTS_ON_TARGET,
}

# Regexes (applied to visible link text, case-insensitive) used to find each
# competition in bet365's navigation. Text survives the class obfuscation.
COMPETITION_LINK_PATTERNS = {
    Competition.PREMIER_LEAGUE: r"^premier league$",
    Competition.LA_LIGA: r"^la ?liga$",
    Competition.WORLD_CUP: r"^(wk( \d{4})?|(fifa )?world cup( \d{4})?|wereldkampioenschap( \d{4})?)$",
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

# Find clickable elements by their visible text (regex), shadow-DOM aware.
# bet365 obfuscates its navigation classes, but the labels users click on
# ("WK 2026", team names) are stable — so navigation keys off text.
_TEXT_CANDIDATES_JS = r"""
const pattern = new RegExp(arguments[0], 'i');
const roots = [];
function collectRoots(root) {
  if (!root.querySelectorAll) return;
  roots.push(root);
  for (const el of root.querySelectorAll('*')) if (el.shadowRoot) collectRoots(el.shadowRoot);
}
collectRoots(document);
const out = [];
for (const root of roots) {
  for (const el of root.querySelectorAll('a, div, span, button')) {
    if (el.children.length > 3) continue;
    const t = (el.innerText || '').trim();
    if (!t || t.length > 60) continue;
    if (pattern.test(t)) out.push(el);
  }
}
return out;
"""

# List the fixtures on a competition coupon page as [element, "A v B"] pairs.
# Prefers bet365's coupon classes; falls back to leaf "X v Y" texts outside
# the (obfuscated) navigation chrome.
_FIXTURE_ROWS_JS = r"""
const roots = [];
function collectRoots(root) {
  if (!root.querySelectorAll) return;
  roots.push(root);
  for (const el of root.querySelectorAll('*')) if (el.shadowRoot) collectRoots(el.shadowRoot);
}
collectRoots(document);

const out = [];
const seen = new Set();
for (const root of roots) {
  for (const el of root.querySelectorAll(
    '.rcl-ParticipantFixtureDetails_TeamNames, [class*="FixtureDetails_TeamNames"], ' +
    '.sl-CouponParticipantWithBookCloses_Name'
  )) {
    const name = (el.innerText || '').trim().replace(/\s*\n\s*/g, ' v ');
    if (name && name.includes(' v ') && !seen.has(name)) {
      seen.add(name);
      out.push([el, name]);
    }
  }
}
if (out.length) return out;

function inChrome(el) {
  let n = el;
  for (let i = 0; i < 14 && n; i++) {
    const c = (n.className && n.className.baseVal !== undefined)
      ? n.className.baseVal : (n.className || '');
    if (/(^|\s)(lhs|sln|hrm|wc|wn|hl|nav)-/.test(String(c))) return true;
    n = n.parentElement || (n.getRootNode && n.getRootNode().host);
  }
  return false;
}
const re = /^[A-Z][\w .'À-ɏ-]{1,28} v [A-Z][\w .'À-ɏ-]{1,28}$/;
for (const root of roots) {
  for (const el of root.querySelectorAll('a, div, span')) {
    if (el.children.length > 3) continue;
    const t = (el.innerText || '').trim();
    if (re.test(t) && !inChrome(el) && !seen.has(t)) {
      seen.add(t);
      out.push([el, t]);
    }
  }
}
return out;
"""

# Extract the shots milestone grids into structured rows. Each shots pod is
# a set of columns: one player column (.srb-ParticipantLabelWithTeam_Name)
# and one column per "N+" milestone. Every milestone column holds exactly
# one .gl-ParticipantOddsOnly cell per player (suspended cells included, with
# empty odds), so cell i aligns to player i. Returns, per pod:
#   { title, players: [name], columns: [ { header: "2+", odds: [".."] } ] }
# with odds[i] the price for players[i] ("" when suspended/absent).
_SHOTS_PROPS_JS = r"""
const roots = [];
function collectRoots(root) {
  if (!root.querySelectorAll) return;
  roots.push(root);
  for (const el of root.querySelectorAll('*')) if (el.shadowRoot) collectRoots(el.shadowRoot);
}
collectRoots(document);

function txt(el) { return el ? (el.innerText || el.textContent || '').trim() : ''; }

function headerOf(col) {
  // The milestone label, e.g. "2+". Scan descendants for the first N+ token.
  for (const el of col.querySelectorAll('*')) {
    const t = txt(el);
    if (/^\d+\+$/.test(t)) return t;
  }
  const first = (txt(col).split('\n')[0] || '').trim();
  return /^\d+\+$/.test(first) ? first : '';
}

const pods = [];
for (const root of roots) {
  for (const pod of root.querySelectorAll('.gl-MarketGroupPod')) {
    const title = txt(pod.querySelector(
      '.cm-MarketGroupWithIconsButton_Text, .srb-ButtonWithBetBuilderIcon_Text'
    ));
    const players = Array.from(pod.querySelectorAll('.srb-ParticipantLabelWithTeam_Name'))
      .map(txt).filter(t => t);
    if (!players.length) continue;

    const columns = [];
    for (const col of pod.querySelectorAll('.gl-Market')) {
      if (col.querySelector('.srb-ParticipantLabelWithTeam_Name')) continue;  // player col
      const cells = col.querySelectorAll('.gl-ParticipantOddsOnly');
      if (!cells.length) continue;
      const header = headerOf(col);
      if (!header) continue;
      const odds = Array.from(cells).map(
        c => txt(c.querySelector('.gl-ParticipantOddsOnly_Odds'))
      );
      columns.push({ header: header, odds: odds });
    }
    if (columns.length) pods.push({ title: title, players: players, columns: columns });
  }
}
return pods;
"""

# Find the match's "Team v Team" name in the MAIN content — not bet365's
# left-nav sidebar, whose obfuscated lhs-/sln-/hrm- containers also list
# other ("trending") fixtures and would otherwise be picked up.
_EVENT_NAME_JS = r"""
const roots = [];
function collectRoots(root) {
  if (!root.querySelectorAll) return;
  roots.push(root);
  for (const el of root.querySelectorAll('*')) if (el.shadowRoot) collectRoots(el.shadowRoot);
}
collectRoots(document);

const re = /^[A-Z][\w .'À-ɏ-]{1,28} v [A-Z][\w .'À-ɏ-]{1,28}$/;

function inChrome(el) {
  // True if el is inside the left-nav / header chrome (obfuscated prefixes).
  let n = el;
  for (let i = 0; i < 14 && n; i++) {
    const c = (n.className && n.className.baseVal !== undefined)
      ? n.className.baseVal : (n.className || '');
    if (/(^|\s)(lhs|sln|hrm|wc|wn|hl|nav)-/.test(String(c))) return true;
    n = n.parentElement || (n.getRootNode && n.getRootNode().host);
  }
  return false;
}

// Prefer explicit event-header selectors in the main content.
for (const sel of ['.sph-EventHeader_Label', '[class*="EventHeader"]',
                   '[class*="FixtureName"]', '[class*="ParticipantHeader"]']) {
  for (const root of roots) {
    for (const el of root.querySelectorAll(sel)) {
      const t = (el.innerText || '').trim();
      if (re.test(t) && !inChrome(el)) return t;
    }
  }
}
// Fallback: shortest "X v Y" text that is NOT in the nav/header chrome.
let best = null;
for (const root of roots) {
  for (const el of root.querySelectorAll('div, span, h1, h2')) {
    if (el.children.length > 2) continue;
    const t = (el.innerText || '').trim();
    if (re.test(t) && !inChrome(el) && (best === null || t.length < best.length)) best = t;
  }
}
return best;
"""

# Targeted odds extractor: for each shots market pod, pull the title, player
# names, column headers (the lines) and the actual odds cells — both the
# Over/Under "stacked" cells (handicap + odds) and the milestone "odds only"
# cells. Returns counts plus samples so the grid arrangement (column-major,
# players-per-column) can be confirmed and mapped to PropOdds.
_SHOTS_EXTRACT_JS = """
const roots = [];
function collectRoots(root) {
  if (!root.querySelectorAll) return;
  roots.push(root);
  for (const el of root.querySelectorAll('*')) if (el.shadowRoot) collectRoots(el.shadowRoot);
}
collectRoots(document);

function txt(el) { return el ? (el.innerText || el.textContent || '').trim() : ''; }

const pods = [];
for (const root of roots) {
  for (const pod of root.querySelectorAll('.gl-MarketGroupPod')) {
    const podText = (pod.innerText || '').toLowerCase();
    if (!(podText.includes('shots') || podText.includes('schoten'))) continue;

    const title = txt(pod.querySelector(
      '.cm-MarketGroupWithIconsButton_Text, .srb-ButtonWithBetBuilderIcon_Text'
    ));
    const players = Array.from(pod.querySelectorAll('.srb-ParticipantLabelWithTeam_Name'))
      .map(txt);
    const headers = Array.from(pod.querySelectorAll('.gl-Market_General-columnheader'))
      .map(txt).filter(t => t);

    const stacked = Array.from(pod.querySelectorAll('.gl-ParticipantCenteredStacked')).map(c => ({
      line: txt(c.querySelector('.gl-ParticipantCenteredStacked_Handicap')),
      odds: txt(c.querySelector('.gl-ParticipantCenteredStacked_Odds')),
      susp: c.className.includes('Suspended'),
    }));
    const oddsOnly = Array.from(pod.querySelectorAll('.gl-ParticipantOddsOnly')).map(c => ({
      odds: txt(c.querySelector('.gl-ParticipantOddsOnly_Odds')) || txt(c),
      susp: c.className.includes('Suspended'),
    }));

    pods.push({
      title: title,
      playerCount: players.length,
      players: players.slice(0, 6),
      headers: headers,
      stackedCount: stacked.length,
      stacked: stacked.filter(c => c.odds).slice(0, 12),
      oddsOnlyCount: oddsOnly.length,
      oddsOnly: oddsOnly.filter(c => c.odds).slice(0, 16),
    });
  }
}
return pods;
"""

# Scroll every shots-related market pod into view and expand it, so bet365
# lazily renders the odds cells (collapsed/off-screen markets stay empty).
_EXPAND_SHOTS_JS = """
const roots = [];
function collectRoots(root) {
  if (!root.querySelectorAll) return;
  roots.push(root);
  for (const el of root.querySelectorAll('*')) if (el.shadowRoot) collectRoots(el.shadowRoot);
}
collectRoots(document);

let expanded = 0;
for (const root of roots) {
  for (const pod of root.querySelectorAll('.gl-MarketGroupPod')) {
    const text = (pod.innerText || '').toLowerCase();
    if (!(text.includes('shots') || text.includes('schoten'))) continue;
    pod.scrollIntoView({ block: 'center' });
    // Expand if no odds are rendered yet.
    if (!pod.querySelector('.gl-ParticipantOddsOnly_Odds, .gl-Participant_General')) {
      const header = pod.querySelector(
        '.cm-MarketGroupWithIconsButton, .srb-ButtonWithBetBuilderIcon, [class*="_Open"]'
      );
      if (header) { header.click(); expanded++; }
    }
  }
}
return expanded;
"""

# Container selectors whose subtree we dump on a match page, so the
# player/line/odds grid can be reverse-engineered. bet365's odds grid uses
# stable gl-/srb- classes (only the left-nav sidebar is obfuscated).
_MARKET_SUBTREE_SELECTORS = [".gl-MarketGroupPod", ".gl-MarketGroup", ".gl-Market_General"]

# Dump the subtree of shots market-pod containers: depth, tag, class list
# and short text for each descendant, revealing the grid layout (pod title,
# column headers, player rows, odds cells). Team-kit images and recent-form
# stat blocks are skipped so the odds columns fit in the node budget.
_MARKET_SUBTREE_JS = """
const selectors = arguments[0];
const maxPods = 4;
const maxNodes = 160;
const skipClass = /^(tk-|prs-)|_Asset|TeamKit/;

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
    for (const el of root.querySelectorAll(selector)) {
      const text = (el.innerText || '').toLowerCase();
      if (text.includes('shots') || text.includes('schoten')) pods.push(el);
    }
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
    if (tag === 'script' || tag === 'style' || tag === 'svg' || tag === 'img') return;
    if (skipClass.test(clsOf(node))) return;
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
        # Explicit match pages to scrape, mapped to their competition. bet365's
        # left-nav classes are obfuscated and its match URLs are session-bound,
        # so the reliable way to point the scraper at games is to list their
        # URLs. Configure with BET365_MATCH_URLS as competition=url pairs,
        # comma/newline separated, e.g.:
        #   BET365_MATCH_URLS=world_cup=https://www.bet365.nl/#/AC/.../
        self.match_urls = _parse_match_urls(os.environ.get("BET365_MATCH_URLS", ""))

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
            # Detect the installed Chrome major version up front so the first
            # launch already downloads the matching driver — avoids uc's
            # default of fetching the newest driver, failing, and relaunching.
            version_main = self._detect_chrome_major()

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
                # Detection failed or was wrong; recover from the error text
                # ("...only supports Chrome version 150. Current browser
                # version is 149...") and retry once with the real version.
                detected = re.search(r"[Cc]urrent browser version is (\d+)", str(exc))
                if detected is None or int(detected.group(1)) == version_main:
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

    def _detect_chrome_major(self) -> int | None:
        """Best-effort detection of the installed Chrome major version.

        Order: BET365_CHROME_VERSION env, Windows registry, running the
        Chrome binary with --version. Returns None if all fail (the caller
        then falls back to uc's default plus the mismatch retry).
        """
        pinned = os.environ.get("BET365_CHROME_VERSION", "")
        if pinned.isdigit():
            return int(pinned)

        # Windows: BLBeacon holds the installed version string.
        try:
            import winreg  # type: ignore

            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, r"Software\Google\Chrome\BLBeacon") as key:
                        version, _ = winreg.QueryValueEx(key, "version")
                        return int(str(version).split(".")[0])
                except OSError:
                    continue
        except ImportError:
            pass  # not Windows

        # POSIX / fallback: ask the binary.
        import shutil
        import subprocess

        binary = self.chrome_binary or shutil.which("google-chrome") or \
            shutil.which("chromium") or shutil.which("chrome")
        if binary:
            try:
                out = subprocess.run(
                    [binary, "--version"], capture_output=True, text=True, timeout=10
                ).stdout
                match = re.search(r"(\d+)\.\d+", out)
                if match:
                    return int(match.group(1))
            except (OSError, subprocess.SubprocessError):
                pass
        return None

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
        # Bootstrap the app on the main site first so its session/cookies are
        # established; deep/hash navigation stalls until the SPA initialised.
        self._bootstrap(driver)

        for competition in competitions:
            explicit = [url for comp, url in self.match_urls if comp is competition]
            try:
                if explicit:
                    for url in explicit:
                        props.extend(self._scrape_match_url(driver, competition, url))
                else:
                    props.extend(self._scrape_competition(driver, competition))
            except Exception as exc:
                log.warning(
                    "Bet365: scraping %s failed (%s). Page title was %r — a "
                    "block/challenge page usually means your IP or headless "
                    "browser was detected; run --dump-bet365 to inspect.",
                    competition.value, exc, _safe_title(driver),
                )
        return props

    def _scrape_match_url(self, driver, competition: Competition, url: str) -> list[PropOdds]:
        self._open_match(driver, url)
        return self._scrape_current_match(driver, competition, fallback_name="")

    def _scrape_competition(self, driver, competition: Competition) -> list[PropOdds]:
        """Open the competition from the nav and scrape every listed match."""
        if not self._open_competition_page(driver, competition):
            return []
        fixtures = [name for _, name in self._list_fixtures(driver)]
        log.info(
            "Bet365: %s — %d fixtures listed: %s",
            competition.value, len(fixtures), fixtures[: self.max_events],
        )
        list_url = driver.current_url
        props: list[PropOdds] = []
        for name in fixtures[: self.max_events]:
            try:
                if not self._open_fixture(driver, name):
                    log.warning("Bet365: could not open fixture %r", name)
                    continue
                props.extend(
                    self._scrape_current_match(driver, competition, fallback_name=name)
                )
            finally:
                self._return_to_list(driver, list_url)
        return props

    def _scrape_current_match(
        self, driver, competition: Competition, fallback_name: str
    ) -> list[PropOdds]:
        self._open_shots_tab(driver)
        event_name = self._read_event_name(driver) or fallback_name
        if fallback_name and not _same_fixture(event_name, fallback_name):
            # The header probe can misfire; the name we clicked is authoritative.
            event_name = fallback_name.replace(" v ", " vs ")
        found = self._parse_match_page(driver, competition, event_name)
        log.info(
            "Bet365: %s (%s) — %d shots props",
            event_name, competition.value, len(found),
        )
        return found

    # ---------------- competition/fixture navigation ------------------- #

    def _open_competition_page(self, driver, competition: Competition) -> bool:
        """Click the competition's nav link (found by text) from the base page."""
        driver.get(f"{self.base_url}/#/AS/B1/")
        driver.refresh()  # hash-only navigation doesn't re-route the SPA
        self._wait_for(driver, "body")
        self._dismiss_cookies(driver)
        pattern = COMPETITION_LINK_PATTERNS[competition]
        deadline = time.time() + self.page_timeout
        links = []
        while time.time() < deadline and not links:
            links = driver.execute_script(_TEXT_CANDIDATES_JS, pattern)
            if not links:
                time.sleep(1)
        if not links:
            log.warning(
                "Bet365: no %s link found in navigation (pattern %s) — check "
                "the competition is listed on the site.",
                competition.value, pattern,
            )
            return False
        self._click(driver, links[0])
        if self._wait_until(lambda: self._list_fixtures(driver)):
            return True
        log.warning(
            "Bet365: clicked %s but no fixture list appeared", competition.value
        )
        return False

    def _list_fixtures(self, driver) -> list[tuple[object, str]]:
        rows = driver.execute_script(_FIXTURE_ROWS_JS)
        return [(el, name) for el, name in rows]

    def _open_fixture(self, driver, name: str) -> bool:
        """Click the fixture row with this name and wait for the match page."""
        for element, row_name in self._list_fixtures(driver):
            if row_name != name:
                continue
            # Click the row (or a clickable ancestor if the label ignores it).
            target = element
            for _ in range(4):
                self._click(driver, target)
                if self._wait_for(
                    driver, ".sph-MarketGroupNavBarButton, .gl-MarketGroupPod",
                    timeout=6,
                ):
                    return True
                parent = driver.execute_script(
                    "return arguments[0].parentElement", target
                )
                if parent is None:
                    break
                target = parent
            return False
        return False

    def _return_to_list(self, driver, list_url: str) -> None:
        driver.get(list_url)
        driver.refresh()  # force the SPA to re-route to the coupon
        self._wait_until(lambda: self._list_fixtures(driver))

    def _wait_until(self, condition) -> bool:
        deadline = time.time() + self.page_timeout
        while time.time() < deadline:
            try:
                if condition():
                    return True
            except Exception:
                pass
            time.sleep(1)
        return False

    def _bootstrap(self, driver) -> None:
        """Load the main site once so the SPA initialises before deep links."""
        try:
            driver.get(f"{self.base_url}/#/AS/B1/")
            self._wait_for(driver, ".hrm-7, .wc-PageView, .gl-MarketGroupPod, .sln-8")
            self._dismiss_cookies(driver)
            time.sleep(2)
        except Exception as exc:  # pragma: no cover - best effort
            log.debug("Bet365: bootstrap load failed: %s", exc)

    def _open_match(self, driver, url: str) -> None:
        """Navigate to a match page, reloading if the markets don't render.

        bet365 is a hash-routed SPA: driver.get() onto a different #/...
        fragment doesn't re-route the app, so force a refresh to boot the
        SPA directly at the match route.
        """
        driver.get(url)
        for attempt in range(3):
            driver.refresh()
            if self._wait_for(driver, ".sph-MarketGroupNavBarButton, .gl-MarketGroupPod"):
                return
            log.info(
                "Bet365: match page markets not ready (attempt %d/3), reloading",
                attempt + 1,
            )
            time.sleep(2)
        log.warning(
            "Bet365: match page never rendered its market bar (%s) — the URL "
            "may be stale/expired or the game may be closed.", url,
        )

    def _open_shots_tab(self, driver) -> None:
        """Click the match page's 'Shots' market-group tab and let it render."""
        self._wait_for(driver, ".sph-MarketGroupNavBarButton, .gl-MarketGroupPod")
        tabs = [self._text(driver, b) for b in
                self._query(driver, ".sph-MarketGroupNavBarButton_Content")]
        clicked = False
        for button in self._query(driver, ".sph-MarketGroupNavBarButton_Content"):
            if self._text(driver, button).lower() in ("shots", "schoten"):
                self._click(driver, button)
                clicked = True
                break
        if not clicked:
            log.warning(
                "Bet365: no 'Shots' tab on this match — player shots markets "
                "may not be offered yet (they usually open ~1-2 days before "
                "kickoff). Tabs present: %s", tabs or "none",
            )
        self._wait_for(driver, ".gl-MarketGroupPod")
        # Scroll the shots pods into view so bet365 renders their odds cells.
        try:
            driver.execute_script(_EXPAND_SHOTS_JS)
        except Exception:  # pragma: no cover - best effort
            pass
        time.sleep(3)

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #

    def _parse_match_page(
        self, driver, competition: Competition, event_name: str
    ) -> list[PropOdds]:
        """Parse the shots milestone grids into Over-side PropOdds.

        bet365 lists player props as "N+ shots" milestone columns (N or more
        = Over (N-0.5)), giving only the Over side; that still arbs against
        Unibet's Under at the same line.
        """
        pods = driver.execute_script(_SHOTS_PROPS_JS)
        props: list[PropOdds] = []
        for pod in pods:
            market = MILESTONE_MARKET_TITLES.get((pod.get("title") or "").strip().lower())
            if market is None:
                continue  # not a plain shots milestone grid (e.g. Over/Under, Headed)
            props.extend(self._pod_to_props(pod, competition, event_name, market))
        if not props:
            # Report what was on the page so 0-prop runs are diagnosable:
            # is it "no shots pods" (markets closed / tab not open) vs "pods
            # present but titles/odds didn't parse" (selectors need updating)?
            all_pods = driver.execute_script(
                "return Array.from(document.querySelectorAll('.gl-MarketGroupPod'))"
                ".map(p => (p.innerText||'').split('\\n')[0]).slice(0, 30)"
            )
            log.warning(
                "Bet365: no shots milestone props parsed on %r. Shots-grid "
                "pods seen by extractor: %d. All market pods on page (%d): %s",
                event_name, len(pods), len(all_pods), all_pods or "none",
            )
        return props

    def _pod_to_props(
        self, pod: dict, competition: Competition, event_name: str, market: Market
    ) -> list[PropOdds]:
        players = pod["players"]
        # player -> best (lowest-line) Over we have, so we emit one prop per
        # (player, line); keep every line since Unibet may match any of them.
        props: list[PropOdds] = []
        for column in pod["columns"]:
            milestone = _milestone_to_int(column["header"])
            if milestone is None:
                continue
            line = milestone - 0.5  # "N+" == Over (N-0.5)
            for index, odds_text in enumerate(column["odds"]):
                if index >= len(players):
                    break
                over = fractional_to_decimal(odds_text)
                if over is None:
                    continue  # suspended / no price for this player at this line
                props.append(
                    PropOdds(
                        bookmaker=self.name,
                        competition=competition,
                        event=event_name,
                        kickoff=None,
                        player=players[index],
                        market=market,
                        line=line,
                        over=over,
                        under=None,
                    )
                )
        return props

    def _read_event_name(self, driver) -> str:
        try:
            name = driver.execute_script(_EVENT_NAME_JS)
        except Exception:
            name = None
        if name:
            return name.replace(" v ", " vs ")
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
            self._dump_shots_odds(driver)
            self._dump_market_subtree(driver)
            self._dump_frames(driver, depth=2)
        finally:
            driver.quit()

    def _dump_shots_odds(self, driver) -> None:
        """Print the actual odds extracted from each shots market pod."""
        try:
            driver.execute_script(_EXPAND_SHOTS_JS)
            time.sleep(2)
        except Exception:  # pragma: no cover - best effort
            pass
        pods = driver.execute_script(_SHOTS_EXTRACT_JS)
        print("\n===== SHOTS ODDS EXTRACTION =====")
        if not pods:
            print("(no shots market pods found)")
            return
        for pod in pods:
            print(f"\n• {pod['title']!r}")
            print(f"    players: {pod['playerCount']} -> {pod['players']}")
            print(f"    column headers: {pod['headers']}")
            print(f"    stacked O/U cells: {pod['stackedCount']} -> {pod['stacked']}")
            print(f"    odds-only cells: {pod['oddsOnlyCount']} -> {pod['oddsOnly']}")

    def _dump_market_subtree(self, driver) -> None:
        """Print the DOM subtree of the shots market pods (the props grid)."""
        try:
            expanded = driver.execute_script(_EXPAND_SHOTS_JS)
            if expanded:
                print(f"\n(expanded {expanded} shots market(s), waiting for render)")
                time.sleep(3)
        except Exception as exc:  # pragma: no cover - best effort
            log.debug("Bet365: could not pre-expand shots markets: %s", exc)
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

    def _wait_for(self, driver, selector: str, timeout: float | None = None) -> bool:
        """Wait until ``selector`` exists, searching iframes and shadow DOM.

        On success the driver context is left switched to whichever
        document contains the selector.
        """
        from selenium.webdriver.support.ui import WebDriverWait

        try:
            WebDriverWait(driver, timeout or self.page_timeout).until(
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


def _milestone_to_int(header: str) -> int | None:
    """"3+" -> 3."""
    match = re.match(r"(\d+)\+", header.strip())
    return int(match.group(1)) if match else None


def _same_fixture(name_a: str, name_b: str) -> bool:
    """Loose comparison of two 'A v B' fixture names."""
    def teams(name: str) -> set:
        return {
            part.strip().lower()
            for part in re.split(r"\s+(?:v|vs)\s+", name)
            if part.strip()
        }

    return bool(teams(name_a) & teams(name_b))


def _parse_match_urls(raw: str) -> list[tuple[Competition, str]]:
    """Parse BET365_MATCH_URLS: "competition=url" pairs, comma/newline split."""
    result: list[tuple[Competition, str]] = []
    for chunk in re.split(r"[,\n]", raw):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        comp_key, _, url = chunk.partition("=")
        comp_key = comp_key.strip().lower().replace(" ", "_")
        if comp_key == "laliga":
            comp_key = "la_liga"
        url = url.strip()
        try:
            result.append((Competition(comp_key), url))
        except ValueError:
            log.warning("Bet365: unknown competition %r in BET365_MATCH_URLS", comp_key)
    return result


def _cell_at(cells, index: int) -> tuple[float | None, float | None]:
    if cells and index < len(cells):
        return cells[index]
    return None, None


def _safe_title(driver) -> str:
    try:
        return driver.title
    except Exception:  # pragma: no cover
        return "<no title>"
