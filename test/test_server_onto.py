"""Tests for replaying a branch somewhere other than its upstream.

The case this exists for: a branch cut from a history that has since been
rewritten. The old upstream still says which commits are the branch's own; the
rewritten history no longer does, because the same work is there under other
shas. So the upstream and the landing place are two different commits, and
naming either one alone gets the range wrong.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.invariants import load_session
from git_rebase_mcp.server import rebase_finish, rebase_preflight, rebase_start, status

from scratch import Scratch


@pytest.fixture
def rewritten(scratch: Scratch) -> Scratch:
    """A branch on the old history, and a rewritten copy of that history.

        root ─ "shared work"        (upstream)  ─ "mine"  [work]
             └ "shared work, again" (landing)
    """
    scratch.commit("root", f="one\n")
    root = scratch.git.out("rev-parse", "HEAD")
    scratch.commit("shared work", u="up\n")
    scratch.git.run("branch", "upstream")
    scratch.git.run("checkout", "-q", "-b", "work")
    scratch.commit("mine", g="mine\n")

    scratch.git.run("checkout", "-q", "-b", "landing", root)
    # The same work, rewritten: a comment changed, so it no longer patch-matches.
    scratch.commit("shared work", u="up\n# reworded\n")

    scratch.git.run("checkout", "-q", "work")
    return scratch


def test_it_replays_only_the_branch_s_own_commits(rewritten: Scratch) -> None:
    rebase_start("upstream", str(rewritten.path), onto="landing")

    assert rewritten.subjects() == ["mine", "shared work", "root"]
    assert rewritten.read("u") == "up\n# reworded\n"  # the landing's version
    assert rewritten.read("g") == "mine\n"  # and the branch's own work


def test_the_finish_check_passes_without_being_waived(rewritten: Scratch) -> None:
    """The payoff. The branch still contributes exactly `mine`, measured from the
    upstream before and from the landing place after -- so nothing has to be
    allowed by hand, and the check keeps its meaning for the run that needs it.
    """
    rebase_start("upstream", str(rewritten.path), onto="landing")

    report = rebase_finish(str(rewritten.path))
    assert report.ok is True
    assert report.branch_change is None


def test_the_upstream_is_recorded_so_the_check_can_use_it(rewritten: Scratch) -> None:
    rebase_start("upstream", str(rewritten.path), onto="landing")

    session = load_session(rewritten.git)
    assert session is not None
    assert session.upstream_sha == rewritten.git.out("rev-parse", "upstream")
    assert session.base_sha == rewritten.git.out("rev-parse", "landing")


def test_an_ordinary_rebase_records_no_upstream(rewritten: Scratch) -> None:
    """Nothing to record when the two are the same commit: the merge-base finds
    it, and a value here would only be a second way to say so."""
    rebase_start("upstream", str(rewritten.path))

    session = load_session(rewritten.git)
    assert session is not None and session.upstream_sha == ""


def test_naming_the_landing_place_as_the_base_gets_the_range_wrong(
    rewritten: Scratch,
) -> None:
    """The contrast, and why `onto` is not a convenience. Asked to rebase onto
    the rewritten history directly, the range is everything the two have not got
    in common -- the upstream's own work included."""
    with_onto = rebase_preflight("upstream", str(rewritten.path), onto="landing")
    assert [c.subject for c in with_onto.commits] == ["mine"]

    without = rebase_preflight("landing", str(rewritten.path))
    assert [c.subject for c in without.commits] == ["shared work", "mine"]


def test_edit_stops_at_a_commit_of_the_branch_s_own(rewritten: Scratch) -> None:
    """`edit` builds its todo from `base..HEAD`, which is the upstream's range,
    so the two compose without either having to know about the other."""
    report = rebase_start(
        "upstream", str(rewritten.path), edit=["work"], onto="landing"
    )

    assert report.status.state == "stopped_after_apply"
    assert report.status.replaying is not None
    assert report.status.replaying.subject == "mine"


def test_the_collision_check_reads_the_landing_place(rewritten: Scratch) -> None:
    """It is the onto that gets checked out, so its tree decides what is in the
    way. A file only the landing place tracks is about to be written, and the
    upstream's tree says nothing about it."""
    rewritten.git.run("checkout", "-q", "landing")
    rewritten.commit("a file only the landing place has", only_here="theirs\n")
    rewritten.git.run("checkout", "-q", "work")
    rewritten.write("only_here", "my scratch work\n")

    with_onto = rebase_preflight("upstream", str(rewritten.path), onto="landing")
    assert with_onto.untracked_collisions == ("only_here",)
    # Without the onto, nothing in the upstream's tree or the replayed range
    # names it, so there is nothing to warn about -- and the warning would be
    # wrong, since that rebase would not write the file.
    without = rebase_preflight("upstream", str(rewritten.path))
    assert without.untracked_collisions == ()
