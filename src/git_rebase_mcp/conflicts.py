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
from collections import Counter
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

# Which of git's own funcname drivers to ask for, by extension. Git ships
# twenty-five and maintains them; what it does not do is pick one, because
# `diff=python` is something a repository states in .gitattributes. Most do not,
# and git's fallback then recognises a definition at column 0 only -- which in
# any language whose definitions nest names the class every time and the method
# never. So the choice is made here, and only the choice: the patterns stay
# git's. A repository that has stated its own driver is asked first, and a
# language with no entry falls back to git's rule, as it does for git.
FUNCNAME_DRIVER = {
    "adb": "ada", "ads": "ada",
    "bash": "bash", "sh": "bash",
    "bib": "bibtex",
    "c": "cpp", "cc": "cpp", "cpp": "cpp", "cxx": "cpp", "c++": "cpp",
    "h": "cpp", "hh": "cpp", "hpp": "cpp", "hxx": "cpp",
    "cs": "csharp",
    "css": "css",
    "dts": "dts", "dtsi": "dts",
    "ex": "elixir", "exs": "elixir",
    "f": "fortran", "f90": "fortran", "f95": "fortran", "for": "fortran",
    "fountain": "fountain",
    "go": "golang",
    "htm": "html", "html": "html", "xhtml": "html",
    "java": "java",
    "kt": "kotlin", "kts": "kotlin",
    "markdown": "markdown", "md": "markdown",
    "mm": "objc",
    "pas": "pascal", "pp": "pascal",
    "pl": "perl", "pm": "perl",
    "php": "php",
    "py": "python",
    "rb": "ruby",
    "rs": "rust",
    "scm": "scheme", "ss": "scheme",
    "tex": "tex",
}

# Inserted at each region to make git produce a hunk there, and to say which
# region a hunk answers for. A private-use character cannot occur in source and
# cannot match a funcname pattern, so it neither collides with the file nor is
# mistaken for a definition by the region below it.
MARKER = "region "

Opcode = tuple[str, int, int, int, int]


@dataclass(frozen=True)
class Sides:
    """The three whole texts, kept as a fallback for when the units are not enough."""

    base: str
    branch_so_far: str
    replaying: str


@dataclass(frozen=True)
class Attribution:
    """A commit that last touched some of the branch's lines in a region."""

    sha: str
    subject: str
    # False when the commit predates the rebase: the lines are upstream code the
    # branch never touched, which is a different thing from the branch's intent.
    from_this_branch: bool


@dataclass(frozen=True)
class CollisionUnit:
    """One region both sides edited.

    `base_range` is 1-based and inclusive of its first line, matching what a
    diff header reports, so it can be quoted straight back to a person. It is
    None for a block whose text the base file does not contain: there is no
    position to report, and the previous block's would be an invention.
    """

    base_range: tuple[int, int] | None
    branch_so_far_diff: str
    replaying_diff: str
    # One sentence for what each side did, with lines that only moved or changed
    # indentation counted separately. A block wrapped in an `if` and reindented
    # is a large diff and a small change, and the diff alone does not say which.
    branch_so_far_summary: str = ""
    replaying_summary: str = ""
    # What the branch side of this region came from. The replayed commit states
    # its intent in its message; the branch so far is an accumulation with no
    # message, so the commits behind these lines are the nearest equivalent.
    branch_so_far_commits: tuple[Attribution, ...] = ()


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
    # Set when every block is both sides inserting at the same point, which
    # composing refuses because nothing says which order was meant. It is
    # almost always "both, branch first", so saying so lets a caller answer in
    # one call instead of reading the file to work out the same thing.
    both_inserted: bool = False


