"""CLI entry point: scan for arbitrage and alert Discord.

Usage:
    python -m arb_tool            # scan repeatedly (POLL_INTERVAL_SECONDS)
    python -m arb_tool --once     # single scan, then exit
"""

from __future__ import annotations

import argparse
import logging
import time

from .arbitrage import find_arbitrages
from .config import Config
from .discord_notifier import DiscordNotifier, format_text
from .matching import match_props
from .models import ArbitrageOpportunity
from .providers import build_providers

log = logging.getLogger("arb_tool")


def scan_once(config: Config, notifier: DiscordNotifier, seen: set) -> list[ArbitrageOpportunity]:
    """Fetch odds from both books, detect arbs and alert new ones."""
    bet365, unibet = build_providers(config)

    bet365_props = bet365.fetch_props(config.competitions)
    unibet_props = unibet.fetch_props(config.competitions)
    log.info(
        "Fetched %d props from %s, %d from %s",
        len(bet365_props), bet365.name, len(unibet_props), unibet.name,
    )

    pairs = match_props(bet365_props, unibet_props)
    log.info("Matched %d player/line pairs across both books", len(pairs))
    if not pairs and bet365_props and unibet_props:
        from .matching import diagnose_no_match

        log.warning(
            "Bet365 %s\n%s", bet365.name,
            diagnose_no_match(bet365_props, unibet_props, bet365.name, unibet.name),
        )

    arbs = find_arbitrages(pairs, config.total_stake, config.min_profit_pct)
    new_arbs = [arb for arb in arbs if arb.key() not in seen]
    log.info("Found %d arbitrage(s), %d new", len(arbs), len(new_arbs))

    for arb in new_arbs:
        print(format_text(arb))
        notifier.send(arb)
        seen.add(arb.key())
    return new_arbs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="arb_tool",
        description="Find player shots arbitrage between Bet365 and Unibet NL "
        "and alert a Discord channel.",
    )
    parser.add_argument("--once", action="store_true", help="run a single scan and exit")
    parser.add_argument(
        "--find-competition",
        metavar="QUERY",
        help="search Unibet/Kambi's football competition tree by name "
        "(e.g. 'world', 'wk') and print the matching paths, then exit",
    )
    parser.add_argument(
        "--dump-bet365",
        nargs="?",
        const="",
        metavar="URL",
        help="open bet365 (football section, or URL if given) with the "
        "Selenium provider and print selector hit counts, CSS classes and "
        "link texts; saves bet365_dump.html/.png for diagnosing DOM changes",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="with --dump-bet365: pause after loading so you can navigate the "
        "browser to a match page, then press Enter to dump it (use with "
        "BET365_HEADLESS=0)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = Config.from_env()

    if args.dump_bet365 is not None:
        from .providers.bet365_selenium import SeleniumBet365Provider

        SeleniumBet365Provider().dump_page(args.dump_bet365 or None, wait=args.wait)
        return 0

    if args.find_competition:
        from .providers.kambi import KambiUnibetProvider

        matches = KambiUnibetProvider().search_groups(args.find_competition)
        if not matches:
            print(f"No football competitions matching {args.find_competition!r}")
            return 1
        width = max(len(path) for path, _ in matches)
        for path, name in matches:
            print(f"{path:<{width}}  {name}")
        print(
            "\nUse a path via KAMBI_PATH_PREMIER_LEAGUE / KAMBI_PATH_LA_LIGA / "
            "KAMBI_PATH_WORLD_CUP in .env"
        )
        return 0

    notifier = DiscordNotifier(config.discord_webhook_url)
    seen: set = set()

    if args.once:
        scan_once(config, notifier, seen)
        return 0

    log.info("Scanning every %d seconds; Ctrl-C to stop", config.poll_interval_seconds)
    while True:
        try:
            scan_once(config, notifier, seen)
        except Exception:
            log.exception("Scan failed; retrying next interval")
        try:
            time.sleep(config.poll_interval_seconds)
        except KeyboardInterrupt:
            log.info("Stopped")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
