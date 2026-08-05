"""Tests for checking a rebase before starting it."""

from __future__ import annotations

import pytest

from git_rebase_mcp.server import rebase_preflight

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
