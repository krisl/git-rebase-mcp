"""Tests for reading rebase state.

The case worth the whole module is the last pair: a rebase stopped at an `edit`
step, once where the commit was applied and once where it conflicted. Git's own
output looks much the same either way; the difference decides whether amending
rewrites the commit you named or the one before it.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.state import (
    Conflicted,
    NotRebasing,
    StoppedAfterApply,
    StoppedWithoutApply,
    read_state,
)

from scratch import Scratch


@pytest.fixture
def three_commits(scratch: Scratch) -> Scratch:
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    return scratch


def test_no_rebase_in_progress(three_commits: Scratch) -> None:
    state = read_state(three_commits.git)
    assert isinstance(state, NotRebasing)
    assert state.head.subject == "third"


def test_an_edit_stop_that_applied_the_commit(three_commits: Scratch) -> None:
    second = three_commits.git.out("rev-parse", "HEAD~1")
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~2", [f"edit {second}", f"pick {third}"])

    state = read_state(three_commits.git)
    assert isinstance(state, StoppedAfterApply)
    assert state.action == "edit"
    assert state.replaying.subject == "second"
    assert state.head.subject == "second"
    assert state.step.index == 1 and state.step.total == 2


def test_an_edit_stop_that_conflicted(three_commits: Scratch) -> None:
    """Replaying "third" onto "base" skips the change it was written against."""
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"edit {third}"], onto="HEAD~2")

    state = read_state(three_commits.git)
    assert isinstance(state, Conflicted)
    assert state.action == "edit"
    assert state.replaying.subject == "third"
    assert state.head.subject == "base"  # NOT "third": the commit does not exist yet
    assert state.unmerged == ("f",)


def test_the_two_edit_stops_disagree_about_head(three_commits: Scratch) -> None:
    """The distinction the module exists for, stated once as a single assertion."""
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"edit {third}"], onto="HEAD~2")
    conflicted = read_state(three_commits.git)
    three_commits.git.run("rebase", "--abort")

    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"edit {second}"])
    applied = read_state(three_commits.git)

    assert isinstance(conflicted, Conflicted)
    assert isinstance(applied, StoppedAfterApply)
    assert conflicted.head.sha != conflicted.replaying.sha
    assert applied.head.sha == applied.replaying.sha


def test_a_break_stop_applied_nothing(three_commits: Scratch) -> None:
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"pick {second}", "break"])

    state = read_state(three_commits.git)
    assert isinstance(state, StoppedWithoutApply)
    assert state.action == "break"


def test_a_failing_exec_applied_nothing(three_commits: Scratch) -> None:
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"pick {second}", "exec false"])

    state = read_state(three_commits.git)
    assert isinstance(state, StoppedWithoutApply)
    assert state.action == "exec"


def test_state_is_read_after_the_rebase_finishes(three_commits: Scratch) -> None:
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"pick {second}"])
    assert isinstance(read_state(three_commits.git), NotRebasing)


def test_a_stopped_fixup_chain_does_not_claim_head_is_the_replayed_commit(
    scratch: Scratch,
) -> None:
    """Seen on a real branch. A fixup conflicts; once its paths are resolved the
    rebase is still stopped, with the fixup accumulated into HEAD but the commit
    being replayed not yet created. Inferring from the action said "HEAD is that
    commit", which was false. Git records the answer in `amend`."""
    for value in ("one", "two", "three", "four"):
        scratch.write("f", value + "\n")
        scratch.commit(f"f={value}")
    first, last = scratch.git.out("rev-parse", "HEAD~2"), scratch.git.out("rev-parse", "HEAD")
    # Skipping a commit makes the fixup, not the pick, the step that conflicts.
    scratch.start_rebase("HEAD~3", [f"pick {first}", f"fixup {last}"])
    scratch.write("f", "resolved\n")
    scratch.git.run("add", "f")

    state = read_state(scratch.git)
    assert isinstance(state, StoppedAfterApply)
    assert state.fixups_pending  # a run of fixups is still mid-flight
    assert state.head.sha != state.replaying.sha  # HEAD is not the replayed commit
    assert state.head.subject == "f=two"
    assert state.replaying.subject == "f=four"


def test_an_edit_stop_has_no_fixups_pending(three_commits: Scratch) -> None:
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"edit {second}"])
    state = read_state(three_commits.git)
    assert isinstance(state, StoppedAfterApply)
    assert state.fixups_pending == ()
    assert state.head.sha == state.replaying.sha


def test_a_resolved_but_still_stopped_pick_is_not_amendable(scratch: Scratch) -> None:
    """All paths resolved, so nothing is unmerged, but the commit has still not
    been created. Git writes no `amend` marker, which is how this is told apart
    from the case above."""
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    third = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")
    scratch.write("f", "resolved\n")
    scratch.git.run("add", "f")

    state = read_state(scratch.git)
    assert isinstance(state, StoppedWithoutApply)


def test_amending_twice_at_an_edit_stop_stays_allowed(scratch: Scratch) -> None:
    """Git leaves `amend` in place but stops it matching HEAD after the first
    amend, since its contents are how git notices one happened. Testing the
    contents rather than the presence refused every amend after the first."""
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    second = scratch.git.out("rev-parse", "HEAD~1")
    scratch.start_rebase("HEAD~2", [f"edit {second}"])

    scratch.write("f", "amended once\n")
    scratch.git.run("add", "f")
    scratch.git.run("commit", "-q", "--amend", "--no-edit")

    assert isinstance(read_state(scratch.git), StoppedAfterApply)
