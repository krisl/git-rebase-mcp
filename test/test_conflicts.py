"""Tests for describing a conflict as what each side did.

The regions come from git's own merge rather than from diffing the whole of each
side against the base. Working them out independently produced regions where one
side had nothing to say, which is what made the output unreadable on a real
branch; the last test here is that case, kept as a measurement.
"""

from __future__ import annotations

from git_rebase_mcp.conflicts import (
    _Block,
    _anchor,
    _attribution,
    _diff_hunks,
    _enclosing,
    _locate,
    _parse_diff3,
    _render,
    _summarise,
    auto_resolution,
    deleted_side,
    read_conflict,
    side_text,
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


def test_a_block_carries_the_line_its_marker_opened_on():
    assert _parse_diff3(MERGED.splitlines())[0].marker_line == 1


def test_a_marker_line_of_the_file_s_own_does_not_shift_a_block():
    """A file may hold a marker line legitimately -- documentation quoting one,
    a fixture of git's output. It opens no block, so counting markers across
    the file finds more of them than there are blocks; the real block must
    still carry the line it actually opened on."""
    documented = ["# a conflict opens with", "<<<<<<< HEAD", ""]
    blocks = _parse_diff3(documented + MERGED.splitlines())
    assert len(blocks) == 1
    assert blocks[0].marker_line == 4
    assert blocks[0].base == ["original"]


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


def test_a_block_that_is_not_in_the_base_is_reported_as_missing():
    """A block whose base text the file does not contain is not invented into
    it. The search position is where the *previous* block ended, so returning
    it as the answer would report the region at lines it does not occupy."""
    assert _locate(["a", "b", "c"], ["z"], 0) is None
    assert _locate(["a", "b", "c"], ["c"], 4) is None  # search window past the end


# ── anchoring a block by git's own diff ──────────────────────────────────────


def test_diff_hunks_parse_asymmetric_counts(scratch: Scratch) -> None:
    """git omits a count of 1 and can pair an unnumbered side with a numbered
    one, so `-19 +19` and `-448 +544,5` and `-449,0 +550` all parse."""
    assert _diff_hunks(scratch.git, "a\nb\n", "a\nb\nc\n") == [(2, 0, 3, 1)]
    assert _diff_hunks(scratch.git, "a\n", "a\nb\n") == [(1, 0, 2, 1)]


def test_anchor_names_the_hunk_holding_the_marker(scratch: Scratch) -> None:
    """A marker at merged line 4 sits in the hunk that starts at merged line 3,
    whose base side names where the block lives."""
    hunks = [(10, 2, 3, 4)]
    assert _anchor(hunks, 3) == 10   # merged line 4, inside the hunk
    assert _anchor(hunks, 2) == 10   # merged line 3, the hunk's first line
    assert _anchor(hunks, 6) is None  # merged line 7, past the hunk


def test_anchor_reads_an_insertion_as_starting_after_the_line_it_names() -> None:
    """`@@ -3,0 +4,3 @@` puts its lines *after* base line 3, so the block
    begins at line 4. Marker hunks are nearly always this shape: diff3 repeats
    the base section verbatim, leaving only the markers themselves as new."""
    assert _anchor([(3, 0, 4, 3)], 3) == 4


def test_a_region_with_no_position_still_says_what_each_side_did():
    """Refusing to place it is not a reason to refuse to describe it. The two
    diffs are what the region is read for; only the line numbers are missing."""
    rendered = _render([], ["old"], ["new"], None, 3, "def render(self):")

    assert rendered.splitlines()[0] == "@@ not found in the base file @@ def render(self):"
    assert "-old" in rendered and "+new" in rendered


def test_a_region_with_no_position_borrows_no_line_numbers():
    """A header counting from anywhere would be the invention this avoids."""
    assert "@@ -" not in _render(["a", "b"], ["old"], ["new"], None, 3)


def test_a_block_whose_lines_are_not_in_the_branch_is_not_attributed(
    scratch: Scratch,
) -> None:
    """Blaming from line 1 because a region was not found would present the
    top of the file as the region's authors. Saying nothing is the honest
    answer, as it is for a region the branch left empty."""
    scratch.commit("base", f="one\ntwo\n")

    assert _attribution(scratch.git, "f", ["one", "two"], ["nothing", "here"], None) == ()


# ── what each side's diff says ───────────────────────────────────────────────


def test_each_diff_describes_only_its_own_side():
    base = ["keep", "old", "tail"]
    branch = _render(base, base, ["keep", "branch", "tail"], 0, 3)
    replaying = _render(base, base, ["keep", "replayed", "tail"], 0, 3)

    assert "-old" in branch and "+branch" in branch and "replayed" not in branch
    assert "-old" in replaying and "+replayed" in replaying and "branch" not in replaying


def test_a_side_that_did_not_touch_the_region_says_so_in_one_line():
    """It used to repeat the region back as context. On a real branch that was
    104 lines of output conveying nothing."""
    base = ["a", "b", "c"]
    assert _render(base, base, list(base), 0, 3) == "(unchanged in this region)"


def test_line_numbers_are_absolute_in_the_base_file():
    base = ["one", "two"]
    assert _render(base, base, ["one", "CHANGED"], 40, 3).splitlines()[0].startswith("@@ -41,2 +41,2 @@")


# ── which definition the region is inside ────────────────────────────────────


def test_the_header_names_the_definition_the_region_is_inside():
    base = ["class Report:", "    def render(self):", "        return rows"]
    header = _render(base, base[2:], ["        return []"], 2, 3, "def render(self):")

    assert header.splitlines()[0] == "@@ -1,3 +1,3 @@ def render(self):"


def test_a_region_with_no_definition_above_it_leaves_the_header_bare():
    base = ["one", "two"]
    assert _render(base, base, ["one", "CHANGED"], 0, 3).splitlines()[0] == "@@ -1,2 +1,2 @@"


def test_an_indented_definition_beats_the_class_it_is_in(scratch: Scratch):
    """Git's fallback recognises a definition at column 0 only, so on Python it
    names the class every time and the method never -- the answer nobody needs.
    Naming the language is the whole of what this module contributes."""
    lines = ["class Report:", "    def unrelated(self):", "        pass", "",
             "    def render(self):", "        rows = []"]

    assert _enclosing(scratch.git, "report.py", lines, [5]) == ["def render(self):"]
    assert _enclosing(scratch.git, "report.txt", lines, [5]) == ["class Report:"]


def test_a_language_this_module_has_never_heard_of_still_gets_git_s_answer(scratch: Scratch):
    """The patterns are git's twenty-five, not this module's, so a language it
    was never taught is named as well as one it was."""
    lines = ["class Report {", "    fun unrelated() {}", "", "    fun render(): List<Row> {",
             "        val rows = mutableListOf<Row>()"]

    assert _enclosing(scratch.git, "Report.kt", lines, [4]) == ["fun render(): List<Row> {"]


def test_a_repository_that_states_its_own_driver_is_obeyed(scratch: Scratch):
    """A project may know better than an extension does -- a bespoke funcname
    driver, or a language served by a driver its extension does not imply."""
    scratch.write(".gitattributes", "*.inc diff=python\n")
    lines = ["class Report:", "    def render(self):", "        rows = []"]

    assert _enclosing(scratch.git, "report.inc", lines, [2]) == ["def render(self):"]


def test_each_region_is_named_independently(scratch: Scratch):
    """One question is asked of git per file, so the regions have to be told
    apart within its answer rather than by the order they come back in."""
    lines = ["def first():", "    a = 1", "", "def second():", "    b = 2"]

    assert _enclosing(scratch.git, "m.py", lines, [1, 4]) == ["def first():", "def second():"]


def test_a_region_with_nothing_above_it_is_named_by_nothing(scratch: Scratch):
    assert _enclosing(scratch.git, "m.py", ["a = 1", "b = 2"], [1]) == [""]


def test_a_region_with_no_position_is_named_by_nothing(scratch: Scratch):
    """It is nowhere in the file, so it is inside nothing -- and the regions
    that do have a position are still answered around it."""
    lines = ["class Report:", "    def a(self):", "        x = 1", "",
             "    def b(self):", "        y = 2"]

    assert _enclosing(scratch.git, "m.py", lines, [None, 5, None]) == ["", "def b(self):", ""]


def test_the_definition_a_region_starts_on_is_not_its_own_enclosing(scratch: Scratch):
    """The line is about to be shown as part of the region; naming it as the
    surroundings too would say nothing about where in the file that is."""
    lines = ["class Report:", "    def render(self):", "        pass"]

    assert _enclosing(scratch.git, "report.py", lines, [1]) == ["class Report:"]


def test_a_long_run_of_removed_lines_is_summarised_by_count():
    """A hundred removed lines are a hundred lines saying one thing: the branch
    has not reached them yet."""
    base = ["keep"] + [f"missing{i}" for i in range(100)]
    rendered = _render(base, base, ["keep"], 0, 3)
    assert "... 94 more lines ..." in rendered
    assert len(rendered.splitlines()) < 12
    assert "-missing0" in rendered and "-missing99" in rendered


def test_long_runs_of_unchanged_text_are_elided():
    base = [f"line{i}" for i in range(60)] + ["target"]
    side = [f"line{i}" for i in range(60)] + ["changed"]
    rendered = _render(base, base, side, 0, 3)
    assert "..." in rendered
    assert len(rendered.splitlines()) < 15
    assert "-target" in rendered and "+changed" in rendered


def test_the_reindent_versus_token_change_reads_as_two_intents():
    """The conflict that motivated the project: one side wrapped a block in an
    `if` and reindented it, the other swapped a call inside it."""
    base = ["rows = []", "for p in packages:", "    rows.append(delta(p))"]
    branch = ["if current:", "    rows = []", "    for p in packages:", "        rows.append(delta(p))"]
    replaying = ["rows = []", "for p in packages:", "    rows.append(delta_count(p))"]

    branch_diff = _render(base, base, branch, 0, 3)
    replaying_diff = _render(base, base, replaying, 0, 3)

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


def deleted_by_the_branch(scratch: Scratch) -> None:
    """Replay a commit that edits `f` onto a branch that removed it.

    The branch deletes `f` outright rather than renaming it, because a rename
    git can detect never reaches this state: it follows the path and applies the
    replayed edit to the new name. What produces a modify/delete is a deletion
    with nothing similar enough to be paired with it -- an outright removal, or
    a rename whose content changed too much to be recognised as one.
    """
    scratch.commit("base", f="one\ntwo\n", other="keep\n")
    base = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("rm", "-q", "f")
    scratch.git.run("commit", "-q", "-m", "branch deletes f")
    deleting = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "-b", "side", base)
    scratch.commit("side edits f", f="one\ntwo\nthree\n")
    side = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "main")
    scratch.start_rebase(base, [f"pick {deleting}", f"pick {side}"])


