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

try:
    from selenium.common.exceptions import StaleElementReferenceException
except ImportError:  # selenium not installed (non-selenium runs)
    class StaleElementReferenceException(Exception):
        pass

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

# Keywords expected in the coupon's main header once we've actually landed on
# the competition's page — used to confirm navigation reached the right place
# (and not, say, the homepage's mixed "Upcoming" widget).
COMPETITION_COUPON_KEYWORDS = {
    Competition.PREMIER_LEAGUE: ("premier league",),
    Competition.LA_LIGA: ("laliga", "la liga"),
    Competition.WORLD_CUP: ("wk voetbal", "wk 2026", "wereldkampioenschap", "world cup"),
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

# Dispatch a full pointer+mouse click sequence, since bet365 binds handlers
# to pointerdown/mousedown (not the 'click' a bare .click() fires).
_FULL_CLICK_JS = r"""
const el = arguments[0];
const o = { bubbles: true, cancelable: true, view: window, button: 0 };
try { el.scrollIntoView({ block: 'center' }); } catch (e) {}
for (const type of ['pointerover', 'pointerenter', 'pointerdown',
                    'mousedown', 'pointerup', 'mouseup', 'click']) {
  const Evt = type.startsWith('pointer') ? PointerEvent : MouseEvent;
  try { el.dispatchEvent(new Evt(type, o)); } catch (e) {}
}
return true;
"""

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
# Handles three bet365 layouts: a single element holding both team names
# (stacked), a pair of sibling team-name elements per fixture, and — as a
# last resort — leaf "X v Y" texts outside the (obfuscated) nav chrome.
_FIXTURE_ROWS_JS = r"""
const roots = [];
function collectRoots(root) {
  if (!root.querySelectorAll) return;
  roots.push(root);
  for (const el of root.querySelectorAll('*')) if (el.shadowRoot) collectRoots(el.shadowRoot);
}
collectRoots(document);

// Scope the search to the coupon's MAIN content column, so the left-nav
// sidebar and homepage 'Upcoming' widget (which mix other sports) are
// excluded. Fall back to the whole document only if no coupon column exists.
let scopes = [];
for (const root of roots) {
  for (const c of root.querySelectorAll(
    '[class*="CouponPageReactResponsive_PageViewMain"], [class*="CouponPage"][class*="Main"]'
  )) scopes.push(c);
}
if (!scopes.length) scopes = roots;

const out = [];
const seen = new Set();
function push(el, name) {
  name = (name || '').trim();
  if (name && name.includes(' v ') && !seen.has(name)) {
    seen.add(name);
    out.push([el, name]);
  }
}
function queryAll(sel) {
  const els = [];
  for (const scope of scopes) for (const el of scope.querySelectorAll(sel)) els.push(el);
  return els;
}

// 1. A single element containing both team names (newline/￼ separated).
for (const el of queryAll(
  '.rcl-ParticipantFixtureDetails_TeamNames, [class*="FixtureDetails_TeamNames"], ' +
  '.sl-CouponParticipantWithBookCloses_Name, [class*="_TeamNames"]'
)) {
  push(el, (el.innerText || '').trim().replace(/\s*\n\s*/g, ' v '));
}
if (out.length) return out;

// 2. Two sibling team-name elements per fixture (home + away).
const teams = queryAll(
  '[class*="FixtureDetails_Team"]:not([class*="TeamNames"]), ' +
  '[class*="TwoWay_TeamName"], [class*="Competitor_Name"], [class*="_TeamName"]'
);
for (let i = 0; i + 1 < teams.length; i += 2) {
  const a = (teams[i].innerText || '').trim();
  const b = (teams[i + 1].innerText || '').trim();
  if (a && b) push(teams[i], a + ' v ' + b);
}
if (out.length) return out;

// 3. Leaf "X v Y" texts within the coupon scope.
const re = /^[A-Z][\w .'À-ɏ-]{1,28} v [A-Z][\w .'À-ɏ-]{1,28}$/;
for (const el of queryAll('a, div, span')) {
  if (el.children.length > 3) continue;
  const t = (el.innerText || '').trim();
  if (re.test(t)) push(el, t);
}
return out;
"""

# When no fixtures are found, report the coupon's fixture-ish classes and
# sample texts so the row selector can be fixed.
_FIXTURE_DIAG_JS = r"""
const roots = [];
function collectRoots(root) {
  if (!root.querySelectorAll) return;
  roots.push(root);
  for (const el of root.querySelectorAll('*')) if (el.shadowRoot) collectRoots(el.shadowRoot);
}
collectRoots(document);
const re = /Fixture|Participant|Competitor|Coupon|Team|Event|Match|Scoreboard/;
const classes = {};
const samples = [];
const seen = new Set();
for (const root of roots) {
  for (const el of root.querySelectorAll('*')) {
    if (!el.classList) continue;
    let hit = false;
    for (const c of el.classList) {
      if (re.test(c)) { classes[c] = (classes[c] || 0) + 1; hit = true; }
    }
    if (hit) {
      const t = (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 50);
      if (t && !seen.has(t) && seen.size < 25) { seen.add(t); samples.push(t); }
    }
  }
}
const ranked = Object.entries(classes).sort((a, b) => b[1] - a[1]).slice(0, 30);
return { classes: ranked, samples: samples };
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
        # Competition coupon pages (competition=url), the reliable way to reach
        # a tournament's fixtures: copy the URL your browser shows on e.g. the
        # "WK 2026" page. The scraper loads it and scrapes every listed match.
        self.coupon_urls = _parse_match_urls(os.environ.get("BET365_COMPETITION_URLS", ""))
        self._current_competition: Competition | None = None
        self._coupon_return_url: str | None = None
        # Persistent Chrome profile so cookie consent (and any login) is
        # remembered across runs — accept the banner once and it stays gone.
        # Defaults to .bet365_profile in the working dir; set empty to disable.
        default_profile = os.path.abspath(".bet365_profile")
        self.user_data_dir = os.environ.get("BET365_USER_DATA_DIR", default_profile).strip()

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
                    options=options, headless=self.headless, version_main=version,
                    user_data_dir=self.user_data_dir or None,
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
        if self.user_data_dir:
            options.add_argument(f"--user-data-dir={self.user_data_dir}")
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

    def setup_profile(self) -> None:
        """Open bet365 and wait for the user to accept cookies / sign in.

        Consent (and any login) is saved into the persistent profile, so
        subsequent automated runs start past the cookie wall — the thing
        that otherwise blocks all navigation. Run with BET365_HEADLESS=0:
            python -m arb_tool --bet365-setup
        """
        if not self.user_data_dir:
            print(
                "BET365_USER_DATA_DIR is disabled; set it (or leave the default "
                ".bet365_profile) so consent can be remembered."
            )
            return
        driver = self._build_driver()
        try:
            driver.get(self.base_url + "/")
            print(
                f"\nProfile: {self.user_data_dir}\n"
                ">>> In the bet365 window, click 'Alles accepteren' (and log in "
                "if you want), wait until the sport content is visible, then\n"
                "    press Enter here to save it to the profile..."
            )
            try:
                input()
            except EOFError:
                time.sleep(30)
            self._dismiss_cookies(driver)  # belt-and-suspenders
            print("Saved. Future runs will reuse this profile.")
        finally:
            driver.quit()

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

    def _click(self, driver, element) -> bool:
        """Click ``element`` with a real (trusted) browser click when possible.

        bet365's consent/menu handlers ignore synthetic JS-dispatched events
        (isTrusted=false), so try Selenium's native click first — that's a
        genuine browser click. Fall back to a dispatched pointer+mouse
        sequence only when the element is covered/not directly clickable.
        """
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", element
            )
        except StaleElementReferenceException:
            return False
        except Exception:
            pass
        try:
            element.click()  # native, trusted
            return True
        except StaleElementReferenceException:
            return False
        except Exception:
            pass
        try:
            driver.execute_script(_FULL_CLICK_JS, element)
            return True
        except StaleElementReferenceException:
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #

    def fetch_props(self, competitions: tuple[Competition, ...]) -> list[PropOdds]:
        if self.match_urls:
            log.info(
                "Bet365: BET365_MATCH_URLS is set (%d URL(s)) — scraping ONLY "
                "those and skipping auto-discovery. Unset it to scan every "
                "listed match. Configured: %s",
                len(self.match_urls),
                [f"{c.value}={u}" for c, u in self.match_urls],
            )
        else:
            log.info(
                "Bet365: auto-discovering matches by competition (no "
                "BET365_MATCH_URLS set)"
            )
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
                    props.extend(self._scrape_competition_resilient(driver, competition))
            except Exception as exc:
                log.warning(
                    "Bet365: scraping %s failed (%s). Page title was %r — a "
                    "block/challenge page usually means your IP or headless "
                    "browser was detected; run --dump-bet365 to inspect.",
                    competition.value, exc, _safe_title(driver),
                )
        return props

    def _scrape_competition_resilient(
        self, driver, competition: Competition
    ) -> list[PropOdds]:
        """Scrape a competition, retrying once if a stale element aborts it."""
        for attempt in range(2):
            try:
                return self._scrape_competition(driver, competition)
            except StaleElementReferenceException:
                log.info(
                    "Bet365: %s hit a stale element (attempt %d/2), retrying",
                    competition.value, attempt + 1,
                )
                self._bootstrap(driver)
        return []

    def _scrape_match_url(self, driver, competition: Competition, url: str) -> list[PropOdds]:
        self._open_match(driver, url)
        if self._is_dead_page(driver):
            log.warning(
                "Bet365: %s is no longer available (game finished/removed). "
                "Remove it from BET365_MATCH_URLS, or unset BET365_MATCH_URLS "
                "entirely to auto-scan the current fixtures.", url,
            )
            return []
        return self._scrape_current_match(driver, competition, fallback_name="")

    def _is_dead_page(self, driver) -> bool:
        """True when bet365 shows its 'page no longer available' notice."""
        try:
            body = driver.execute_script(
                "return document.body ? document.body.innerText : ''"
            ).lower()
        except Exception:
            return False
        return (
            "niet langer beschikbaar" in body  # NL
            or "no longer available" in body  # EN
        )

    def _scrape_competition(self, driver, competition: Competition) -> list[PropOdds]:
        """Open the competition's coupon and scrape every listed match."""
        self._current_competition = competition
        if not self._open_competition_page(driver, competition):
            return []

        fixtures = [name for _, name in self._list_fixtures(driver)]
        log.info(
            "Bet365: %s — %d fixtures listed: %s",
            competition.value, len(fixtures), fixtures[: self.max_events],
        )
        if not fixtures:
            # Navigation may have landed directly on a match page that already
            # shows the shots grid — parse it instead of reporting empty.
            direct = self._scrape_current_match(driver, competition, fallback_name="")
            if direct:
                log.info(
                    "Bet365: no fixture list, but the current page had %d shots "
                    "props — scraped those.", len(direct),
                )
                return direct
            self._report_empty_coupon(driver, competition)
            return []
        props: list[PropOdds] = []
        for name in fixtures[: self.max_events]:
            try:
                if not self._open_fixture(driver, name):
                    log.warning("Bet365: could not open fixture %r", name)
                    continue
                props.extend(
                    self._scrape_current_match(driver, competition, fallback_name=name)
                )
            except StaleElementReferenceException:
                log.debug("Bet365: stale element on fixture %r, skipping", name)
            finally:
                self._return_to_list(driver)
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
        """Reach the competition's coupon, ready to list fixtures.

        Order: (1) a configured coupon URL if it still resolves; (2) clicking
        the competition's sidebar link, verified by the coupon header so we
        don't accept the homepage's mixed 'Upcoming' widget. No hard refresh
        of hash routes (bet365 treats that as an expired page).
        """
        coupon = next((u for c, u in self.coupon_urls if c is competition), None)
        if coupon:
            driver.get(coupon)  # plain load, no refresh
            if self._on_competition_coupon(driver, competition):
                self._settle_coupon(driver)
                if self._list_fixtures(driver):
                    self._coupon_return_url = coupon
                    return True
            log.info(
                "Bet365: coupon URL for %s not usable (expired or wrong page); "
                "navigating via the sidebar instead.", competition.value,
            )

        # Sidebar navigation (URL-free, robust to expiry).
        self._coupon_return_url = None
        if not self._page_has_content(driver):
            self._bootstrap(driver)
        pattern = COMPETITION_LINK_PATTERNS[competition]
        if not driver.execute_script(_TEXT_CANDIDATES_JS, pattern):
            self._quick_click_text(driver, r"^(voetbal|football)$")  # reveal menu
            time.sleep(2)

        if self._click_text(
            driver, pattern, lambda: self._on_competition_coupon(driver, competition)
        ):
            self._settle_coupon(driver)
            return True

        # Save where we ended up so the navigation can be diagnosed.
        shot = f"bet365_nav_{competition.value}.png"
        try:
            driver.save_screenshot(shot)
        except Exception:
            shot = "nothing"
        try:
            head = driver.execute_script(
                "return (document.body ? document.body.innerText : '').slice(0, 200)"
            )
        except Exception:
            head = ""
        log.warning(
            "Bet365: could not reach %s coupon (dead page=%s). Saved %s — send "
            "it over. Page starts with: %r",
            competition.value, self._is_dead_page(driver), shot, head,
        )
        return False

    def _on_competition_coupon(self, driver, competition: Competition) -> bool:
        """True once we've reached this competition's content.

        Accepts either the coupon's own header naming the competition (e.g.
        "WK voetbal 2026 …" — a coupon-specific container, NOT a broad one
        that also wraps the sidebar's "WK 2026" nav item) OR a page that
        already shows shots markets (navigation can land straight on a match).
        """
        if self._is_dead_page(driver):
            return False
        try:
            header = driver.execute_script(
                "for (const s of ['[class*=\"CouponPageReactResponsive\"]',"
                "                  '[class*=\"wc-CouponPage\"]',"
                "                  '[class*=\"CouponPage\"][class*=\"Main\"]']) {"
                "  const el = document.querySelector(s);"
                "  if (el) return (el.innerText || '').slice(0, 80).toLowerCase();"
                "}"
                "return null;"
            )
        except Exception:
            header = None
        if header and any(k in header for k in COMPETITION_COUPON_KEYWORDS[competition]):
            return True
        return self._has_shots(driver)

    def _has_shots(self, driver) -> bool:
        """True when the current page renders any player-shots market grid."""
        try:
            return bool(driver.execute_script(_SHOTS_PROPS_JS))
        except Exception:
            return False

    def _quick_click_text(self, driver, pattern: str) -> bool:
        """One-shot: click the first element matching ``pattern`` (no polling)."""
        try:
            candidates = driver.execute_script(_TEXT_CANDIDATES_JS, pattern)
        except Exception:
            return False
        for element in candidates:
            if self._click(driver, element):
                return True
        return False

    def _click_text(self, driver, pattern: str, done) -> bool:
        """Click the element whose visible text matches ``pattern``.

        Tries each candidate and climbs to clickable ancestors (bet365 often
        puts the click handler above the text span), polling ``done`` after
        each click. Returns True as soon as ``done()`` holds. Fully tolerant
        of stale elements — clicking often re-renders the page and invalidates
        references mid-loop.
        """
        deadline = time.time() + self.page_timeout
        while time.time() < deadline:
            try:
                candidates = driver.execute_script(_TEXT_CANDIDATES_JS, pattern)
            except Exception:
                candidates = []
            for element in candidates:
                target = element
                for _ in range(4):
                    if not self._click(driver, target):
                        break  # element went stale
                    time.sleep(1.5)
                    try:
                        if done():
                            return True
                    except Exception:
                        pass
                    try:
                        target = driver.execute_script(
                            "return arguments[0].parentElement", target
                        )
                    except StaleElementReferenceException:
                        break
                    if target is None:
                        break
            time.sleep(1)
        return False

    def _report_empty_coupon(self, driver, competition: Competition) -> None:
        """Save a screenshot + HTML and log the DOM classes for diagnosis."""
        from pathlib import Path

        prefix = f"bet365_coupon_{competition.value}"
        try:
            driver.execute_script("window.scrollBy(0, 1200);")
            time.sleep(2)
        except Exception:
            pass
        saved = []
        try:
            driver.save_screenshot(f"{prefix}.png")
            saved.append(f"{prefix}.png")
        except Exception:
            pass
        try:
            Path(f"{prefix}.html").write_text(driver.page_source, encoding="utf-8")
            saved.append(f"{prefix}.html")
        except Exception:
            pass
        try:
            diag = driver.execute_script(_FIXTURE_DIAG_JS)
            classes, samples = diag.get("classes"), diag.get("samples")
        except Exception:
            classes, samples = None, None
        try:
            body_len = driver.execute_script(
                "return document.body ? document.body.innerText.length : 0"
            )
        except Exception:
            body_len = "?"
        log.warning(
            "Bet365: reached the %s page but found 0 fixtures (page text "
            "length=%s). Saved %s — open the .png to see the page state and "
            "send it over. Fixture-ish classes: %s\n  sample texts: %s",
            competition.value, body_len, saved or "nothing",
            classes, samples,
        )

    def _settle_coupon(self, driver) -> None:
        """Wait for and scroll the coupon so its fixtures lazily render."""
        self._wait_for(
            driver,
            '.wc-CouponPageReactResponsive_PageViewMain, [class*="CouponPage"], '
            '.gl-MarketGroupPod, [class*="ParticipantFixtureDetails"]',
        )
        # Make sure the "Wedstrijden"/"Matches" (fixtures) view is selected.
        self._quick_click_text(driver, r"^(wedstrijden|matches|fixtures)$")
        deadline = time.time() + self.page_timeout
        while time.time() < deadline:
            try:
                if self._list_fixtures(driver):
                    return
                # bet365 virtualises long lists; nudge the window and any inner
                # scroll containers so more fixtures render.
                driver.execute_script(
                    "window.scrollBy(0, 700);"
                    "document.querySelectorAll("
                    "  '[class*=\"PageViewMain\"], [class*=\"Scroller\"], "
                    "   [class*=\"CouponPage\"], [class*=\"Wrapper\"]'"
                    ").forEach(e => { e.scrollTop += 700; });"
                )
            except StaleElementReferenceException:
                pass
            time.sleep(1)

    def _list_fixtures(self, driver) -> list[tuple[object, str]]:
        rows = driver.execute_script(_FIXTURE_ROWS_JS)
        return [(el, name) for el, name in rows]

    def _open_fixture(self, driver, name: str) -> bool:
        """Click the fixture row with this name and wait for the match page.

        Re-queries the row fresh (elements from a prior render go stale after
        navigation) and climbs to a clickable ancestor if the label itself
        doesn't handle the click.
        """
        try:
            row = next(
                (el for el, row_name in self._list_fixtures(driver) if row_name == name),
                None,
            )
        except StaleElementReferenceException:
            row = None
        if row is None:
            return False

        target = row
        for _ in range(4):
            if not self._click(driver, target):
                return False  # went stale
            if self._wait_for(
                driver, ".sph-MarketGroupNavBarButton, .gl-MarketGroupPod", timeout=6
            ):
                return True
            try:
                target = driver.execute_script(
                    "return arguments[0].parentElement", target
                )
            except StaleElementReferenceException:
                return False
            if target is None:
                break
        return False

    def _return_to_list(self, driver) -> None:
        """Return to the coupon between matches."""
        if self._coupon_return_url:
            driver.get(self._coupon_return_url)  # plain load, reliable
            self._settle_coupon(driver)
        else:
            try:
                driver.back()  # in-app history restores the coupon
            except Exception:
                pass
            if not self._wait_until(lambda: self._list_fixtures(driver)):
                # History didn't restore it; re-navigate from the sidebar.
                if self._current_competition is not None:
                    self._open_competition_page(driver, self._current_competition)

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

    def _page_has_content(self, driver) -> bool:
        """True when the app has booted to a real page (not blank/dead)."""
        try:
            text = driver.execute_script(
                "return document.body ? document.body.innerText : ''"
            )
        except Exception:
            return False
        return len(text.strip()) > 200 and not self._is_dead_page(driver)

    def _bootstrap(self, driver) -> None:
        """Load the site root (no hash) and let the SPA fully initialise."""
        try:
            driver.get(self.base_url + "/")
            self._wait_for(driver, "body")
            self._dismiss_cookies(driver)
            self._wait_until(lambda: self._page_has_content(driver))
            time.sleep(2)
        except Exception as exc:  # pragma: no cover - best effort
            log.debug("Bet365: bootstrap load failed: %s", exc)

    def _open_match(self, driver, url: str) -> None:
        """Navigate to an explicit match URL and wait for its market bar.

        Uses a single driver.get() (which loads the full document at that
        hash route) — no driver.refresh(), since a hard refresh makes bet365
        report the hash route as an expired page.
        """
        for attempt in range(3):
            driver.get(url)
            if self._wait_for(driver, ".sph-MarketGroupNavBarButton, .gl-MarketGroupPod"):
                return
            if self._is_dead_page(driver):
                return  # caller detects and reports the dead page
            log.info(
                "Bet365: match page markets not ready (attempt %d/3), retrying",
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
        """Accept the cookie banner — bet365 won't render content until then.

        The banner can appear a beat after load and has no stable class, so
        poll for the accept button (matched by visible text) for a few
        seconds and confirm the banner is actually gone.
        """
        accept_re = (
            r"^(alles accepteren|accepteren|accepteer alles|accept all|"
            r"accept|ik ga akkoord|akkoord|allow all)$"
        )
        deadline = time.time() + 12
        while time.time() < deadline:
            if not self._cookie_banner_present(driver):
                return
            self._click_all_accept(driver, accept_re)
            time.sleep(1)
            if not self._cookie_banner_present(driver):
                return
            time.sleep(0.5)
        log.warning(
            "Bet365: could not dismiss the cookie banner automatically. Run "
            "once with BET365_HEADLESS=0 and click 'Alles accepteren' yourself "
            "— the persistent profile (%s) will remember it for next time.",
            self.user_data_dir or "<disabled>",
        )

    def _click_all_accept(self, driver, accept_re: str) -> None:
        """Click every accept-cookies control in the top doc and each iframe."""
        from selenium.webdriver.common.by import By

        try:
            for el in driver.execute_script(_TEXT_CANDIDATES_JS, accept_re):
                self._click(driver, el)
            for button in self._query(driver, SELECTORS["cookie_accept"]):
                self._click(driver, button)
        except Exception:
            pass
        for frame in driver.find_elements(By.TAG_NAME, "iframe"):
            try:
                driver.switch_to.frame(frame)
                for el in driver.execute_script(_TEXT_CANDIDATES_JS, accept_re):
                    self._click(driver, el)
            except Exception:
                pass
            finally:
                driver.switch_to.default_content()

    def _cookie_banner_present(self, driver) -> bool:
        try:
            text = driver.execute_script(
                "return document.body ? document.body.innerText.toLowerCase() : ''"
            )
        except Exception:
            return False
        return "gebruik van cookies" in text or "use of cookies" in text

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
