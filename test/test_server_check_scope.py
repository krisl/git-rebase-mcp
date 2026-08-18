"""Tests for where `check_command` runs.

From the run that prompted it: the check was the repository's own 1.4s ratchet,
and it stopped the rebase on the second commit of sixty-three -- because a line
budget had been raised one commit after the file that outgrew it, so the branch
had never been green there. The check was right and useless.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.server import _with_checks, rebase_start

from scratch import Scratch


@pytest.fixture
def series(scratch: Scratch) -> Scratch:
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.commit("adds c", c="c\n")
    scratch.commit("adds d", d="d\n")
    return scratch


def test_by_default_it_runs_after_every_commit() -> None:
    woven = _with_checks(["pick aaaaaaa one", "edit bbbbbbb two"], "make test")
    assert woven == [
        "pick aaaaaaa one",
        "exec make test",
        "edit bbbbbbb two",
        "exec make test",
    ]


def test_edits_only_narrows_it_to_the_stops() -> None:
    """What was wanted all along: prove the commits I changed are sound, without
    re-testing history that was already red."""
    woven = _with_checks(
        ["pick aaaaaaa one", "edit bbbbbbb two"], "make test", edits_only=True
    )
    assert woven == ["pick aaaaaaa one", "edit bbbbbbb two", "exec make test"]


def test_edits_only_covers_reword_as_well() -> None:
    woven = _with_checks(["reword aaaaaaa one"], "make test", edits_only=True)
    assert woven == ["reword aaaaaaa one", "exec make test"]


def test_lines_naming_no_commit_never_get_a_check() -> None:
    woven = _with_checks(["break", "exec echo hi"], "make test")
    assert woven == ["break", "exec echo hi"]


def test_no_check_command_leaves_the_todo_alone() -> None:
    assert _with_checks(["pick aaaaaaa one"], None) == ["pick aaaaaaa one"]


def test_saying_when_without_saying_what_is_refused(series: Scratch) -> None:
    with pytest.raises(ValueError, match="which is unset"):
        rebase_start("HEAD~3", str(series.path), check_edits_only=True)


def test_a_failing_check_on_an_unedited_commit_is_skipped(series: Scratch) -> None:
    """The point, end to end: a check that fails everywhere except the commits
    being stopped at still lets the rebase reach them.

    `test -f d` holds only once "adds d" has been replayed, so with a check after
    every commit the rebase stops on "adds b" -- history that was always like
    that -- and never reaches the commit the caller cares about.
    """
    report = rebase_start(
        "HEAD~3",
        str(series.path),
        edit=["HEAD"],
        check_command="test -f d",
        check_edits_only=True,
    )
    assert report.status.state == "stopped_after_apply"
    assert report.status.replaying is not None
    assert report.status.replaying.subject == "adds d"


def test_the_same_check_after_every_commit_stops_early(series: Scratch) -> None:
    """The contrast, so the narrowing is shown to be doing something."""
    report = rebase_start(
        "HEAD~3", str(series.path), edit=["HEAD"], check_command="test -f d"
    )
    assert report.status.state == "stopped_without_apply"
    assert report.status.action == "exec"
