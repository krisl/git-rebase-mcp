"""Tests for the stop an `edit` or `reword` loses when it conflicts.

The distinction under test is not which commit HEAD is -- `test_server_amend`
covers that -- but whether the caller gets another stop at all. A conflicted
`edit` is the caller's only chance to change that commit: it can read the
amending advice, follow it exactly, resolve, continue, and still find the commit
went past unchanged.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.server import conflicts, proceed, rebase_amend, status

from scratch import Scratch


@pytest.fixture
def three_commits(scratch: Scratch) -> Scratch:
    """Three commits over one file, so replaying the last onto the first
    conflicts -- the same shape the amend tests use."""
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    return scratch


def test_a_conflicted_edit_says_it_is_the_only_stop(three_commits: Scratch) -> None:
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"edit {third}"], onto="HEAD~2")

    report = status(str(three_commits.path))
    assert report.state == "conflicted"
    assert report.action == "edit"
    assert report.action_stop_lost is True
    assert "spent the stop `edit` promised" in report.guidance
    # And says what is done about it, rather than only that it happened.
    assert "gives that stop back" in report.guidance
    assert "keep_stop=False" in report.guidance
    # The advice that was already there has to survive: the caller still has to
    # resolve, and a warning that replaced the instruction would be a step back.
    assert "Read the conflicted paths with `conflicts`" in report.guidance


def test_a_conflicted_pick_spends_no_stop(three_commits: Scratch) -> None:
    """A `pick` never promised one, so saying it is gone would be noise."""
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")

    report = status(str(three_commits.path))
    assert report.state == "conflicted"
    assert report.action == "pick"
    assert report.action_stop_lost is False
    assert "spent the stop" not in report.guidance


def test_a_clean_edit_stop_is_not_reported_as_lost(three_commits: Scratch) -> None:
    """The stop happened, and the caller is standing in it."""
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"edit {second}"])

    report = status(str(three_commits.path))
    assert report.state == "stopped_after_apply"
    assert report.action_stop_lost is False


def test_git_alone_really_does_not_stop_again(three_commits: Scratch) -> None:
    """What git does unaided, pinned so the reason for keep_stop cannot drift.

    Measured rather than assumed: git commits the resolution and carries on, so
    the `edit` step's stop never happens. If a future git learns to stop here,
    this fails and keep_stop should be withdrawn rather than left restoring a
    stop that already arrives.
    """
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"edit {third}"], onto="HEAD~2")

    three_commits.write("f", "resolved\n")
    three_commits.git.run("add", "f")
    after = proceed(str(three_commits.path), keep_stop=False)

    # No second stop: the one step was committed and the rebase ended.
    assert after.state == "not_rebasing"
    assert three_commits.subjects() == ["third", "base"]
    assert three_commits.read("f") == "resolved\n"


def test_the_lost_stop_is_given_back(three_commits: Scratch) -> None:
    """The stop `edit` promised, arriving one step later than it would have.

    The commit exists by then, which is the whole difference: at the conflict
    HEAD was still the commit before it and amending would have rewritten that
    one, and here HEAD is the commit the resolution just made.
    """
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"edit {third}"], onto="HEAD~2")

    three_commits.write("f", "resolved\n")
    three_commits.git.run("add", "f")
    after = proceed(str(three_commits.path))

    assert after.state != "not_rebasing"
    assert after.can_amend is True
    assert after.head.subject == "third"
    assert "the stop this step's conflict spent" in after.guidance


def test_the_given_back_stop_can_amend_the_commit(three_commits: Scratch) -> None:
    """What the stop is for: the caller who did not know to stage everything
    before continuing still gets to change the commit."""
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"edit {third}"], onto="HEAD~2")

    three_commits.write("f", "resolved\n")
    three_commits.git.run("add", "f")
    proceed(str(three_commits.path))

    three_commits.write("g", "late\n")
    three_commits.git.run("add", "g")
    rebase_amend(str(three_commits.path), message="third, reconsidered")
    proceed(str(three_commits.path))

    assert three_commits.subjects() == ["third, reconsidered", "base"]
    assert three_commits.read("g") == "late\n"


def test_a_conflicted_pick_is_given_no_stop(three_commits: Scratch) -> None:
    """A `pick` promised none, and inventing one would be steps the caller did
    not ask for."""
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")

    three_commits.write("f", "resolved\n")
    three_commits.git.run("add", "f")
    after = proceed(str(three_commits.path))

    assert after.state == "not_rebasing"


def test_a_conflicted_reword_keeps_its_stop_too(three_commits: Scratch) -> None:
    """Worse than a conflicted edit unaided -- the rebase finishes reporting
    success with the original message -- so it needs the stop more."""
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"reword {third}"], onto="HEAD~2")

    three_commits.write("f", "resolved\n")
    three_commits.git.run("add", "f")
    after = proceed(str(three_commits.path))

    assert after.can_amend is True
    assert after.head.subject == "third"


def test_the_conflict_reader_repeats_the_warning(three_commits: Scratch) -> None:
    """Because that is the tool a caller reads while deciding to continue: a
    warning it saw one call earlier is one it has already scrolled past."""
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"edit {third}"], onto="HEAD~2")

    report = conflicts(str(three_commits.path))
    assert "whose stop this conflict has spent" in report.guidance
    assert "queues a break to give the stop back" in report.guidance


def test_the_conflict_reader_stays_quiet_for_a_pick(three_commits: Scratch) -> None:
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")

    assert "spent" not in conflicts(str(three_commits.path)).guidance


def test_a_conflicted_reword_says_so_too(three_commits: Scratch) -> None:
    """Worse than a conflicted edit, not better: git finishes the rebase with
    the original message and reports success."""
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"reword {third}"], onto="HEAD~2")

    report = status(str(three_commits.path))
    assert report.action == "reword"
    assert report.action_stop_lost is True
    assert "reword" in report.guidance


def test_the_given_back_stop_does_not_contradict_itself(three_commits: Scratch) -> None:
    """One account of the stop, not two.

    The marker saying which commit the break is holding used to be written after
    the report was built, so the report answered "is this a restored stop?" with
    no, and its generic break text -- HEAD is not a commit this step created --
    was left standing beside a sentence saying the opposite.
    """
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"edit {third}"], onto="HEAD~2")
    three_commits.write("f", "resolved\n")
    three_commits.git.run("add", "f")

    after = proceed(str(three_commits.path))

    assert after.can_amend is True
    assert "rebase_amend applies to it" in after.guidance
    assert "not the one to amend" not in after.guidance
    # And a later reader of the same stop is told the same thing
    later = status(str(three_commits.path))
    assert later.can_amend is True
    assert later.guidance == after.guidance