def read_conflict(
    git: Git, path: str, context: int = CONTEXT, branch_base: str | None = None
) -> FileConflict:
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
    branch_lines = branch.splitlines()
    blocks = _blocks(git, branch, base, replaying)

    # Where each block sits in the base file. Blocks come in order and do not
    # overlap, so each search starts where the last block ended. Worked out for
    # all of them before any unit is built, because naming what a region sits
    # inside is one question asked of git about the whole file.
    # A block whose base text the file does not contain has no position, and
    # says so rather than borrowing the previous block's -- but it keeps its two
    # diffs, which are what the region is read for. Refusing the whole file over
    # it would take every other region down with it, in the one call somebody
    # makes when they are stuck.
    starts: list[int | None] = []
    search_from = 0
    for block in blocks:
        found = _locate(base_lines, block.base, search_from)
        starts.append(found)
        if found is not None:
            search_from = found + len(block.base)
    enclosings = _enclosing(git, path, base_lines, starts)

    units: list[CollisionUnit] = []
    for block, start, enclosing in zip(blocks, starts, enclosings, strict=True):
        units.append(
            CollisionUnit(
                base_range=None if start is None else (start + 1, start + len(block.base)),
                branch_so_far_diff=_render(
                    base_lines, block.base, block.branch_so_far, start, context, enclosing
                ),
                replaying_diff=_render(
                    base_lines, block.base, block.replaying, start, context, enclosing
                ),
                branch_so_far_summary=_summarise(block.base, block.branch_so_far),
                replaying_summary=_summarise(block.base, block.replaying),
                branch_so_far_commits=_attribution(
                    git, path, branch_lines, block.branch_so_far, branch_base
                ),
            )
        )
    return FileConflict(
        path=path,
        units=tuple(units),
        sides=sides,
        both_inserted=bool(blocks) and all(not block.base for block in blocks),
    )


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


def _locate(base_lines: list[str], section: list[str], from_line: int) -> int | None:
    """Where a block's base section sits in the base file.

    Blocks come in order and do not overlap, so the search starts after the last
    one. An empty section belongs at the search position: the block adds lines
    the base never had. Anything else that is not there is reported as None
    rather than guessed at -- a base_range invented from the search position
    would be shown to a person as fact.
    """
    if not section:
        return from_line
    for start in range(from_line, len(base_lines) - len(section) + 1):
        if base_lines[start : start + len(section)] == section:
            return start
    return None


def take_side(git: Git, path: str, side: str) -> str:
    """The whole file with every conflict block resolved the same stated way.

    For the blocks composing refuses: two insertions at the same point, which is
    almost always "both, in this order", and a genuine disagreement, which is
    almost always one side or the other. Saying which is far cheaper than
    sending the finished file back, and it cannot introduce a typo.
    """
    base = _stage(git, BASE, path)
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
                block = _parse_diff3(block_lines)[0]
                resolved.extend(_chosen(block, side))
                inside = False
        else:
            resolved.append(line)
    return "".join(line + "\n" for line in resolved)


