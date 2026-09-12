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


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip `real_git`-marked tests unless the flag opts in."""
    if config.getoption("real_git"):
        return
    skip = pytest.mark.skip(reason="needs --real-git (pure unit tests run by default)")
    for item in items:
        if item.get_closest_marker("real_git") is not None:
            item.add_marker(skip)


@pytest.fixture
def scratch(tmp_path: Path, request: pytest.FixtureRequest) -> Scratch:
    """An initialised repository with an identity, ready to be committed into."""
    if not request.config.getoption("real_git"):
        pytest.skip("needs --real-git (pure unit tests run by default)")
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    return Scratch(tmp_path)
