"""Tests for checking a todo before it is used.

The first test is a real incident in miniature: a hand-written todo listed two
of the five commits in the range, git accepted it, and three commits' work
disappeared with no warning of any kind.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.plan import (
    autosquash_targets,
    base_relationship,
    check_plan,
    commits_in_range,
)

from scratch import Scratch


@pytest.fixture
def series(scratch: Scratch) -> Scratch:
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.commit("adds c", c="c\n")
    scratch.commit("adds d", d="d\n")
    return scratch


def shas(repo: Scratch) -> list[str]:
    return [c.sha for c in commits_in_range(repo.git, "HEAD~3")]


def test_the_range_is_listed_oldest_first(series: Scratch) -> None:
    assert [c.subject for c in commits_in_range(series.git, "HEAD~3")] == [
        "adds b",
        "adds c",
        "adds d",
    ]


def test_a_todo_that_leaves_a_commit_out_is_a_problem(series: Scratch) -> None:
    b, _, d = shas(series)
    check = check_plan(series.git, "HEAD~3", [f"pick {b}", f"pick {d}"])

    assert not check.safe
    assert [c.subject for c in check.dropped] == ["adds c"]
    assert "would be dropped without a warning" in check.problems[0]


def test_a_complete_todo_is_safe(series: Scratch) -> None:
    b, c, d = shas(series)
    check = check_plan(series.git, "HEAD~3", [f"pick {b}", f"pick {d}", f"pick {c}"])
    assert check.safe
    assert check.dropped == ()


def test_dropping_on_purpose_is_not_a_problem(series: Scratch) -> None:
    """`drop` says so out loud, which is the difference that matters."""
    b, c, d = shas(series)
    check = check_plan(series.git, "HEAD~3", [f"pick {b}", f"drop {c}", f"pick {d}"])
    assert check.safe
    assert [x.subject for x in check.deliberately_dropped] == ["adds c"]


def test_fixup_and_squash_keep_their_commit(series: Scratch) -> None:
    b, c, d = shas(series)
    check = check_plan(series.git, "HEAD~3", [f"pick {b}", f"fixup {c}", f"squash {d}"])
    assert check.safe


def test_abbreviated_shas_are_resolved(series: Scratch) -> None:
    b, c, d = shas(series)
    check = check_plan(
        series.git, "HEAD~3", [f"pick {b[:8]}", f"pick {c[:8]}", f"pick {d[:8]}"]
    )
    assert check.safe


def test_comments_and_blank_lines_are_ignored(series: Scratch) -> None:
    b, c, d = shas(series)
    todo = ["# this is a comment", "", f"pick {b}", f"pick {c}", f"  pick {d}"]
    assert check_plan(series.git, "HEAD~3", todo).safe


def test_a_commit_outside_the_range_is_reported(series: Scratch) -> None:
    b, c, d = shas(series)
    outside = series.git.out("rev-parse", "HEAD~3")
    check = check_plan(series.git, "HEAD~3", [f"pick {b}", f"pick {c}", f"pick {d}",
                                             f"pick {outside}"])
    assert not check.safe
    assert check.unknown == (outside,)


def test_no_todo_means_keep_everything(series: Scratch) -> None:
    """Nothing can be dropped by a list that was never written."""
    check = check_plan(series.git, "HEAD~3", None)
    assert check.safe
    assert len(check.named) == 3


def test_commits_already_in_the_base_are_reported(scratch: Scratch) -> None:
    """A merged branch keeps its old commits: the merge brought in rewritten
    copies with different shas, so the range still lists every one and a rebase
    replays work that is already there, conflicting with itself. Hit on a real
    branch whose pull request had been merged an hour earlier."""
    scratch.commit("base", a="one\n")
    scratch.git.run("checkout", "-q", "-b", "upstream")
    scratch.commit("the work, as merged", b="feature\n")
    scratch.git.run("checkout", "-q", "main")
    scratch.commit("the work, as originally written", b="feature\n")

    check = check_plan(scratch.git, "upstream", None)
    assert not check.safe
    assert [c.subject for c in check.already_upstream] == ["the work, as originally written"]
    assert "already in upstream under different shas" in check.problems[0]


def test_a_normal_branch_reports_nothing_upstream(series: Scratch) -> None:
    check = check_plan(series.git, "HEAD~3", None)
    assert check.already_upstream == ()
    assert check.safe


def test_fixups_are_matched_to_their_targets(scratch: Scratch) -> None:
    scratch.commit("base", a="one\n")
    scratch.commit("Add the thing", b="thing\n")
    target = scratch.git.out("rev-parse", "HEAD")
    scratch.commit("fixup! Add the thing", b="thing, fixed\n")
    fixup = scratch.git.out("rev-parse", "HEAD")

    targets = autosquash_targets(commits_in_range(scratch.git, "HEAD~2"))
    matched = targets[fixup]
    assert matched is not None
    assert matched.sha == target


def test_a_fixup_naming_nothing_in_the_range_is_a_problem(scratch: Scratch) -> None:
    """Git leaves it where it is and says nothing, so a stray `fixup!` survives
    into the final history -- usually because the target is already upstream, or
    its subject was edited after the fixup was written."""
    scratch.commit("base", a="one\n")
    scratch.commit("Add the thing", b="thing\n")
    scratch.commit("fixup! Add something that is not here", b="thing, fixed\n")

    check = check_plan(scratch.git, "HEAD~2", None)
    assert not check.safe
    assert [c.subject for c in check.stray_fixups] == ["fixup! Add something that is not here"]
    assert "survive into the final history" in check.problems[0]


# ── where the base sits relative to the branch ───────────────────────────────


def test_base_relationship_names_each_shape(series: Scratch) -> None:
    assert base_relationship(series.git, "HEAD") == "same"
    assert base_relationship(series.git, "HEAD~2") == "ancestor"
    series.git.run("branch", "ahead")
    series.commit("adds e", e="e\n")
    series.git.run("checkout", "-q", "ahead")
    # `ahead` is now behind main, so from here main is the descendant.
    assert base_relationship(series.git, "main") == "descendant"
    series.git.run("checkout", "-q", "-b", "sideways", "HEAD~1")
    series.commit("a different line", f="f\n")
    assert base_relationship(series.git, "main") == "diverged"
