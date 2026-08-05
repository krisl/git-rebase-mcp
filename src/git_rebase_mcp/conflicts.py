"""Conflicts presented as two intents rather than as marker soup.

A conflict is two edits to the same starting text. Git renders that as three
interleaved blocks and leaves you to work out what each side did, which on code
that repeats -- a file of similar test functions, a block that moved and was
reindented -- is slow and easy to get wrong.

What actually resolves a conflict quickly is knowing what each side *did* to the
base: "wrap this loop in `if current:`" against "replace `delta` with
`delta_count(...)`" composes in seconds. So this module reports, per contested
region, the diff from the base to each side.

The idea is DiffDiff's (https://github.com/krisl/DiffDiff), which shows exactly
those two diffs in vim, and so is the boundary they are computed over.

That boundary matters more than it looks. Git's merge has already decided which
parts of the file could not be reconciled, and marked exactly those. Working out
the regions independently -- by diffing the whole of each side against the whole
of the base and intersecting -- re-derives that decision, and does it worse: two
independent diffs cannot know what a merge could reconcile, so a large block one
side has not reached yet gets fused with a one-line change next to it, and the
result is a region where one side has nothing at all to say. Measured on a real
branch: 4556 characters of output, of which one side was 104 lines of unchanged
context.

So the blocks come from git, via `git merge-file --diff3` over the three stages,
which is the same merge machinery without touching the working file.

In a rebase the sides are not "ours" and "theirs" in the sense anyone means by
those words -- they are inverted relative to a merge -- so they are named for
their roles: the branch built so far, and the commit being replayed.
"""

from __future__ import annotations

import itertools
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from patiencediff import PatienceSequenceMatcher  # type: ignore[import-untyped]

from .git import Git

CONTEXT = 3

# Added lines are elided far more reluctantly than removed ones, because they
# are what a caller copies to reapply the replayed commit's intent. Removed
# lines are context: a count conveys them. Past this many additions the region
# is big enough that include_full_sides is the better answer anyway.
ADDED_LIMIT = 40

# Index stages of a conflicted path, as git records them.
BASE, BRANCH_SO_FAR, REPLAYING = "1", "2", "3"

Opcode = tuple[str, int, int, int, int]


@dataclass(frozen=True)
class Sides:
    """The three whole texts, kept as a fallback for when the units are not enough."""

    base: str
    branch_so_far: str
    replaying: str


@dataclass(frozen=True)
class CollisionUnit:
    """One region both sides edited.

    `base_range` is 1-based and inclusive of its first line, matching what a
    diff header reports, so it can be quoted straight back to a person.
    """

    base_range: tuple[int, int]
    branch_so_far_diff: str
    replaying_diff: str


@dataclass(frozen=True)
class FileConflict:
    path: str
    units: tuple[CollisionUnit, ...]
    sides: Sides
    # Set when the two sides have no common ancestor for this path: both added
    # it, or one added it while the other deleted it. There is no base to diff
    # against, so the two-intents framing does not apply and the whole text of
    # each side is the only useful answer.
    no_common_base: bool = False


def read_conflict(git: Git, path: str, context: int = CONTEXT) -> FileConflict:
    """Describe one conflicted path, one region per block git could not merge."""
    base = _stage(git, BASE, path)
    branch = _stage(git, BRANCH_SO_FAR, path)
    replaying = _stage(git, REPLAYING, path)
    sides = Sides(base=base, branch_so_far=branch, replaying=replaying)

    # Git records no stage 1 when the sides share no ancestor for this path. With
    # no base there is nothing to diff against, so the two-intents framing does
    # not apply and the whole text of each side is the only useful answer.
    if not git.succeeds("rev-parse", "--verify", "--quiet", f":{BASE}:{path}"):
        return FileConflict(path=path, units=(), sides=sides, no_common_base=True)

    base_lines = base.splitlines()
    units: list[CollisionUnit] = []
    for block in _blocks(git, branch, base, replaying):
        start = _locate(base_lines, block.base, len(units) and units[-1].base_range[1] or 0)
        units.append(
            CollisionUnit(
                base_range=(start + 1, start + len(block.base)),
                branch_so_far_diff=_render(block.base, block.branch_so_far, start, context),
                replaying_diff=_render(block.base, block.replaying, start, context),
            )
        )
    return FileConflict(path=path, units=tuple(units), sides=sides)


