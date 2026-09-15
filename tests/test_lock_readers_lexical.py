"""#2761: lock readers compare lexically, per docs/lock-readers.md.

Reproduced on deployed afc0fcd: an exclusive 'src/**' lock anchored to repo A,
and POST /api/locks/check with the ABSOLUTE path /srv/.../a/src/x.py returned
locked=false (with or without repo_root); pre-commit-check returned
can_proceed=true. whos_editing sends the absolute, rootless shape.

The pure truth-table tests pin every row of the contract. The REST tests are the
operator's red/green set: rows marked RED fail on afc0fcd.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import time

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ai_team_sync.mcp.server as mcp
from ai_team_sync.database import get_db
from ai_team_sync import scope_paths as sp
from ai_team_sync.server import create_app

A, B, AB = "/srv/a", "/srv/b", "/srv/ab"


def covers(pattern, lock_root, path, caller_root=""):
    return sp.reader_covers(sp.reader_query(path, caller_root), sp.reader_lock(pattern, lock_root))


# ── the truth table (docs/lock-readers.md), row by row ─────────────────────

@pytest.mark.parametrize("row,pattern,lock_root,path,caller_root,expected", [
    ("1 abs same repo, rooted", "src/**", A, "/srv/a/src/x.py", A, True),
    ("1 abs same repo, rootless", "src/**", A, "/srv/a/src/x.py", "", True),
    ("2 rel same repo", "src/**", A, "src/x.py", A, True),
    ("3 abs other repo", "src/**", A, "/srv/b/src/x.py", "", False),
    ("3b rel other repo", "src/**", A, "src/x.py", B, False),
    ("5 unanchored, rooted rel", "src/**", "", "src/x.py", B, True),
    ("5b unanchored, abs rootless", "src/**", "", "/srv/b/src/x.py", "", True),
    ("5b unanchored, abs rooted elsewhere", "src/**", "", "/srv/b/src/x.py", A, True),
    ("6 unanchored, rootless rel", "src/**", "", "src/x.py", "", True),
    ("7 anchored, rootless rel (legacy)", "src/**", A, "src/x.py", "", True),
    ("8b abs path, mismatched caller root", "src/**", A, "/srv/a/src/x.py", B, True),
    ("9a *.py does not cover .md", "src/*.py", A, "src/readme.md", A, False),
    ("9a *.py covers .py", "src/*.py", A, "src/x.py", A, True),
    ("9b *.py does not cover docs/a.md", "*.py", A, "docs/a.md", A, False),
    ("9c **/*.md does not cover .py", "**/*.md", A, "src/x.py", A, False),
    ("9d class match", "src/[ab].py", A, "src/a.py", A, True),
    ("9d class miss", "src/[ab].py", A, "src/c.py", A, False),
    ("9e * crosses / as before", "src/*", A, "src/sub/deep.txt", A, True),
    ("10c non-absolute stored root is raw", "src/**", "ra", "src/x.py", "/srv/rb", False),
    ("11 no repo-prefix collision", "**", A, "/srv/ab/x.py", "", False),
    ("11 no repo-prefix collision, rooted", "**", A, "x.py", AB, False),
    ("12 escaping .. is never re-rooted", "src/**", B, "../b/src/x.py", A, False),
    ("12b absolute .. is normalised", "src/**", B, "/srv/a/../b/src/x.py", "", True),
    ("13 root spelling", "src/**", A, "src/x.py", "/srv//a/", True),
    ("13 lock root spelling", "src/**", "/srv//a/", "src/x.py", A, True),
    ("14 dot segments in a glob, rooted", "./src//**", A, "./src/x.py", A, True),
    ("14 dot segments in a glob, rootless", "./src//**", A, "./src/x.py", "", True),
    ("14 dot segments in an absolute glob", "/srv/a/./src//*.py", "", "/srv/a/src/x.py", "", True),
    ("15 directory form: src/ under src/**", "src/**", A, "src/", A, True),
    ("15 directory form: src/.", "src/**", A, "src/.", A, True),
    ("15 directory form: src/x/..", "src/**", A, "src/x/..", A, True),
    ("15 directory form, rootless", "src/**", A, "src/", "", True),
    ("15 directory form, absolute", "src/**", A, "/srv/a/src/", "", True),
    ("15 directory form, unanchored legacy", "src/**", "", "src/", B, True),
    ("15 directory form, other repository", "src/**", A, "src/", B, False),
    ("15 directory form, other repository absolute", "src/**", A, "/srv/b/src/", "", False),
    ("16 src is not src/", "src/**", A, "src", A, False),
    ("16 src/ is not the file src", "src", A, "src/", A, False),
    ("16 src/* covers src/", "src/*", A, "src/", A, True),
    ("17 directory-form pattern", "src/", A, "/srv/a/src/", "", True),
    ("18 old-rule floor: leading space", "* x", A, " x", A, True),
    ("glob chars in a root are literal", "src/**", "/srv/r[12]", "/srv/r1/src/x.py", "", False),
    ("glob chars in a root still match themselves", "src/**", "/srv/r[12]", "/srv/r[12]/src/x.py", "", True),
])
def test_truth_table(row, pattern, lock_root, path, caller_root, expected):
    assert covers(pattern, lock_root, path, caller_root) is expected, row


def test_same_relative_path_in_two_repositories_is_two_namespaces():
    assert covers("src/**", A, "src/x.py", A) is True
    assert covers("src/**", A, "src/x.py", B) is False


@pytest.mark.parametrize("path,caller_root,pattern,lock_root", [
    ("src/\x00x.py", A, "src/**", A),
    ("/srv/a/src/\x00", "", "src/**", A),
    ("src/x.py", "/srv/\x00a", "src/**", A),
    ("src/x.py", A, "src/\x00**", A),
    ("src/x.py", A, "src/**", "/srv/\x00a"),
    ("", A, "src/**", A),
    ("src/x.py", A, "", A),
    ("/" * 5000 + "x", "", "**", A),
])
def test_pathological_input_uses_the_pre_2761_rule_and_never_raises(path, caller_root, pattern, lock_root):
    got = covers(pattern, lock_root, path, caller_root)
    a, b = caller_root.rstrip("/"), lock_root.rstrip("/")
    import fnmatch
    if sp.reader_query(path, caller_root).raw or sp.reader_lock(pattern, lock_root).raw:
        assert got is ((not (a and b and a != b)) and fnmatch.fnmatch(path, pattern))
    else:
        assert isinstance(got, bool)


def test_readers_do_no_filesystem_io(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("filesystem I/O on the reader path")
    for mod, name in ((os.path, "realpath"), (os.path, "exists"), (os.path, "isdir"), (os.path, "isfile"),
                      (os, "stat"), (os, "lstat"), (os, "listdir"), (os, "scandir")):
        monkeypatch.setattr(mod, name, boom)
    for name in ("resolve", "exists", "stat", "is_dir", "is_file"):
        monkeypatch.setattr(pathlib.Path, name, boom)
    assert covers("src/**", A, "/srv/a/src/x.py") is True
    assert covers("src/**", "", "/srv/b/src/x.py", A) is True
    assert covers("src/**", A, "../x", A) is False


# ── REST red/green ─────────────────────────────────────────────────────────

async def _session(client, root, scope, mode="exclusive", developer="owner"):
    body = {"developer": developer, "agent": "default", "scope": scope, "auto_lock": True, "lock_mode": mode}
    if root is not None:
        body["repo_root"] = root
    r = await client.post("/api/sessions", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _check(client, path, repo_root=None):
    body = {"paths": [path]}
    if repo_root is not None:
        body["repo_root"] = repo_root
    r = await client.post("/api/locks/check", json=body)
    assert r.status_code == 200, r.text
    return r.json()[0]


async def _precommit(client, path, repo_root):
    r = await client.post("/api/git/pre-commit-check", json={"staged_files": [path], "repo_root": repo_root})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_1_absolute_path_is_blocked_by_its_own_repositorys_exclusive_lock(client):  # RED on afc0fcd
    owner = await _session(client, A, ["src/**"])
    for repo_root in (A, None):
        hit = await _check(client, "/srv/a/src/x.py", repo_root)
        assert (hit["locked"], hit["mode"], hit["session_id"]) == (True, "exclusive", owner), repo_root


@pytest.mark.asyncio
async def test_2_same_relative_path_in_repo_b_is_not_blocked_by_repo_a(client):
    await _session(client, A, ["src/**"])
    assert (await _check(client, "src/x.py", B))["locked"] is False
    assert (await _check(client, "/srv/b/src/x.py"))["locked"] is False


@pytest.mark.asyncio
async def test_3_precommit_never_clears_the_repo_a_exclusive_conflict(client):  # RED on afc0fcd
    await _session(client, A, ["src/**"])
    for path in ("/srv/a/src/x.py", "src/x.py"):
        r = await _precommit(client, path, A)
        assert r["can_proceed"] is False and len(r["blocking_locks"]) == 1, path


@pytest.mark.asyncio
async def test_4_unanchored_legacy_lock_still_matches_when_a_root_is_supplied(client):  # RED on afc0fcd (absolute)
    legacy = await _session(client, None, ["docs/**"], mode="advisory", developer="legacy")
    for path, repo_root in (("docs/a.md", B), ("/srv/b/docs/a.md", B), ("/srv/b/docs/a.md", None)):
        hit = await _check(client, path, repo_root)
        assert (hit["locked"], hit["session_id"]) == (True, legacy), (path, repo_root)


@pytest.mark.asyncio
async def test_5_nul_and_pathological_input_never_produce_http_500(client):
    await _session(client, A, ["src/**"])
    await _session(client, "/srv/nul", ["src/\x00x/**"], mode="advisory", developer="nul")
    for body in ({"paths": ["/srv/a/src/\x00.py", "src/x.py"], "repo_root": A},
                 {"paths": ["src/x.py"], "repo_root": "/srv/\x00a"},
                 {"paths": ["/srv/a/src/x.py"]}):
        r = await client.post("/api/locks/check", json=body)
        assert r.status_code == 200, (body, r.text)
    r = await client.post("/api/git/pre-commit-check",
                          json={"staged_files": ["/srv/a/src/\x00.py", "/srv/a/src/x.py"], "repo_root": A})
    assert r.status_code == 200 and r.json()["can_proceed"] is False


@pytest.mark.asyncio
async def test_6_glob_patterns_keep_their_matching(client):
    await _session(client, A, ["src/*.py"], developer="glob")
    assert (await _check(client, "src/readme.md", A))["locked"] is False
    assert (await _check(client, "src/x.py", A))["locked"] is True
    assert (await _check(client, "/srv/a/src/x.py"))["locked"] is True
    assert (await _precommit(client, "src/readme.md", A))["can_proceed"] is True


@pytest.mark.asyncio
async def test_7_rootless_relative_callers_stay_conservative(client):
    """Documented limit: no repository identity, so any repository's matching lock is reported."""
    owner = await _session(client, A, ["src/**"])
    hit = await _check(client, "src/x.py")
    assert (hit["locked"], hit["session_id"]) == (True, owner)
    r = await client.post("/api/git/pre-commit-check", json={"staged_files": ["src/x.py"]})
    assert r.status_code == 200 and r.json()["can_proceed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("exclusive_first", [True, False])
async def test_8_advisory_exclusive_semantics_and_ordering(client, exclusive_first):
    # The same repository spelled twice is not a conflict to session creation, so both locks exist.
    specs = [("/srv/ord", "exclusive", "x"), ("/srv//ord/", "advisory", "a")]
    if not exclusive_first:
        specs.reverse()
    ids = {mode: await _session(client, root, ["src/**"], mode=mode, developer=dev) for root, mode, dev in specs}
    for repo_root in ("/srv/ord", "/srv//ord/"):
        hit = await _check(client, "src/x.py", repo_root)
        assert (hit["mode"], hit["session_id"]) == ("exclusive", ids["exclusive"]), repo_root
    r = await _precommit(client, "src/x.py", "/srv/ord")
    assert (r["can_proceed"], len(r["blocking_locks"]), len(r["advisory_locks"])) == (False, 1, 1)


@pytest.mark.asyncio
async def test_8b_an_advisory_lock_warns_and_does_not_block(client):
    await _session(client, A, ["lib/**"], mode="advisory")
    hit = await _check(client, "/srv/a/lib/y.py")
    assert (hit["locked"], hit["mode"]) == (True, "advisory")
    r = await _precommit(client, "lib/y.py", A)
    assert r["can_proceed"] is True and len(r["advisory_locks"]) == 1


@pytest.mark.asyncio
async def test_10_directory_form_queries_keep_their_coverage(client):  # RED on #2761 attempt 2
    owner = await _session(client, A, ["src/**"])
    legacy = await _session(client, None, ["docs/**"], mode="advisory", developer="legacy")
    for path, repo_root in (("src/", A), ("src/.", A), ("src/", None), ("/srv/a/src/", None)):
        hit = await _check(client, path, repo_root)
        assert (hit["locked"], hit["mode"], hit["session_id"]) == (True, "exclusive", owner), (path, repo_root)
    for path, repo_root in (("docs/", B), ("docs/", None), ("/srv/b/docs/", None)):
        hit = await _check(client, path, repo_root)
        assert (hit["locked"], hit["session_id"]) == (True, legacy), (path, repo_root)
    assert (await _check(client, "src/", B))["locked"] is False
    assert (await _check(client, "src", A))["locked"] is False
    r = await _precommit(client, "src/", A)
    assert r["can_proceed"] is False and len(r["blocking_locks"]) == 1


@pytest.mark.asyncio
async def test_10b_rootless_cli_lock_check_on_a_directory_stays_blocked(client, monkeypatch):
    from click.testing import CliRunner

    import ai_team_sync.cli as cli

    await _session(client, A, ["src/**"])
    payload = (await client.post("/api/locks/check", json={"paths": ["src/"]})).json()
    sent = []

    class _Resp:
        def json(self):
            return payload

    def fake_api(method, path, **kwargs):
        sent.append((method, path, kwargs.get("json")))
        return _Resp()

    monkeypatch.setattr(cli, "_api", fake_api)
    result = CliRunner().invoke(cli.cli, ["lock", "check", "src/"])
    assert sent == [("post", "/locks/check", {"paths": ["src/"]})]
    assert result.exit_code == 1 and "[BLOCKED] src/" in result.output, result.output


@pytest.mark.asyncio
async def test_9_5000_paths_by_50_locks_stays_well_under_the_hook_timeout(client):
    for r in range(9):
        await _session(client, f"/srv/perf{r}", [f"pkg{r}/mod{j}/**" for j in range(5)], mode="advisory",
                       developer=f"d{r}")
    await _session(client, None, [f"legacy{j}/*.md" for j in range(5)], mode="advisory", developer="legacy")
    rel = [f"pkg0/mod{i % 5}/file{i}.py" for i in range(5000)]
    timings = {}
    for label, url, body in (
            ("check rel rooted", "/api/locks/check", {"paths": rel, "repo_root": "/srv/perf0"}),
            ("check abs", "/api/locks/check", {"paths": [f"/srv/perf0/{p}" for p in rel]}),
            ("precommit rel rooted", "/api/git/pre-commit-check", {"staged_files": rel, "repo_root": "/srv/perf0"})):
        t = time.perf_counter()
        resp = await client.post(url, json=body)
        timings[label] = time.perf_counter() - t
        assert resp.status_code == 200
    print("reader timings (s):", {k: round(v, 2) for k, v in timings.items()})
    assert max(timings.values()) < 3.0, timings


# ── MCP whos_editing ──────────────────────────────────────────────────────

def _wire(monkeypatch, db_engine):
    app = create_app()
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)

    class _ASGIClient:
        def __init__(self, *a, **k):
            self._c = AsyncClient(transport=transport, base_url="http://localhost:8400")

        async def __aenter__(self):
            return self._c

        async def __aexit__(self, *exc):
            await self._c.aclose()

    monkeypatch.setattr(mcp.httpx, "AsyncClient", _ASGIClient)
    return transport


@pytest.mark.asyncio
async def test_whos_editing_is_repository_correct_for_absolute_and_rooted_paths(db_engine, monkeypatch):
    transport = _wire(monkeypatch, db_engine)
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        foreign = await _session(c, "/srv/c", ["**"], mode="advisory", developer="elsewhere")
        owner = await _session(c, A, ["src/**"])
    here = (await mcp.call_tool("whos_editing", {"paths": ["/srv/b/src/x.py"]}))[0].text
    assert foreign not in here and "Clear to go" in here
    theirs = (await mcp.call_tool("whos_editing", {"paths": ["/srv/a/src/x.py"]}))[0].text
    assert owner in theirs and "exclusive" in theirs
    rooted = (await mcp.call_tool("whos_editing", {"paths": ["src/x.py"], "repo_root": B}))[0].text
    assert "Clear to go" in rooted
    directory = (await mcp.call_tool("whos_editing", {"paths": ["src/"], "repo_root": A}))[0].text
    assert owner in directory
    elsewhere = (await mcp.call_tool("whos_editing", {"paths": ["src/"], "repo_root": B}))[0].text
    assert owner not in elsewhere


def test_whos_editing_declares_repo_root():
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert "repo_root" in tools["whos_editing"].inputSchema["properties"]
