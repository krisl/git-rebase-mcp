"""Tests for reading and replacing a running rebase's remaining steps.

The need is real and shows up mid-run: on a real branch a `fixup` turned out to
depend on a commit scheduled after it, and the choice was to move one line or to
abandon thirty resolved conflicts and start again.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.server import rebase_todo

from scratch import Scratch


@pytest.fixture
def stopped(scratch: Scratch) -> Scratch:
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.commit("adds c", c="c\n")
    scratch.commit("adds d", d="d\n")
    b, c, d = (scratch.git.out("rev-parse", f"HEAD~{n}") for n in (2, 1, 0))
    scratch.start_rebase("HEAD~3", [f"edit {b}", f"pick {c}", f"pick {d}"])
    return scratch


def test_the_remaining_steps_are_reported(stopped: Scratch) -> None:
    report = rebase_todo(str(stopped.path))
    assert len(report.remaining) == 2
    assert all(line.startswith("pick ") for line in report.remaining)


def test_the_steps_can_be_reordered(stopped: Scratch) -> None:
    """Moving one line, which is the whole reason this exists."""
    remaining = rebase_todo(str(stopped.path)).remaining
    rebase_todo(str(stopped.path), todo=list(reversed(remaining)))
    assert rebase_todo(str(stopped.path)).remaining == tuple(reversed(remaining))


def test_dropping_a_commit_is_refused(stopped: Scratch) -> None:
    """Replacing the steps can lose work exactly as writing them badly can."""
    remaining = rebase_todo(str(stopped.path)).remaining
    with pytest.raises(ValueError, match="would be dropped without a warning"):
        rebase_todo(str(stopped.path), todo=[remaining[0]])


def test_force_allows_a_deliberate_drop(stopped: Scratch) -> None:
    remaining = rebase_todo(str(stopped.path)).remaining
    report = rebase_todo(str(stopped.path), todo=[remaining[0]], force=True)
    assert len(report.dropped) == 1
    assert report.remaining == (remaining[0],)


def test_losing_the_check_steps_is_called_out(scratch: Scratch) -> None:
    """Dropping every `exec` silently turns off the per-commit check."""
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.commit("adds c", c="c\n")
    b, c = scratch.git.out("rev-parse", "HEAD~1"), scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~2", [f"edit {b}", "exec true", f"pick {c}", "exec true"])

    remaining = rebase_todo(str(scratch.path)).remaining
    kept = [line for line in remaining if not line.startswith("exec ")]
    report = rebase_todo(str(scratch.path), todo=kept)
    assert "less is checked" in report.guidance


def test_reading_the_steps_with_no_rebase_is_refused(scratch: Scratch) -> None:
    scratch.commit("base", a="one\n")
    with pytest.raises(ValueError, match="No rebase in progress"):
        rebase_todo(str(scratch.path))