@dataclass(frozen=True)
class _Block:
    """One region git marked as unmergeable, as its three sides."""

    branch_so_far: list[str]
    base: list[str]
    replaying: list[str]


def _blocks(git: Git, branch: str, base: str, replaying: str) -> list[_Block]:
    """Ask git which regions could not be merged, and return their three sides.

    `merge-file` runs the same merge as the rebase did, so the regions match the
    ones already marked in the working file -- but it writes to stdout, so
    nothing the caller may have started editing is disturbed.
    """
    return _parse_diff3(_merged(git, branch, base, replaying).splitlines())


def _merged(git: Git, branch: str, base: str, replaying: str) -> str:
    """git's own merge of the three sides, with conflict markers, as text."""
    with tempfile.TemporaryDirectory() as directory:
        paths: list[str] = []
        for name, text in (("ours", branch), ("base", base), ("theirs", replaying)):
            written = Path(directory) / name
            written.write_text(text)
            paths.append(str(written))
        return git.run("merge-file", "-p", "--diff3", *paths, check=False).stdout


def _parse_diff3(lines: Sequence[str]) -> list[_Block]:
    """Split merge-file's output into the blocks it could not merge."""
    blocks: list[_Block] = []
    side: list[str] | None = None
    ours: list[str] = []
    base: list[str] = []
    theirs: list[str] = []
    for line in lines:
        if line.startswith("<<<<<<<"):
            ours, base, theirs = [], [], []
            side = ours
        elif line.startswith("|||||||") and side is not None:
            side = base
        elif line.startswith("=======") and side is not None:
            side = theirs
        elif line.startswith(">>>>>>>") and side is not None:
            blocks.append(_Block(branch_so_far=ours, base=base, replaying=theirs))
            side = None
        elif side is not None:
            side.append(line)
    return blocks


def _locate(base_lines: list[str], section: list[str], from_line: int) -> int:
    """Where a block's base section sits in the base file.

    Blocks come in order and do not overlap, so the search starts after the last
    one. An empty section belongs at the search position: the block adds lines
    the base never had.
    """
    if not section:
        return from_line
    for start in range(from_line, len(base_lines) - len(section) + 1):
        if base_lines[start : start + len(section)] == section:
            return start
    return from_line


def auto_resolve_file(git: Git, path: str) -> str | None:
    """The whole file with every block composed, or None if any block cannot be.

    All or nothing on purpose: half-resolving a file leaves markers behind for
    the caller to find, which is worse than leaving the file as git wrote it.
    """
    base = _stage(git, BASE, path)
    if not git.succeeds("rev-parse", "--verify", "--quiet", f":{BASE}:{path}"):
        return None  # no common base, so there is nothing to compose against

    merged = _merged(git, _stage(git, BRANCH_SO_FAR, path), base, _stage(git, REPLAYING, path))
    resolved: list[str] = []
    block_lines: list[str] = []
    inside = False
    for line in merged.splitlines():
        if line.startswith("<<<<<<<"):
            inside, block_lines = True, [line]
        elif inside:
            block_lines.append(line)
            if line.startswith(">>>>>>>"):
                blocks = _parse_diff3(block_lines)
                composed = auto_resolution(blocks[0]) if blocks else None
                if composed is None:
                    return None
                resolved.extend(composed)
                inside = False
        else:
            resolved.append(line)
    return "".join(line + "\n" for line in resolved)


