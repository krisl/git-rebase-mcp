"""Tests for the rebase_status tool.

The tools are ordinary functions with a decorator, so they are called directly
here rather than over the protocol. What the protocol adds -- schemas, transport
-- is the SDK's business, not this project's.
"""

from __future__ import annotations

import asyncio

import pytest

from git_rebase_mcp.server import mcp, rebase_status

from scratch import Scratch


@pytest.fixture
def three_commits(scratch: Scratch) -> Scratch:
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    return scratch


def test_reports_no_rebase(three_commits: Scratch) -> None:
    report = rebase_status(str(three_commits.path))
    assert report.state == "not_rebasing"
    assert report.head.subject == "third"
    assert not report.can_amend


def test_a_conflicted_stop_refuses_to_encourage_amending(three_commits: Scratch) -> None:
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"edit {third}"], onto="HEAD~2")

    report = rebase_status(str(three_commits.path))
    assert report.state == "conflicted"
    assert not report.head_is_replaying_commit
    assert not report.can_amend
    assert report.conflicted_files == ("f",)
    assert report.head.subject == "base"
    assert report.replaying is not None and report.replaying.subject == "third"


def test_an_applied_stop_says_amending_is_safe(three_commits: Scratch) -> None:
    second = three_commits.git.out("rev-parse", "HEAD~1")
    three_commits.start_rebase("HEAD~2", [f"edit {second}"])

    report = rebase_status(str(three_commits.path))
    assert report.state == "stopped_after_apply"
    assert report.head_is_replaying_commit
    assert report.can_amend
    assert report.step is not None and report.step.total == 1


def test_the_guidance_names_the_risk_rather_than_only_the_state(three_commits: Scratch) -> None:
    """A caller that reads one field reads this one, so it has to be the field
    that says what goes wrong."""
    third = three_commits.git.out("rev-parse", "HEAD")
    three_commits.start_rebase("HEAD~1", [f"edit {third}"], onto="HEAD~2")

    guidance = rebase_status(str(three_commits.path)).guidance
    assert "amending would rewrite the wrong commit" in guidance
    assert "does not exist yet" in guidance


def test_a_missing_repository_is_rejected_clearly(tmp_path) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        rebase_status(str(tmp_path / "nowhere"))


def test_the_tool_is_registered_with_a_description() -> None:
    tools = asyncio.run(mcp.list_tools())
    registered = {tool.name: tool for tool in tools}
    assert "rebase_status" in registered
    assert registered["rebase_status"].description
    assert "repo" in registered["rebase_status"].input_schema["properties"]
