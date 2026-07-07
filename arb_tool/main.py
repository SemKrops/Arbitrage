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
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = Config.from_env()
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
