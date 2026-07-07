# Arbitrage — Player Shots (Bet365 × Unibet NL)

Python tool that compares **player shots** and **player shots on target**
odds between **Bet365** and **Unibet Netherlands** for the **Premier League**,
**La Liga** and the **World Cup**. When backing Over at one bookmaker and
Under at the other guarantees a profit (an arbitrage), it splits your stake
so the payout is identical whichever side hits and **sends the bet to a
Discord channel**.

## How it works

1. Two *providers* fetch current player prop odds, one per bookmaker.
2. Events and players are matched across the books with fuzzy name matching
   ("Man City" ↔ "Manchester City", "Salah, Mohamed" ↔ "Mohamed Salah").
   Only identical markets **and identical lines** are compared.
3. For every matched line, both directions are checked:
   `1/odds_over + 1/odds_under < 1` ⇒ guaranteed profit.
4. The configured total stake is split so both outcomes pay the same, and the
   alert (match, player, line, both legs with odds and stakes, guaranteed
   profit in € and %) is posted to your Discord webhook. Each arb is only
   alerted once per run.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in DISCORD_WEBHOOK_URL

python -m arb_tool --once   # one scan
python -m arb_tool          # scan every POLL_INTERVAL_SECONDS
```

Out of the box both providers are set to `mock`, which uses built-in sample
odds containing real arbitrage situations — so you can verify the full
pipeline including the Discord message immediately.

Run the tests with `pytest`.

## Configuration

All settings live in `.env` (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | — | Webhook of the channel to alert |
| `TOTAL_STAKE` | `100` | € split across the two legs |
| `MIN_PROFIT_PCT` | `0.5` | Ignore arbs below this guaranteed % |
| `POLL_INTERVAL_SECONDS` | `300` | Delay between scans |
| `BET365_PROVIDER` | `mock` | `mock` or `file` |
| `UNIBET_PROVIDER` | `mock` | `mock`, `kambi` or `file` |
| `BET365_DATA_FILE` / `UNIBET_DATA_FILE` | — | JSON path or https URL for the `file` provider |

## Odds sources — read this

**Unibet NL** runs on the Kambi sportsbook platform. Setting
`UNIBET_PROVIDER=kambi` fetches live odds from the same JSON API the
unibet.nl site itself uses. The endpoints are unofficial and may change;
base URL, locale and per-competition paths are overridable via `KAMBI_*`
variables (see `.env.example`). If the API shape changes, the provider logs
a warning and returns what it can.

**Bet365 has no public API** and uses aggressive anti-bot protection, so
this project deliberately does not ship a Bet365 scraper — one would be
fragile, quickly IP-banned, and against Bet365's terms of service. Instead,
set `BET365_PROVIDER=file` and point `BET365_DATA_FILE` at a JSON file or
URL in the simple format shown in `data/sample_bet365.json`, fed by whatever
source you are licensed to use (a commercial odds feed such as OddsJam /
OpticOdds / The Odds API, or your own collector). Any source that can emit
that JSON works without touching the code. Alternatively, implement a new
provider: subclass `arb_tool.providers.base.OddsProvider` and register it in
`arb_tool/providers/__init__.py`.

## Example alert

```
💰 Arbitrage found
Competition: Premier League      Match: Liverpool vs Arsenal
Player: Mohamed Salah            Market: Player Shots 2.5
Leg 1 — Bet365: Over 2.5 @ 2.10 — stake €50.59
Leg 2 — Unibet: Under 2.5 @ 2.15 — stake €49.41
Guaranteed profit: €6.23 (6.23%) on €100.00
```

## Disclaimer

For educational use. Odds move fast — always confirm both legs are still
available at the shown prices before betting, respect each bookmaker's terms
of service, and be aware that bookmakers may limit or close accounts that
consistently take arbitrage positions. Gamble responsibly (18+).
