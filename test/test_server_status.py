"""Tests for the status tool.

The tools are ordinary functions with a decorator, so they are called directly
here rather than over the protocol. What the protocol adds -- schemas, transport
-- is the SDK's business, not this project's.
"""

from __future__ import annotations

import asyncio

import pytest

from git_rebase_mcp.server import mcp, rebase_amend, rebase_finish, rebase_start, status

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


def test_an_edit_stop_of_a_rewritten_commit_is_not_reported_as_a_fixup_run(
    scratch: Scratch,
) -> None:
    """Found by using it. A commit whose parent moved is applied under a new sha,
    so comparing HEAD against the commit being replayed says "different" on the
    most ordinary stop there is -- and the report then explained the difference
    as a run of fixup steps, of which there were none, and said the message was
    git's template, which it was not."""
    scratch.commit("base", f="one\n")
    scratch.git.run("checkout", "-q", "-b", "up")
    scratch.commit("upstream", other="x\n")
    upstream = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "main")
    scratch.commit("second", f="two\n")
    second = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~1", [f"edit {second}"], onto=upstream)

    report = status(str(scratch.path))
    assert report.state == "stopped_after_apply"
    assert report.head.sha != second  # the rebase had to rewrite it
    assert report.head_is_replaying_commit
    assert report.can_amend
    assert "fixup" not in report.guidance
    assert "template" not in report.guidance
    # Both shas, so `replaying` cannot read as a commit that failed to apply.
    assert second[:9] in report.guidance
    assert report.head.sha[:9] in report.guidance


def test_a_commit_taken_back_out_is_not_reported_as_amendable(
    three_commits: Scratch,
) -> None:
    """Git leaves its "you may amend" record in place through a mixed reset, so
    the state alone still says amending is safe -- of a HEAD that is now the
    commit before the one this step applied. That reset is how a commit is split,
    and it is a thing people do to a stopped rebase by hand."""
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"edit {second}"])
    three_commits.git.run("reset", "-q", "HEAD^")

    report = status(str(three_commits.path))
    assert report.unapplied
    assert not report.can_amend
    assert not report.head_is_replaying_commit
    assert "taken back out" in report.guidance
    assert "proceed" in report.guidance


def test_amending_a_commit_that_was_taken_back_out_is_refused(
    three_commits: Scratch,
) -> None:
    """The refusal that field exists for: amending here rewrites the commit
    before the one the caller has in mind, and git reports it as success."""
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"edit {second}"])
    three_commits.git.run("reset", "-q", "HEAD^")
    before = three_commits.git.out("rev-parse", "HEAD")

    with pytest.raises(ValueError) as raised:
        rebase_amend(str(three_commits.path), message="would rewrite the wrong commit")

    assert "Refusing to amend" in str(raised.value)
    assert three_commits.git.out("rev-parse", "HEAD") == before


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


def test_a_report_says_which_checkout_it_is_about(separate_files: Scratch) -> None:
    """The field that exists because a shell got this wrong and a tool did not.

    A repository with worktrees has several checkouts of one history side by
    side, each on its own branch at its own commit.  Every tool here takes
    `repo` and is therefore always right; a shell in the same session drifts
    between directories, and `git show HEAD:file` then answers about whichever
    one it happens to be in.  Two answers that look alike and are about
    different trees is how a healthy rebase gets abandoned on a misreading.
    """
    separate_files.git.run("branch", "-q", "side")
    elsewhere = separate_files.path.parent / "elsewhere"
    separate_files.git.run("worktree", "add", "-q", str(elsewhere), "side")

    here = status(str(separate_files.path))
    there = status(str(elsewhere))

    assert here.worktree == str(separate_files.path)
    assert here.branch == "main"
    assert there.worktree == str(elsewhere)
    assert there.branch == "side"


def test_the_checkout_is_named_through_a_stop_too(separate_files: Scratch) -> None:
    """Not only when idle: the reports a caller reads mid-rebase are the ones
    it is comparing against its own `git` output."""
    b, c = (separate_files.git.out("rev-parse", r) for r in ("HEAD~1", "HEAD"))
    started = rebase_start(
        "HEAD~2", str(separate_files.path), [f"edit {b}", f"pick {c}"]
    )
    assert started.status.worktree == str(separate_files.path)
    assert started.status.branch == "main"


def test_a_report_built_without_a_repository_still_returns(
    separate_files: Scratch,
) -> None:
    """The stamp needs git, and the callers that pass none want the bare state.
    Empty rather than absent, so a reader never has to test for the field."""
    from git_rebase_mcp.server import _report
    from git_rebase_mcp.state import read_state

    report = _report(read_state(separate_files.git))
    assert report.worktree == ""
    assert report.branch == ""


def test_a_detached_head_that_is_not_a_rebase_names_no_branch(
    separate_files: Scratch,
) -> None:
    """The honest answer, since there is no branch to name.

    Guarding the fallback: reading `head-name` where no rebase wrote one must
    not resurrect a stale name from a previous operation.
    """
    separate_files.git.run("checkout", "-q", "--detach", "HEAD~1")
    report = status(str(separate_files.path))
    assert report.branch == ""
    assert report.worktree == str(separate_files.path)
