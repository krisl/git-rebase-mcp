"""Tests for where `check_command` runs.

From the run that prompted it: the check was the repository's own 1.4s ratchet,
and it stopped the rebase on the second commit of sixty-three -- because a line
budget had been raised one commit after the file that outgrew it, so the branch
had never been green there. The check was right and useless.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.server import _with_checks, proceed, rebase_start

from scratch import Scratch


@pytest.fixture
def diverged(scratch: Scratch) -> Scratch:
    """A sibling branch touching the same file, so rebasing onto it conflicts."""
    scratch.commit("base", f="one\n")
    scratch.git.run("branch", "side")
    scratch.commit("mine", f="two\n")
    scratch.git.run("checkout", "-q", "side")
    scratch.commit("theirs", f="three\n")
    scratch.git.run("checkout", "-q", "main")
    return scratch


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


class TestObservingInsteadOfGating:
    """`check_halts=False`. From the run that prompted it: a branch legitimately
    red at four of its seven commits, where a gate stops on history rather than
    on anything the rebase did, and recovering means rewriting the todo."""

    def test_the_result_comes_back_in_the_report(self, series: Scratch) -> None:
        report = rebase_start(
            "HEAD~3",
            str(series.path),
            edit_every=True,
            check_command="echo measured; exit 0",
            check_halts=False,
        )
        assert report.status.check is not None
        assert report.status.check.ok is True
        assert "measured" in report.status.check.output
        assert report.status.check.command == "echo measured; exit 0"

    def test_a_failing_check_does_not_stop_the_rebase(self, series: Scratch) -> None:
        """The whole point. `test -f d` holds only at the last commit, so a gate
        would halt on the first and never reach it."""
        report = rebase_start(
            "HEAD~3",
            str(series.path),
            edit_every=True,
            check_command="test -f d",
            check_halts=False,
        )
        assert report.status.state == "stopped_after_apply"
        assert report.status.replaying is not None
        assert report.status.replaying.subject == "adds b"
        assert report.status.check is not None and report.status.check.ok is False

    def test_it_is_reported_at_each_stop(self, series: Scratch) -> None:
        """Red until the commit that makes it true, then green -- which is the
        shape a caller compares against its baseline."""
        rebase_start(
            "HEAD~3",
            str(series.path),
            edit_every=True,
            check_command="test -f d",
            check_halts=False,
        )
        seen = []
        for _ in range(3):
            report = proceed(str(series.path))
            if report.check is not None:
                seen.append(report.check.ok)
        # Red at "adds c", green at "adds d", and green again on the report that
        # says the rebase has finished -- which is the answer for the branch as a
        # whole and the one worth having last.
        assert seen == [False, True, True]

    def test_it_is_skipped_at_a_conflicted_stop(self, diverged: Scratch) -> None:
        """The tree still holds markers there, so the answer would be about the
        conflict, and a long suite run would be spent saying so."""
        report = rebase_start(
            "side", str(diverged.path), check_command="echo ran", check_halts=False
        )
        assert report.status.state == "conflicted"
        assert report.status.check is None

    def test_a_gate_reports_nothing(self, series: Scratch) -> None:
        """The default is unchanged: an exec line halts, and there is no result
        to carry back because nothing ran here."""
        report = rebase_start(
            "HEAD~3", str(series.path), edit_every=True, check_command="echo ran"
        )
        assert report.status.check is None

    def test_saying_how_without_saying_what_is_refused(self, series: Scratch) -> None:
        with pytest.raises(ValueError, match="check_halts says how"):
            rebase_start("HEAD~3", str(series.path), check_halts=False)

    def test_it_cannot_also_be_narrowed_to_the_edits(self, series: Scratch) -> None:
        """`check_edits_only` chooses which steps get an exec line, and this
        writes none."""
        with pytest.raises(ValueError, match="Pass one"):
            rebase_start(
                "HEAD~3",
                str(series.path),
                check_command="true",
                check_edits_only=True,
                check_halts=False,
            )
