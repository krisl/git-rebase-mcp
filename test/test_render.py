"""What a person watching reads.

Rendering is checked on the reports the tools actually return, built by running
real rebases, rather than on hand-made dataclasses: the point of it is to describe
a real stop, and a fixture cannot disagree with git.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.render import render
from git_rebase_mcp.server import conflicts, proceed, rebase_start, resolve, status

from scratch import Scratch


@pytest.fixture
def conflicting(scratch: Scratch) -> Scratch:
    scratch.commit("base", f="one\nkeep\n")
    scratch.commit("second", f="two\nkeep\n")
    scratch.commit("third", f="three\nkeep\n")
    return scratch


def test_a_conflicted_stop_says_where_it_is(conflicting: Scratch) -> None:
    third = conflicting.git.out("rev-parse", "HEAD")
    conflicting.start_rebase("HEAD~1", [f"edit {third}"], onto="HEAD~2")

    text = render(status(str(conflicting.path)))

    # The step, the action and the branch, in git's own words
    assert text.startswith("rebase · step 1/1 · edit · main")
    assert "replaying" in text and "third" in text
    assert "conflicted  f" in text
    # And the guidance it was given, verbatim and set apart
    assert "\n\n" in text
    assert "Read the conflicted paths with `conflicts`" in text


def test_the_conflict_reading_names_each_region(conflicting: Scratch) -> None:
    third = conflicting.git.out("rev-parse", "HEAD")
    conflicting.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")

    text = render(conflicts(str(conflicting.path)))

    assert "conflicts · 1 file · 1 region" in text
    assert "region 1  base 1-1" in text
    assert "branch_so_far" in text and "replaying" in text


def test_the_conflict_reading_shows_each_sides_diff(conflicting: Scratch) -> None:
    third = conflicting.git.out("rev-parse", "HEAD")
    conflicting.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")

    text = render(conflicts(str(conflicting.path)))

    # Summaries say how much changed; the diffs say what. Both sides' actual
    # lines are in the reading, so the usual conflict resolves without
    # opening the file. (Against the pick's parent as base, the branch side
    # reads -two/+one and the replayed side -two/+three.)
    assert text.count("@@ ") >= 2
    assert "+one" in text
    assert "+three" in text


def test_a_repeated_line_is_shown_where_it_is_decided(conflicting: Scratch) -> None:
    third = conflicting.git.out("rev-parse", "HEAD")
    conflicting.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")

    text = render(resolve("f", "three\nkeep\nkeep\n", str(conflicting.path)))

    assert "repeated 1" in text
    assert "2x here, 1x branch, 1x replaying" in text


def test_a_finished_rebase_says_so(conflicting: Scratch) -> None:
    third = conflicting.git.out("rev-parse", "HEAD")
    conflicting.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")
    conflicting.write("f", "three\nkeep\n")
    conflicting.git.run("add", "f")

    text = render(proceed(str(conflicting.path)))

    assert text.startswith("not_rebasing")
    assert "HEAD" in text


def test_a_report_with_no_renderer_still_gives_its_guidance() -> None:
    """Nothing is hidden by a renderer being missing."""

    class Odd:
        guidance = "something to say"

    assert render(Odd()) == "something to say"


def test_counts_read_as_english(conflicting: Scratch) -> None:
    """One commit, not "1 commits": a report read by a person is read closely."""
    third = conflicting.git.out("rev-parse", "HEAD")
    # Through rebase_start, so a session exists and the finished line is reported
    rebase_start("HEAD~1", str(conflicting.path), [f"pick {third}"], onto="HEAD~2")
    conflicting.write("f", "three\nkeep\n")
    conflicting.git.run("add", "f")

    text = render(proceed(str(conflicting.path)))

    assert "1 commit over" in text
    assert "1 commits" not in text