def test_a_path_one_side_deleted_says_so_rather_than_diffing_its_lines(
    scratch: Scratch,
) -> None:
    """Regions could be computed here -- there is a base and one side -- and they
    would read as "the branch removed every line", which is true of the text and
    is not what happened. The decision is whether the path lives."""
    deleted_by_the_branch(scratch)

    conflict = read_conflict(scratch.git, "f")
    assert conflict.deleted_by == "branch"
    assert conflict.units == ()
    assert conflict.sides.base == "one\ntwo\n"
    assert conflict.sides.branch_so_far == ""
    assert conflict.sides.replaying == "one\ntwo\nthree\n"


def test_the_side_that_deleted_is_named_even_when_it_is_the_replayed_one(
    scratch: Scratch,
) -> None:
    """The mirror case: the branch keeps editing a path the replayed commit
    removes. Both are modify/delete conflicts and the answers are opposites, so
    which side is which is the whole of what the caller needs told."""
    scratch.commit("base", f="one\ntwo\n")
    base = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "-b", "side", base)
    scratch.git.run("rm", "-q", "f")
    scratch.git.run("commit", "-q", "-m", "side deletes f")
    deleting = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "main")
    scratch.commit("branch edits f", f="one\ntwo\nthree\n")
    editing = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase(base, [f"pick {editing}", f"pick {deleting}"])

    conflict = read_conflict(scratch.git, "f")
    assert conflict.deleted_by == "replaying"
    assert conflict.sides.replaying == ""
    assert conflict.sides.branch_so_far == "one\ntwo\nthree\n"


