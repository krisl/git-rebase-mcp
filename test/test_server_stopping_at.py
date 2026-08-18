"""Tests for `edit` on rebase_start: stop at these, pick everything else.

From the run that prompted it: 21 of 63 commits marked `edit`, which meant
sending all 63 lines, three times over.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.server import rebase_start, status

from scratch import Scratch


@pytest.fixture
def series(scratch: Scratch) -> Scratch:
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.commit("adds c", c="c\n")
    scratch.commit("adds d", d="d\n")
    return scratch


def test_it_stops_where_it_was_told_to(series: Scratch) -> None:
    c = series.git.out("rev-parse", "HEAD~1")
    report = rebase_start("HEAD~3", str(series.path), edit=[c])

    assert report.status.state == "stopped_after_apply"
    assert report.status.replaying is not None
    assert report.status.replaying.subject == "adds c"


def test_it_keeps_every_other_commit(series: Scratch) -> None:
    """The reason to prefer it to a hand-written todo."""
    rebase_start("HEAD~3", str(series.path), edit=["HEAD~1"])
    assert "adds b" in series.subjects()


def test_it_stops_at_each_of_several(series: Scratch) -> None:
    report = rebase_start("HEAD~3", str(series.path), edit=["HEAD~2", "HEAD"])
    assert report.status.replaying is not None
    assert report.status.replaying.subject == "adds b"


def test_it_cannot_be_combined_with_a_todo(series: Scratch) -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        rebase_start("HEAD~3", str(series.path), todo=["pick HEAD~1"], edit=["HEAD~1"])


def test_it_cannot_be_combined_with_autosquash(series: Scratch) -> None:
    with pytest.raises(ValueError, match="autosquash"):
        rebase_start("HEAD~3", str(series.path), edit=["HEAD~1"], autosquash=True)


def test_a_commit_outside_the_range_stops_the_start(series: Scratch) -> None:
    """Rather than starting a rebase that will never stop where it was asked."""
    base = series.git.out("rev-parse", "HEAD~3")
    with pytest.raises(ValueError, match="not a commit in"):
        rebase_start("HEAD~3", str(series.path), edit=[base])
    assert status(str(series.path)).state == "not_rebasing"


def test_it_cannot_be_combined_with_update_refs(series: Scratch) -> None:
    """Both generate the todo. `edit` hands one over, which is how git's
    update-ref lines would be discarded -- silently, leaving the sibling
    branches on commits that no longer exist."""
    with pytest.raises(ValueError, match="update_refs"):
        rebase_start("HEAD~3", str(series.path), edit=["HEAD~1"], update_refs=True)
