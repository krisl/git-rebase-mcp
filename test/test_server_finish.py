"""Tests for checking a finished rebase, and for abandoning one."""

from __future__ import annotations

import pytest

from git_rebase_mcp.invariants import load_session
from git_rebase_mcp.server import (
    abort,
    proceed,
    rebase_amend,
    rebase_finish,
    rebase_start,
)

from scratch import Scratch


@pytest.fixture
def series(scratch: Scratch) -> Scratch:
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.commit("adds c", c="c\n")
    return scratch


def two(repo: Scratch) -> tuple[str, str]:
    return repo.git.out("rev-parse", "HEAD~1"), repo.git.out("rev-parse", "HEAD")


def test_a_clean_reorder_checks_out(series: Scratch) -> None:
    b, c = two(series)
    rebase_start("HEAD~2", str(series.path), [f"pick {c}", f"pick {b}"])

    report = rebase_finish(str(series.path))
    assert report.ok
    assert report.branch_change is None
    assert [x.subject for x in report.commits] == ["adds c", "adds b"]


def test_a_dropped_commit_is_caught(series: Scratch) -> None:
    """The todo that lost three commits, caught this time."""
    _, c = two(series)
    rebase_start("HEAD~2", str(series.path), [f"pick {c}"], force=True)

    report = rebase_finish(str(series.path))
    assert not report.ok
    assert report.branch_change is not None
    assert "something was lost" in report.guidance


def test_a_commit_dropped_because_the_base_has_it_is_not_called_damage(
    scratch: Scratch,
) -> None:
    """The report a real rebase got wrong. A commit was dropped on purpose -- its
    content had moved into the new base -- and the check said "something was lost
    or resolved wrongly" of a result whose content was byte for byte what it
    started as. The verdict is still that the branch's change differs, because it
    does; what changes is that the report says which of the two reasons it is."""
    scratch.commit("base", a="one\n")
    base = scratch.git.out("rev-parse", "HEAD")
    scratch.commit("adds b", b="b\n")
    scratch.commit("adds c", c="c\n")
    adds_c = scratch.git.out("rev-parse", "HEAD")
    # The new base already carries "adds b", under a different sha.
    scratch.git.run("checkout", "-q", "-b", "up", base)
    scratch.commit("adds b upstream", b="b\n")
    upstream = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "main")

    rebase_start(upstream, str(scratch.path), [f"pick {adds_c}"], force=True)
    report = rebase_finish(str(scratch.path))

    assert not report.ok  # the branch's own change really did shrink
    assert report.tree_identical
    assert "content is identical" in report.guidance
    assert "allow_change=true" in report.guidance
    assert "lost or resolved wrongly" not in report.guidance


def test_a_rebase_that_lost_content_still_reads_as_damage(series: Scratch) -> None:
    """The other side of the same report. Content missing from the result is what
    the check exists for, and the wording that covers a deliberate
    redistribution must not soften it."""
    _, c = two(series)
    rebase_start("HEAD~2", str(series.path), [f"pick {c}"], force=True)

    report = rebase_finish(str(series.path))
    assert not report.tree_identical
    assert "something was lost or resolved wrongly" in report.guidance


def test_an_amended_commit_is_not_reported_as_damage(series: Scratch) -> None:
    """The case the wording was missing, and it is the ordinary one.

    Changing a commit's content at an `edit` stop is what an `edit` stop is
    for, and this server hands out `rebase_amend` to do it -- so every rebase
    that used one ended up told its own work looked like something lost or
    resolved wrongly, with a hard reset as the only exit named.  A check that
    cries wolf on its primary workflow is a check nobody reads.
    """
    b, c = two(series)
    rebase_start("HEAD~2", str(series.path), [f"edit {b}", f"pick {c}"])
    (series.path / "b").write_text("b, corrected\n")
    rebase_amend(repo=str(series.path), stage_tracked=True)
    proceed(str(series.path))

    report = rebase_finish(str(series.path))
    # Still not ok: an amend explains that *a* change is expected, never that
    # the change on screen is only that one.  The caller confirms.
    assert not report.ok
    assert "which is what amending does" in report.guidance
    assert "allow_change=true" in report.guidance
    assert "lost or resolved wrongly" not in report.guidance


