"""Tests for comparing the replayed commits against the originals, mid-rebase.

`rebase_finish` answers this at the end. The question is asked at every stop:
"was that commit replayed faithfully?" A test suite cannot answer it -- it reports
the branch's own state, not whether this replay matches what it replayed -- so a
real seven-commit rebase had `git range-diff` built by hand at all eight stops.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.server import proceed, rebase_amend, rebase_compare, rebase_start

from scratch import Scratch


@pytest.fixture
def series(scratch: Scratch) -> Scratch:
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    scratch.commit("adds c", c="c\n")
    scratch.commit("adds d", d="d\n")
    return scratch


def test_a_faithful_replay_reports_nothing_changed(series: Scratch) -> None:
    rebase_start("HEAD~3", str(series.path), edit_every=True)

    report = rebase_compare(str(series.path))
    assert report.changed == ()
    assert report.replayed == 1
    assert report.total == 3
    assert "carry the patch they came from" in report.guidance


def test_the_tail_not_yet_reached_is_pending_not_lost(series: Scratch) -> None:
    """It pairs as "dropped" because it is genuinely not there. Calling that a
    difference would make the report unreadable at every stop but the last."""
    rebase_start("HEAD~3", str(series.path), edit_every=True)

    report = rebase_compare(str(series.path))
    assert len(report.pending) == 2
    assert report.changed == ()
    assert "2 still to come" in report.guidance


def test_a_commit_changed_on_purpose_is_named(series: Scratch) -> None:
    """A whole new file is more than range-diff's creation threshold, so it
    cannot pair the two and reports a drop and an add of the same subject rather
    than one "changed". That is its own reading and this passes it on: collapsing
    the pair would also collapse a genuine drop and add that shared a subject."""
    rebase_start("HEAD~3", str(series.path), edit_every=True)
    series.write("extra", "folded in\n")
    series.git.run("add", "extra")
    rebase_amend(str(series.path))

    report = rebase_compare(str(series.path))
    assert {c.subject for c in report.changed} == {"adds b"}
    assert sorted(c.status for c in report.changed) == ["added", "dropped"]
    assert "unless you made it" in report.guidance


def test_a_small_change_pairs_as_changed(scratch: Scratch) -> None:
    """Within the threshold the same commit is reported once, as altered.

    The alteration has to be small *relative to the commit's own diff*, which is
    what the threshold measures. Two patches that share nothing -- a one-line
    commit whose one line is rewritten -- are two patches, and range-diff says so.
    """
    scratch.commit("base", f="line 0\n")
    scratch.commit("grows f", f="".join(f"line {n}\n" for n in range(20)))
    rebase_start("HEAD~1", str(scratch.path), edit_every=True)

    scratch.write("f", "".join(f"line {n}\n" for n in range(21)))
    rebase_amend(str(scratch.path), stage_tracked=True)

    report = rebase_compare(str(scratch.path))
    assert [(c.subject, c.status) for c in report.changed] == [("grows f", "changed")]
    assert "1 differ" in report.guidance


def test_it_keeps_up_as_the_rebase_goes_on(series: Scratch) -> None:
    rebase_start("HEAD~3", str(series.path), edit_every=True)
    seen = []
    for _ in range(3):
        report = rebase_compare(str(series.path))
        seen.append((report.replayed, len(report.pending), len(report.changed)))
        proceed(str(series.path))
    assert seen == [(1, 2, 0), (2, 1, 0), (3, 0, 0)]


def test_the_detail_is_only_sent_when_asked_for(series: Scratch) -> None:
    rebase_start("HEAD~3", str(series.path), edit_every=True)

    assert rebase_compare(str(series.path)).detail is None
    assert rebase_compare(str(series.path), include_diff=True).detail is not None


def test_it_works_at_a_conflicted_stop(scratch: Scratch) -> None:
    """The commit part-way through is simply not in the comparison yet."""
    scratch.commit("base", f="one\n")
    scratch.git.run("branch", "side")
    scratch.commit("mine", f="two\n")
    scratch.git.run("checkout", "-q", "side")
    scratch.commit("theirs", f="three\n")
    scratch.git.run("checkout", "-q", "main")
    rebase_start("side", str(scratch.path))

    report = rebase_compare(str(scratch.path))
    assert report.replayed == 0
    assert report.total == 1
    assert len(report.pending) == 1
    assert report.changed == ()


def test_it_refuses_without_a_rebase_of_its_own(series: Scratch) -> None:
    with pytest.raises(ValueError, match="nothing to compare against"):
        rebase_compare(str(series.path))
