"""Tests for the finish check knowing that a fold-in was deliberate.

`_why_the_change_might_be_meant` used to end at "something was lost or resolved
wrongly -- or was changed on purpose by hand, which this run has no record of".
Staging a change at an `edit` stop and continuing is one such change, and it is
the one the README recommends over `rebase_amend` -- "staging it and calling
proceed does that" -- so the tool was reading its own advice as damage.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.invariants import load_session
from git_rebase_mcp.server import proceed, rebase_amend, rebase_finish, rebase_start

from scratch import Scratch


@pytest.fixture
def series(scratch: Scratch) -> Scratch:
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
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
