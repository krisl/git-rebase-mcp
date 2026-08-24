"""Tests for the amend guard and for continuing.

The first test is the reason the project exists: amending at a conflicted stop
folds two commits into one, git reports success, and nothing downstream notices.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.server import (
    proceed,
    rebase_amend,
    rebase_start,
    resolve,
    status,
)

from scratch import Scratch


@pytest.fixture
def three_commits(scratch: Scratch) -> Scratch:
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    return scratch


def test_amending_at_a_conflicted_stop_is_refused(three_commits: Scratch) -> None:
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"edit {third}"], onto="HEAD~2")
    before = three_commits.subjects()

    with pytest.raises(ValueError, match="Refusing to amend"):
        rebase_amend(str(three_commits.path))

    assert three_commits.subjects() == before  # nothing was folded


def test_the_refusal_says_what_to_do_instead(three_commits: Scratch) -> None:
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"edit {third}"], onto="HEAD~2")

    with pytest.raises(ValueError) as caught:
        rebase_amend(str(three_commits.path))
    # What to do instead: read the conflict, resolve it, and take the stop back.
    assert "Read the conflicted paths with `conflicts`" in str(caught.value)
    assert "gives that stop back" in str(caught.value)


def test_amending_after_an_applied_stop_is_allowed(three_commits: Scratch) -> None:
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"edit {second}"])

    report = rebase_amend(str(three_commits.path), message="second, reworded")
    assert report.before.sha != report.after.sha
    assert report.after.subject == "second, reworded"


def test_amending_carries_whatever_is_staged(three_commits: Scratch) -> None:
    """A file git does not track yet has to be staged deliberately. The tool
    will not go looking for it: that is the whole difference between `add -u`
    and `add -A`, and the latter is how junk gets into history."""
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"edit {second}"])

    three_commits.write("extra", "added while stopped\n")
    three_commits.git.run("add", "extra")
    rebase_amend(str(three_commits.path))

    assert "extra" in three_commits.git.lines("show", "--name-only", "--format=", "HEAD")


def test_amending_with_nothing_in_progress_is_refused(three_commits: Scratch) -> None:
    """Not because it would be unsafe -- HEAD is exactly what it looks like --
    but because there is no step whose commit it would be, and amending an
    ordinary HEAD needs none of what this tool guards."""
    with pytest.raises(ValueError, match="ordinary `git commit --amend`"):
        rebase_amend(str(three_commits.path))


def test_amending_at_a_break_is_refused(three_commits: Scratch) -> None:
    """`break` applied nothing, so HEAD is not this step's commit either."""
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"pick {second}", "break"])

    with pytest.raises(ValueError, match="stopped_without_apply"):
        rebase_amend(str(three_commits.path))


def test_continuing_while_unmerged_is_refused(three_commits: Scratch) -> None:
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")

    with pytest.raises(ValueError, match="still unmerged"):
        proceed(str(three_commits.path))


def test_continuing_after_resolving_finishes_the_rebase(three_commits: Scratch) -> None:
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")

    resolve("f", "resolved\n", str(three_commits.path))
    report = proceed(str(three_commits.path))

    assert report.state == "not_rebasing"
    assert three_commits.read("f") == "resolved\n"


def test_continuing_reports_the_next_stop_rather_than_failing(three_commits: Scratch) -> None:
    """Stopping again is an ordinary outcome; git's exit status says otherwise."""
    second = three_commits.git.out("rev-parse", "HEAD~1")
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~2", [f"edit {second}", f"edit {third}"])

    report = proceed(str(three_commits.path))
    assert report.state == "stopped_after_apply"
    assert report.replaying is not None and report.replaying.subject == "third"


def test_amending_stays_allowed_after_a_first_amend(three_commits: Scratch) -> None:
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"edit {second}"])

    first = rebase_amend(str(three_commits.path), message="once")
    again = rebase_amend(str(three_commits.path), message="twice")
    assert first.after.sha != again.after.sha
    assert status(str(three_commits.path)).can_amend


