"""Tests for the checks that catch a rebase going wrong.

These are the ones that matter most: each corresponds to a real rewrite that git
carried out silently and reported as success.
"""

from __future__ import annotations

from git_rebase_mcp.invariants import (
    commits_with_markers,
    has_markers,
    record_backup,
    tree_change,
)

from scratch import Scratch


def test_a_reorder_leaves_the_tree_unchanged(scratch: Scratch) -> None:
    """The property the whole harness rests on."""
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="two\n")
    scratch.commit("adds c", c="three\n")
    backup = record_backup(scratch.git, label="test")

    adds_b, adds_c = scratch.git.out("rev-parse", "HEAD~1"), scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~2", [f"pick {adds_c}", f"pick {adds_b}"])

    assert scratch.subjects() == ["adds b", "adds c", "base"]  # order really did change
    assert tree_change(scratch.git, backup) is None


def test_a_dropped_commit_changes_the_tree(scratch: Scratch) -> None:
    """The todo that lost three commits, in miniature."""
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="two\n")
    scratch.commit("adds c", c="three\n")
    backup = record_backup(scratch.git, label="test")

    adds_c = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~2", [f"pick {adds_c}"])  # "adds b" simply left out

    change = tree_change(scratch.git, backup)
    assert change is not None
    assert "b" in change


def test_folding_two_commits_together_changes_nothing_in_the_tree(scratch: Scratch) -> None:
    """Honest limit: squashing preserves content, so the tree check cannot see
    it. The state types are what prevent that one, not this."""
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="two\n")
    scratch.commit("adds c", c="three\n")
    backup = record_backup(scratch.git, label="test")

    adds_b, adds_c = scratch.git.out("rev-parse", "HEAD~1"), scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~2", [f"pick {adds_b}", f"fixup {adds_c}"])

    assert len(scratch.subjects()) == 2  # three commits became two ("base" plus one)
    assert tree_change(scratch.git, backup) is None


def test_the_backup_tag_survives_as_a_real_ref(scratch: Scratch) -> None:
    scratch.commit("base", a="one\n")
    backup = record_backup(scratch.git, label="test")
    assert scratch.git.out("rev-parse", backup.ref) == backup.sha


def test_a_commit_carrying_markers_is_found(scratch: Scratch) -> None:
    scratch.commit("base", a="one\n")
    scratch.commit(
        "resolved badly",
        a="<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> abc123 (something)\n",
    )

    hits = commits_with_markers(scratch.git, "HEAD~1..HEAD")
    assert [hit.subject for hit in hits] == ["resolved badly"]
    assert hits[0].paths == ("a",)


def test_a_clean_range_reports_nothing(scratch: Scratch) -> None:
    scratch.commit("base", a="one\n")
    scratch.commit("second", a="two\n")
    assert commits_with_markers(scratch.git, "HEAD~1..HEAD") == []


def test_every_commit_is_scanned_not_only_the_tip(scratch: Scratch) -> None:
    """A marker committed part-way and tidied up later still leaves a commit
    nobody can build."""
    scratch.commit("base", a="one\n")
    scratch.commit("resolved badly", a="<<<<<<< HEAD\nmine\n>>>>>>> abc123\n")
    scratch.commit("tidied up", a="mine\n")

    hits = commits_with_markers(scratch.git, "HEAD~2..HEAD")
    assert [hit.subject for hit in hits] == ["resolved badly"]


def test_prose_about_conflicts_is_not_mistaken_for_one(scratch: Scratch) -> None:
    """This project's own documentation would trip a looser pattern, and so
    would any markdown heading underlined with equals signs."""
    scratch.commit(
        "documentation",
        readme="Staging a file that still contains `<<<<<<<` is an error.\n"
        "\nHeading\n=======\n\nAnd `>>>>>>>` closes it.\n",
    )
    assert commits_with_markers(scratch.git, "HEAD") == []


def test_has_markers_checks_resolved_content() -> None:
    assert has_markers("a\n<<<<<<< HEAD\nb\n")
    assert has_markers("||||||| parent of abc\n")
    assert not has_markers("a\nb\n")
    assert not has_markers("Heading\n=======\n")
