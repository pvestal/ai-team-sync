"""Shared operator governance configuration for prompt and lock resolution."""

from __future__ import annotations

import pytest

from ai_team_sync.context_resolution import governed_roots, resolve_request_target
from ai_team_sync.operator_config import OperatorConfigError


def _operator_config(tmp_path, body: str):
    config = tmp_path / "operator.toml"
    config.write_text(body)
    return config


def test_shared_operator_config_resolves_project_without_client_env(tmp_path, monkeypatch):
    config = _operator_config(
        tmp_path,
        '[governance]\nrepositories = ["/opt/anime-studio", "/opt/tower-echo-brain"]\n',
    )
    monkeypatch.setenv("ATS_OPERATOR_CONFIG", str(config))
    monkeypatch.delenv("ATS_COORDINATED_REPOS", raising=False)

    target = resolve_request_target(
        "Give me the Anime Studio status.", cwd="/home/patrick/Documents"
    )

    assert governed_roots() == ["/opt/anime-studio", "/opt/tower-echo-brain"]
    assert target is not None
    assert target.repo_root == "/opt/anime-studio"
    assert target.reason == "project_name"


def test_legacy_env_is_an_explicit_compatibility_override(tmp_path, monkeypatch):
    config = _operator_config(tmp_path, '[governance]\nrepositories = ["/operator/repo"]\n')
    monkeypatch.setenv("ATS_OPERATOR_CONFIG", str(config))
    monkeypatch.setenv("ATS_COORDINATED_REPOS", "/override/repo")

    assert governed_roots() == ["/override/repo"]


@pytest.mark.parametrize(
    "body",
    [
        "not valid [[toml",
        '[governance]\nrepositories = ["relative/path"]\n',
        '[unknown]\nrepositories = ["/opt/repo"]\n',
    ],
)
def test_malformed_operator_config_is_rejected(tmp_path, monkeypatch, body):
    config = _operator_config(tmp_path, body)
    monkeypatch.setenv("ATS_OPERATOR_CONFIG", str(config))
    monkeypatch.delenv("ATS_COORDINATED_REPOS", raising=False)

    with pytest.raises(OperatorConfigError):
        governed_roots()
