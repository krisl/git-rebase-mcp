"""Tests for starting a rebase."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_rebase_mcp.git import GitError
from git_rebase_mcp.invariants import load_session
from git_rebase_mcp.server import rebase_start, status

from scratch import Scratch


@pytest.fixture
def series(scratch: Scratch) -> Scratch:
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.commit("adds c", c="c\n")
    return scratch


def test_a_reorder_runs_to_completion(series: Scratch) -> None:
    b, c = series.git.out("rev-parse", "HEAD~1"), series.git.out("rev-parse", "HEAD")
    report = rebase_start("HEAD~2", str(series.path), [f"pick {c}", f"pick {b}"])

    assert report.status.state == "not_rebasing"
    assert series.subjects() == ["adds b", "adds c", "base"]


def test_a_repo_path_containing_a_quote_still_drives_the_todo(tmp_path: Path) -> None:
    """The todo is fed to git through a shell command, so the path has to be
    quoted the way a shell would quote it. A repo named `re'po` used to make
    the editor command fail to parse, and the rebase silently did nothing."""
    repo = Scratch(tmp_path / "re'po")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo.path)], check=True)
    repo.git.run("config", "user.name", "Test")
    repo.git.run("config", "user.email", "test@example.com")
    repo.commit("base", a="one\n")
    repo.commit("adds b", b="two\n")
    repo.commit("adds c", c="three\n")
    adds_b, adds_c = repo.git.out("rev-parse", "HEAD~1"), repo.git.out("rev-parse", "HEAD")

    report = rebase_start("HEAD~2", str(repo.path), [f"pick {adds_c}", f"pick {adds_b}"])
    assert report.status.state == "not_rebasing"
    assert repo.subjects() == ["adds b", "adds c", "base"]  # the todo really was used


def test_the_tip_is_tagged_before_anything_changes(series: Scratch) -> None:
    tip = series.git.out("rev-parse", "HEAD")
    b, c = series.git.out("rev-parse", "HEAD~1"), series.git.out("rev-parse", "HEAD")
    report = rebase_start("HEAD~2", str(series.path), [f"pick {c}", f"pick {b}"])

    assert series.git.out("rev-parse", report.backup_ref) == tip


def test_the_session_records_what_to_check_against(series: Scratch) -> None:
    b, c = series.git.out("rev-parse", "HEAD~1"), series.git.out("rev-parse", "HEAD")
    rebase_start("HEAD~2", str(series.path), [f"pick {c}", f"pick {b}"],
                 check_command="true")

    session = load_session(series.git)
    assert session is not None
    assert session.base == "HEAD~2"
    assert session.check_command == "true"


def test_an_unsafe_plan_is_refused(series: Scratch) -> None:
    c = series.git.out("rev-parse", "HEAD")
    with pytest.raises(ValueError, match="Refusing to start"):
        rebase_start("HEAD~2", str(series.path), [f"pick {c}"])  # "adds b" left out

    assert status(str(series.path)).state == "not_rebasing"
    assert series.subjects() == ["adds c", "adds b", "base"]  # untouched


def test_a_rebase_git_never_started_is_reported_as_a_failure(scratch: Scratch) -> None:
    """A dirty tree stops git before the rebase begins, and force is the one
    path past the preflight refusal -- so it is where git can fail having been
    told to start. It used to report 'Started' with no rebase in progress, and
    to leave a session behind for a rebase that never ran."""
    scratch.commit("base", a="one\n")
    scratch.commit("second", b="two\n")
    scratch.write("a", "uncommitted\n")  # unstaged change in a tracked file

    with pytest.raises(GitError, match="unstaged changes"):
        rebase_start("HEAD~1", str(scratch.path), force=True)

    assert load_session(scratch.git) is None  # nothing left to check later
    assert scratch.read("a") == "uncommitted\n"  # and nothing was moved


def test_force_overrides_the_refusal(series: Scratch) -> None:
    """Dropping a commit on purpose has to remain possible."""
    c = series.git.out("rev-parse", "HEAD")
    rebase_start("HEAD~2", str(series.path), [f"pick {c}"], force=True)
    assert series.subjects() == ["adds c", "base"]


def test_stopping_on_a_conflict_is_reported_not_raised(scratch: Scratch) -> None:
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    second, third = scratch.git.out("rev-parse", "HEAD~1"), scratch.git.out("rev-parse", "HEAD")

    report = rebase_start("HEAD~2", str(scratch.path), [f"pick {third}", f"pick {second}"])
    assert report.status.state == "conflicted"
    assert report.status.conflicted_files == ("f",)


def test_a_colliding_untracked_file_is_moved_aside(scratch: Scratch) -> None:
    """Otherwise the rebase refuses to begin, over a file nobody asked about."""
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.git.run("rm", "-q", "b")
    scratch.git.run("commit", "-q", "-m", "removes b")
    scratch.write("b", "local scratch of my own\n")
    adds_b = scratch.git.out("rev-parse", "HEAD~1")
    removes_b = scratch.git.out("rev-parse", "HEAD")

    report = rebase_start("HEAD~2", str(scratch.path),
                          [f"pick {adds_b}", f"pick {removes_b}"])

    assert report.stashed == ("b",)
    assert report.status.state == "not_rebasing"
    assert scratch.git.lines("stash", "list")  # still recoverable


def test_unrelated_untracked_files_are_left_alone(series: Scratch) -> None:
    series.write("notes.txt", "mine\n")
    b, c = series.git.out("rev-parse", "HEAD~1"), series.git.out("rev-parse", "HEAD")

    report = rebase_start("HEAD~2", str(series.path), [f"pick {c}", f"pick {b}"])
    assert report.stashed == ()
    assert series.read("notes.txt") == "mine\n"


def test_a_check_command_that_fails_stops_the_rebase(series: Scratch) -> None:
    """The only thing that catches a commit which applies cleanly but is broken."""
    b, c = series.git.out("rev-parse", "HEAD~1"), series.git.out("rev-parse", "HEAD")
    report = rebase_start("HEAD~2", str(series.path), [f"pick {b}", f"pick {c}"],
                          check_command="false")

    assert report.status.state == "stopped_without_apply"
    assert report.status.action == "exec"


def test_a_check_command_that_passes_does_not(series: Scratch) -> None:
    b, c = series.git.out("rev-parse", "HEAD~1"), series.git.out("rev-parse", "HEAD")
    report = rebase_start("HEAD~2", str(series.path), [f"pick {b}", f"pick {c}"],
                          check_command="true")
    assert report.status.state == "not_rebasing"


def test_the_base_is_recorded_as_a_resolved_sha(series: Scratch) -> None:
    """`HEAD~2` names a different commit once history has been rewritten, so
    the spelling cannot be used to name the range afterwards."""
    base_before = series.git.out("rev-parse", "HEAD~2")
    b, c = series.git.out("rev-parse", "HEAD~1"), series.git.out("rev-parse", "HEAD")
    rebase_start("HEAD~2", str(series.path), [f"pick {c}", f"pick {b}"])

    session = load_session(series.git)
    assert session is not None
    assert session.base == "HEAD~2"
    assert session.base_sha == base_before
    assert series.git.out("rev-parse", "HEAD~2") == base_before  # unchanged here...
    assert session.base_sha != series.git.out("rev-parse", "HEAD")


def test_git_own_words_are_kept_when_it_stops(scratch: Scratch) -> None:
    """Git explains a stop in ways nothing else can reconstruct. Swallowing that
    left a real run inscrutable for several minutes."""
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    second, third = scratch.git.out("rev-parse", "HEAD~1"), scratch.git.out("rev-parse", "HEAD")

    report = rebase_start("HEAD~2", str(scratch.path), [f"pick {third}", f"pick {second}"])
    assert report.status.state == "conflicted"
    assert "CONFLICT" in report.status.git_said


def test_progress_ticks_and_generic_advice_are_left_out(series: Scratch) -> None:
    """"Rebasing (2/20)" and git's hints are length without meaning."""
    b, c = series.git.out("rev-parse", "HEAD~1"), series.git.out("rev-parse", "HEAD")
    report = rebase_start("HEAD~2", str(series.path), [f"pick {c}", f"pick {b}"])
    assert "Rebasing (" not in report.status.git_said
    assert "hint:" not in report.status.git_said


