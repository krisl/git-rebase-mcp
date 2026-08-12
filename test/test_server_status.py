"""Tests for the status tool.

The tools are ordinary functions with a decorator, so they are called directly
here rather than over the protocol. What the protocol adds -- schemas, transport
-- is the SDK's business, not this project's.
"""

from __future__ import annotations

import asyncio

import pytest

from git_rebase_mcp.server import mcp, rebase_finish, rebase_start, status

from scratch import Scratch


@pytest.fixture
def three_commits(scratch: Scratch) -> Scratch:
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    return scratch


@pytest.fixture
def separate_files(scratch: Scratch) -> Scratch:
    """Three commits that can be reordered without conflicting."""
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.commit("adds c", c="c\n")
    return scratch


def test_reports_no_rebase(three_commits: Scratch) -> None:
    report = status(str(three_commits.path))
    assert report.state == "not_rebasing"
    assert report.head.subject == "third"
    assert not report.can_amend


def test_a_conflicted_stop_refuses_to_encourage_amending(three_commits: Scratch) -> None:
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"edit {third}"], onto="HEAD~2")

    report = status(str(three_commits.path))
    assert report.state == "conflicted"
    assert not report.head_is_replaying_commit
    assert not report.can_amend
    assert report.conflicted_files == ("f",)
    assert report.head.subject == "base"
    assert report.replaying is not None and report.replaying.subject == "third"


def test_an_applied_stop_says_amending_is_safe(three_commits: Scratch) -> None:
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"edit {second}"])

    report = status(str(three_commits.path))
    assert report.state == "stopped_after_apply"
    assert report.head_is_replaying_commit
    assert report.can_amend
    assert report.step is not None and report.step.total == 1


def test_the_guidance_names_the_risk_rather_than_only_the_state(three_commits: Scratch) -> None:
    """A caller that reads one field reads this one, so it has to be the field
    that says what goes wrong."""
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"edit {third}"], onto="HEAD~2")

    guidance = status(str(three_commits.path)).guidance
    assert "amending would rewrite the wrong commit" in guidance
    assert "does not exist yet" in guidance


def test_a_finished_rebase_is_not_reported_as_nothing_having_happened(
    separate_files: Scratch,
) -> None:
    """The state name answers "is a rebase running", and after a clean run the
    answer is no -- which on its own reads as though the call did nothing, at the
    one moment the branch has been rewritten and nothing has checked it."""
    b, c = separate_files.git.out("rev-parse", "HEAD~1"), separate_files.git.out(
        "rev-parse", "HEAD"
    )
    rebase_start("HEAD~2", str(separate_files.path), [f"pick {c}", f"pick {b}"])

    report = status(str(separate_files.path))
    assert report.state == "not_rebasing"  # still the right answer to that question
    assert report.finished is not None
    assert report.finished.rewritten == 2
    assert report.finished.branch_moved
    assert "Rebase finished" in report.guidance
    assert "rebase_finish" in report.guidance
    assert report.finished.backup_ref in report.guidance


def test_an_untouched_repository_reports_no_finished_rebase(three_commits: Scratch) -> None:
    report = status(str(three_commits.path))
    assert report.finished is None
    assert report.guidance == "No rebase in progress."


def test_the_finished_report_stops_once_it_has_been_checked(separate_files: Scratch) -> None:
    """rebase_finish clears the session, which is what says the rewrite has been
    verified. Without that the report would keep asking for a check already done."""
    b, c = separate_files.git.out("rev-parse", "HEAD~1"), separate_files.git.out(
        "rev-parse", "HEAD"
    )
    rebase_start("HEAD~2", str(separate_files.path), [f"pick {c}", f"pick {b}"])
    rebase_finish(str(separate_files.path))

    report = status(str(separate_files.path))
    assert report.finished is None
    assert report.guidance == "No rebase in progress."


def test_a_rebase_abandoned_by_hand_does_not_claim_to_have_finished(
    three_commits: Scratch,
) -> None:
    """`git rebase --abort` ends a rebase exactly as finishing it does, so the
    session outliving it cannot mean the branch was rewritten. Where the branch
    is says which happened."""
    second, third = (
        three_commits.git.out("rev-parse", "HEAD~1"),
        three_commits.git.out("rev-parse", "HEAD"),
    )
    tip = three_commits.git.out("rev-parse", "HEAD")
    rebase_start("HEAD~2", str(three_commits.path), [f"pick {third}", f"pick {second}"])
    three_commits.git.run("rebase", "--abort")

    report = status(str(three_commits.path))
    assert three_commits.git.out("rev-parse", "HEAD") == tip
    assert report.finished is not None
    assert not report.finished.branch_moved
    assert "exactly where it started" in report.guidance
    assert "abandoned outside this server" in report.guidance
    assert "Rebase finished" not in report.guidance


def test_the_count_is_the_commits_left_not_the_steps_run(scratch: Scratch) -> None:
    """A fixup consumes two commits and leaves one. The number worth reporting is
    the one the caller can go and read on the branch."""
    for value in ("one", "two", "three"):
        scratch.write("f", value + "\n")
        scratch.commit(f"f={value}")
    second, third = scratch.git.out("rev-parse", "HEAD~1"), scratch.git.out("rev-parse", "HEAD")
    rebase_start("HEAD~2", str(scratch.path), [f"pick {second}", f"fixup {third}"])

    report = status(str(scratch.path))
    assert report.finished is not None
    assert report.finished.rewritten == 1
    assert "1 commit between" in report.guidance


def test_a_missing_repository_is_rejected_clearly(tmp_path) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        status(str(tmp_path / "nowhere"))


def test_the_tool_is_registered_with_a_description() -> None:
    tools = asyncio.run(mcp.list_tools())
    registered = {tool.name: tool for tool in tools}
    assert "status" in registered
    assert registered["status"].description
    assert "repo" in registered["status"].input_schema["properties"]


def test_a_stopped_fixup_chain_is_not_reported_as_the_replayed_commit(
    scratch: Scratch,
) -> None:
    """HEAD is the accumulation of the fixups so far, and its subject is still
    git's raw template, so the report must not present it as the commit."""
    for value in ("one", "two", "three", "four"):
        scratch.write("f", value + "\n")
        scratch.commit(f"f={value}")
    first, last = scratch.git.out("rev-parse", "HEAD~2"), scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~3", [f"pick {first}", f"fixup {last}"])
    scratch.write("f", "resolved\n")
    scratch.git.run("add", "f")

    report = status(str(scratch.path))
    assert report.state == "stopped_after_apply"
    assert not report.head_is_replaying_commit
    assert report.can_amend  # git does mean HEAD; it is just not the replayed commit
    assert "not " + last[:9] in report.guidance
    assert "ignore the subject" in report.guidance
