"""Tests for composing the decidable conflicts instead of asking about them.

Nine of the eleven conflicts in the last real rebase were shapes with one answer
both sides would recognise, and each was resolved by hand. These are that shape,
and the guard that keeps the genuinely ambiguous ones out of it.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.server import rebase_continue, rebase_start, rebase_status

from scratch import Scratch


@pytest.fixture
def stacked(scratch: Scratch) -> Scratch:
    """Three commits, each appending a line the one before has not seen."""
    scratch.commit("base", f="a\n")
    scratch.commit("adds b", f="a\nb\n")
    scratch.commit("adds c", f="a\nb\nc\n")
    return scratch


def test_a_conflict_over_different_lines_is_composed_and_the_rebase_carries_on(
    stacked: Scratch,
) -> None:
    """Replaying "adds c" without "adds b" conflicts: the branch never got b,
    and c is appended past it. Both sides applied is the only answer."""
    adds_c = stacked.git.out("rev-parse", "HEAD")
    report = rebase_start("HEAD~2", str(stacked.path), [f"pick {adds_c}"], force=True)

    assert report.status.state == "not_rebasing"  # never came back to ask
    assert report.status.auto_resolved == ("f",)
    assert stacked.read("f") == "a\nc\n"


def test_the_composition_is_named_not_silent(stacked: Scratch) -> None:
    """An automatic resolution is still a resolution and is worth a look."""
    adds_c = stacked.git.out("rev-parse", "HEAD")
    report = rebase_start("HEAD~2", str(stacked.path), [f"pick {adds_c}"], force=True)
    assert "f" in report.status.auto_resolved


def test_switching_it_off_leaves_the_conflict_to_the_caller(stacked: Scratch) -> None:
    adds_c = stacked.git.out("rev-parse", "HEAD")
    report = rebase_start(
        "HEAD~2", str(stacked.path), [f"pick {adds_c}"], auto_resolve=False, force=True
    )
    assert report.status.state == "conflicted"
    assert report.status.auto_resolved == ()


def test_both_sides_editing_the_same_line_still_stops(scratch: Scratch) -> None:
    """The guard: where there is no answer both sides would recognise, ask."""
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    second, third = scratch.git.out("rev-parse", "HEAD~1"), scratch.git.out("rev-parse", "HEAD")

    report = rebase_start("HEAD~2", str(scratch.path), [f"pick {third}", f"pick {second}"])
    assert report.status.state == "conflicted"
    assert report.status.auto_resolved == ()


def test_several_composable_conflicts_in_one_run_are_all_taken(scratch: Scratch) -> None:
    """The point of doing this during the run rather than one at a time."""
    scratch.commit("base", f="a\n", g="a\n")
    scratch.commit("adds b to both", f="a\nb\n", g="a\nb\n")
    scratch.commit("adds c to f", f="a\nb\nc\n", g="a\nb\n")
    scratch.commit("adds c to g", f="a\nb\nc\n", g="a\nb\nc\n")
    to_f, to_g = scratch.git.out("rev-parse", "HEAD~1"), scratch.git.out("rev-parse", "HEAD")

    report = rebase_start("HEAD~3", str(scratch.path), [f"pick {to_f}", f"pick {to_g}"], force=True)
    assert report.status.state == "not_rebasing"
    assert set(report.status.auto_resolved) == {"f", "g"}
    assert scratch.read("f") == "a\nc\n"
    assert scratch.read("g") == "a\nc\n"


def test_a_check_command_still_guards_an_automatic_resolution(stacked: Scratch) -> None:
    """Composing is a judgement, so it is checked like any other resolution."""
    adds_c = stacked.git.out("rev-parse", "HEAD")
    report = rebase_start(
        "HEAD~2", str(stacked.path), [f"pick {adds_c}"], check_command="grep -q b f", force=True
    )
    assert report.status.state == "stopped_without_apply"  # the check refused it
    assert report.status.action == "exec"


def test_continuing_also_composes(scratch: Scratch) -> None:
    scratch.commit("base", f="a\n", g="x\n")
    scratch.commit("adds b", f="a\nb\n")
    scratch.commit("adds c", f="a\nb\nc\n")
    adds_b, adds_c = scratch.git.out("rev-parse", "HEAD~1"), scratch.git.out("rev-parse", "HEAD")
    rebase_start("HEAD~2", str(scratch.path), [f"edit {adds_b}", f"pick {adds_c}"])

    # Take b back out at the edit stop, so the following pick conflicts over a
    # line the branch no longer has. Touch g too, or the amend is empty.
    scratch.write("f", "a\n")
    scratch.write("g", "y\n")
    scratch.git.run("add", "f", "g")
    scratch.git.run("-c", "core.editor=true", "commit", "-q", "--amend", "--no-edit")

    assert rebase_continue(str(scratch.path)).state == "not_rebasing"
    assert scratch.read("f") == "a\nc\n"
