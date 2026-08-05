"""Tests for describing a conflict as what each side did.

The regions come from git's own merge rather than from diffing the whole of each
side against the base. Working them out independently produced regions where one
side had nothing to say, which is what made the output unreadable on a real
branch; the last test here is that case, kept as a measurement.
"""

from __future__ import annotations

from git_rebase_mcp.conflicts import (
    _locate,
    _parse_diff3,
    _render,
    read_conflict,
)
from git_rebase_mcp.state import Conflicted, read_state

from scratch import Scratch


# ── splitting git's output into blocks ───────────────────────────────────────

MERGED = """unchanged head
<<<<<<< ours
mine
||||||| base
original
=======
theirs
>>>>>>> theirs
unchanged tail
"""


def test_a_block_is_split_into_its_three_sides():
    blocks = _parse_diff3(MERGED.splitlines())
    assert len(blocks) == 1
    assert blocks[0].branch_so_far == ["mine"]
    assert blocks[0].base == ["original"]
    assert blocks[0].replaying == ["theirs"]


def test_text_outside_a_block_is_not_reported():
    """Git merged it, so there is no decision left to make about it."""
    blocks = _parse_diff3(MERGED.splitlines())
    assert "unchanged head" not in blocks[0].base
    assert "unchanged tail" not in blocks[0].base


def test_no_blocks_when_nothing_conflicted():
    assert _parse_diff3(["just", "some", "lines"]) == []


# ── locating a block in the base ─────────────────────────────────────────────


def test_a_block_is_located_by_its_base_text():
    base = ["a", "b", "c", "d"]
    assert _locate(base, ["c"], 0) == 2


def test_the_search_starts_after_the_previous_block():
    """Blocks come in order, so a repeated line matches the later one."""
    base = ["x", "same", "y", "same", "z"]
    assert _locate(base, ["same"], 0) == 1
    assert _locate(base, ["same"], 2) == 3


def test_a_block_adding_lines_the_base_never_had_sits_at_the_search_point():
    assert _locate(["a", "b"], [], 2) == 2


# ── what each side's diff says ───────────────────────────────────────────────


def test_each_diff_describes_only_its_own_side():
    base = ["keep", "old", "tail"]
    branch = _render(base, ["keep", "branch", "tail"], 0, 3)
    replaying = _render(base, ["keep", "replayed", "tail"], 0, 3)

    assert "-old" in branch and "+branch" in branch and "replayed" not in branch
    assert "-old" in replaying and "+replayed" in replaying and "branch" not in replaying


def test_a_side_that_did_not_touch_the_region_says_so_in_one_line():
    """It used to repeat the region back as context. On a real branch that was
    104 lines of output conveying nothing."""
    base = ["a", "b", "c"]
    assert _render(base, list(base), 0, 3) == "(unchanged in this region)"


def test_line_numbers_are_absolute_in_the_base_file():
    base = ["one", "two"]
    assert _render(base, ["one", "CHANGED"], 40, 3).splitlines()[0].startswith("@@ -41,2 +41,2 @@")


def test_a_long_run_of_removed_lines_is_summarised_by_count():
    """A hundred removed lines are a hundred lines saying one thing: the branch
    has not reached them yet."""
    base = ["keep"] + [f"missing{i}" for i in range(100)]
    rendered = _render(base, ["keep"], 0, 3)
    assert "... 94 more lines ..." in rendered
    assert len(rendered.splitlines()) < 12
    assert "-missing0" in rendered and "-missing99" in rendered


def test_long_runs_of_unchanged_text_are_elided():
    base = [f"line{i}" for i in range(60)] + ["target"]
    side = [f"line{i}" for i in range(60)] + ["changed"]
    rendered = _render(base, side, 0, 3)
    assert "..." in rendered
    assert len(rendered.splitlines()) < 15
    assert "-target" in rendered and "+changed" in rendered


def test_the_reindent_versus_token_change_reads_as_two_intents():
    """The conflict that motivated the project: one side wrapped a block in an
    `if` and reindented it, the other swapped a call inside it."""
    base = ["rows = []", "for p in packages:", "    rows.append(delta(p))"]
    branch = ["if current:", "    rows = []", "    for p in packages:", "        rows.append(delta(p))"]
    replaying = ["rows = []", "for p in packages:", "    rows.append(delta_count(p))"]

    branch_diff = _render(base, branch, 0, 3)
    replaying_diff = _render(base, replaying, 0, 3)

    assert "+if current:" in branch_diff
    assert "delta_count" not in branch_diff
    assert "+    rows.append(delta_count(p))" in replaying_diff
    assert "if current:" not in replaying_diff


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


def test_an_add_add_conflict_says_there_is_no_common_base(scratch: Scratch) -> None:
    """Both sides adding the same path leaves no base stage, so the units would
    all come out empty -- reporting nothing at the moment there is most to say."""
    scratch.commit("base", other="x\n")
    scratch.commit("branch adds", f="from branch\n")
    branch_side = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "-b", "side", "HEAD~1")
    scratch.commit("side adds", f="from side\n")
    side = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "main")
    scratch.start_rebase(branch_side, [f"pick {side}"])

    conflict = read_conflict(scratch.git, "f")
    assert conflict.no_common_base
    assert conflict.units == ()
    assert conflict.sides.branch_so_far == "from branch\n"
    assert conflict.sides.replaying == "from side\n"


def test_a_block_the_branch_has_not_reached_stays_small(scratch: Scratch) -> None:
    """The measurement that prompted the redesign. The branch is missing a large
    block a later commit adds, and the replayed commit appends one line beside
    it. Deriving the regions independently fused the two and then printed the
    whole missing block back as context for a side that had not touched it."""
    body = [f"def test_{i}(): pass" for i in range(300)]
    later = [f"def later_{i}(): pass" for i in range(100)]
    scratch.write("f.py", "\n".join(body) + "\n")
    scratch.commit("base")
    scratch.write("f.py", "\n".join(body + later) + "\n")
    scratch.commit("adds a hundred later tests")
    scratch.write("f.py", "\n".join(body + later + ["def appended(): pass"]) + "\n")
    scratch.commit("appends one more")

    last = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~1", [f"pick {last}"], onto="HEAD~2")

    conflict = read_conflict(scratch.git, "f.py")
    payload = sum(
        len(unit.branch_so_far_diff) + len(unit.replaying_diff) for unit in conflict.units
    )
    assert payload < 600, f"payload is {payload} characters"  # was 4556
    assert any("appended" in unit.replaying_diff for unit in conflict.units)