def test_amending_one_commit_twice_counts_once(series: Scratch) -> None:
    """The record names the step, not the commit, so it survives the sha
    changing under it -- which is exactly what an amend does."""
    b, c = two(series)
    rebase_start("HEAD~2", str(series.path), [f"edit {b}", f"pick {c}"])
    (series.path / "b").write_text("once\n")
    rebase_amend(repo=str(series.path), stage_tracked=True)
    (series.path / "b").write_text("twice\n")
    rebase_amend(repo=str(series.path), stage_tracked=True)
    proceed(str(series.path))

    assert "1 commit" in rebase_finish(str(series.path)).guidance


def test_a_hand_edit_nobody_recorded_still_reads_as_a_warning(series: Scratch) -> None:
    """No amend, no explanation -- but the waiver is named anyway.

    The old wording offered only `reset --hard`, which is the wrong advice for
    someone who resolved by hand outside this server and knows the result is
    right.  "Only if all of it is yours" keeps the burden where it belongs.
    """
    _, c = two(series)
    rebase_start("HEAD~2", str(series.path), [f"pick {c}"], force=True)

    guidance = rebase_finish(str(series.path)).guidance
    assert "something was lost or resolved wrongly" in guidance
    assert "allow_change=true only if all of it is yours" in guidance


def test_the_refusal_says_how_to_undo(series: Scratch) -> None:
    _, c = two(series)
    started = rebase_start("HEAD~2", str(series.path), [f"pick {c}"], force=True)

    guidance = rebase_finish(str(series.path)).guidance
    assert started.backup_ref in guidance
    assert "reset --hard" in guidance


def test_a_deliberate_change_can_be_accepted(series: Scratch) -> None:
    _, c = two(series)
    rebase_start("HEAD~2", str(series.path), [f"pick {c}"], force=True)

    report = rebase_finish(str(series.path), allow_change=True)
    assert report.ok
    assert report.branch_change is not None  # still reported, just not fatal


def test_committed_markers_are_caught(scratch: Scratch) -> None:
    """Someone can always resolve by hand and go around resolve, so the
    check at the end does not rely on having been the one to write the file."""
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    third = scratch.git.out("rev-parse", "HEAD")
    rebase_start("HEAD~2", str(scratch.path), [f"pick {third}"], force=True)

    scratch.write("f", "<<<<<<< HEAD\none\n=======\nthree\n>>>>>>> abc (third)\n")
    scratch.git.run("add", "-A")
    scratch.git.run("-c", "core.editor=true", "rebase", "--continue", check=False)

    report = rebase_finish(str(scratch.path), allow_change=True)
    assert not report.ok
    assert report.commits_with_markers
    assert "conflict markers were committed" in report.guidance


def test_markers_that_are_content_can_be_allowed(scratch: Scratch) -> None:
    """A rebase may legitimately bring in a file that shows a conflict: this
    project's own documentation does. Without a way to say so the rebase could
    not be finished through the tool at all."""
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    third = scratch.git.out("rev-parse", "HEAD")
    rebase_start("HEAD~2", str(scratch.path), [f"pick {third}"], force=True)

    scratch.write("f", "<<<<<<< HEAD\none\n=======\nthree\n>>>>>>> abc (third)\n")
    scratch.git.run("add", "--", "f")
    scratch.git.run("-c", "core.editor=true", "rebase", "--continue", check=False)

    report = rebase_finish(str(scratch.path), allow_change=True, allow_markers=True)
    assert report.ok
    # Waiving the check is not hiding what it found.
    assert report.commits_with_markers
    assert "Conflict markers are committed" in report.guidance
    assert "allowed" in report.guidance


def test_finishing_before_the_rebase_is_over_is_refused(series: Scratch) -> None:
    b, c = two(series)
    rebase_start("HEAD~2", str(series.path), [f"edit {c}", f"pick {b}"])
    with pytest.raises(ValueError, match="still stopped_after_apply"):
        rebase_finish(str(series.path))


def test_finishing_clears_the_session_but_keeps_the_backup(series: Scratch) -> None:
    """Deleting the only record of where the branch was is not ours to decide."""
    b, c = two(series)
    started = rebase_start("HEAD~2", str(series.path), [f"pick {c}", f"pick {b}"])

    rebase_finish(str(series.path))
    assert load_session(series.git) is None
    assert series.git.succeeds("rev-parse", "--verify", started.backup_ref)


def test_a_failed_check_keeps_the_session_for_a_retry(series: Scratch) -> None:
    _, c = two(series)
    rebase_start("HEAD~2", str(series.path), [f"pick {c}"], force=True)

    rebase_finish(str(series.path))
    assert load_session(series.git) is not None


