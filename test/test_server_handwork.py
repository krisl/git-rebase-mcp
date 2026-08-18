"""Tests for the finish check knowing what the caller did on purpose.

`_why_the_change_might_be_meant` used to end at "something was lost or resolved
wrongly -- or was changed on purpose by hand, which this run has no record of".
The two ways a caller changes a commit deliberately are the two this records: it
resolved a conflict, or it staged something at an `edit` stop and continued.

The second is the one the README recommends over `rebase_amend` -- "staging it
and calling proceed does that" -- so the tool was reading its own advice as
damage.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.invariants import Session, load_session
from git_rebase_mcp.server import (
    _why_the_change_might_be_meant,
    proceed,
    rebase_amend,
    rebase_finish,
    rebase_start,
)

from scratch import Scratch


@pytest.fixture
def series(scratch: Scratch) -> Scratch:
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    return scratch


@pytest.fixture
def diverged(scratch: Scratch) -> Scratch:
    """A sibling branch that touched the same file, so rebasing onto it
    conflicts -- which `rebase_start` can set up, unlike an `--onto`."""
    scratch.commit("base", f="one\n")
    scratch.git.run("branch", "side")
    scratch.commit("second", f="two\n")
    scratch.git.run("checkout", "-q", "side")
    scratch.commit("side edits f", f="side\n")
    scratch.git.run("checkout", "-q", "main")
    return scratch


class TestStagingAtAnEditStop:
    """The documented alternative to rebase_amend."""

    def test_it_is_recorded_as_an_amend(self, series: Scratch) -> None:
        rebase_start("HEAD~2", str(series.path), edit=["HEAD~1"])
        series.write("extra", "folded in\n")
        series.git.run("add", "extra")
        proceed(str(series.path))

        session = load_session(series.git)
        assert session is not None and len(session.amended) == 1

    def test_the_finish_explains_it_rather_than_calling_it_damage(
        self, series: Scratch
    ) -> None:
        rebase_start("HEAD~2", str(series.path), edit=["HEAD~1"])
        series.write("extra", "folded in\n")
        series.git.run("add", "extra")
        proceed(str(series.path))

        report = rebase_finish(str(series.path))
        assert report.ok is False  # a difference, still worth confirming
        assert "which is what amending does" in report.guidance
        assert "was lost or resolved wrongly" not in report.guidance

    def test_an_ordinary_continue_records_nothing(self, series: Scratch) -> None:
        """Nothing staged means no content changed, so there is nothing to
        explain and claiming otherwise would make the check vaguer."""
        rebase_start("HEAD~2", str(series.path), edit=["HEAD~1"])
        proceed(str(series.path))

        session = load_session(series.git)
        assert session is not None and session.amended == ()

    def test_amending_then_staging_the_same_step_is_one_entry(
        self, series: Scratch
    ) -> None:
        """`rebase_amend` records the step; so does staging and continuing. The
        step is what is named, so doing both is still one commit changed."""
        rebase_start("HEAD~2", str(series.path), edit=["HEAD~1"])
        rebase_amend(str(series.path), message="second, reworded")
        series.write("extra", "folded in\n")
        series.git.run("add", "extra")
        proceed(str(series.path))

        session = load_session(series.git)
        assert session is not None and len(session.amended) == 1


class TestResolvingAConflict:

    def test_it_is_recorded(self, diverged: Scratch) -> None:
        rebase_start("side", str(diverged.path))
        diverged.write("f", "composed\n")
        diverged.git.run("add", "f")
        proceed(str(diverged.path))

        session = load_session(diverged.git)
        assert session is not None and len(session.resolved) == 1

    def test_the_finish_names_resolving_as_the_reason(self, diverged: Scratch) -> None:
        rebase_start("side", str(diverged.path))
        diverged.write("f", "composed\n")
        diverged.git.run("add", "f")
        proceed(str(diverged.path))

        report = rebase_finish(str(diverged.path))
        assert "which is what resolving does" in report.guidance
        assert "was lost or resolved wrongly" not in report.guidance

    def test_allowing_it_still_works(self, diverged: Scratch) -> None:
        rebase_start("side", str(diverged.path))
        diverged.write("f", "composed\n")
        diverged.git.run("add", "f")
        proceed(str(diverged.path))

        assert rebase_finish(str(diverged.path), allow_change=True).ok is True


def _session(**kwargs: object) -> Session:
    return Session(
        backup_ref="rebase-backup/x",
        backup_sha="a" * 40,
        backup_tree="b" * 40,
        base="main",
        base_sha="c" * 40,
        **kwargs,  # type: ignore[arg-type]
    )


class TestWhichReadingIsOffered:
    """Tested on the function rather than through a repository: the rule is about
    precedence between three readings, and building a rebase that produces each
    combination says more about git than about the rule."""

    def test_amending_comes_before_resolving(self) -> None:
        """The narrower claim: a named commit was deliberately rewritten. A
        rebase that also had conflicts is better explained by that."""
        said = _why_the_change_might_be_meant(
            _session(amended=("d" * 40,), resolved=("e" * 40,)), unchanged_tree=False
        )
        assert "what amending does" in said

    def test_resolving_comes_before_an_unchanged_tree(self) -> None:
        said = _why_the_change_might_be_meant(
            _session(resolved=("e" * 40,)), unchanged_tree=True
        )
        assert "what resolving does" in said

    def test_with_neither_the_old_reading_stands(self) -> None:
        """The honest answer when the run really has no record: it might be
        damage. What changed is only how often that sentence is reached."""
        said = _why_the_change_might_be_meant(_session(), unchanged_tree=False)
        assert "was lost or resolved wrongly" in said
