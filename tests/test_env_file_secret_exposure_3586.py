"""#3586: ATS settings never read the caller's CWD .env and never echo input values.

Two JWT_SECRET exposures (2026-10-12, 2026-10-15) came from running `ats` with
CWD inside another repo: pydantic-settings loaded that repo's .env, rejected the
keys ATS does not declare, and printed their values in the ValidationError.

Every test runs the real CLI entrypoint in a subprocess, because the leak is in
what reaches stdout/stderr. Values are random sentinels, never real secrets.
"""

import os
import secrets
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _sentinel() -> str:
    return "SENTINEL" + secrets.token_hex(28)


def _run_ats(cwd: Path, home: Path, extra_env: dict[str, str] | None = None):
    """Run `ats team` from `cwd`, a git repo whose team config points the CLI at
    a closed loopback port. `team` resolves the server URL through the config
    module, which builds Settings (the incident path); the HTTP call then fails
    to connect, so the test never reaches a live ATS server."""
    cwd.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(cwd)], check=True)
    (cwd / ".ai-team-sync.toml").write_text('[server]\nurl = "http://127.0.0.1:1"\n')
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ATS_", "CLAUDE")) and k not in ("DATABASE_URL", "PYTHONPATH")
    }
    env.update({"HOME": str(home), "PYTHONPATH": SRC})
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "ai_team_sync.cli", "team"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _assert_settings_loaded(proc):
    """Settings built cleanly: the run got as far as the (refused) connection."""
    assert "ValidationError" not in proc.stderr, proc.stderr[-2000:]
    assert "ConnectError" in proc.stderr, proc.stderr[-2000:]


def _assert_settings_refused(proc):
    assert proc.returncode != 0
    assert "ValidationError" in proc.stderr, proc.stderr[-2000:]


def _assert_not_echoed(proc, sentinel: str):
    assert sentinel not in proc.stdout, "sentinel leaked to stdout"
    assert sentinel not in proc.stderr, "sentinel leaked to stderr"
    # pydantic truncates long reprs to head...tail; neither half may appear either.
    for part in (sentinel[:24], sentinel[-23:]):
        assert part not in proc.stdout + proc.stderr, "sentinel fragment leaked"


def test_cwd_dotenv_with_undeclared_key_is_not_read(tmp_path):
    sentinel = _sentinel()
    cwd = tmp_path / "other_repo"
    cwd.mkdir()
    (cwd / ".env").write_text(f"JWT_SECRET={sentinel}\n")

    proc = _run_ats(cwd, tmp_path / "home")

    _assert_settings_loaded(proc)
    _assert_not_echoed(proc, sentinel)


def test_cwd_dotenv_with_invalid_declared_key_is_not_read(tmp_path):
    sentinel = _sentinel()
    cwd = tmp_path / "other_repo"
    cwd.mkdir()
    (cwd / ".env").write_text(f"ATS_PORT={sentinel}\n")

    proc = _run_ats(cwd, tmp_path / "home")

    _assert_settings_loaded(proc)
    _assert_not_echoed(proc, sentinel)


def test_ats_env_file_undeclared_key_is_ignored_not_echoed(tmp_path):
    sentinel = _sentinel()
    env_file = tmp_path / "ats.env"
    env_file.write_text(f"JWT_SECRET={sentinel}\n")

    proc = _run_ats(tmp_path / "repo", tmp_path / "home", {"ATS_ENV_FILE": str(env_file)})

    _assert_settings_loaded(proc)
    _assert_not_echoed(proc, sentinel)


def test_invalid_declared_value_in_ats_env_file_is_not_echoed(tmp_path):
    sentinel = _sentinel()
    env_file = tmp_path / "ats.env"
    env_file.write_text(f"ATS_PORT={sentinel}\n")

    proc = _run_ats(tmp_path / "repo", tmp_path / "home", {"ATS_ENV_FILE": str(env_file)})

    _assert_settings_refused(proc)
    assert "ats_port" in proc.stderr.lower()
    _assert_not_echoed(proc, sentinel)


def test_invalid_declared_value_in_process_env_is_not_echoed(tmp_path):
    sentinel = _sentinel()

    proc = _run_ats(tmp_path / "repo", tmp_path / "home", {"ATS_PORT": sentinel})

    _assert_settings_refused(proc)
    _assert_not_echoed(proc, sentinel)


def test_default_ats_owned_env_file_is_honored(tmp_path):
    home = tmp_path / "home"
    cfg = home / ".config" / "ai-team-sync"
    cfg.mkdir(parents=True)
    (cfg / "ats.env").write_text("ATS_PORT=8511\n")
    probe = "from ai_team_sync.config import settings; print(settings.ats_port)"
    env = {k: v for k, v in os.environ.items() if not k.startswith("ATS_")}
    env.update({"HOME": str(home), "PYTHONPATH": SRC})

    proc = subprocess.run(
        [sys.executable, "-c", probe], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=60,
    )

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip() == "8511"


def test_relative_ats_env_file_is_not_read_from_cwd(tmp_path):
    """Review r1 item 1: ATS_ENV_FILE=.env would resolve against the CWD again."""
    sentinel = _sentinel()
    cwd = tmp_path / "other_repo"
    cwd.mkdir()
    (cwd / ".env").write_text(f"ATS_PORT={sentinel}\nJWT_SECRET={sentinel}\n")

    proc = _run_ats(cwd, tmp_path / "home", {"ATS_ENV_FILE": ".env"})

    _assert_settings_loaded(proc)
    _assert_not_echoed(proc, sentinel)


def test_setup_script_never_creates_a_cwd_dotenv():
    """Review r1 item 4: setup.sh wrote a repo-root .env the settings no longer read."""
    script = (Path(__file__).resolve().parents[1] / "setup.sh").read_text()
    code = [ln.split("#", 1)[0] for ln in script.splitlines()]

    assert not any("cp .env.example .env" in ln for ln in code)
    assert any(".config/ai-team-sync/ats.env" in ln for ln in code)
