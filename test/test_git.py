"""Tests for the git command wrapper."""

from __future__ import annotations

import pytest

from git_rebase_mcp.git import Git, GitError

from scratch import Scratch


def test_out_strips_the_trailing_newline(scratch: Scratch) -> None:
    scratch.commit("first", a="one\n")
    assert scratch.git.out("log", "--format=%s") == "first"


def test_lines_splits_output(scratch: Scratch) -> None:
    scratch.commit("first", a="one\n")
    scratch.commit("second", a="two\n")
    assert scratch.git.lines("log", "--format=%s") == ["second", "first"]


def test_lines_is_empty_rather_than_one_empty_string(scratch: Scratch) -> None:
    """"".split("\\n") is [""], which reads as one result when there are none."""
    scratch.commit("first", a="one\n")
    assert scratch.git.lines("log", "--format=%s", "--grep", "nothing matches this") == []


def test_a_failing_command_raises_by_default(scratch: Scratch) -> None:
    with pytest.raises(GitError) as caught:
        scratch.git.run("rev-parse", "does-not-exist")
    assert "rev-parse" in str(caught.value)
    assert caught.value.result.returncode != 0


def test_check_false_returns_the_failure_instead(scratch: Scratch) -> None:
    result = scratch.git.run("rev-parse", "does-not-exist", check=False)
    assert not result.ok
    assert result.args == ("rev-parse", "does-not-exist")


def test_succeeds_answers_without_raising(scratch: Scratch) -> None:
    scratch.commit("first", a="one\n")
    assert scratch.git.succeeds("rev-parse", "--verify", "HEAD")
    assert not scratch.git.succeeds("rev-parse", "--verify", "no-such-ref")


def test_the_error_message_carries_gits_own_explanation(scratch: Scratch) -> None:
    """A wrapper that swallows git's diagnosis is worse than no wrapper."""
    with pytest.raises(GitError) as caught:
        scratch.git.run("checkout", "no-such-branch")
    assert "no-such-branch" in str(caught.value)


def test_commands_run_in_the_given_repository(scratch: Scratch, tmp_path_factory) -> None:
    import subprocess

    other = tmp_path_factory.mktemp("other")
    subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
    scratch.commit("in scratch", a="one\n")
    assert Git(other).lines("log", "--format=%s", "--all") == []
    assert scratch.git.lines("log", "--format=%s") == ["in scratch"]


def test_stdin_is_passed_through(scratch: Scratch) -> None:
    scratch.commit("first", a="one\n")
    sha = scratch.git.out("rev-parse", "HEAD")
    assert scratch.git.run("cat-file", "--batch-check", stdin=sha).stdout.startswith(sha)