def test_a_conflict_with_both_sides_present_reports_no_deletion(scratch: Scratch) -> None:
    """The check has to stay quiet on the ordinary conflict: a report of a
    deletion that did not happen would send the caller to `take` for a question
    the regions were there to answer."""
    scratch.commit("base", f="one\n")
    scratch.commit("second", f="two\n")
    scratch.commit("third", f="three\n")
    third = scratch.git.out("rev-parse", "HEAD")
    scratch.start_rebase("HEAD~1", [f"pick {third}"], onto="HEAD~2")

    assert deleted_side(scratch.git, "f") is None
    assert read_conflict(scratch.git, "f").deleted_by is None


def test_an_add_add_conflict_is_not_reported_as_a_deletion(scratch: Scratch) -> None:
    """No base stage means neither side removed anything -- both introduced the
    path. Reading a missing base as a deletion would offer to stage one."""
    scratch.commit("base", other="x\n")
    scratch.commit("branch adds", f="from branch\n")
    branch_side = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "-b", "side", "HEAD~1")
    scratch.commit("side adds", f="from side\n")
    side = scratch.git.out("rev-parse", "HEAD")
    scratch.git.run("checkout", "-q", "main")
    scratch.start_rebase(branch_side, [f"pick {side}"])

    assert deleted_side(scratch.git, "f") is None
    assert read_conflict(scratch.git, "f").no_common_base