def auto_resolution(block: _Block) -> list[str] | None:
    """Both sides applied together, when that is unambiguous. Otherwise None.

    Git marks a region as conflicted when the two sides edited near each other,
    not only when they edited the same thing. A great many of those are
    decidable without understanding the language at all: the branch has not
    reached a block a later commit adds, and the replayed commit appends beside
    it; or the two sides insert at different points. Applying both is the only
    answer either side would recognise, and doing it by hand is where the time
    goes.

    Unambiguous means the base lines the two sides touch do not overlap. An
    insertion occupies no base lines, so it is widened to one for that test:
    two insertions at the same point have no defined order, and a insertion
    inside the other side's edit has no defined place, so both are refused.
    """
    ours = _edits(block.base, block.branch_so_far)
    theirs = _edits(block.base, block.replaying)
    # Only across the two sides: the edits within one diff never overlap.
    for one, other in itertools.product(ours, theirs):
        if _overlaps(_widened(one), _widened(other)):
            return None
    edits = ours + theirs

    resolved: list[str] = []
    position = 0
    for start, end, replacement in sorted(edits):
        resolved.extend(block.base[position:start])
        resolved.extend(replacement)
        position = end
    resolved.extend(block.base[position:])
    return resolved


def _edits(base: list[str], side: list[str]) -> list[tuple[int, int, list[str]]]:
    """What one side did to the base: the range it replaced, and with what."""
    return [
        (i1, i2, side[j1:j2])
        for tag, i1, i2, j1, j2 in _opcodes(base, side)
        if tag != "equal"
    ]


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _widened(edit: tuple[int, int, list[str]]) -> tuple[int, int]:
    """An edit's base range, with an insertion given one line to collide over."""
    start, end, _ = edit
    return (start, end if end > start else start + 1)


def _stage(git: Git, stage: str, path: str) -> str:
    """One side of a conflicted path.

    A stage is absent when only one side has the file at all -- an add/add or a
    delete/modify conflict -- and empty text is the honest reading of that.
    """
    result = git.run("show", f":{stage}:{path}", check=False)
    return result.stdout if result.ok else ""


def _opcodes(base: list[str], other: list[str]) -> list[Opcode]:
    """How `other` differs from `base`, as difflib-shaped opcodes.

    Patience rather than difflib's default: on text with many similar blocks the
    default matcher pairs up the wrong ones, and the hunk boundaries it invents
    are this module's entire output.
    """
    # patiencediff ships no type information; the cast is where that stops
    # spreading, so nothing downstream has to deal with an unknown type.
    return cast("list[Opcode]", PatienceSequenceMatcher(None, base, other).get_opcodes())


def _render(base: list[str], side: list[str], start: int, context: int) -> str:
    """A unified diff of one side of a block, against the block's base.

    Line numbers are absolute in the base file, so they can be checked against
    it. A side that did not touch this block says so in one line instead of
    repeating it: the region was chosen because *some* side changed it, and
    printing unchanged text for the other is what made this output unreadable.
    """
    opcodes = _opcodes(base, side)
    if all(tag == "equal" for tag, *_ in opcodes):
        return "(unchanged in this region)"

    body: list[str] = []
    for index, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            kept = _trim(base[i1:i2], context, first=index == 0, last=index == len(opcodes) - 1)
            body += [" " + line for line in kept]
        else:
            body += _changed(base[i1:i2], "-", context)
            body += _changed(side[j1:j2], "+", ADDED_LIMIT // 2)
    header = f"@@ -{start + 1},{len(base)} +{start + 1},{len(side)} @@"
    return "\n".join([header, *body])


def _changed(lines: list[str], prefix: str, keep: int) -> list[str]:
    """One side's added or removed lines, with a long run summarised.

    A hundred removed lines are a hundred lines of output saying one thing: the
    branch has not reached them yet, and a count says it in one. Additions get a
    much larger allowance: they are the text a caller copies to reapply the
    commit, so summarising them makes the region unusable rather than merely
    long.
    """
    if len(lines) <= keep * 2 + 1:
        return [prefix + line for line in lines]
    return (
        [prefix + line for line in lines[:keep]]
        + [f"{prefix}... {len(lines) - keep * 2} more lines ..."]
        + [prefix + line for line in lines[-keep:]]
    )


def _trim(lines: list[str], context: int, first: bool, last: bool) -> list[str]:
    """Keep only the context around a change, with an elision in the middle."""
    if len(lines) <= context * 2 + 1:
        return lines
    if first:
        return ["..."] + lines[-context:]
    if last:
        return lines[:context] + ["..."]
    return lines[:context] + ["..."] + lines[-context:]
