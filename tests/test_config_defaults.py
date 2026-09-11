"""Configuration defaults that protect local-only ATS deployments."""

from pathlib import Path

from ai_team_sync.config import Settings


def test_ats_host_defaults_to_localhost(monkeypatch):
    monkeypatch.delenv("ATS_HOST", raising=False)

    assert Settings().ats_host == "127.0.0.1"


def test_ats_host_can_be_overridden_for_trusted_network(monkeypatch):
    monkeypatch.setenv("ATS_HOST", "0.0.0.0")

    assert Settings().ats_host == "0.0.0.0"


def test_shipped_unit_pins_an_absolute_database_url_and_loopback_bind():
    """The unit is the deploy SSOT for two settings that fail SILENTLY.

    `database_url` is relative, so it resolves against the process CWD: a unit
    with a different WorkingDirectory opens a different database and every
    session, lock and decision vanishes without an error. And the write API is
    unauthenticated, so the bind must stay on loopback.
    """
    unit = (Path(__file__).resolve().parents[1] / "deploy" / "ats-server.service").read_text()
    directives = [ln.strip() for ln in unit.splitlines() if not ln.strip().startswith("#")]

    db = [d for d in directives if d.startswith("Environment=DATABASE_URL=")]
    assert db, "unit must pin DATABASE_URL"
    assert db[0].split("=", 2)[2].startswith("sqlite+aiosqlite:////"), \
        "DATABASE_URL must be ABSOLUTE (four slashes) — a relative URL follows CWD"

    assert "Environment=ATS_HOST=127.0.0.1" in directives
    # A user unit, not a system one: two servers would fight over 8400.
    assert "WantedBy=default.target" in directives
