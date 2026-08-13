"""Tests for splitting the commit a step has applied.

Git's own way to split a commit mid-rebase is `edit`, then `git reset HEAD^`,
then commit the pieces. The reset is the part worth a tool: it is the one step
that leaves the rebase in a state where amending rewrites the wrong commit, and
git's records still say amending is safe through it.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.server import (
    proceed,
    rebase_amend,
    rebase_split,
    status,
)

from scratch import Scratch


@pytest.fixture
def two_files_in_one_commit(scratch: Scratch) -> Scratch:
    """A rebase stopped at an `edit` of a commit that touches two files.

    The commit worth splitting: two unrelated changes that belong in separate
    commits, which is what the whole exercise is for.
    """
    scratch.commit("base", a="one\n")
    scratch.commit("two things at once", a="one\ntwo\n", b="new file\n")
    together = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~1", [f"edit {together}"])
    return scratch


def test_splitting_leaves_the_commits_changes_in_the_tree(
    two_files_in_one_commit: Scratch,
) -> None:
    report = rebase_split(str(two_files_in_one_commit.path))

    assert report.unapplied.subject == "two things at once"
    assert report.head.subject == "base"
    assert sorted(report.paths) == ["a", "b"]
    # Unstaged, so a `git commit` with no paths cannot sweep the whole thing back
    # in -- which is the opposite of splitting it.
    assert two_files_in_one_commit.git.lines("diff", "--cached", "--name-only") == []
    assert two_files_in_one_commit.read("b") == "new file\n"


def test_the_pieces_become_commits_and_the_rebase_carries_on(
    two_files_in_one_commit: Scratch,
) -> None:
    """The whole point, end to end: one commit in, two commits out, with the rest
    of the todo replayed on top of them."""
    rebase_split(str(two_files_in_one_commit.path))

    git = two_files_in_one_commit.git
    git.run("add", "b")
    git.run("commit", "-q", "-m", "add b")
    git.run("add", "a")
    git.run("commit", "-q", "-m", "extend a")

    report = proceed(str(two_files_in_one_commit.path))
    assert report.state == "not_rebasing"
    assert two_files_in_one_commit.subjects() == ["extend a", "add b", "base"]
    # The content still arrives: splitting is a division of the same change.
    assert two_files_in_one_commit.read("a") == "one\ntwo\n"


def test_amending_after_a_split_is_refused(two_files_in_one_commit: Scratch) -> None:
    """The hazard the tool exists to keep out of reach. Git's `amend` record
    survives the reset, so without the check this folds the split's pieces into
    the commit before them and reports success."""
    rebase_split(str(two_files_in_one_commit.path))
    before = two_files_in_one_commit.subjects()

    with pytest.raises(ValueError, match="Refusing to amend"):
        rebase_amend(str(two_files_in_one_commit.path))

    assert two_files_in_one_commit.subjects() == before


def test_continuing_with_a_piece_left_uncommitted_is_refused(
    two_files_in_one_commit: Scratch,
) -> None:
    """Git refuses this as "merge conflicts that need `git add`", of a rebase
    with no conflict in it. What is actually left is half of a commit that was
    taken apart, and saying that is the difference between one call and a hunt."""
    rebase_split(str(two_files_in_one_commit.path))
    two_files_in_one_commit.git.run("add", "b")
    two_files_in_one_commit.git.run("commit", "-q", "-m", "add b")

    with pytest.raises(ValueError) as raised:
        proceed(str(two_files_in_one_commit.path))

    assert "not in a commit" in str(raised.value)
    assert "changed a" in str(raised.value)


def test_continuing_with_an_added_file_left_untracked_is_refused(
    two_files_in_one_commit: Scratch,
) -> None:
    """The case git does not refuse at all. A mixed reset leaves a file the
    commit added untracked; `git rebase --continue` then reports success, the
    change is not in the branch, and the file is still in the tree looking like
    it was dealt with. Measured against git, not assumed."""
    rebase_split(str(two_files_in_one_commit.path))
    two_files_in_one_commit.git.run("add", "a")
    two_files_in_one_commit.git.run("commit", "-q", "-m", "extend a")

    with pytest.raises(ValueError) as raised:
        proceed(str(two_files_in_one_commit.path))

    assert "b is untracked" in str(raised.value)
    assert "report success" in str(raised.value)
    # Still stopped, so nothing has been lost yet.
    assert status(str(two_files_in_one_commit.path)).state == "stopped_after_apply"


def test_an_ordinary_resolution_is_not_mistaken_for_a_leftover(scratch: Scratch) -> None:
    """Staged content at a stop is what `--continue` is for -- it is how every
    resolved conflict gets committed. The leftover check counting it refused the
    normal flow, which the golden replay caught."""
    for value in ("one", "two", "three", "four"):
        scratch.write("f", value + "\n")
        scratch.commit(f"f={value}")
    first, last = scratch.git.out("rev-parse", "HEAD~2"), scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~3", [f"pick {first}", f"fixup {last}"])
    scratch.write("f", "resolved\n")
    scratch.git.run("add", "f")

    assert proceed(str(scratch.path)).state == "not_rebasing"


def test_the_status_after_a_split_says_what_to_do(two_files_in_one_commit: Scratch) -> None:
    rebase_split(str(two_files_in_one_commit.path))

    report = status(str(two_files_in_one_commit.path))
    assert report.unapplied
    assert not report.can_amend
    assert "taken back out" in report.guidance


def test_splitting_twice_is_refused_with_the_state_named(
    two_files_in_one_commit: Scratch,
) -> None:
    """A second call would reset past a commit the caller has not made yet."""
    rebase_split(str(two_files_in_one_commit.path))

    with pytest.raises(ValueError) as raised:
        rebase_split(str(two_files_in_one_commit.path))

    assert "already been taken back out" in str(raised.value)


def test_splitting_outside_a_rebase_is_refused(scratch: Scratch) -> None:
    """Not a rebase's business, and the plain-git answer is one command."""
    scratch.commit("base", a="one\n")

    with pytest.raises(ValueError) as raised:
        rebase_split(str(scratch.path))

    assert "nothing is in progress" in str(raised.value)
    assert "git reset HEAD^" in str(raised.value)