def test_amending_never_stages_untracked_files(three_commits: Scratch) -> None:
    """`git add -A` here would sweep scratch files, stray binaries and local
    notes into history, and the amend would report success either way."""
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"edit {second}"])
    three_commits.write("tracked.txt", "edited\n")
    three_commits.git.run("add", "tracked.txt")
    three_commits.git.run("commit", "-q", "--amend", "--no-edit")  # now tracked
    three_commits.write("tracked.txt", "edited again\n")
    three_commits.write("my-scratch-notes.txt", "not for history\n")

    rebase_amend(str(three_commits.path), stage_tracked=True)

    committed = three_commits.git.lines("show", "--name-only", "--format=", "HEAD")
    assert "tracked.txt" in committed
    assert "my-scratch-notes.txt" not in committed
    assert three_commits.read("my-scratch-notes.txt") == "not for history\n"


def test_staging_then_proceeding_folds_the_change_in_without_an_amend(
    scratch: Scratch,
) -> None:
    """The shorter path, which git already provides: `rebase --continue` at an
    `edit` stop puts whatever is staged into the commit the step applied.

    Tested because the guidance now says so. It was true before and nothing
    said it, so the two-call route read as required.
    """
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.commit("adds c", c="c\n")
    b = scratch.git.out("rev-parse", "HEAD~1")
    c = scratch.git.out("rev-parse", "HEAD")
    rebase_start("HEAD~2", str(scratch.path), [f"edit {b}", f"pick {c}"])

    (scratch.path / "b").write_text("b, corrected\n")
    scratch.git.run("add", "--", "b")
    assert "calling proceed folds them into it" in status(str(scratch.path)).guidance

    proceed(str(scratch.path))
    # Folded into "adds b" itself, not left as a commit of its own.
    assert scratch.git.out("show", "HEAD~1:b") == "b, corrected"
    assert scratch.git.lines("log", "--format=%s", "HEAD~2..HEAD") == ["adds c", "adds b"]


def test_an_untracked_file_beside_what_was_staged_is_named(three_commits: Scratch) -> None:
    """The rule above is right and reads as a trap exactly once: when the file the
    amended commit needs is the new one, and the amend reports success without it.

    Naming it costs nothing and changes nothing -- the file is still not staged.
    """
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"edit {second}"])
    three_commits.write("pkg/tracked.txt", "edited\n")
    three_commits.git.run("add", "pkg/tracked.txt")
    three_commits.git.run("commit", "-q", "--amend", "--no-edit")  # now tracked
    three_commits.write("pkg/tracked.txt", "imports the new module\n")
    three_commits.write("pkg/new_module.txt", "the module it imports\n")

    report = rebase_amend(str(three_commits.path), stage_tracked=True)

    assert report.beside == ("pkg/new_module.txt",)
    assert "pkg/new_module.txt" in report.guidance
    # Named, not staged: the rule is unchanged
    committed = three_commits.git.lines("show", "--name-only", "--format=", "HEAD")
    assert "pkg/new_module.txt" not in committed


def test_untracked_files_elsewhere_are_not_named(three_commits: Scratch) -> None:
    """A working tree accumulates scratch wherever it was dropped. Reporting all of
    it would bury the one file that matters, so only the staged directories count."""
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"edit {second}"])
    three_commits.write("pkg/tracked.txt", "edited\n")
    three_commits.git.run("add", "pkg/tracked.txt")
    three_commits.git.run("commit", "-q", "--amend", "--no-edit")
    three_commits.write("pkg/tracked.txt", "edited again\n")
    three_commits.write("notes.txt", "dropped at the root\n")
    three_commits.write("other/stray.bin", "elsewhere entirely\n")

    report = rebase_amend(str(three_commits.path), stage_tracked=True)

    assert report.beside == ()
    assert "notes.txt" not in report.guidance


def test_nothing_is_named_when_nothing_was_staged(three_commits: Scratch) -> None:
    """Without stage_tracked the caller staged by hand, and has already decided."""
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"edit {second}"])
    three_commits.write("beside.txt", "untracked\n")

    report = rebase_amend(str(three_commits.path), message="reworded")

    assert report.beside == ()
