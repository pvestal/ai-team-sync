"""#2762: the suite's worker registry is CI's, whatever the workstation has in ~/.config.

Before: ~/.config/ai-team-sync/workers.toml (the deliberate echo-executor class)
extended the registry under test, so test_codex_close_authority's registry
assertion was permanently red on the developer box and green in CI. The fix is
in the harness (tests/conftest.py), not in the test's expectation and not in the
operator's file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ai_team_sync import workers
from ai_team_sync.workers import _BUILTINS, registry

SRC = Path(__file__).resolve().parents[1] / "src"


def _home_with_extra_worker(tmp_path: Path) -> Path:
    cfg = tmp_path / ".config" / "ai-team-sync"
    cfg.mkdir(parents=True)
    (cfg / "workers.toml").write_text(
        "[workers.box-only]\ncapabilities = ['repo_read']\n"
        "[workers.box-only.authority]\nedit = 'none'\n")
    return tmp_path


def test_the_registry_under_test_is_exactly_the_builtins():
    registry.cache_clear()
    try:
        assert set(registry().names()) == set(_BUILTINS)
        assert not registry().config_error
    finally:
        registry.cache_clear()


def test_a_workstation_registry_file_does_not_leak_into_the_suite(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(_home_with_extra_worker(tmp_path)))
    registry.cache_clear()
    try:
        assert "box-only" not in registry().names()
        assert set(registry().names()) == set(_BUILTINS)
    finally:
        registry.cache_clear()


def test_a_spawned_process_inherits_the_ci_registry(tmp_path):
    """ats-server and hook subprocesses read the registry themselves."""
    env = {**os.environ, "HOME": str(_home_with_extra_worker(tmp_path)),
           "PYTHONPATH": str(SRC)}
    out = subprocess.run(
        [sys.executable, "-c",
         "from ai_team_sync.workers import registry; print(sorted(registry().names()))"],
        env=env, capture_output=True, text=True, check=True).stdout
    assert "box-only" not in out
    assert out.strip() == str(sorted(_BUILTINS))


def test_a_test_that_names_its_own_registry_file_still_gets_it(tmp_path, monkeypatch):
    cfg = tmp_path / "workers.toml"
    cfg.write_text("[workers.bench]\ncapabilities = ['repo_read']\n")
    monkeypatch.setenv("ATS_WORKERS_CONFIG", str(cfg))
    registry.cache_clear()
    try:
        assert "bench" in registry().names()
    finally:
        registry.cache_clear()
    assert workers._config_path() == cfg
