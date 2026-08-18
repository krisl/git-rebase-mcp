"""Tests for warning that the backup tag would destroy a local file.

The case, as it happened: a commit was amended to stop tracking a config file
and to ignore it, so from then on the working copy was the only one. Later, to
check whether a failing test predated the rebase, the backup tag was checked out
and the branch checked out again. That deleted the file -- the tag writes the
tracked version over it, and returning removes it, because the branch does not
track it and git treats an ignored file as expendable.

The rebase was not at fault and reported success. What failed was three tests
about a missing package, an hour later, with nothing pointing back.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.invariants import (
    locals_the_backup_tracks,
    locals_the_rewrite_removed,
    record_backup,
)
from git_rebase_mcp.server import proceed, rebase_amend, rebase_finish, rebase_start

from scratch import Scratch


@pytest.fixture
def untracks_a_config(scratch: Scratch) -> Scratch:
    """A branch whose rewrite turns `local.cfg` into a local file.

    Done the way it happens: an `edit` stop, `git rm --cached`, a .gitignore
    line, amend. The commit being rewritten is the one that added the file, so
    the branch ends up never having tracked it and the backup tag still does.
    """
    scratch.commit("base", a="one\n")
    scratch.commit("adds a config", **{"local.cfg": "root = /somewhere\n"})
    scratch.commit("adds c", c="c\n")

    rebase_start("HEAD~2", str(scratch.path), edit=["HEAD~1"])
    scratch.git.run("rm", "--cached", "-q", "local.cfg")
    scratch.write(".gitignore", "local.cfg\n")
    scratch.git.run("add", ".gitignore")
    rebase_amend(str(scratch.path), stage_tracked=True)
    proceed(str(scratch.path))
    return scratch


def test_the_round_trip_really_does_destroy_it(untracks_a_config: Scratch) -> None:
    """Pinned first, because the warning is only worth having if this holds."""
    report = rebase_finish(str(untracks_a_config.path), allow_change=True)
    assert (untracks_a_config.path / "local.cfg").is_file()

    untracks_a_config.git.run("checkout", "-q", report.backup_ref)
    untracks_a_config.git.run("checkout", "-q", "main")

    assert not (untracks_a_config.path / "local.cfg").exists()


def test_finishing_names_the_path(untracks_a_config: Scratch) -> None:
    report = rebase_finish(str(untracks_a_config.path), allow_change=True)

    assert report.fragile_locals == ("local.cfg",)
    assert "local.cfg" in report.guidance
    assert "Copy it aside" in report.guidance


def test_it_names_the_tag_that_would_do_it(untracks_a_config: Scratch) -> None:
    report = rebase_finish(str(untracks_a_config.path), allow_change=True)
    assert report.backup_ref in report.guidance


def test_it_does_not_stop_the_finish(untracks_a_config: Scratch) -> None:
    """The rewrite did what it was asked. Untracking a path is the result, not
    a fault, and nothing here is a reason to withhold the tidy-up."""
    assert rebase_finish(str(untracks_a_config.path), allow_change=True).ok is True


def test_an_ordinary_rebase_reports_nothing(scratch: Scratch) -> None:
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="b\n")
    rebase_start("HEAD~1", str(scratch.path))

    assert rebase_finish(str(scratch.path)).fragile_locals == ()


class TestWhatItDeclinesToReport:
    """Each condition, shown to be carrying its weight. A warning that fired on
    ordinary rebases would stop being read, and the check would be worth less
    than nothing."""

    def test_a_file_the_branch_deliberately_deletes(self, scratch: Scratch) -> None:
        """Not on disk, so there is no local copy to lose."""
        scratch.commit("base", a="one\n", doomed="x\n")
        backup = record_backup(scratch.git)
        scratch.git.run("rm", "-q", "doomed")
        scratch.git.run("commit", "-q", "-m", "removes doomed")

        assert locals_the_backup_tracks(scratch.git, backup) == ()

    def test_a_file_that_is_still_tracked(self, scratch: Scratch) -> None:
        """Tracked on both sides: the round trip restores it, not destroys it."""
        scratch.commit("base", a="one\n", kept="x\n")
        backup = record_backup(scratch.git)
        scratch.commit("touches it", kept="y\n")

        assert locals_the_backup_tracks(scratch.git, backup) == ()

    def test_an_untracked_file_that_nothing_ignores(self, scratch: Scratch) -> None:
        """Git refuses to clobber an untracked file, so the checkout errors
        rather than destroying anything. Loud is safe, and needs no warning."""
        scratch.commit("base", a="one\n", plain="x\n")
        backup = record_backup(scratch.git)
        scratch.git.run("rm", "--cached", "-q", "plain")
        scratch.git.run("commit", "-q", "-m", "untracked, not ignored")

        assert (scratch.path / "plain").is_file()
        assert locals_the_backup_tracks(scratch.git, backup) == ()


@pytest.fixture
def onto_a_history_that_untracked_it(scratch: Scratch) -> Scratch:
    """The shape that caught this, reduced.

        root ─ tracks local.cfg ─ "mine"        [work, the branch]
             └ local.cfg is local from here     [landing]

    The branch is on a history that tracks the file; the landing place is a
    rewrite of that history which does not. Rebasing onto it checks out the
    landing place, and git deletes the working copy on the way -- silently,
    because the landing place ignores it.
    """
    scratch.commit("root", a="one\n")
    scratch.commit("tracks a config", **{"local.cfg": "root = /somewhere\n"})
    scratch.git.run("branch", "upstream")
    scratch.git.run("checkout", "-q", "-b", "work")
    scratch.commit("mine", g="mine\n")

    scratch.git.run("checkout", "-q", "-b", "landing", "upstream")
    scratch.git.run("rm", "--cached", "-q", "local.cfg")
    scratch.write(".gitignore", "local.cfg\n")
    scratch.git.run("add", ".gitignore")
    scratch.git.run("commit", "-q", "-m", "local.cfg is local from here")

    scratch.git.run("checkout", "-q", "work")
    return scratch


class TestTheCopyTheRewriteAlreadyTook:
    """The other half. Requiring the file to still be on disk made the check
    above blind to the loss actually happening -- which is how a real rebase
    deleted a config file and reported nothing at risk."""

    def test_the_rebase_really_does_delete_it(
        self, onto_a_history_that_untracked_it: Scratch
    ) -> None:
        repo = onto_a_history_that_untracked_it
        assert (repo.path / "local.cfg").is_file()

        rebase_start("upstream", str(repo.path), onto="landing")

        assert not (repo.path / "local.cfg").exists()

    def test_the_start_report_says_so_at_once(
        self, onto_a_history_that_untracked_it
    ) -> None:
        """Where it happened, rather than only at the finish: the sooner it is
        said the less of the run has to be re-read to understand it."""
        repo = onto_a_history_that_untracked_it
        report = rebase_start("upstream", str(repo.path), onto="landing")

        assert report.lost_locals == ("local.cfg",)
        assert "local.cfg" in report.guidance
        assert f"git show {report.backup_ref}:<path> > <path>" in report.guidance

    def test_the_finish_report_says_so_too(
        self, onto_a_history_that_untracked_it
    ) -> None:
        repo = onto_a_history_that_untracked_it
        rebase_start("upstream", str(repo.path), onto="landing")

        report = rebase_finish(str(repo.path), allow_change=True)
        assert report.lost_locals == ("local.cfg",)
        assert "Nothing else holds it" in report.guidance

    def test_it_does_not_stop_the_finish(
        self, onto_a_history_that_untracked_it
    ) -> None:
        """Untracking a path is the result the rewrite was asked for. The loss is
        worth saying and is not a reason to withhold the tidy-up."""
        repo = onto_a_history_that_untracked_it
        rebase_start("upstream", str(repo.path), onto="landing")

        assert rebase_finish(str(repo.path), allow_change=True).ok is True

    def test_an_ordinary_rebase_reports_nothing(self, scratch: Scratch) -> None:
        scratch.commit("base", a="one\n")
        scratch.commit("adds b", b="b\n")
        rebase_start("HEAD~1", str(scratch.path))

        assert rebase_finish(str(scratch.path)).lost_locals == ()

    def test_a_file_deleted_on_purpose_is_not_reported(self, scratch: Scratch) -> None:
        """Tracked before, gone now, and meant to be gone. Nothing ignores it,
        which is the discriminator that keeps this quiet."""
        scratch.commit("base", a="one\n", doomed="x\n")
        backup = record_backup(scratch.git)
        scratch.git.run("rm", "-q", "doomed")
        scratch.git.run("commit", "-q", "-m", "removes doomed")

        assert locals_the_rewrite_removed(scratch.git, backup) == ()

    def test_a_file_moved_aside_by_this_server_is_not_reported(
        self, scratch: Scratch
    ) -> None:
        """It is absent because we moved it, and rebase_finish puts it back.
        Calling that a loss would fire on every rebase that stashes anything."""
        scratch.commit("base", a="one\n", moved="x\n")
        backup = record_backup(scratch.git)
        scratch.git.run("rm", "--cached", "-q", "moved")
        scratch.write(".gitignore", "moved\n")
        scratch.git.run("add", ".gitignore")
        scratch.git.run("commit", "-q", "-m", "moved is local now")
        (scratch.path / "moved").unlink()

        assert locals_the_rewrite_removed(scratch.git, backup) == ("moved",)
        assert locals_the_rewrite_removed(scratch.git, backup, stashed=("moved",)) == ()

    def test_the_two_halves_do_not_both_fire(
        self, onto_a_history_that_untracked_it
    ) -> None:
        """Still here means the tag will take it; gone means the rewrite has.
        One file cannot be in both states."""
        repo = onto_a_history_that_untracked_it
        rebase_start("upstream", str(repo.path), onto="landing")

        report = rebase_finish(str(repo.path), allow_change=True)
        assert report.lost_locals == ("local.cfg",)
        assert report.fragile_locals == ()
