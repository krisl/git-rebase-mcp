"""Fixtures providing scratch repositories.

Every test builds a real repository and runs real git against it. The point of
the server is that it agrees with git, so mocking git would test nothing worth
testing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scratch import Scratch


@pytest.fixture
def scratch(tmp_path: Path) -> Scratch:
    """An initialised repository with an identity, ready to be committed into."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    repo = Scratch(tmp_path)
    repo.git.run("config", "user.name", "Test")
    repo.git.run("config", "user.email", "test@example.com")
    repo.git.run("config", "commit.gpgsign", "false")
    return repo