def _chosen(block: _Block, side: str) -> list[str]:
    if side == "branch":
        return block.branch_so_far
    if side == "replaying":
        return block.replaying
    if side == "both":
        # The branch first: it is what the file already reads like, and the
        # replayed commit is the newer thought arriving on top of it.
        return block.branch_so_far + block.replaying
    raise ValueError(f"side must be branch, replaying or both, not {side!r}")


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

    Unambiguous means the base lines the two sides touch do not overlap, with
    two cases for insertions, which occupy no base lines: two at the same point
    have no defined order, and one strictly inside the other side's edit has no
    defined place once the surrounding lines are gone. An insertion at the
    *boundary* of the other side's edit is fine, and it is the common case --
    the branch dropped a block a later commit adds, and the replayed commit puts
    something immediately before it.
    """
    ours = _edits(block.base, block.branch_so_far)
    theirs = _edits(block.base, block.replaying)
    # Only across the two sides: the edits within one diff never overlap.
    for one, other in itertools.product(ours, theirs):
        if _ambiguous((one[0], one[1]), (other[0], other[1])):
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


def _ambiguous(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """Whether two edits to the same base have no single obvious composition."""
    a_inserts, b_inserts = a[0] == a[1], b[0] == b[1]
    if a_inserts and b_inserts:
        return a[0] == b[0]  # same point, and nothing says which comes first
    if a_inserts:
        return b[0] < a[0] < b[1]  # strictly inside: no place left to put it
    if b_inserts:
        return a[0] < b[0] < a[1]
    return a[0] < b[1] and b[0] < a[1]  # two real ranges: plain overlap


def _summarise(base: list[str], side: list[str]) -> str:
    """What one side did to a region, in a sentence.

    Lines whose content is unchanged apart from leading whitespace are counted
    as reindented rather than as an addition and a removal. Wrapping a block in
    an `if` produces a diff the size of the block and a change of one line, and
    reading the diff is the slow way to find that out.
    """
    # Lines that are identical are not a change of any kind, so they come out
    # before anything is counted; what is left is compared without indentation.
    untouched = Counter(base) & Counter(side)
    gone = Counter(line.strip() for line in (Counter(base) - untouched).elements() if line.strip())
    arrived = Counter(line.strip() for line in (Counter(side) - untouched).elements() if line.strip())
    moved = sum((gone & arrived).values())
    added = sum((arrived - gone).values())
    removed = sum((gone - arrived).values())

    parts: list[str] = []
    if added:
        parts.append(f"adds {plural(added, 'line')}")
    if removed:
        parts.append(f"removes {plural(removed, 'line')}")
    if moved:
        parts.append(f"reindents or moves {plural(moved, 'line')}")
    return " and ".join(parts) if parts else "unchanged in this region"


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _attribution(
    git: Git,
    path: str,
    branch_lines: list[str],
    block_lines: list[str],
    branch_base: str | None,
) -> tuple[Attribution, ...]:
    """Which commits last touched the branch's lines in this region.

    During a rebase HEAD is the branch built so far, so blaming it answers the
    question the replayed commit's message answers for the other side. A region
    the branch left empty has no lines to attribute, and says so by being empty
    rather than by attributing something next to it.
    """
    if not block_lines:
        return ()
    located = _locate(branch_lines, block_lines, 0)
    if located is None:
        return ()
    first = located + 1
    blamed = git.run(
        "blame", "--line-porcelain", "-L", f"{first},{first + len(block_lines) - 1}",
        "HEAD", "--", path, check=False,
    )
    if not blamed.ok:
        return ()

    found: dict[str, str] = {}
    sha = ""
    for line in blamed.stdout.splitlines():
        head = line.split(" ", 1)[0]
        if len(head) == 40 and all(c in "0123456789abcdef" for c in head):
            sha = head
        elif line.startswith("summary ") and sha:
            found.setdefault(sha, line[len("summary ") :])
    return tuple(
        Attribution(sha=sha, subject=subject, from_this_branch=_within(git, branch_base, sha))
        for sha, subject in found.items()
    )


def _within(git: Git, branch_base: str | None, sha: str) -> bool:
    """Whether a commit is part of what this rebase is replaying."""
    if branch_base is None:
        return False
    return not git.succeeds("merge-base", "--is-ancestor", sha, branch_base)


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


def _enclosing(
    git: Git, path: str, file_base: list[str], starts: Sequence[int | None]
) -> list[str]:
    """Which definition each region sits inside, named the way git names it.

    Git already works this out for its own hunk headers, using the language's
    funcname driver, and it ships twenty-five of them. So rather than keep
    patterns here, a marker is inserted at each region and git is asked to diff
    the file against itself: what comes back on each hunk header is the answer
    for the region whose marker that hunk contains, from the language's own
    driver rather than from a pattern this module guessed at.

    Answered from the region rather than from the first line shown, so it does
    not change when the caller asks for more surrounding lines: it is where the
    contested region lives, not a property of how much of the file is on show.
    """
    # A region with no position has nothing to be inside, so it gets no marker
    # and keeps the empty label it starts with.
    placed = [(index, start) for index, start in enumerate(starts) if start is not None]
    if not placed:
        return [""] * len(starts)
    marked = list(file_base)
    for index, start in sorted(placed, key=lambda pair: pair[1], reverse=True):
        marked.insert(start, f"{MARKER}{index}")

    labels = [""] * len(starts)
    with tempfile.TemporaryDirectory() as directory:
        # Named for the real file, because the funcname driver is chosen by
        # matching the path against the attributes file's pattern.
        name = Path(path).name
        written: list[str] = []
        for folder, text in (("before", file_base), ("after", marked)):
            target = Path(directory) / folder / name
            target.parent.mkdir()
            target.write_text("\n".join(text) + "\n")
            written.append(str(target))
        attributes = Path(directory) / "attributes"
        attributes.write_text(f"* diff={_driver(git, path)}\n")
        diff = git.run(
            "-c", f"core.attributesFile={attributes}",
            "diff", "--no-index", "--unified=0", *written,
            check=False,
        )

    label = ""
    for line in diff.stdout.splitlines():
        if line.startswith("@@"):
            # `@@ -1,0 +2,1 @@ def render(self):` -- everything past the second
            # marker is what git decided the hunk sits inside.
            label = line.split("@@", 2)[2].strip()
        elif line.startswith(f"+{MARKER}"):
            labels[int(line[len(MARKER) + 1 :])] = label
    return labels


def _driver(git: Git, path: str) -> str:
    """The funcname driver to ask git for, on this path.

    A repository that has stated its own in .gitattributes gets that, including
    a custom one it defined itself; otherwise the language is taken from the
    extension. `default` is git's own fallback, which is what an unknown
    extension should get -- and is a real driver name, so it is safe to set.
    """
    stated = git.out("check-attr", "diff", "--", path).rpartition(": ")[2]
    if stated not in ("unspecified", "unset", "set", ""):
        return stated
    return FUNCNAME_DRIVER.get(path.rpartition(".")[2].lower(), "default")


def _render(
    file_base: list[str],
    base: list[str],
    side: list[str],
    start: int | None,
    context: int,
    enclosing: str = "",
) -> str:
    """A unified diff of one side of a block, against the block's base.

    Line numbers are absolute in the base file, so they can be checked against
    it. A side that did not touch this block says so in one line instead of
    repeating it: the region was chosen because *some* side changed it, and
    printing unchanged text for the other is what made this output unreadable.

    The surrounding lines come from the file rather than the block: git marks
    only what could not be merged, so a block on its own can be a single line
    with nothing to place it by. The header names the definition the region is
    inside, as git's does; asking for more context is how you find out whether
    the lines above already do what the replayed commit is adding.

    A `start` of None is a block the base file does not contain, so there are no
    surrounding lines to show and no numbers to put in the header. It says that,
    rather than counting from a line it had to pick.
    """
    opcodes = _opcodes(base, side)
    if all(tag == "equal" for tag, *_ in opcodes):
        return "(unchanged in this region)"

    changes = _changes(opcodes, base, side, context)
    if start is None:
        header = f"@@ not found in the base file @@ {enclosing}"
        return "\n".join([header.rstrip(), *changes])

    leading = file_base[max(0, start - context) : start]
    trailing = file_base[start + len(base) : start + len(base) + context]
    body = [" " + line for line in leading] + changes + [" " + line for line in trailing]
    first = start - len(leading) + 1
    span = len(leading) + len(trailing)
    header = f"@@ -{first},{len(base) + span} +{first},{len(side) + span} @@ {enclosing}"
    return "\n".join([header.rstrip(), *body])


def _changes(opcodes: list[Opcode], base: list[str], side: list[str], context: int) -> list[str]:
    """The block itself, as diff lines: what one side did to the base section."""
    body: list[str] = []
    for index, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            kept = _trim(base[i1:i2], context, first=index == 0, last=index == len(opcodes) - 1)
            body += [" " + line for line in kept]
        else:
            body += _changed(base[i1:i2], "-", context)
            body += _changed(side[j1:j2], "+", ADDED_LIMIT // 2)
    return body


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