def test_aborting_restores_what_was_moved_aside(scratch: Scratch) -> None:
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.git.run("rm", "-q", "b")
    scratch.git.run("commit", "-q", "-m", "removes b")
    scratch.write("b", "local scratch of my own\n")
    adds_b, removes_b = two(scratch)
    rebase_start("HEAD~2", str(scratch.path), [f"pick {adds_b}", f"pick {removes_b}"])
    assert not (scratch.path / "b").exists()  # moved aside, not deleted

    report = abort(str(scratch.path))
    assert report.restored == ("b",)
    assert scratch.read("b") == "local scratch of my own\n"
    assert load_session(scratch.git) is None


def test_restoring_targets_our_stash_not_a_stash_made_meanwhile(
    scratch: Scratch,
) -> None:
    """A stash the user makes mid-rebase sits on top of the stash stack, and a
    positional pop would take that one and leave the moved-aside file hidden."""
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.git.run("rm", "-q", "b")
    scratch.git.run("commit", "-q", "-m", "removes b")
    scratch.write("b", "local scratch of my own\n")
    adds_b, removes_b = two(scratch)
    rebase_start("HEAD~2", str(scratch.path), [f"pick {adds_b}", f"pick {removes_b}"])
    assert not (scratch.path / "b").exists()

    scratch.write("notes.txt", "mine\n")
    scratch.git.run("stash", "push", "-q", "-u", "-m", "user work", "--", "notes.txt")

    report = rebase_finish(str(scratch.path))
    assert report.ok
    assert report.restored == ("b",)
    assert scratch.read("b") == "local scratch of my own\n"  # ours came back
    assert "user work" in "\n".join(scratch.git.lines("stash", "list"))  # theirs remains


def test_aborting_mid_conflict_returns_to_where_it_started(scratch: Scratch) -> None:
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    before = scratch.git.out("rev-parse", "HEAD")
    second, third = two(scratch)
    rebase_start("HEAD~2", str(scratch.path), [f"pick {third}", f"pick {second}"])

    abort(str(scratch.path))
    assert scratch.git.out("rev-parse", "HEAD") == before


def test_finishing_reports_the_resulting_commits(series: Scratch) -> None:
    b, c = two(series)
    rebase_start("HEAD~2", str(series.path), [f"pick {c}", f"pick {b}"])
    report = rebase_finish(str(series.path))
    assert [x.subject for x in report.commits] == ["adds c", "adds b"]


def test_the_same_lines_in_a_different_order_is_not_reported_as_damage(scratch: Scratch) -> None:
    """Both real runs ended with this reported as a loss. A resolution that puts
    a block somewhere else changes the patch-id and loses nothing, and a check
    that calls it damage stops being believed."""
    scratch.commit("base", f="keep\n")
    scratch.commit("adds two helpers", f="keep\ndef one(): pass\ndef two(): pass\n")
    helpers = scratch.git.out("rev-parse", "HEAD")
    rebase_start("HEAD~1", str(scratch.path), [f"edit {helpers}"])

    # Resolve it the other way round: same lines, different order.
    scratch.write("f", "keep\ndef two(): pass\ndef one(): pass\n")
    scratch.git.run("add", "f")
    scratch.git.run("-c", "core.editor=true", "commit", "-q", "--amend", "--no-edit")
    scratch.git.run("-c", "core.editor=true", "rebase", "--continue", check=False)

    report = rebase_finish(str(scratch.path))
    assert report.ok
    assert report.reordered_only
    assert report.branch_change is not None  # still reported, just not fatal
    assert "different order" in report.guidance


def test_a_lost_line_is_still_damage(scratch: Scratch) -> None:
    """The distinction has to hold in the direction that matters."""
    scratch.commit("base", f="keep\n")
    scratch.commit("adds two helpers", f="keep\ndef one(): pass\ndef two(): pass\n")
    helpers = scratch.git.out("rev-parse", "HEAD")
    rebase_start("HEAD~1", str(scratch.path), [f"edit {helpers}"])

    scratch.write("f", "keep\ndef one(): pass\n")  # two() dropped
    scratch.git.run("add", "f")
    scratch.git.run("-c", "core.editor=true", "commit", "-q", "--amend", "--no-edit")
    scratch.git.run("-c", "core.editor=true", "rebase", "--continue", check=False)

    report = rebase_finish(str(scratch.path))
    assert not report.ok
    assert not report.reordered_only
