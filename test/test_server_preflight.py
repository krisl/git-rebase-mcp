"""Tests for checking a rebase before starting it."""

from __future__ import annotations

import pytest

from git_rebase_mcp.server import rebase_finish, rebase_preflight, rebase_start

from scratch import Scratch


@pytest.fixture
def series(scratch: Scratch) -> Scratch:
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.commit("adds c", c="c\n")
    scratch.commit("adds d", d="d\n")
    return scratch


def shas(repo: Scratch) -> list[str]:
    return [repo.git.out("rev-parse", f"HEAD~{n}") for n in (2, 1, 0)]


def test_a_sound_plan_is_safe_to_start(series: Scratch) -> None:
    b, c, d = shas(series)
    report = rebase_preflight("HEAD~3", str(series.path), [f"pick {d}", f"pick {b}", f"pick {c}"])
    assert report.safe_to_start
    assert [x.subject for x in report.commits] == ["adds b", "adds c", "adds d"]


def test_a_todo_missing_a_commit_is_not_safe_to_start(series: Scratch) -> None:
    b, _, d = shas(series)
    report = rebase_preflight("HEAD~3", str(series.path), [f"pick {b}", f"pick {d}"])

    assert not report.safe_to_start
    assert [x.subject for x in report.dropped] == ["adds c"]
    assert "dropped without a warning" in report.guidance


def test_uncommitted_changes_block_starting(series: Scratch) -> None:
    series.write("a", "changed\n")
    report = rebase_preflight("HEAD~3", str(series.path))
    assert not report.safe_to_start
    assert "uncommitted changes" in report.guidance


def test_a_rebase_already_running_blocks_starting(series: Scratch) -> None:
    head = series.git.out("rev-parse", "HEAD")
    series.start_rebase("HEAD~1", [f"edit {head}"])
    report = rebase_preflight("HEAD~3", str(series.path))
    assert not report.safe_to_start
    assert "already in progress" in report.guidance


def test_untracked_files_that_would_collide_are_named(scratch: Scratch) -> None:
    """A rebase stops dead on these before doing anything, which is confusing
    when the file is unrelated scratch work that merely shares a name."""
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.git.run("rm", "-q", "b")
    scratch.git.run("commit", "-q", "-m", "removes b")
    scratch.write("b", "local scratch of my own\n")

    report = rebase_preflight("HEAD~2", str(scratch.path))
    assert "b" in report.untracked_collisions
    assert "moved aside" in report.guidance


def test_unrelated_untracked_files_are_left_out(series: Scratch) -> None:
    series.write("notes.txt", "mine\n")
    report = rebase_preflight("HEAD~3", str(series.path))
    assert report.untracked_collisions == ()
    assert report.safe_to_start


def test_preflight_changes_nothing(series: Scratch) -> None:
    """It is a question, so asking it must not have an answer of its own."""
    before = series.git.out("rev-parse", "HEAD")
    tags_before = series.git.lines("tag")
    b, _, d = shas(series)

    rebase_preflight("HEAD~3", str(series.path), [f"pick {b}", f"pick {d}"])

    assert series.git.out("rev-parse", "HEAD") == before
    assert series.git.lines("tag") == tags_before
    assert series.git.lines("status", "--porcelain") == []


def test_preflight_names_commits_already_in_the_base(scratch: Scratch) -> None:
    scratch.commit("base", a="one\n")
    scratch.git.run("checkout", "-q", "-b", "upstream")
    scratch.commit("the work, as merged", b="feature\n")
    scratch.git.run("checkout", "-q", "main")
    scratch.commit("the work, as originally written", b="feature\n")

    report = rebase_preflight("upstream", str(scratch.path))
    assert not report.safe_to_start
    assert len(report.already_upstream) == 1
    assert "probably been merged already" in report.guidance


def test_the_backup_ref_is_named_as_not_an_ancestor(series: Scratch) -> None:
    """The report a real session got stuck on. Handed this server's own backup
    tag as a base, preflight refused -- correctly -- but explained it as "the
    branch has probably been merged already, or the base is wrong", which sent
    the caller looking for a merge. The relationship is the fact that settles
    it, and git answers it directly."""
    c = series.git.out("rev-parse", "HEAD")
    started = rebase_start("HEAD~2", str(series.path), [f"pick {c}"], force=True)
    rebase_finish(str(series.path), allow_change=True)

    report = rebase_preflight(started.backup_ref, str(series.path))
    assert not report.safe_to_start
    assert "is not an ancestor of this branch" in report.guidance
    assert "backup tag" in report.guidance


def test_a_base_with_nothing_to_replay_is_not_safe_to_start(series: Scratch) -> None:
    """It used to answer "Nothing found; safe to start", which is true of the
    checks and false of the question. A rebase that would replay nothing is not
    a safe rebase, it is a mistaken base -- and which mistake it is comes from
    where the base sits."""
    series.git.run("checkout", "-q", "-b", "behind", "HEAD~2")

    report = rebase_preflight("main", str(series.path))
    assert not report.safe_to_start
    assert "would replay nothing" in report.guidance
    assert "is ahead of this branch" in report.guidance


def test_the_branch_s_own_tip_as_a_base_says_so(series: Scratch) -> None:
    report = rebase_preflight("HEAD", str(series.path))
    assert not report.safe_to_start
    assert "this branch's own tip" in report.guidance


def test_a_todo_that_reorders_says_which_commit_moved(series: Scratch) -> None:
    """The one thing a rebase changes that nothing later reports.

    A commit written against the one before it, replayed ahead of it, still
    merges: the reference is textually fine and git has nothing to say. It only
    fails when something runs. So the reorder is named before it happens.
    """
    b, c, d = shas(series)

    report = rebase_preflight("HEAD~3", str(series.path), [f"pick {d}", f"pick {b}", f"pick {c}"])

    assert [(x.subject, y.subject) for x, y in report.reordered] == [
        ("adds d", "adds b"),
        ("adds d", "adds c"),
    ]
    assert "now replays before" in report.guidance


def test_a_todo_in_history_order_reorders_nothing(series: Scratch) -> None:
    b, c, d = shas(series)

    report = rebase_preflight("HEAD~3", str(series.path), [f"pick {b}", f"pick {c}", f"pick {d}"])

    assert report.reordered == ()
    assert "now replays before" not in report.guidance


def test_reordering_is_not_by_itself_unsafe(series: Scratch) -> None:
    """Reordering is what a todo is for; naming it is not the same as refusing it."""
    b, c, d = shas(series)

    report = rebase_preflight("HEAD~3", str(series.path), [f"pick {d}", f"pick {b}", f"pick {c}"])

    assert report.safe_to_start
