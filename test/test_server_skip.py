"""Tests for dropping the commit being replayed.

Git offers this at every conflict. Without it here the only way to take the
offer is to reach past these tools, which is how a rebase ends up half driven
from each side -- and how, in a real run, the remaining steps got hand-edited.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.server import rebase_start, skip, status

from scratch import Scratch


def test_skipping_drops_the_commit_and_carries_on(scratch: Scratch) -> None:
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    second, third = scratch.git.out("rev-parse", "HEAD~1"), scratch.git.out("rev-parse", "HEAD")
    rebase_start("HEAD~2", str(scratch.path), [f"pick {third}", f"pick {second}"])

    report = skip(str(scratch.path))
    assert report.state == "not_rebasing"
    assert scratch.subjects("HEAD~1..HEAD") == ["second"]  # "third" dropped


def test_skipping_reports_where_it_stops_next(scratch: Scratch) -> None:
    scratch.commit("base", f="one\n", g="one\n")
    scratch.commit("second", f="two\n", g="two\n")
    scratch.commit("third", f="three\n", g="three\n")
    second, third = scratch.git.out("rev-parse", "HEAD~1"), scratch.git.out("rev-parse", "HEAD")
    rebase_start("HEAD~2", str(scratch.path), [f"pick {third}", f"pick {second}"])

    assert skip(str(scratch.path)).state in ("conflicted", "not_rebasing")


def test_skipping_with_nothing_in_progress_is_refused(scratch: Scratch) -> None:
    scratch.commit("base", f="one\n")
    with pytest.raises(ValueError, match="Nothing in progress"):
        skip(str(scratch.path))


def test_skipping_where_no_commit_is_being_replayed_is_refused(scratch: Scratch) -> None:
    """At a `break` there is no commit in question, so the word means nothing."""
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    second = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~1", [f"pick {second}", "break"])

    with pytest.raises(ValueError, match="nothing to skip"):
        skip(str(scratch.path))
