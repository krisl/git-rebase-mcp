"""Tests for `edit` on rebase_start: stop at these, pick everything else.

From the run that prompted it: 21 of 63 commits marked `edit`, which meant
sending all 63 lines, three times over.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.server import proceed, rebase_start, status

from scratch import Scratch


@pytest.fixture
def series(scratch: Scratch) -> Scratch:
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.commit("adds c", c="c\n")
    scratch.commit("adds d", d="d\n")
    return scratch


@pytest.mark.live_repo
def test_it_stops_where_it_was_told_to(series: Scratch) -> None:
    c = series.git.out("rev-parse", "HEAD~1")
    report = rebase_start("HEAD~3", str(series.path), edit=[c])

    assert report.status.state == "stopped_after_apply"
    assert report.status.replaying is not None
    assert report.status.replaying.subject == "adds c"


@pytest.mark.live_repo
def test_it_keeps_every_other_commit(series: Scratch) -> None:
    """The reason to prefer it to a hand-written todo."""
    rebase_start("HEAD~3", str(series.path), edit=["HEAD~1"])
    assert "adds b" in series.subjects()


@pytest.mark.live_repo
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


class TestStoppingEverywhere:

    @pytest.mark.live_repo
    def test_it_stops_at_the_first_commit(self, series: Scratch) -> None:
        report = rebase_start("HEAD~3", str(series.path), edit_every=True)
        assert report.status.replaying is not None
        assert report.status.replaying.subject == "adds b"

    @pytest.mark.live_repo
    def test_break_first_stops_with_nothing_applied(self, series: Scratch) -> None:
        """HEAD is the base, so a suite run here is the baseline the rest is
        measured against."""
        base = series.git.out("rev-parse", "HEAD~3")
        report = rebase_start("HEAD~3", str(series.path), edit_every=True, break_first=True)

        assert report.status.state == "stopped_without_apply"
        assert report.status.action == "break"
        assert report.status.head.sha == base

    @pytest.mark.live_repo
    def test_it_reaches_every_commit(self, series: Scratch) -> None:
        rebase_start("HEAD~3", str(series.path), edit_every=True, break_first=True)
        seen = []
        for _ in range(4):
            report = proceed(str(series.path))
            if report.replaying is not None:
                seen.append(report.replaying.subject)
            if report.state == "not_rebasing":
                break
        assert seen == ["adds b", "adds c", "adds d"]

    def test_naming_some_and_asking_for_all_is_refused(self, series: Scratch) -> None:
        with pytest.raises(ValueError, match="two different things"):
            rebase_start("HEAD~3", str(series.path), edit=["HEAD"], edit_every=True)

    def test_a_todo_of_your_own_still_refuses_them(self, series: Scratch) -> None:
        with pytest.raises(ValueError, match="cannot be combined"):
            rebase_start("HEAD~3", str(series.path), todo=["pick HEAD"], break_first=True)


def test_a_dropped_step_gets_no_check(scratch: Scratch) -> None:
    """A drop creates no commit, so the tree after it is the tree already checked.

    Counted on the todo rather than by watching the command run: the exec lines
    are what git will execute, and a check woven after a step that commits
    nothing is a suite run bought twice at the same price.
    """
    from git_rebase_mcp.server import _with_checks

    woven = _with_checks(["pick aaaaaaa", "drop bbbbbbb", "edit ccccccc"], "make test")

    assert woven == [
        "pick aaaaaaa",
        "exec make test",
        "drop bbbbbbb",
        "edit ccccccc",
        "exec make test",
    ]
