"""Tests for reporting and resolving conflicts through the tools."""

from __future__ import annotations

import pytest

from git_rebase_mcp.server import rebase_conflicts, rebase_resolve, rebase_status

from scratch import Scratch


@pytest.fixture
def conflicted(scratch: Scratch) -> Scratch:
    """A rebase stopped on a conflict in `f`.

    Replaying "third" onto "base" skips the change it was written against.
    """
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    third = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")
    return scratch


def test_each_side_is_reported_as_what_it_did(conflicted: Scratch) -> None:
    report = rebase_conflicts(str(conflicted.path))
    assert [f.path for f in report.files] == ["f"]
    unit = report.files[0].units[0]
    assert "+one" in unit.branch_so_far_diff
    assert "+three" in unit.replaying_diff


def test_the_replayed_commit_states_its_own_intent(conflicted: Scratch) -> None:
    """The message is a free statement of what the side was trying to do."""
    report = rebase_conflicts(str(conflicted.path))
    assert report.replaying is not None and report.replaying.subject == "third"
    assert "third" in report.replaying_body


def test_the_whole_texts_are_available_but_not_by_default(conflicted: Scratch) -> None:
    assert rebase_conflicts(str(conflicted.path)).files[0].base is None
    verbose = rebase_conflicts(str(conflicted.path), include_full_sides=True)
    assert verbose.files[0].base == "two\n"
    assert verbose.files[0].branch_so_far == "one\n"


def test_nothing_conflicted_is_not_an_error(scratch: Scratch) -> None:
    scratch.commit("base", f="one\n")
    report = rebase_conflicts(str(scratch.path))
    assert report.files == ()
    assert "Nothing is conflicted" in report.guidance


def test_resolving_stages_the_file_and_clears_the_conflict(conflicted: Scratch) -> None:
    report = rebase_resolve("f", "resolved\n", str(conflicted.path))
    assert report.still_conflicted == ()
    assert "rebase_continue" in report.guidance
    assert conflicted.read("f") == "resolved\n"
    assert rebase_status(str(conflicted.path)).conflicted_files == ()


def test_content_with_markers_is_refused(conflicted: Scratch) -> None:
    """Staging one is how a commit ends up with markers in it."""
    with pytest.raises(ValueError, match="still contains conflict markers"):
        rebase_resolve("f", "<<<<<<< HEAD\none\n=======\nthree\n>>>>>>> abc (third)\n",
                       str(conflicted.path))
    assert rebase_status(str(conflicted.path)).conflicted_files == ("f",)


def test_a_refused_resolve_leaves_the_file_alone(conflicted: Scratch) -> None:
    before = conflicted.read("f")
    with pytest.raises(ValueError):
        rebase_resolve("f", "<<<<<<< HEAD\nbad\n", str(conflicted.path))
    assert conflicted.read("f") == before


def test_remaining_conflicts_are_named(scratch: Scratch) -> None:
    scratch.commit("base", f="one\n", g="one\n")
    scratch.commit("second", f="two\n", g="two\n")
    scratch.commit("third", f="three\n", g="three\n")
    third = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")

    report = rebase_resolve("f", "resolved\n", str(scratch.path))
    assert report.still_conflicted == ("g",)
    assert "g" in report.guidance


def test_an_add_add_conflict_hands_over_both_versions(scratch: Scratch) -> None:
    """With no base there are no regions, so withholding the texts behind
    include_full_sides would leave the caller nothing at all."""
    scratch.commit("base", other="x\n")
    scratch.commit("branch adds", f="from branch\n")
    branch_side = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "-b", "side", "HEAD~1")
    scratch.commit("side adds", f="from side\n")
    side = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "main")
    scratch.start_rebase(branch_side, [f"pick {side}"])

    report = rebase_conflicts(str(scratch.path))
    entry = report.files[0]
    assert entry.no_common_base
    assert entry.units == ()
    assert entry.branch_so_far == "from branch\n"
    assert entry.replaying == "from side\n"
    assert "No common base for f" in report.guidance


def test_resolving_can_stage_what_is_already_in_the_working_tree(scratch: Scratch) -> None:
    """Sending a large file back through a tool parameter costs more than
    editing it in place; a 900-line file was about ten thousand tokens."""
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    third = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")

    scratch.write("f", "resolved in place\n")
    report = rebase_resolve("f", repo=str(scratch.path))

    assert report.still_conflicted == ()
    assert scratch.git.out("show", ":0:f") == "resolved in place"


def test_staging_the_working_tree_still_refuses_markers(scratch: Scratch) -> None:
    """The check must not be skippable by taking the other route into the tool."""
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    third = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")

    with pytest.raises(ValueError, match="conflict markers"):
        rebase_resolve("f", repo=str(scratch.path))  # git left markers in the file


def test_staging_a_file_that_is_not_there_says_so(scratch: Scratch) -> None:
    scratch.commit("base", f="one\n")
    with pytest.raises(ValueError, match="nothing to stage"):
        rebase_resolve("absent", repo=str(scratch.path))


@pytest.fixture
def both_appended(scratch: Scratch) -> Scratch:
    """Both sides added at the same point, which composing refuses on purpose."""
    scratch.commit("base", f="head\n")
    scratch.commit("branch adds", f="head\nfrom branch\n")
    branch_side = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "-b", "side", "HEAD~1")
    scratch.commit("side adds", f="head\nfrom replaying\n")
    side = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "main")
    scratch.start_rebase(branch_side, [f"pick {side}"])
    return scratch


def test_taking_both_keeps_the_branch_first(both_appended: Scratch) -> None:
    """What two insertions at the same point almost always mean."""
    rebase_resolve("f", take="both", repo=str(both_appended.path))
    assert both_appended.read("f") == "head\nfrom branch\nfrom replaying\n"


def test_taking_the_replayed_commit(both_appended: Scratch) -> None:
    rebase_resolve("f", take="replaying", repo=str(both_appended.path))
    assert both_appended.read("f") == "head\nfrom replaying\n"


def test_taking_the_branch(both_appended: Scratch) -> None:
    rebase_resolve("f", take="branch", repo=str(both_appended.path))
    assert both_appended.read("f") == "head\nfrom branch\n"


def test_taking_a_side_stages_it(both_appended: Scratch) -> None:
    report = rebase_resolve("f", take="both", repo=str(both_appended.path))
    assert report.still_conflicted == ()


def test_an_unknown_side_is_refused(both_appended: Scratch) -> None:
    with pytest.raises(ValueError, match="branch, replaying or both"):
        rebase_resolve("f", take="ours", repo=str(both_appended.path))


def test_take_and_content_together_are_refused(both_appended: Scratch) -> None:
    with pytest.raises(ValueError, match="pass one"):
        rebase_resolve("f", content="x\n", take="both", repo=str(both_appended.path))
