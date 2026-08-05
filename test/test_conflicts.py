"""Tests for turning index stages into collision units."""

from __future__ import annotations

from git_rebase_mcp.conflicts import (
    _contested_windows,
    _opcodes,
    _render,
    read_conflict,
)
from git_rebase_mcp.state import Conflicted, read_state

from scratch import Scratch


def windows(base: str, branch: str, replaying: str, context: int = 0):
    base_lines = base.splitlines()
    return _contested_windows(
        _opcodes(base_lines, branch.splitlines()),
        _opcodes(base_lines, replaying.splitlines()),
        len(base_lines),
        context,
    )


# ── which regions are reported ───────────────────────────────────────────────


def test_a_region_both_sides_edited_is_contested():
    assert windows("a\nb\nc\n", "a\nB\nc\n", "a\nX\nc\n") == [(1, 2)]


def test_a_region_only_one_side_edited_is_not_reported():
    """Git merged it cleanly and it is already in the working file; reporting it
    would ask for a decision that has already been made."""
    assert windows("a\nb\nc\n", "a\nB\nc\n", "a\nb\nc\n") == []


def test_two_separate_regions_are_two_units():
    base = "a\nb\nc\nd\ne\nf\ng\nh\n"
    branch = "a\nB\nc\nd\ne\nf\nG\nh\n"
    replaying = "a\nX\nc\nd\ne\nf\nY\nh\n"
    assert windows(base, branch, replaying) == [(1, 2), (6, 7)]


def test_touching_edits_become_one_unit():
    base = "a\nb\nc\nd\n"
    branch = "a\nB\nC\nd\n"
    replaying = "a\nX\nY\nd\n"
    assert windows(base, branch, replaying) == [(1, 3)]


def test_context_widens_the_window_without_running_off_the_file():
    assert windows("a\nb\nc\n", "a\nB\nc\n", "a\nX\nc\n", context=5) == [(0, 3)]


def test_an_insertion_by_each_side_at_the_same_point_is_contested():
    """An insertion covers no base lines, so it needs widening to collide."""
    assert windows("a\nb\n", "a\nnew\nb\n", "a\nother\nb\n") == [(1, 2)]


# ── what the diffs say ───────────────────────────────────────────────────────


def test_each_diff_describes_only_its_own_side():
    base = ["keep", "old", "tail"]
    branch = ["keep", "branch", "tail"]
    replaying = ["keep", "replayed", "tail"]
    window = (1, 2)

    branch_diff = _render(base, branch, _opcodes(base, branch), window)
    replaying_diff = _render(base, replaying, _opcodes(base, replaying), window)

    assert "-old" in branch_diff and "+branch" in branch_diff
    assert "replayed" not in branch_diff
    assert "-old" in replaying_diff and "+replayed" in replaying_diff
    assert "branch" not in replaying_diff


def test_line_numbers_are_absolute_not_window_relative():
    base = [f"line{i}" for i in range(20)]
    side = list(base)
    side[10] = "changed"
    diff = _render(base, side, _opcodes(base, side), (8, 13))
    assert diff.splitlines()[0].startswith("@@ -9,5 +9,5 @@")


def test_the_reindent_versus_token_change_reads_as_two_intents():
    """The conflict that motivated this project: one side wrapped a block in an
    `if` and reindented it, the other swapped a call inside it. As markers these
    are three near-identical blocks; as two diffs they compose on sight."""
    base = ["rows = []", "for p in packages:", "    rows.append(delta(p))"]
    branch = ["if current:", "    rows = []", "    for p in packages:", "        rows.append(delta(p))"]
    replaying = ["rows = []", "for p in packages:", "    rows.append(delta_count(p))"]
    window = (0, 3)

    branch_diff = _render(base, branch, _opcodes(base, branch), window)
    replaying_diff = _render(base, replaying, _opcodes(base, replaying), window)

    assert "+if current:" in branch_diff
    assert "delta_count" not in branch_diff  # the branch did not touch the call
    assert "+    rows.append(delta_count(p))" in replaying_diff
    assert "if current:" not in replaying_diff  # the replayed commit did not reindent


# ── against a real conflicted repository ─────────────────────────────────────


def test_reads_the_three_sides_out_of_the_index(scratch: Scratch) -> None:
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    third = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")

    state = read_state(scratch.git)
    assert isinstance(state, Conflicted)

    conflict = read_conflict(scratch.git, state.unmerged[0])
    assert conflict.sides.base == "two\n"  # the replayed commit's parent
    assert conflict.sides.branch_so_far == "one\n"
    assert conflict.sides.replaying == "three\n"
    assert len(conflict.units) == 1
    assert "+one" in conflict.units[0].branch_so_far_diff
    assert "+three" in conflict.units[0].replaying_diff


def test_a_missing_stage_reads_as_empty(scratch: Scratch) -> None:
    """Both sides adding the same path leaves no base stage at all."""
    scratch.commit("base", other="x\n")
    scratch.commit("branch adds", f="from branch\n")
    branch_side = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "-b", "side", "HEAD~1")
    scratch.commit("side adds", f="from side\n")
    side = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "main")
    scratch.start_rebase(branch_side, [f"pick {side}"])

    state = read_state(scratch.git)
    assert isinstance(state, Conflicted)
    conflict = read_conflict(scratch.git, "f")
    assert conflict.sides.base == ""
    assert conflict.sides.branch_so_far == "from branch\n"
    assert conflict.sides.replaying == "from side\n"
