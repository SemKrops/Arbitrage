"""Config / .env loading tests."""

import os

from arb_tool.config import DEFAULT_COMPETITIONS, _parse_competitions, load_dotenv
from arb_tool.models import Competition


def test_parse_competitions():
    assert _parse_competitions("") == DEFAULT_COMPETITIONS
    assert _parse_competitions("world_cup") == (Competition.WORLD_CUP,)
    assert _parse_competitions("premier_league, laliga") == (
        Competition.PREMIER_LEAGUE,
        Competition.LA_LIGA,
    )
    assert _parse_competitions("La Liga") == (Competition.LA_LIGA,)


def test_load_dotenv_handles_utf8_bom(tmp_path, monkeypatch):
    """Windows PowerShell 5.1 writes UTF-8 with a BOM; the loader must cope."""
    env_file = tmp_path / ".env"
    # utf-8-sig prepends the BOM, exactly like PowerShell's Out-File.
    env_file.write_bytes(
        "DISCORD_WEBHOOK_URL=https://example.com/hook\nTOTAL_STAKE=250\n".encode("utf-8-sig")
    )
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TOTAL_STAKE", raising=False)

    load_dotenv(env_file)

    assert os.environ["DISCORD_WEBHOOK_URL"] == "https://example.com/hook"
    assert os.environ["TOTAL_STAKE"] == "250"


def test_load_dotenv_plain_and_comments(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "\n"
        "MIN_PROFIT_PCT=1.5\n"
        'DISCORD_WEBHOOK_URL="https://example.com/quoted"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("MIN_PROFIT_PCT", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    load_dotenv(env_file)

    assert os.environ["MIN_PROFIT_PCT"] == "1.5"
    assert os.environ["DISCORD_WEBHOOK_URL"] == "https://example.com/quoted"


def test_existing_env_wins(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TOTAL_STAKE=999\n", encoding="utf-8")
    monkeypatch.setenv("TOTAL_STAKE", "50")

    load_dotenv(env_file)

    assert os.environ["TOTAL_STAKE"] == "50"
