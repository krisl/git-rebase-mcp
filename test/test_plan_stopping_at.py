"""Tests for building a todo that stops at named commits.

The point of it is that "replay everything, stop at these" cannot be said by
hand without writing out every commit -- which is both bulky and the one place a
commit goes missing silently.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.plan import todo_stopping_at

from scratch import Scratch


@pytest.fixture
def series(scratch: Scratch) -> Scratch:
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.commit("adds c", c="c\n")
    scratch.commit("adds d", d="d\n")
    return scratch


def test_every_commit_in_the_range_is_named(series: Scratch) -> None:
    """The property that makes this safer than a hand-written list: nothing can
    be left out, because the list is the range."""
    todo = todo_stopping_at(series.git, "HEAD~3", [])

    assert [line.split(maxsplit=1)[0] for line in todo] == ["pick", "pick", "pick"]
    assert [line.split(maxsplit=2)[2] for line in todo] == ["adds b", "adds c", "adds d"]


def test_the_named_commits_stop_and_the_rest_are_picked(series: Scratch) -> None:
    c = series.git.out("rev-parse", "HEAD~1")
    todo = todo_stopping_at(series.git, "HEAD~3", [c])

    actions = {line.split(maxsplit=2)[2]: line.split(maxsplit=1)[0] for line in todo}
    assert actions == {"adds b": "pick", "adds c": "edit", "adds d": "pick"}


def test_the_order_is_the_history_s_own(series: Scratch) -> None:
    """Oldest first, which is the order a rebase replays and the order a todo is
    read in. Reversed, it would rewrite the branch backwards."""
    todo = todo_stopping_at(series.git, "HEAD~3", [])
    assert [line.split(maxsplit=2)[2] for line in todo] == [
        "adds b",
        "adds c",
        "adds d",
    ]


def test_an_abbreviated_sha_is_accepted(series: Scratch) -> None:
    short = series.git.out("rev-parse", "--short", "HEAD")
    todo = todo_stopping_at(series.git, "HEAD~3", [short])
    assert todo[-1].startswith("edit ")


def test_a_relative_revision_is_accepted(series: Scratch) -> None:
    """Because a caller naming a commit says it the way it says everything else."""
    todo = todo_stopping_at(series.git, "HEAD~3", ["HEAD~1"])
    assert todo[1].startswith("edit ")


def test_the_action_can_be_something_other_than_edit(series: Scratch) -> None:
    todo = todo_stopping_at(series.git, "HEAD~3", ["HEAD"], action="reword")
    assert todo[-1].startswith("reword ")


def test_a_commit_outside_the_range_is_refused(series: Scratch) -> None:
    """Not ignored: the rebase would run to the end without ever stopping where
    it was asked to, which is a silent no-op for the caller's whole intent."""
    base = series.git.out("rev-parse", "HEAD~3")
    with pytest.raises(ValueError, match="not a commit in"):
        todo_stopping_at(series.git, "HEAD~3", [base])


def test_a_revision_that_names_nothing_is_refused(series: Scratch) -> None:
    with pytest.raises(ValueError, match="not a commit in"):
        todo_stopping_at(series.git, "HEAD~3", ["no-such-thing"])
