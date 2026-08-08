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
from git_rebase_mcp.conflicts import take_side
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


def test_the_refusal_names_the_way_past_it(conflicted: Scratch) -> None:
    """A refusal a caller cannot act on is one they work around by hand."""
    with pytest.raises(ValueError, match="allow_markers=True"):
        resolve("f", "<<<<<<< HEAD\none\n", str(conflicted.path))


def test_markers_that_are_content_can_be_staged_when_said_so(
    conflicted: Scratch,
) -> None:
    """The check reads text a person wrote, so it cannot tell documentation
    from a resolution abandoned half way. It refuses when unsure, and the
    caller who knows which it is overrides it."""
    documented = "The file then reads:\n\n<<<<<<< HEAD\nyours\n=======\ntheirs\n>>>>>>> feature\n"
    report = resolve("f", documented, str(conflicted.path), allow_markers=True)
    assert report.still_conflicted == ()
    assert conflicted.read("f") == documented
    assert status(str(conflicted.path)).conflicted_files == ()


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


@pytest.fixture
def deleted_by_the_branch(scratch: Scratch) -> Scratch:
    """A rebase stopped where the branch deleted `f` and the replayed commit edits it.

    An outright deletion rather than a rename: a rename git can detect is
    followed, and the replayed edit lands on the new name without conflicting.
    """
    scratch.commit("base", f="one\ntwo\n", other="keep\n")
    base = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("rm", "-q", "f")
    scratch.git.run("commit", "-q", "-m", "branch deletes f")
    deleting = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "-b", "side", base)
    scratch.commit("side edits f", f="one\ntwo\nthree\n")
    side = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "main")
    scratch.start_rebase(base, [f"pick {deleting}", f"pick {side}"])
    return scratch


def test_a_deleted_path_is_reported_as_a_deletion_not_as_regions(
    deleted_by_the_branch: Scratch,
) -> None:
    """Git leaves the surviving side's text in the working file with no markers
    in it, so nothing about the file says the question is whether it exists. The
    report has to say it: `take` means something different here, and a caller
    reading "removes 57 lines" has no reason to suspect that."""
    report = conflicts(str(deleted_by_the_branch.path))
    entry = report.files[0]

    assert entry.path == "f"
    assert entry.deleted_by == "branch"
    assert entry.units == ()
    assert "gone from the branch" in report.guidance
    # The two answers, and the reason the deletion is often not the whole story.
    assert 'take="branch"' in report.guidance
    assert "include_file_diffs" in report.guidance


def test_taking_the_side_that_deleted_stages_a_deletion(
    deleted_by_the_branch: Scratch,
) -> None:
    """The bug this was written for: `take` composed the file's blocks, a
    modify/delete has none, and the empty string that fell out was staged as the
    file's new content. An empty file in the commit, the path no longer
    conflicted, and nothing downstream with a reason to complain."""
    report = resolve("f", repo=str(deleted_by_the_branch.path), take="branch")

    assert report.deleted
    assert report.still_conflicted == ()
    # Gone from the index, not staged as an empty blob.
    assert deleted_by_the_branch.git.lines("ls-files", "--", "f") == []
    assert not (deleted_by_the_branch.path / "f").exists()

    deleted_by_the_branch.git.run("-c", "core.editor=true", "rebase", "--continue")
    assert "f" not in deleted_by_the_branch.git.lines("ls-tree", "--name-only", "HEAD")


def test_taking_the_surviving_side_keeps_what_that_side_has(
    deleted_by_the_branch: Scratch,
) -> None:
    """The other answer to the same conflict. It has to come from that side's
    stage: the working file holds one side's text and says nothing about being
    the answer, and composing has no blocks to work from here either."""
    report = resolve("f", repo=str(deleted_by_the_branch.path), take="replaying")

    assert not report.deleted
    assert report.still_conflicted == ()
    assert deleted_by_the_branch.read("f") == "one\ntwo\nthree\n"
    assert deleted_by_the_branch.git.run("show", ":f").stdout == "one\ntwo\nthree\n"


def test_both_is_refused_on_a_path_one_side_deleted(
    deleted_by_the_branch: Scratch,
) -> None:
    """"Both" means one side's lines and then the other's, and a side that
    deleted the path has none. Answering it as "keep the file" would be a guess
    at which of two opposite intents was meant."""
    with pytest.raises(ValueError) as raised:
        resolve("f", repo=str(deleted_by_the_branch.path), take="both")

    assert 'take="branch"' in str(raised.value)  # the deletion
    assert 'take="replaying"' in str(raised.value)  # keeping it
    assert deleted_by_the_branch.git.lines("diff", "--name-only", "--diff-filter=U") == ["f"]


def test_deleting_the_file_and_resolving_stages_the_deletion(
    deleted_by_the_branch: Scratch,
) -> None:
    """Answering by hand, which is what a caller does when the deletion is the
    obvious half of a rename they are about to finish. An absent file was
    refused as "nothing to stage" -- true of an ordinary path, and on a
    conflicted one the only thing its absence can mean."""
    (deleted_by_the_branch.path / "f").unlink()

    report = resolve("f", repo=str(deleted_by_the_branch.path))

    assert report.deleted
    assert deleted_by_the_branch.git.lines("ls-files", "--", "f") == []