def test_a_surviving_side_can_be_read_whole(scratch: Scratch) -> None:
    """What keeping the path means, for the side that still has it. Composing
    blocks cannot say: a modify/delete has none."""
    deleted_by_the_branch(scratch)

    assert side_text(scratch.git, "f", "replaying") == "one\ntwo\nthree\n"
    assert side_text(scratch.git, "f", "branch") == ""


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


def test_added_lines_survive_where_removed_ones_are_summarised():
    """Additions are the text a caller copies to reapply the commit; a count
    instead of them makes the region unusable rather than merely long."""
    base = [f"missing{i}" for i in range(30)]
    side = [f"new{i}" for i in range(30)]
    rendered = _render(base, base, side, 0, 3)

    assert "... 24 more lines ..." in rendered  # the removals
    for i in range(30):
        assert f"+new{i}" in rendered  # every addition, verbatim


def test_a_very_large_addition_is_still_capped():
    base = ["x"]
    side = [f"new{i}" for i in range(200)]
    rendered = _render(base, base, side, 0, 3)
    assert "more lines ..." in rendered
    assert len(rendered.splitlines()) < 50


# ── composing both sides when that is unambiguous ────────────────────────────


def block(ours: str, base: str, theirs: str) -> _Block:
    return _Block(
        branch_so_far=ours.splitlines(),
        base=base.splitlines(),
        replaying=theirs.splitlines(),
        marker_line=0,
    )


def test_a_block_the_branch_has_not_reached_composes():
    """The commonest shape by far: the branch is missing lines a later commit
    adds, and the replayed commit appends beside them."""
    resolved = auto_resolution(block(
        ours="a\nb\n",                    # has not reached c and d
        base="a\nb\nc\nd\n",
        theirs="a\nb\nc\nd\nappended\n",  # appends past the end
    ))
    assert resolved == ["a", "b", "appended"]


