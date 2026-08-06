"""Tests for the checks that catch a rebase going wrong.

These are the ones that matter most: each corresponds to a real rewrite that git
carried out silently and reported as success.
"""

from __future__ import annotations

from git_rebase_mcp.invariants import (
    Session,
    clear_session,
    commits_with_markers,
    load_session,
    save_session,
    has_markers,
    record_backup,
    branch_change,
    _changed_lines,
)

from scratch import Scratch


def test_a_reorder_leaves_the_branch_change_unchanged(scratch: Scratch) -> None:
    """The property the whole harness rests on."""
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="two\n")
    scratch.commit("adds c", c="three\n")
    base = scratch.git.out("rev-parse", "HEAD~2")
    backup = record_backup(scratch.git, label="test")

    adds_b, adds_c = scratch.git.out("rev-parse", "HEAD~1"), scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~2", [f"pick {adds_c}", f"pick {adds_b}"])

    assert scratch.subjects() == ["adds b", "adds c", "base"]  # order really did change
    assert branch_change(scratch.git, backup, base) is None


def test_a_dropped_commit_changes_the_branch_change(scratch: Scratch) -> None:
    """The todo that lost three commits, in miniature."""
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="two\n")
    scratch.commit("adds c", c="three\n")
    base = scratch.git.out("rev-parse", "HEAD~2")
    backup = record_backup(scratch.git, label="test")

    adds_c = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~2", [f"pick {adds_c}"])  # "adds b" simply left out

    change = branch_change(scratch.git, backup, base)
    assert change is not None
    assert not change.reordered_only  # a dropped commit is not a reordering
    assert "b" in change.summary


def test_moving_onto_newer_upstream_work_is_not_damage(scratch: Scratch) -> None:
    """The tree necessarily changes when the base has moved on. Checking the
    tree reported that as a loss on the most ordinary rebase there is; checking
    the branch's own contribution does not."""
    scratch.commit("base", shared="one\n")
    fork = scratch.git.out("rev-parse", "HEAD")
    scratch.commit("branch work", mine="feature\n")
    backup = record_backup(scratch.git, label="test")

    # Upstream gains a commit the branch has never seen.
    scratch.git.run("checkout", "-q", "-b", "upstream", fork)
    scratch.commit("upstream work", theirs="other\n")
    moved_base = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "main")

    branch_work = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase(fork, [f"pick {branch_work}"], onto=moved_base)

    assert scratch.read("theirs") == "other\n"  # the tree really did change
    assert branch_change(scratch.git, backup, moved_base) is None


def test_folding_two_commits_together_is_not_visible_here(scratch: Scratch) -> None:
    """Honest limit: squashing preserves the branch's contribution, so this
    check cannot see it. The state types are what prevent that one."""
    scratch.commit("base", a="one\n")
    scratch.commit("adds b", b="two\n")
    scratch.commit("adds c", c="three\n")
    base = scratch.git.out("rev-parse", "HEAD~2")
    backup = record_backup(scratch.git, label="test")

    adds_b, adds_c = scratch.git.out("rev-parse", "HEAD~1"), scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~2", [f"pick {adds_b}", f"fixup {adds_c}"])

    assert len(scratch.subjects()) == 2  # three commits became two
    assert branch_change(scratch.git, backup, base) is None


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


def test_a_file_that_always_held_a_marker_is_not_blamed_on_the_rebase(scratch: Scratch) -> None:
    """A fixture of git's merge output is a marker nobody put there by mistake.
    Scanning trees flagged it in every commit of every rebase of the repository
    -- including both commits of the rebase that found this, neither of which
    had touched the file."""
    fixture = "<<<<<<< ours\nmine\n||||||| base\nold\n>>>>>>> theirs\n"
    scratch.commit("base", test_fixture=fixture, a="one\n")
    scratch.commit("unrelated", test_fixture=fixture, a="two\n")

    assert commits_with_markers(scratch.git, "HEAD~1..HEAD") == []