def test_an_absent_path_that_is_not_conflicted_is_still_refused(
    conflicted: Scratch,
) -> None:
    """The refusal has to survive: a mistyped path is absent too, and staging a
    deletion for it would answer a conflict that was never asked about."""
    with pytest.raises(ValueError) as raised:
        resolve("typo", repo=str(conflicted.path))

    assert "not in the working tree" in str(raised.value)


def test_a_block_whose_base_is_a_repeated_line_is_placed_at_the_conflict(
    scratch: Scratch,
) -> None:
    """A block whose base section is a single line repeated through the file
    (a moved import, a common fixture line) must be reported where the conflict
    actually is, not at the first occurrence of that line.

    The repeat has to be in the *merge base*, because that is the side a
    block's base section is cut from and the file `base_range` counts lines
    in. Repeating the line in the two contesting sides instead proves nothing:
    the contested text is then unique in the file being searched, and looking
    from the top finds it at the right place by luck.
    """
    same = "from petri.server import NullBackend"
    # Replaying `third` onto `first` merges them over `second`, so `second` is
    # the base: `same` four times, with only the last of them contested.
    scratch.commit("first", f="\n".join([same, same, same, "import ours", ""]))
    scratch.commit("second", f="\n".join([same, same, same, same, ""]))
    scratch.commit("third", f="\n".join([same, same, same, "import theirs", ""]))
    third = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")

    report = conflicts(str(scratch.path))
    unit = report.files[0].units[0]
    # Lines 1-3 of the base repeat the contested line 4 exactly.
    assert unit.base_range == (4, 4)
    assert "+import ours" in unit.branch_so_far_diff
    assert "+import theirs" in unit.replaying_diff


def test_a_file_documenting_a_conflict_keeps_its_documentation(
    scratch: Scratch,
) -> None:
    """A README explaining conflicts shows the markers, in prose. Read by
    prefix that is a region of its own, so the file reports a conflict nobody
    has, and taking a side rewrites the explanation -- silently, in a file
    somebody asked to resolve for the real conflict further down.
    """
    doc = [
        "# Resolving conflicts",
        "",
        "When git stops, the file looks like this:",
        "",
        "<<<<<<< HEAD",
        "the version on your branch",
        "=======",
        "the version being applied",
        ">>>>>>> feature",
        "",
        "Pick one, delete the markers, and carry on.",
        "",
    ]
    scratch.commit("first", f="\n".join(doc + ["status = 'ours'", ""]))
    scratch.commit("second", f="\n".join(doc + ["status = 'base'", ""]))
    scratch.commit("third", f="\n".join(doc + ["status = 'theirs'", ""]))
    third = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")

    report = conflicts(str(scratch.path))
    units = report.files[0].units
    assert len(units) == 1, "the documented example is not a second conflict"
    assert units[0].base_range == (13, 13)

    taken = take_side(scratch.git, "f", "branch")
    assert "\n".join(doc) in taken, "the explanation must survive being resolved"
    assert taken.endswith("status = 'ours'\n")

    # Composed correctly, the file still holds the markers it is meant to, so
    # staging it needs the caller to say those are content.
    report = resolve("f", repo=str(scratch.path), take="branch", allow_markers=True)
    assert report.still_conflicted == ()
    assert "\n".join(doc) in scratch.read("f")


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


def _guidance_for(base_range: tuple[int, int] | None) -> str:
    """The advice for one file holding one region, placed or not."""
    state = Conflicted(
        step=Step(index=1, total=1),
        action="pick abc123",
        replaying=Commit(sha="a" * 40, subject="replayed"),
        head=Commit(sha="b" * 40, subject="before"),
        unmerged=("src/report.py",),
    )
    files = (
        FileReport(
            path="src/report.py",
            units=(
                UnitReport(
                    base_range=base_range,
                    branch_so_far_diff="@@ not found in the base file @@",
                    replaying_diff="@@ not found in the base file @@",
                ),
            ),
        ),
    )
    return _conflict_guidance(files, state)


def test_the_guidance_names_a_file_with_a_region_it_cannot_place() -> None:
    """A region with no base_range is found by its two diffs, and the guidance
    says so -- otherwise the only thing distinguishing it would be silence.

    Named in full, and against a path that is not a substring of the advice
    itself: `f` appears in "file" and in "diffs", so asserting on it passes
    whether the file is named or not."""
    advice = _guidance_for(None)

    assert "A region in src/report.py has no base_range" in advice
    assert "none is guessed" in advice
    assert "two diffs" in advice


def test_the_guidance_says_nothing_of_the_kind_when_the_region_is_placed() -> None:
    """The other half of the same claim: advice that appeared either way would
    tell a caller nothing, and is what an assertion on `f` alone would allow."""
    advice = _guidance_for((1, 2))

    assert "src/report.py" not in advice
    assert "base_range" not in advice