def test_a_plain_status_carries_no_git_output(series: Scratch) -> None:
    """Nothing was run, so there is nothing for git to have said."""
    assert status(str(series.path)).git_said == ""


def test_autosquash_folds_fixups_into_their_targets(scratch: Scratch) -> None:
    """The workflow `git commit --fixup` sets up, which the README recommends."""
    scratch.commit("base", a="one\n")
    scratch.commit("Add the thing", b="thing\n")
    scratch.commit("Add another thing", c="other\n")
    scratch.commit("fixup! Add the thing", b="thing, fixed\n")

    report = rebase_start("HEAD~3", str(scratch.path), autosquash=True,
                          check_command="test -f b")
    assert report.status.state == "not_rebasing"
    assert scratch.subjects("HEAD~2..HEAD") == ["Add another thing", "Add the thing"]
    assert scratch.read("b") == "thing, fixed\n"


def test_autosquash_and_a_todo_together_are_refused(scratch: Scratch) -> None:
    scratch.commit("base", a="one\n")
    scratch.commit("second", b="two\n")
    second = scratch.git.out("rev-parse", "HEAD")
    with pytest.raises(ValueError, match="cannot be given one"):
        rebase_start("HEAD~1", str(scratch.path), todo=[f"pick {second}"], autosquash=True)


def test_a_resolution_is_replayed_when_the_same_conflict_comes_back(scratch: Scratch) -> None:
    """A rebase that is retried hits the identical conflicts a second time.
    Without rerere they are resolved again by hand for nothing."""
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    before = scratch.git.out("rev-parse", "HEAD")
    second, third = scratch.git.out("rev-parse", "HEAD~1"), scratch.git.out("rev-parse", "HEAD")
    todo = [f"pick {third}", f"pick {second}"]

    assert rebase_start("HEAD~2", str(scratch.path), todo).status.state == "conflicted"
    scratch.write("f", "resolved by hand\n")
    scratch.git.run("add", "f")
    scratch.git.run("-c", "core.editor=true", "-c", "rerere.enabled=true",
                    "rebase", "--continue", check=False)
    scratch.git.run("rebase", "--abort", check=False)
    scratch.git.run("reset", "-q", "--hard", before)

    assert rebase_start("HEAD~2", str(scratch.path), todo).status.state == "conflicted"
    assert scratch.read("f") == "resolved by hand\n"  # replayed, no markers
