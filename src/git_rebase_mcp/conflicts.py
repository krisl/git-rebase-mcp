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
those two diffs in vim. The mechanism differs: DiffDiff parses conflict markers
because it works inside a buffer, whereas git keeps all three sides in the index
as stages 1, 2 and 3, so there is nothing to parse.

In a rebase the sides are not "ours" and "theirs" in the sense anyone means by
those words -- they are inverted relative to a merge -- so they are named for
their roles: the branch built so far, and the commit being replayed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from patiencediff import PatienceSequenceMatcher  # type: ignore[import-untyped]

from .git import Git

CONTEXT = 3

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


def read_conflict(git: Git, path: str, context: int = CONTEXT) -> FileConflict:
    """Build the collision units for one conflicted path."""
    base = _stage(git, BASE, path)
    branch = _stage(git, BRANCH_SO_FAR, path)
    replaying = _stage(git, REPLAYING, path)

    base_lines = base.splitlines()
    branch_ops = _opcodes(base_lines, branch.splitlines())
    replaying_ops = _opcodes(base_lines, replaying.splitlines())

    units = tuple(
        CollisionUnit(
            base_range=(window[0] + 1, window[1]),
            branch_so_far_diff=_render(base_lines, branch.splitlines(), branch_ops, window),
            replaying_diff=_render(base_lines, replaying.splitlines(), replaying_ops, window),
        )
        for window in _contested_windows(branch_ops, replaying_ops, len(base_lines), context)
    )
    return FileConflict(
        path=path,
        units=units,
        sides=Sides(base=base, branch_so_far=branch, replaying=replaying),
    )


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


def _edited_ranges(ops: list[Opcode]) -> list[tuple[int, int]]:
    """Base line ranges a side touched, half-open and 0-based.

    An insertion occupies no base lines, so it is widened to one to give it
    something to overlap with.
    """
    ranges: list[tuple[int, int]] = []
    for tag, i1, i2, _, _ in ops:
        if tag == "equal":
            continue
        ranges.append((i1, i2 if i2 > i1 else i1 + 1))
    return ranges


def _contested_windows(
    branch_ops: list[Opcode], replaying_ops: list[Opcode], base_length: int, context: int
) -> list[tuple[int, int]]:
    """Regions both sides edited, widened by context lines.

    Regions only one side touched are left out on purpose: git merged those
    cleanly and they are already in the working file. What needs a decision is
    where the two edits meet.
    """
    branch_ranges = _edited_ranges(branch_ops)
    replaying_ranges = _edited_ranges(replaying_ops)

    merged = _merge_overlapping(sorted(branch_ranges + replaying_ranges))
    contested = [
        window
        for window in merged
        if any(_overlaps(window, r) for r in branch_ranges)
        and any(_overlaps(window, r) for r in replaying_ranges)
    ]
    return [
        (max(0, start - context), min(base_length, end + context)) for start, end in contested
    ]


def _merge_overlapping(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Join ranges that overlap or touch, so one region is reported once."""
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _render(
    base: list[str], side: list[str], ops: list[Opcode], window: tuple[int, int]
) -> str:
    """A unified diff of one side, over the base lines in `window`.

    Line numbers are absolute so they can be compared against the file, which
    means the header is assembled here rather than by difflib.
    """
    start, end = window
    body: list[str] = []
    side_start: int | None = None
    side_end = 0

    for tag, i1, i2, j1, j2 in ops:
        if i2 <= start and not (tag == "insert" and i1 >= start):
            continue
        if i1 >= end:
            break
        low, high = max(i1, start), min(i2, end)
        if tag == "equal":
            body += [" " + line for line in base[low:high]]
            side_low, side_high = j1 + (low - i1), j1 + (high - i1)
        else:
            body += ["-" + line for line in base[low:high]]
            body += ["+" + line for line in side[j1:j2]]
            side_low, side_high = j1, j2
        if side_start is None:
            side_start = side_low
        side_end = side_high

    if side_start is None:
        side_start = side_end = 0
    header = f"@@ -{start + 1},{end - start} +{side_start + 1},{side_end - side_start} @@"
    return "\n".join([header, *body])
