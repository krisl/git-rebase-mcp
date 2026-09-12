"""Fixtures providing scratch repositories.

Tests that need git take the `scratch` fixture below and run real git
against it. Pure unit tests take no fixture and always run.

Real git is opt-in for speed: plain `pytest` skips fixture-backed tests,
`pytest --real-git` runs the full suite (including CI).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scratch import Scratch

# Identity and hermetic settings for every git process in the session, via
# env passthrough in Git.run. Saves three `git config` procs per repository;
# setdefault so CI can still override.
os.environ.setdefault("GIT_AUTHOR_NAME", "Test")
os.environ.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
os.environ.setdefault("GIT_COMMITTER_NAME", "Test")
os.environ.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")
os.environ.setdefault("GIT_CONFIG_COUNT", "1")
os.environ.setdefault("GIT_CONFIG_KEY_0", "commit.gpgsign")
os.environ.setdefault("GIT_CONFIG_VALUE_0", "false")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--real-git",
        action="store_true",
        default=False,
        help="run tests that shell out to real git (slow); skipped by default",
    )
    parser.addoption(
        "--record",
        action="store_true",
        default=False,
        help="record real git answers to test/cassettes for replay runs",
    )


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip `live_repo`-marked tests unless running them for real.

    These depend on repository state replay cannot reproduce -- worktree
    checkouts on disk, files a rebase deleted -- so they run only under
    `--real-git`, never from cassettes.
    """
    if config.getoption("real_git"):
        return
    skip = pytest.mark.skip(reason="needs a live repo; run with --real-git")
    for item in items:
        if item.get_closest_marker("live_repo") is not None:
            item.add_marker(skip)


@pytest.fixture
def scratch(tmp_path: Path, request: pytest.FixtureRequest) -> Scratch:
    """An initialised repository with an identity, ready to be committed into.

    Real git under `--real-git` (or `--record`, which also writes the
    cassette); a replay of recorded answers otherwise. `git init` itself
    always runs for real: it is one proc, and the working tree stays real.
    """
    from git_rebase_mcp import server
    from replay import CassetteStore

    record = bool(request.config.getoption("record"))
    real = bool(request.config.getoption("real_git"))
    store = None if real else CassetteStore.for_node(request.node.nodeid, record=record)
    if store is not None and not record and not store.exists():
        pytest.skip(f"no cassette recorded; run with --record ({store.path.name})")
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    if store is None:
        return Scratch(tmp_path)
    previous = server._git_factory
    server._git_factory = store.factory
    request.addfinalizer(lambda: setattr(server, "_git_factory", previous))
    request.addfinalizer(store.finalize)
    return Scratch(tmp_path, git=store.factory(tmp_path))
