"""Tests for the amend guard and for continuing.

The first test is the reason the project exists: amending at a conflicted stop
folds two commits into one, git reports success, and nothing downstream notices.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.server import (
    rebase_amend,
    rebase_continue,
    rebase_resolve,
    rebase_status,
)

from scratch import Scratch


@pytest.fixture
def three_commits(scratch: Scratch) -> Scratch:
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    return scratch


def test_amending_at_a_conflicted_stop_is_refused(three_commits: Scratch) -> None:
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"edit {third}"], onto="HEAD~2")
    before = three_commits.subjects()

    with pytest.raises(ValueError, match="Refusing to amend"):
        rebase_amend(str(three_commits.path))

    assert three_commits.subjects() == before  # nothing was folded


def test_the_refusal_says_what_to_do_instead(three_commits: Scratch) -> None:
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"edit {third}"], onto="HEAD~2")

    with pytest.raises(ValueError) as caught:
        rebase_amend(str(three_commits.path))
    assert "Resolve the conflicted paths" in str(caught.value)


def test_amending_after_an_applied_stop_is_allowed(three_commits: Scratch) -> None:
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"edit {second}"])

    report = rebase_amend(str(three_commits.path), message="second, reworded")
    assert report.before.sha != report.after.sha
    assert report.after.subject == "second, reworded"


def test_amending_can_carry_new_content(three_commits: Scratch) -> None:
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"edit {second}"])

    three_commits.write("extra", "added while stopped\n")
    rebase_amend(str(three_commits.path), stage_all=True)

    assert "extra" in three_commits.git.lines("show", "--name-only", "--format=", "HEAD")


def test_amending_with_no_rebase_is_refused(three_commits: Scratch) -> None:
    with pytest.raises(ValueError, match="not_rebasing"):
        rebase_amend(str(three_commits.path))


def test_amending_at_a_break_is_refused(three_commits: Scratch) -> None:
    """`break` applied nothing, so HEAD is not this step's commit either."""
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"pick {second}", "break"])

    with pytest.raises(ValueError, match="stopped_without_apply"):
        rebase_amend(str(three_commits.path))


def test_continuing_while_unmerged_is_refused(three_commits: Scratch) -> None:
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")

    with pytest.raises(ValueError, match="still unmerged"):
        rebase_continue(str(three_commits.path))


def test_continuing_after_resolving_finishes_the_rebase(three_commits: Scratch) -> None:
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")

    rebase_resolve("f", "resolved\n", str(three_commits.path))
    report = rebase_continue(str(three_commits.path))

    assert report.state == "not_rebasing"
    assert three_commits.read("f") == "resolved\n"


def test_continuing_reports_the_next_stop_rather_than_failing(three_commits: Scratch) -> None:
    """Stopping again is an ordinary outcome; git's exit status says otherwise."""
    second = three_commits.git.out("rev-parse", "HEAD~1")
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~2", [f"edit {second}", f"edit {third}"])

    report = rebase_continue(str(three_commits.path))
    assert report.state == "stopped_after_apply"
    assert report.replaying is not None and report.replaying.subject == "third"


def test_amending_stays_allowed_after_a_first_amend(three_commits: Scratch) -> None:
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"edit {second}"])

    first = rebase_amend(str(three_commits.path), message="once")
    again = rebase_amend(str(three_commits.path), message="twice")
    assert first.after.sha != again.after.sha
    assert rebase_status(str(three_commits.path)).can_amend
