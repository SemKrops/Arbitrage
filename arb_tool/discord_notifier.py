"""Send arbitrage alerts to a Discord channel via webhook."""

from __future__ import annotations

import logging

import requests

from .models import ArbitrageOpportunity

log = logging.getLogger(__name__)

EMBED_COLOR_GREEN = 0x2ECC71


def build_embed(arb: ArbitrageOpportunity) -> dict:
    leg_over, leg_under = arb.legs
    fields = [
        {"name": "Competition", "value": arb.competition.display_name, "inline": True},
        {"name": "Match", "value": arb.event, "inline": True},
        {"name": "Kick-off", "value": arb.kickoff or "unknown", "inline": True},
        {"name": "Player", "value": arb.player, "inline": True},
        {
            "name": "Market",
            "value": f"{arb.market.display_name} {arb.line:g}",
            "inline": True,
        },
        {
            "name": "Guaranteed profit",
            "value": f"**€{arb.profit:.2f}** ({arb.profit_pct:.2f}%)",
            "inline": True,
        },
        {
            "name": f"Leg 1 — {leg_over.bookmaker}",
            "value": f"{leg_over.side} {arb.line:g} @ **{leg_over.odds:.2f}** — stake €{leg_over.stake:.2f}",
            "inline": False,
        },
        {
            "name": f"Leg 2 — {leg_under.bookmaker}",
            "value": f"{leg_under.side} {arb.line:g} @ **{leg_under.odds:.2f}** — stake €{leg_under.stake:.2f}",
            "inline": False,
        },
    ]
    return {
        "title": "💰 Arbitrage found",
        "description": (
            f"Total stake €{arb.total_stake:.2f} → guaranteed return "
            f"€{arb.guaranteed_return:.2f} whichever side hits."
        ),
        "color": EMBED_COLOR_GREEN,
        "fields": fields,
    }


class DiscordNotifier:
    def __init__(self, webhook_url: str, timeout: float = 10.0):
        self.webhook_url = webhook_url
        self.timeout = timeout

    def send(self, arb: ArbitrageOpportunity) -> bool:
        """Post one arbitrage alert. Returns True when Discord accepted it."""
        if not self.webhook_url:
            log.warning("DISCORD_WEBHOOK_URL not set; alert only shown on console")
            return False
        try:
            response = requests.post(
                self.webhook_url,
                json={"embeds": [build_embed(arb)]},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return True
        except requests.RequestException as exc:
            log.error("Failed to send Discord alert: %s", exc)
            return False


def format_text(arb: ArbitrageOpportunity) -> str:
    """Plain-text rendering of an alert, used for console output."""
    leg_over, leg_under = arb.legs
    return (
        f"ARBITRAGE: {arb.event} ({arb.competition.display_name}) — "
        f"{arb.player}, {arb.market.display_name} {arb.line:g}\n"
        f"  {leg_over.bookmaker}: {leg_over.side} @ {leg_over.odds:.2f}, stake €{leg_over.stake:.2f}\n"
        f"  {leg_under.bookmaker}: {leg_under.side} @ {leg_under.odds:.2f}, stake €{leg_under.stake:.2f}\n"
        f"  Guaranteed profit: €{arb.profit:.2f} ({arb.profit_pct:.2f}%) "
        f"on €{arb.total_stake:.2f} total stake"
    )
