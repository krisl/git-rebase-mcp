"""Tests for naming the commit a conflicted resolution is about to become.

The gap this closes: at a conflicted `edit` the commit does not exist, so
`rebase_amend` is refused, and there is no later stop for that step -- so a
resolution that deserves a word in the message could not have one. Hit for real
raising a line budget during a resolution, where the repository's own convention
says the raise should say why in the commit message.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.server import proceed, rebase_amend, rebase_start, status

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


def _resolve(repo: Scratch) -> None:
    repo.write("f", "composed\n")
    repo.git.run("add", "f")


def test_the_resolution_commits_with_the_message_given(diverged: Scratch) -> None:
    rebase_start("side", str(diverged.path))
    _resolve(diverged)

    proceed(str(diverged.path), message="mine, composed with theirs")

    assert diverged.subjects()[0] == "mine, composed with theirs"


def test_without_one_the_original_message_stands(diverged: Scratch) -> None:
    rebase_start("side", str(diverged.path))
    _resolve(diverged)

    proceed(str(diverged.path))

    assert diverged.subjects()[0] == "mine"


def test_a_body_survives(diverged: Scratch) -> None:
    rebase_start("side", str(diverged.path))
    _resolve(diverged)

    proceed(str(diverged.path), message="mine\n\nAnd why the resolution went this way.\n")

    assert diverged.git.out("log", "-1", "--format=%B").strip().endswith(
        "And why the resolution went this way."
    )


def test_it_works_at_a_conflicted_edit_which_has_no_later_stop(
    diverged: Scratch,
) -> None:
    """The case it exists for. `action_stop_lost` says this is the only stop, and
    the message is one of the two things that has to be done at it."""
    mine = diverged.git.out("rev-parse", "HEAD")
    rebase_start("side", str(diverged.path), todo=[f"edit {mine}"], force=True)
    assert status(str(diverged.path)).action_stop_lost is True
    _resolve(diverged)

    proceed(str(diverged.path), message="mine, reworded at the conflict")

    assert diverged.subjects()[0] == "mine, reworded at the conflict"


class TestWhereItIsRefused:
    """Each with its own answer to "then how do I set it", because they differ."""

    def test_at_a_stop_with_the_commit_applied(self, diverged: Scratch) -> None:
        rebase_start("side", str(diverged.path))
        _resolve(diverged)
        proceed(str(diverged.path))
        # Now a clean `edit` stop on a fresh rebase.
        rebase_start("HEAD~1", str(diverged.path), edit_every=True)

        with pytest.raises(ValueError, match="rebase_amend"):
            proceed(str(diverged.path), message="too late")

    def test_at_a_break(self, diverged: Scratch) -> None:
        rebase_start("HEAD~1", str(diverged.path), edit_every=True, break_first=True)

        with pytest.raises(ValueError, match="creates no commit"):
            proceed(str(diverged.path), message="nothing to name")

    def test_when_nothing_is_in_progress(self, diverged: Scratch) -> None:
        with pytest.raises(ValueError, match="no pending commit"):
            proceed(str(diverged.path), message="nothing to name")

    def test_the_refusal_leaves_the_message_alone(self, diverged: Scratch) -> None:
        """Refused rather than written somewhere git will not read: a message
        that vanishes silently is the failure worth avoiding."""
        rebase_start("HEAD~1", str(diverged.path), edit_every=True)
        with pytest.raises(ValueError):
            proceed(str(diverged.path), message="too late")

        report = rebase_amend(str(diverged.path), message="named the right way")
        assert report.after.subject == "named the right way"
