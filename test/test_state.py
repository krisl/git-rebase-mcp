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