def test_a_marker_arriving_in_such_a_file_is_still_caught(scratch: Scratch) -> None:
    """Not flagging the file wholesale is not the same as trusting it."""
    scratch.commit("base", a="one\n")
    scratch.commit("resolved badly", a="<<<<<<< HEAD\nmine\n>>>>>>> abc123\n")

    hits = commits_with_markers(scratch.git, "HEAD~1..HEAD")
    assert [hit.paths for hit in hits] == [("a",)]


def test_the_commit_that_introduced_a_marker_is_named_not_the_ones_after(
    scratch: Scratch,
) -> None:
    """Every commit after it carries the marker too, and naming them all buries
    the one commit anybody can do something about."""
    scratch.commit("base", a="one\n")
    scratch.commit("resolved badly", a="<<<<<<< HEAD\nmine\n>>>>>>> abc123\n")
    scratch.commit("carries on", b="unrelated\n")

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


def test_a_marker_with_its_label_stripped_is_still_a_marker() -> None:
    """Half-resolving by hand leaves `<<<<<<<` with no trailing space, and
    that has to be refused as loudly as git's own spelling."""
    assert has_markers("<<<<<<<HEAD\n")
    assert has_markers(">>>>>>>abc123\n")
    assert has_markers("|||||||parent\n")
    assert has_markers("<<<<<<<\n")


def test_a_marker_without_a_trailing_space_is_found(scratch: Scratch) -> None:
    scratch.commit("base", a="one\n")
    scratch.commit("resolved badly", a="<<<<<<<HEAD\nmine\n>>>>>>>abc\n")

    hits = commits_with_markers(scratch.git, "HEAD~1..HEAD")
    assert [hit.subject for hit in hits] == ["resolved badly"]
    assert hits[0].paths == ("a",)


def test_a_removed_line_that_looks_like_a_diff_header_is_counted(
    scratch: Scratch,
) -> None:
    """A removed line whose content starts with `-- ` renders as `--- ` in the
    diff, exactly like a `--- a/f` header. It is still a body line: dropping it
    made a rebase that deleted such a line look identical to one that did not."""
    scratch.commit("base", f="x\n-- removed\n")
    base = scratch.git.out("rev-parse", "HEAD")
    scratch.commit("tip", f="x\n")

    changed = _changed_lines(scratch.git, base, scratch.git.out("rev-parse", "HEAD"))
    assert changed == {"f": ["--- removed"]}


def test_an_added_line_that_looks_like_a_diff_header_is_counted(
    scratch: Scratch,
) -> None:
    """An added line whose content starts with `++ ` renders as `+++ `, which
    the parser must not take for the `+++ b/f` header and read a path from."""
    scratch.commit("base", f="x\n")
    base = scratch.git.out("rev-parse", "HEAD")
    scratch.commit("tip", f="x\n++ b/added\n")

    changed = _changed_lines(scratch.git, base, scratch.git.out("rev-parse", "HEAD"))
    assert changed == {"f": ["+++ b/added"]}


def test_a_session_survives_being_written_and_read(scratch: Scratch) -> None:
    scratch.commit("base", a="one\n")
    backup = record_backup(scratch.git, label="test")
    session = Session(
        backup_ref=backup.ref,
        backup_sha=backup.sha,
        backup_tree=backup.tree,
        base="HEAD~1",
        base_sha=backup.sha,
        stashed=("pytest.ini",),
        check_command="pytest -q",
    )
    save_session(scratch.git, session)

    loaded = load_session(scratch.git)
    assert loaded == session
    assert loaded is not None and loaded.backup == backup


def test_no_session_reads_as_none(scratch: Scratch) -> None:
    scratch.commit("base", a="one\n")
    assert load_session(scratch.git) is None


def test_clearing_a_session_removes_it(scratch: Scratch) -> None:
    scratch.commit("base", a="one\n")
    backup = record_backup(scratch.git, label="test")
    save_session(
        scratch.git,
        Session(backup.ref, backup.sha, backup.tree, base="HEAD~1", base_sha=backup.sha),
    )
    clear_session(scratch.git)
    assert load_session(scratch.git) is None


def test_an_unreadable_session_is_treated_as_absent(scratch: Scratch) -> None:
    """A file from another version must not stop the server working."""
    scratch.commit("base", a="one\n")
    scratch.git.git_path("rebase-mcp.json").write_text("{not json")
    assert load_session(scratch.git) is None
