"""Tests for starting a rebase."""

from __future__ import annotations

import pytest

from git_rebase_mcp.invariants import load_session
from git_rebase_mcp.server import rebase_start, rebase_status

from scratch import Scratch


@pytest.fixture
def series(scratch: Scratch) -> Scratch:
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.commit("adds c", c="c\n")
    return scratch


def test_a_reorder_runs_to_completion(series: Scratch) -> None:
    b, c = series.git.out("rev-parse", "HEAD~1"), series.git.out("rev-parse", "HEAD")
    report = rebase_start("HEAD~2", str(series.path), [f"pick {c}", f"pick {b}"])

    assert report.status.state == "not_rebasing"
    assert series.subjects() == ["adds b", "adds c", "base"]


def test_the_tip_is_tagged_before_anything_changes(series: Scratch) -> None:
    tip = series.git.out("rev-parse", "HEAD")
    b, c = series.git.out("rev-parse", "HEAD~1"), series.git.out("rev-parse", "HEAD")
    report = rebase_start("HEAD~2", str(series.path), [f"pick {c}", f"pick {b}"])

    assert series.git.out("rev-parse", report.backup_ref) == tip


def test_the_session_records_what_to_check_against(series: Scratch) -> None:
    b, c = series.git.out("rev-parse", "HEAD~1"), series.git.out("rev-parse", "HEAD")
    rebase_start("HEAD~2", str(series.path), [f"pick {c}", f"pick {b}"],
                 check_command="true")

    session = load_session(series.git)
    assert session is not None
    assert session.base == "HEAD~2"
    assert session.check_command == "true"


def test_an_unsafe_plan_is_refused(series: Scratch) -> None:
    c = series.git.out("rev-parse", "HEAD")
    with pytest.raises(ValueError, match="Refusing to start"):
        rebase_start("HEAD~2", str(series.path), [f"pick {c}"])  # "adds b" left out

    assert rebase_status(str(series.path)).state == "not_rebasing"
    assert series.subjects() == ["adds c", "adds b", "base"]  # untouched


def test_force_overrides_the_refusal(series: Scratch) -> None:
    """Dropping a commit on purpose has to remain possible."""
    c = series.git.out("rev-parse", "HEAD")
    rebase_start("HEAD~2", str(series.path), [f"pick {c}"], force=True)
    assert series.subjects() == ["adds c", "base"]


def test_stopping_on_a_conflict_is_reported_not_raised(scratch: Scratch) -> None:
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    second, third = scratch.git.out("rev-parse", "HEAD~1"), scratch.git.out("rev-parse", "HEAD")

    report = rebase_start("HEAD~2", str(scratch.path), [f"pick {third}", f"pick {second}"])
    assert report.status.state == "conflicted"
    assert report.status.conflicted_files == ("f",)


def test_a_colliding_untracked_file_is_moved_aside(scratch: Scratch) -> None:
    """Otherwise the rebase refuses to begin, over a file nobody asked about."""
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.git.run("rm", "-q", "b")
    scratch.git.run("commit", "-q", "-m", "removes b")
    scratch.write("b", "local scratch of my own\n")
    adds_b = scratch.git.out("rev-parse", "HEAD~1")
    removes_b = scratch.git.out("rev-parse", "HEAD")

    report = rebase_start("HEAD~2", str(scratch.path),
                          [f"pick {adds_b}", f"pick {removes_b}"])

    assert report.stashed == ("b",)
    assert report.status.state == "not_rebasing"
    assert scratch.git.lines("stash", "list")  # still recoverable


def test_unrelated_untracked_files_are_left_alone(series: Scratch) -> None:
    series.write("notes.txt", "mine\n")
    b, c = series.git.out("rev-parse", "HEAD~1"), series.git.out("rev-parse", "HEAD")

    report = rebase_start("HEAD~2", str(series.path), [f"pick {c}", f"pick {b}"])
    assert report.stashed == ()
    assert series.read("notes.txt") == "mine\n"


def test_a_check_command_that_fails_stops_the_rebase(series: Scratch) -> None:
    """The only thing that catches a commit which applies cleanly but is broken."""
    b, c = series.git.out("rev-parse", "HEAD~1"), series.git.out("rev-parse", "HEAD")
    report = rebase_start("HEAD~2", str(series.path), [f"pick {b}", f"pick {c}"],
                          check_command="false")

    assert report.status.state == "stopped_without_apply"
    assert report.status.action == "exec"


def test_a_check_command_that_passes_does_not(series: Scratch) -> None:
    b, c = series.git.out("rev-parse", "HEAD~1"), series.git.out("rev-parse", "HEAD")
    report = rebase_start("HEAD~2", str(series.path), [f"pick {b}", f"pick {c}"],
                          check_command="true")
    assert report.status.state == "not_rebasing"


def test_the_base_is_recorded_as_a_resolved_sha(series: Scratch) -> None:
    """`HEAD~2` names a different commit once history has been rewritten, so
    the spelling cannot be used to name the range afterwards."""
    base_before = series.git.out("rev-parse", "HEAD~2")
    b, c = series.git.out("rev-parse", "HEAD~1"), series.git.out("rev-parse", "HEAD")
    rebase_start("HEAD~2", str(series.path), [f"pick {c}", f"pick {b}"])

    session = load_session(series.git)
    assert session is not None
    assert session.base == "HEAD~2"
    assert session.base_sha == base_before
    assert series.git.out("rev-parse", "HEAD~2") == base_before  # unchanged here...
    assert session.base_sha != series.git.out("rev-parse", "HEAD")