def test_splitting_at_a_conflicted_stop_is_refused(scratch: Scratch) -> None:
    """The commit does not exist yet, so there is nothing to take back out --
    and the reset would drop a commit the branch had already replayed."""
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    third = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")
    before = scratch.git.out("rev-parse", "HEAD")

    with pytest.raises(ValueError, match="Refusing to split"):
        rebase_split(str(scratch.path))

    assert scratch.git.out("rev-parse", "HEAD") == before


def test_splitting_a_fixup_run_is_refused(scratch: Scratch) -> None:
    """HEAD is the accumulation of the run so far, with git's template for a
    message. Taking that apart is not splitting one commit."""
    for value in ("one", "two", "three", "four"):
        scratch.write("f", value + "\n")
        scratch.commit(f"f={value}")
    first, last = scratch.git.out("rev-parse", "HEAD~2"), scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~3", [f"pick {first}", f"fixup {last}"])
    scratch.write("f", "resolved\n")
    scratch.git.run("add", "f")

    with pytest.raises(ValueError) as raised:
        rebase_split(str(scratch.path))

    assert "fixup or squash steps" in str(raised.value)


def test_splitting_with_uncommitted_changes_already_there_is_refused(
    two_files_in_one_commit: Scratch,
) -> None:
    """They would be indistinguishable from the commit's own changes once both
    are in the tree, and the caller is about to divide those into commits."""
    two_files_in_one_commit.write("a", "edited by hand\n")

    with pytest.raises(ValueError) as raised:
        rebase_split(str(two_files_in_one_commit.path))

    assert "uncommitted changes" in str(raised.value)
    assert two_files_in_one_commit.subjects()[0] == "two things at once"  # not reset
