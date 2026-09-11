"""Configuration defaults that protect local-only ATS deployments."""

from ai_team_sync.config import Settings


def test_ats_host_defaults_to_localhost(monkeypatch):
    monkeypatch.delenv("ATS_HOST", raising=False)

    assert Settings().ats_host == "127.0.0.1"


def test_ats_host_can_be_overridden_for_trusted_network(monkeypatch):
    monkeypatch.setenv("ATS_HOST", "0.0.0.0")

    assert Settings().ats_host == "0.0.0.0"
