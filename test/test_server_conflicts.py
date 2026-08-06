"""Tests for reporting and resolving conflicts through the tools."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_rebase_mcp.server import (
    FileReport,
    UnitReport,
    _conflict_guidance,
    _contained,
    conflicts,
    rebase_start,
    resolve,
    status,
)
from git_rebase_mcp.state import Commit, Conflicted, Step

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
    report = conflicts(str(conflicted.path))
    assert [f.path for f in report.files] == ["f"]
    unit = report.files[0].units[0]
    assert "+one" in unit.branch_so_far_diff
    assert "+three" in unit.replaying_diff


def test_the_replayed_commit_states_its_own_intent(conflicted: Scratch) -> None:
    """The message is a free statement of what the side was trying to do."""
    report = conflicts(str(conflicted.path))
    assert report.replaying is not None and report.replaying.subject == "third"
    assert "third" in report.replaying_body


def test_the_whole_texts_are_available_but_not_by_default(conflicted: Scratch) -> None:
    assert conflicts(str(conflicted.path)).files[0].base is None
    verbose = conflicts(str(conflicted.path), include_full_sides=True)
    assert verbose.files[0].base == "two\n"
    assert verbose.files[0].branch_so_far == "one\n"


def test_nothing_conflicted_is_not_an_error(scratch: Scratch) -> None:
    scratch.commit("base", f="one\n")
    report = conflicts(str(scratch.path))
    assert report.files == ()
    assert "Nothing is conflicted" in report.guidance


def test_resolving_stages_the_file_and_clears_the_conflict(conflicted: Scratch) -> None:
    report = resolve("f", "resolved\n", str(conflicted.path))
    assert report.still_conflicted == ()
    assert "proceed" in report.guidance
    assert conflicted.read("f") == "resolved\n"
    assert status(str(conflicted.path)).conflicted_files == ()


def test_content_with_markers_is_refused(conflicted: Scratch) -> None:
    """Staging one is how a commit ends up with markers in it."""
    with pytest.raises(ValueError, match="still contains conflict markers"):
        resolve("f", "<<<<<<< HEAD\none\n=======\nthree\n>>>>>>> abc (third)\n",
                       str(conflicted.path))
    assert status(str(conflicted.path)).conflicted_files == ("f",)


def test_a_refused_resolve_leaves_the_file_alone(conflicted: Scratch) -> None:
    before = conflicted.read("f")
    with pytest.raises(ValueError):
        resolve("f", "<<<<<<< HEAD\nbad\n", str(conflicted.path))
    assert conflicted.read("f") == before


def test_a_marker_with_its_label_stripped_is_refused(conflicted: Scratch) -> None:
    """Git always writes a space after `<<<<<<<`; a hand edit can strip it."""
    with pytest.raises(ValueError, match="still contains conflict markers"):
        resolve("f", "<<<<<<<HEAD\none\n=======\nthree\n", str(conflicted.path))


def test_remaining_conflicts_are_named(scratch: Scratch) -> None:
    scratch.commit("base", f="one\n", g="one\n")
    scratch.commit("second", f="two\n", g="two\n")
    scratch.commit("third", f="three\n", g="three\n")
    third = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")

    report = resolve("f", "resolved\n", str(scratch.path))
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

    report = conflicts(str(scratch.path))
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
    report = resolve("f", repo=str(scratch.path))

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
        resolve("f", repo=str(scratch.path))  # git left markers in the file


def test_staging_a_file_that_is_not_there_says_so(scratch: Scratch) -> None:
    scratch.commit("base", f="one\n")
    with pytest.raises(ValueError, match="nothing to stage"):
        resolve("absent", repo=str(scratch.path))


def test_resolve_refuses_an_absolute_path_outside_the_repo(scratch: Scratch) -> None:
    """A path is resolved before it is trusted, or `resolve` could write
    anywhere the process can. The write happens after this check."""
    scratch.commit("base", f="one\n")
    with pytest.raises(ValueError, match="not inside"):
        resolve("/etc/passwd", "owned\n", str(scratch.path))
    assert not Path("/etc/passwd").read_text().startswith("owned")


def test_resolve_refuses_a_relative_path_that_leaves_the_repo(scratch: Scratch) -> None:
    scratch.commit("base", f="one\n")
    with pytest.raises(ValueError, match="not inside"):
        resolve("../owned", "owned\n", str(scratch.path))


def test_take_refuses_a_path_outside_the_repo(scratch: Scratch) -> None:
    """The `take` route writes a file too, so it is refused by the same check."""
    scratch.commit("base", f="one\n")
    with pytest.raises(ValueError, match="not inside"):
        resolve("/etc/passwd", take="both", repo=str(scratch.path))


def test_resolve_refuses_a_path_under_dot_git(scratch: Scratch) -> None:
    """Inside the repository is not inside the working tree. A hook written
    there runs during the rebase this tool is driving, and git declines to
    track such a path by ignoring it and exiting zero -- so the write would
    otherwise happen and be reported as an ordinary resolution."""
    scratch.commit("base", f="one\n")
    hook = scratch.path / ".git" / "hooks" / "pre-commit"

    with pytest.raises(ValueError, match="under .git"):
        resolve(".git/hooks/pre-commit", "#!/bin/sh\ntouch owned\n", str(scratch.path))
    assert not hook.exists()


def test_resolve_refuses_a_path_that_reaches_dot_git_the_long_way(scratch: Scratch) -> None:
    scratch.commit("base", f="one\n")
    with pytest.raises(ValueError, match="under .git"):
        resolve("subdir/../.git/config", "[core]\n", str(scratch.path))


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
    resolve("f", take="both", repo=str(both_appended.path))
    assert both_appended.read("f") == "head\nfrom branch\nfrom replaying\n"


def test_taking_the_replayed_commit(both_appended: Scratch) -> None:
    resolve("f", take="replaying", repo=str(both_appended.path))
    assert both_appended.read("f") == "head\nfrom replaying\n"


def test_taking_the_branch(both_appended: Scratch) -> None:
    resolve("f", take="branch", repo=str(both_appended.path))
    assert both_appended.read("f") == "head\nfrom branch\n"


def test_taking_a_side_stages_it(both_appended: Scratch) -> None:
    report = resolve("f", take="both", repo=str(both_appended.path))
    assert report.still_conflicted == ()


def test_an_unknown_side_is_refused(both_appended: Scratch) -> None:
    with pytest.raises(ValueError, match="branch, replaying or both"):
        resolve("f", take="ours", repo=str(both_appended.path))


def test_take_and_content_together_are_refused(both_appended: Scratch) -> None:
    with pytest.raises(ValueError, match="pass one"):
        resolve("f", content="x\n", take="both", repo=str(both_appended.path))


def test_both_sides_appending_is_named_with_the_answer(both_appended: Scratch) -> None:
    """The shape a real rebase hit twice. Naming it lets a caller answer in one
    call instead of reading the file to work out the same thing."""
    report = conflicts(str(both_appended.path))
    assert report.files[0].both_inserted
    assert 'take="both"' in report.guidance


def test_an_ordinary_conflict_is_not_named_that_way(conflicted: Scratch) -> None:
    report = conflicts(str(conflicted.path))
    assert not report.files[0].both_inserted
    assert "Both sides inserted at the same point" not in report.guidance


def test_more_context_can_be_asked_for(scratch: Scratch) -> None:
    """Raise it when the region is hard to place: which function it is in, or
    whether the lines above already do what the replayed commit is adding."""
    body = [f"line {i}" for i in range(30)]
    scratch.write("f", "\n".join(body + ["target"]) + "\n")
    scratch.commit("base")
    scratch.write("f", "\n".join(body + ["from branch"]) + "\n")
    scratch.commit("branch edits it")
    scratch.write("f", "\n".join(body + ["from replaying"]) + "\n")
    scratch.commit("replaying edits it")
    last = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~1", [f"pick {last}"], onto="HEAD~2")

    tight = conflicts(str(scratch.path), context=1).files[0].units[0]
    wide = conflicts(str(scratch.path), context=10).files[0].units[0]
    assert len(wide.branch_so_far_diff) > len(tight.branch_so_far_diff)
    assert "line 21" in wide.branch_so_far_diff
    assert "line 21" not in tight.branch_so_far_diff


def test_the_branch_side_names_the_commits_behind_it(scratch: Scratch) -> None:
    """The replayed commit states its intent in its message; the branch so far
    is an accumulation with no message, and these are the nearest equivalent.

    It only says anything once commits have actually been replayed: at the first
    conflict the branch so far is still just the base, and those lines are not
    something this branch meant.
    """
    scratch.commit("base", f="shared\n")
    scratch.commit("teach it to greet", f="shared\nhello\n")
    scratch.commit("and add a farewell", f="shared\nhello\nbye\n")
    scratch.commit("shout the greeting", f="shared\nHELLO\nbye\n")
    greet, farewell, shout = (scratch.git.out("rev-parse", f"HEAD~{n}") for n in (2, 1, 0))

    # Reordering so the farewell comes last: "shout the greeting" then lands on
    # a branch that has the greeting but not the farewell, and conflicts over
    # lines a replayed commit put there.
    rebase_start("HEAD~3", str(scratch.path),
                 [f"pick {greet}", f"pick {shout}", f"pick {farewell}"])

    unit = conflicts(str(scratch.path)).files[0].units[0]
    assert [c.subject for c in unit.branch_so_far_commits] == ["teach it to greet"]


def test_the_base_is_not_claimed_as_the_branch_s_intent(scratch: Scratch) -> None:
    """Lines that were there before the rebase started are upstream code."""
    scratch.commit("base", f="shared\n")
    scratch.commit("teach it to greet", f="shared\nhello from the branch\n")
    branch_tip = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "-b", "side", "HEAD~1")
    scratch.commit("greet differently", f="shared\nhi from the replayed commit\n")
    side = scratch.git.out("rev-parse", "HEAD")
    rebase_start(branch_tip, str(scratch.path), [f"pick {side}"])

    # Nothing has been replayed yet, so the branch so far is the base itself.
    unit = conflicts(str(scratch.path)).files[0].units[0]
    assert unit.branch_so_far_commits == ()


def test_a_region_the_branch_left_empty_attributes_nothing(scratch: Scratch) -> None:
    """No lines to attribute, so it says so rather than blaming the neighbours."""
    scratch.commit("base", f="a\n")
    scratch.commit("adds b", f="a\nb\n")
    scratch.commit("adds c", f="a\nb\nc\n")
    adds_c = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~1", [f"pick {adds_c}"], onto="HEAD~2")

    unit = conflicts(str(scratch.path)).files[0].units[0]
    assert unit.branch_so_far_commits == ()


def test_the_replayed_commit_s_whole_file_diff_can_be_asked_for(scratch: Scratch) -> None:
    """What a commit did elsewhere in a file is how you tell whether the region
    in front of you is the whole of its intent. Fetching it meant leaving the
    tool for `git show` about six times in one real run."""
    scratch.commit("base", f="one\nfar away\n")
    scratch.commit("second", f="two\nfar away\n")
    scratch.commit("third", f="three\nchanged elsewhere too\n")
    second, third = scratch.git.out("rev-parse", "HEAD~1"), scratch.git.out("rev-parse", "HEAD")
    rebase_start("HEAD~2", str(scratch.path), [f"pick {third}", f"pick {second}"])

    without = conflicts(str(scratch.path)).files[0]
    with_it = conflicts(str(scratch.path), include_file_diffs=True).files[0]

    assert without.replaying_file_diff is None
    assert with_it.replaying_file_diff is not None
    assert "changed elsewhere too" in with_it.replaying_file_diff


def test_a_repo_reached_through_a_symlink_is_still_its_own_inside(tmp_path: Path) -> None:
    """The containment check resolves the path it is given, so it has to resolve
    what it compares against too -- or every path in a repository reached
    through a symlink is refused as though it were outside one."""
    real = tmp_path / "real"
    subprocess.run(["git", "init", "-q", "-b", "main", str(real)], check=True)
    scratch = Scratch(real)
    for name, value in (("user.name", "T"), ("user.email", "t@e.com")):
        scratch.git.run("config", name, value)
    scratch.git.run("config", "commit.gpgsign", "false")
    scratch.commit("base", f="one\n")
    link = tmp_path / "link"
    link.symlink_to(real)

    report = resolve("f", "resolved\n", str(link))
    assert report.path == "f"
    assert scratch.read("f") == "resolved\n"


def test_the_containment_check_resolves_the_repository_it_compares_against(
    tmp_path: Path,
) -> None:
    """The path is resolved; the repository must be too. A symlinked repository
    compared against unresolved would have every path refused, which is the
    failure this guards: the check resolves both sides."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "f").write_text("one\n")
    link = tmp_path / "link"
    link.symlink_to(real)

    root, target = _contained(link, "f")
    assert root == real.resolve()
    assert target == (real / "f").resolve()


def test_the_containment_check_still_refuses_an_outside_path(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    with pytest.raises(ValueError, match="not inside"):
        _contained(real, "../outside")


def test_the_guidance_names_a_file_with_a_region_it_cannot_place() -> None:
    """A region with no base_range is found by its two diffs, and the guidance
    says so -- otherwise the only thing distinguishing it would be silence."""
    state = Conflicted(
        step=Step(index=1, total=1),
        action="pick abc123",
        replaying=Commit(sha="a" * 40, subject="replayed"),
        head=Commit(sha="b" * 40, subject="before"),
        unmerged=("f",),
    )
    files = (
        FileReport(
            path="f",
            units=(
                UnitReport(
                    base_range=None,
                    branch_so_far_diff="@@ not found in the base file @@",
                    replaying_diff="@@ not found in the base file @@",
                ),
            ),
        ),
    )

    advice = _conflict_guidance(files, state)
    assert "f" in advice
    assert "no base_range" in advice
    assert "none is guessed" in advice
    assert "two diffs" in advice
