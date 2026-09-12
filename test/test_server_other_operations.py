"""Tests for conflicts a rebase did not cause.

A rebase is not the only thing that leaves three stages in the index, and for a
while everything else was reported as "no rebase in progress" -- which the
conflict tool turned into "Nothing is conflicted.", said of a repository with
unmerged paths in it. These are the cases that has to keep covering: the reading
of a conflict is the same whatever applied it, and only the command that carries
on differs.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.server import (
    abort,
    conflicts,
    proceed,
    rebase_amend,
    rebase_todo,
    resolve,
    skip,
    status,
)

from scratch import Scratch


@pytest.fixture
def diverged(scratch: Scratch) -> Scratch:
    """A branch and a main that changed the same line, ready to collide."""
    scratch.commit("base", f="def render():\n    return one\n")
    scratch.git.run("checkout", "-q", "-b", "side")
    scratch.commit("side change", f="def render():\n    return side\n")
    scratch.git.run("checkout", "-q", "main")
    scratch.commit("main change", f="def render():\n    return main\n")
    return scratch


def test_a_cherry_pick_conflict_is_reported_as_a_conflict(diverged: Scratch) -> None:
    diverged.git.run("cherry-pick", "side", check=False)

    report = status(str(diverged.path))
    assert report.state == "conflicted"
    assert report.operation == "cherry-pick"
    assert report.conflicted_files == ("f",)


def test_a_merge_conflict_is_reported_as_a_conflict(diverged: Scratch) -> None:
    diverged.git.run("merge", "side", check=False)

    report = status(str(diverged.path))
    assert report.state == "conflicted"
    assert report.operation == "merge"


def test_a_revert_conflict_is_reported_as_a_conflict(scratch: Scratch) -> None:
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    scratch.git.run("revert", "--no-edit", "HEAD~1", check=False)

    report = status(str(scratch.path))
    assert report.state == "conflicted"
    assert report.operation == "revert"


def test_a_conflict_nothing_recorded_is_still_a_conflict(scratch: Scratch) -> None:
    """A stash popped into a conflict leaves the stages and no note of where
    they came from. Reporting a clean tree there is the worst answer available."""
    scratch.commit("base", f="one\n")
    scratch.write("f", "stashed\n")
    scratch.git.run("stash", "-q")
    scratch.commit("moved on", f="moved\n")
    scratch.git.run("stash", "pop", check=False)

    report = status(str(scratch.path))
    assert report.state == "conflicted"
    assert report.operation == "unknown"
    assert report.replaying is None


def test_the_regions_read_the_same_whatever_applied_them(diverged: Scratch) -> None:
    diverged.git.run("cherry-pick", "side", check=False)

    report = conflicts(str(diverged.path))
    assert report.replaying is not None
    assert report.replaying.subject == "side change"
    unit = report.files[0].units[0]
    assert "+    return main" in unit.branch_so_far_diff
    assert "+    return side" in unit.replaying_diff


def test_the_incoming_side_of_a_merge_is_the_branch_not_its_tip(diverged: Scratch) -> None:
    """A merge applies everything the other branch did, so a file diff taken
    from the tip commit alone would understate what is arriving."""
    diverged.git.run("checkout", "-q", "side")
    diverged.commit("side, again", f="def render():\n    return side\n", g="extra\n")
    diverged.git.run("checkout", "-q", "main")
    diverged.git.run("merge", "side", check=False)

    report = conflicts(str(diverged.path), include_file_diffs=True)
    diff = report.files[0].replaying_file_diff
    assert diff is not None
    assert "+    return side" in diff  # from the tip's parent, not the tip


def test_a_cherry_pick_is_carried_on_by_its_own_continue(diverged: Scratch) -> None:
    """`git rebase --continue` does not finish a cherry-pick, and calling it
    would leave the conflict where it was while reporting that something
    happened."""
    diverged.git.run("cherry-pick", "side", check=False)
    resolve("f", repo=str(diverged.path), take="replaying")

    after = proceed(str(diverged.path))
    assert after.state == "not_rebasing"
    assert diverged.subjects()[0] == "side change"


def test_a_merge_is_carried_on_by_its_own_continue(diverged: Scratch) -> None:
    diverged.git.run("merge", "side", check=False)
    resolve("f", repo=str(diverged.path), take="replaying")

    after = proceed(str(diverged.path))
    assert after.state == "not_rebasing"
    assert after.head.subject.startswith("Merge branch")


def test_an_operation_with_everything_staged_is_still_in_progress(diverged: Scratch) -> None:
    """It has still to be told to commit. Reporting "no rebase in progress"
    here left a caller who had done everything right with nowhere to go."""
    diverged.git.run("cherry-pick", "side", check=False)
    resolve("f", repo=str(diverged.path), take="replaying")

    report = status(str(diverged.path))
    assert report.state == "applying"
    assert report.operation == "cherry-pick"
    assert "proceed" in report.guidance


def test_resolving_a_conflict_nothing_owns_does_not_promise_a_continue(
    scratch: Scratch,
) -> None:
    scratch.commit("base", f="one\n")
    scratch.write("f", "stashed\n")
    scratch.git.run("stash", "-q")
    scratch.commit("moved on", f="moved\n")
    scratch.git.run("stash", "pop", check=False)

    report = resolve("f", repo=str(scratch.path), take="replaying")
    assert report.still_conflicted == ()
    assert "nothing" in report.guidance.lower()
    with pytest.raises(ValueError, match="Nothing in progress"):
        proceed(str(scratch.path))


def test_a_merge_offers_nothing_to_skip(diverged: Scratch) -> None:
    """Skipping is a decision about a commit; a merge applies a branch."""
    diverged.git.run("merge", "side", check=False)

    with pytest.raises(ValueError, match="a merge applies a whole branch"):
        skip(str(diverged.path))


def test_a_cherry_pick_can_be_skipped(diverged: Scratch) -> None:
    diverged.git.run("cherry-pick", "side", check=False)

    after = skip(str(diverged.path))
    assert after.state == "not_rebasing"
    assert diverged.subjects()[0] == "main change"  # the pick was dropped


@pytest.mark.live_repo
def test_a_cherry_pick_is_abandoned_by_its_own_abort(diverged: Scratch) -> None:
    diverged.git.run("cherry-pick", "side", check=False)

    report = abort(str(diverged.path))
    assert report.head.subject == "main change"
    assert diverged.git.lines("diff", "--name-only", "--diff-filter=U") == []


@pytest.mark.live_repo
def test_aborting_a_conflict_nothing_owns_is_refused(scratch: Scratch) -> None:
    """There is no operation to abandon, and no way to tell what undoing it
    would discard."""
    scratch.commit("base", f="one\n")
    scratch.write("f", "stashed\n")
    scratch.git.run("stash", "-q")
    scratch.commit("moved on", f="moved\n")
    scratch.git.run("stash", "pop", check=False)

    with pytest.raises(ValueError, match="no operation to abandon"):
        abort(str(scratch.path))


def test_a_cherry_pick_is_not_mistaken_for_a_rebase_with_a_todo(diverged: Scratch) -> None:
    """The todo lives in `.git/rebase-merge/`, which a cherry-pick has not got.
    Asking whether a rebase is in progress is not the same as asking whether
    anything is."""
    diverged.git.run("cherry-pick", "side", check=False)

    with pytest.raises(ValueError, match="No rebase in progress"):
        rebase_todo(repo=str(diverged.path))


def test_the_amend_refusal_names_the_operation_that_is_actually_running(
    diverged: Scratch,
) -> None:
    """It used to say "the rebase is conflicted" whatever had conflicted, which
    is the same false claim this file exists to keep out of the replies."""
    diverged.git.run("cherry-pick", "side", check=False)

    with pytest.raises(ValueError, match="the cherry-pick is conflicted"):
        rebase_amend(str(diverged.path))


def test_aborting_over_a_finished_but_unstaged_resolution_is_refused(diverged: Scratch) -> None:
    """The work git keeps no copy of. Somebody resolved the file and stopped short
    of staging, so the index still calls it unmerged, rerere has learned nothing,
    and an abort takes the answer with it leaving no trace it ever existed."""
    diverged.git.run("cherry-pick", "side", check=False)
    diverged.write("f", "the resolution, written and not staged\n")

    with pytest.raises(ValueError, match="never staged"):
        abort(str(diverged.path))

    # Still in progress, and the resolution still on disk
    assert diverged.read("f") == "the resolution, written and not staged\n"


def test_a_finished_resolution_can_be_abandoned_on_purpose(diverged: Scratch) -> None:
    """Forced, because the resolution being wrong is the ordinary reason to abort."""
    diverged.git.run("cherry-pick", "side", check=False)
    diverged.write("f", "a resolution worth abandoning\n")

    report = abort(str(diverged.path), force=True)

    assert report.discarded == ("f",)
    assert "f" in report.guidance
    assert diverged.git.lines("diff", "--name-only", "--diff-filter=U") == []


@pytest.mark.live_repo
def test_an_untouched_conflict_does_not_block_an_abort(diverged: Scratch) -> None:
    """Markers left in the file mean nobody has answered it yet, and refusing on
    that would refuse every ordinary abort."""
    diverged.git.run("cherry-pick", "side", check=False)

    report = abort(str(diverged.path))

    assert report.discarded == ()