def test_insertions_at_different_points_compose():
    resolved = auto_resolution(block(
        ours="head\nfrom branch\nmiddle\ntail\n",
        base="head\nmiddle\ntail\n",
        theirs="head\nmiddle\nfrom replaying\ntail\n",
    ))
    assert resolved == ["head", "from branch", "middle", "from replaying", "tail"]


def test_one_side_leaving_the_block_alone_composes():
    resolved = auto_resolution(block(ours="a\nB\nc\n", base="a\nb\nc\n", theirs="a\nb\nc\n"))
    assert resolved == ["a", "B", "c"]


def test_both_sides_editing_the_same_line_is_refused():
    assert auto_resolution(block(ours="a\nMINE\nc\n", base="a\nb\nc\n", theirs="a\nTHEIRS\nc\n")) is None


def test_both_sides_appending_at_the_same_point_is_refused():
    """Both added at the end; nothing says which order was meant."""
    assert auto_resolution(block(
        ours="a\nfrom branch\n", base="a\n", theirs="a\nfrom replaying\n"
    )) is None


def test_an_insertion_inside_the_other_side_s_edit_is_refused():
    """It has no defined place once the surrounding lines are gone."""
    assert auto_resolution(block(
        ours="a\nREPLACED\nd\n",
        base="a\nb\nc\nd\n",
        theirs="a\nb\ninserted\nc\nd\n",
    )) is None


def test_a_deletion_and_a_distant_edit_compose():
    resolved = auto_resolution(block(
        ours="a\nb\nc\nd\nE\n",     # edits the last line
        base="a\nb\nc\nd\ne\n",
        theirs="a\nd\ne\n",          # deletes b and c
    ))
    assert resolved == ["a", "d", "E"]


def test_an_insertion_at_the_boundary_of_a_deletion_composes():
    """The shape a real branch turned up: the branch dropped a block a later
    commit adds, and the replayed commit puts something immediately before it.
    Treating any insertion touching an edit as ambiguous refused this."""
    resolved = auto_resolution(block(
        ours="head\ntail\n",                       # dropped the middle block
        base="head\nkept\nblock\ntail\n",
        theirs="head\nnew\nkept\nblock\ntail\n",   # inserts just before it
    ))
    assert resolved == ["head", "new", "tail"]


def test_an_insertion_at_the_end_boundary_composes_too():
    resolved = auto_resolution(block(
        ours="head\ntail\n",
        base="head\nkept\nblock\ntail\n",
        theirs="head\nkept\nblock\nnew\ntail\n",
    ))
    assert resolved == ["head", "new", "tail"]


# ── saying what each side did, in a sentence ─────────────────────────────────


def test_wrapping_a_block_reads_as_one_added_line_not_a_rewrite():
    """The shape that cost the most time on a real branch: the diff is the size
    of the block and the change is one line."""
    base = ["rows = []", "for p in packages:", "    rows.append(delta(p))"]
    wrapped = ["if current:", "    rows = []", "    for p in packages:",
               "        rows.append(delta(p))"]
    assert _summarise(base, wrapped) == "adds 1 line and reindents or moves 3 lines"


def test_a_changed_call_reads_as_one_line_each_way():
    base = ["rows = []", "    rows.append(delta(p))"]
    swapped = ["rows = []", "    rows.append(delta_count(p))"]
    assert _summarise(base, swapped) == "adds 1 line and removes 1 line"


def test_identical_lines_are_not_counted_as_moved():
    base = ["a", "b", "c"]
    assert _summarise(base, base) == "unchanged in this region"


def test_an_append_reads_as_an_append():
    base = ["a", "b"]
    assert _summarise(base, base + ["c"]) == "adds 1 line"


def test_a_region_the_branch_never_reached_reads_as_a_removal():
    assert _summarise(["a", "b", "c"], []) == "removes 3 lines"
