"""The MCP tools.

Thin wiring over the modules that do the work. The one thing it adds is that
every reply says what is safe to do next, at the point where a caller would
otherwise have to infer it -- which is where the mistakes happen.

Internally the rebase state is a closed union so that unsafe operations can be
made unrepresentable. On the wire it is flattened into one shape with a
discriminator, because a stable schema is easier for a caller to rely on.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Callable, Sequence
from functools import wraps
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Never, ParamSpec, TypeVar, assert_never, cast

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import TypeAdapter

from .conflicts import (
    repeated_lines,
    FileConflict,
    auto_resolve_file,
    deleted_side,
    plural,
    read_conflict,
    side_text,
    take_side,
)
from .git import Git, GitError, GitResult
from .render import render
from .invariants import (
    Change,
    Backup,
    CarriedRef,
    CommitChange,
    Session,
    clear_session,
    commits_with_markers,
    compare_commits,
    has_markers,
    load_session,
    locals_the_backup_tracks,
    locals_the_rewrite_removed,
    record_backup,
    same_tree,
    save_session,
    branch_change,
)
from .plan import DROPPING_ACTIONS, TODO_LINE, check_plan, todo_stopping_at
from .state import (
    COMMITTING_ACTIONS,
    STOPPING_ACTIONS,
    Applying,
    Commit,
    Conflicted,
    NotRebasing,
    Operation,
    RebaseState,
    StoppedAfterApply,
    StoppedWithoutApply,
    is_rebasing,
    read_state,
)

mcp = MCPServer(
    "git-rebase",
    instructions=(
        "Drives an interactive git rebase safely, and reads any conflict -- a "
        "cherry-pick, revert or merge that stopped, or a rebase somebody started "
        "by hand -- as what each side did rather than as markers. Call "
        "status before acting: it names the operation in progress, and "
        "reports whether the commit being replayed has actually been created "
        "yet, which decides whether amending would rewrite the commit you mean "
        "or the one before it."
    ),
)

# Record every resolution and replay it if the same conflict comes back. A rebase
# that is aborted and retried, or a series replayed onto a different base, hits
# the identical conflicts a second time; without this they are resolved again by
# hand for nothing. Set per invocation rather than written to the repo config,
# because turning on a recording facility in someone's repository is not this
# tool's decision to make.
#
# rerere.autoUpdate stays off on purpose: a replayed resolution is a guess from
# an earlier context and should be looked at before it is staged.
RERERE = ("-c", "rerere.enabled=true")

# How much of an observed check's output to carry back. The tail rather than
# the head: a test runner puts the summary last, and the summary is the answer.
CHECK_OUTPUT = 2000

StateName = Literal[
    "not_rebasing",
    "conflicted",
    # Mid cherry-pick, revert or merge with nothing unmerged: the conflicts are
    # resolved and staged, and the operation still has to be told to commit them.
    "applying",
    "stopped_after_apply",
    "stopped_without_apply",
]


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    subject: str


@dataclass(frozen=True)
class StepInfo:
    index: int
    total: int


@dataclass(frozen=True)
class FinishedRebase:
    """A rebase this server started that is no longer running and not yet checked.

    Covers the branch being left where it started as well as rewritten, because
    the two are not distinguishable from the state alone -- `git rebase --abort`
    run by hand ends the rebase exactly as finishing it does -- and reporting a
    rebase as finished when it was abandoned would be a guess presented as fact.
    `branch_moved` is that difference.

    A count rather than the commits themselves: rebase_finish returns those,
    together with the check that they still make the branch's change. What is
    needed here is only the difference between nothing having happened and a
    branch that has been rewritten with nobody having verified it.
    """

    # Commits between the base the rebase was given and HEAD now: what the branch
    # has, not how many steps the todo ran. A run of fixups leaves fewer commits
    # than it consumed, and the number a caller wants is the one it can go and
    # read.
    rewritten: int
    base: str
    backup_ref: str
    branch_moved: bool


@dataclass(frozen=True)
class CheckResult:
    """What an observed `check_command` said at this stop.

    Present only for a rebase started with `check_halts=False`, and absent at a
    conflicted stop, where the working tree still holds markers and the answer
    would be about the conflict rather than about the commit.
    """

    command: str
    ok: bool
    output: str


@dataclass(frozen=True)
class StatusReport:
    state: StateName
    head: CommitInfo
    head_is_replaying_commit: bool
    can_amend: bool
    guidance: str
    # Set when a rebase this server started has ended and rebase_finish has not
    # checked it yet. A field rather than a sixth `state`, for the reason the
    # conflict states share one name: `state` answers "is a rebase in progress",
    # and a caller reading it for that would have to learn a new name to keep
    # getting the same answer right. What was missing was never the name -- it
    # was that "not_rebasing" alone reads as "nothing happened" at the moment a
    # branch has just been rewritten and not verified.
    finished: FinishedRebase | None = None
    # What left the index in this state: "rebase", "cherry-pick", "revert",
    # "merge", or "unknown" for a conflict nothing recorded -- a stash popped
    # into one, say. `state` stays "conflicted" for all of them, because what a
    # caller does first is the same in every case, and a second state name would
    # only be a way for a caller checking the old one to miss a real conflict.
    operation: Operation | None = None
    step: StepInfo | None = None
    action: str | None = None
    replaying: CommitInfo | None = None
    # The step's commit has been taken back out with a mixed reset, so its
    # changes are in the working tree and HEAD is the commit before it. Git keeps
    # its own "you may amend" record through that, so a caller reading only
    # `state` would be told a stop where amending is safe.
    unapplied: bool = False
    # The step's action was `edit` or `reword`, which stop for the caller once
    # the commit applies -- and it conflicted instead, so this stop is the one
    # that stop would have been. Continuing commits the resolution and moves to
    # the next step; there is no second stop at which to make the change.
    #
    # Kept beside `can_amend` because the two answer questions that sound alike
    # and are not. `can_amend` is about which commit HEAD is *now*; this is
    # about whether there will be another chance *later*. A caller can read the
    # first correctly, do exactly what it says, and still lose the edit.
    action_stop_lost: bool = False
    conflicted_files: tuple[str, ...] = ()
    # Paths this server composed and staged without asking, because the two
    # sides edited different lines. Named rather than left silent: an automatic
    # resolution is still a resolution, and worth a look.
    auto_resolved: tuple[str, ...] = ()
    # Paths git resolved from a resolution recorded on an earlier run of the
    # same conflict. The same argument as `auto_resolved`, and it applies
    # harder: this one was decided in a context the caller may no longer be
    # in -- a rebase abandoned and restarted onto a moved base hits the
    # identical conflict and replays an answer written for the old one. It was
    # visible only as a line inside git's passthrough text, which is not where
    # a caller looks for something it is being asked to check.
    replayed_resolutions: tuple[str, ...] = ()
    # What git printed when it stopped, when this report follows a git command.
    # Git often explains a stop in a way nothing else can reconstruct -- "the
    # previous cherry-pick is now empty" being the one that cost the most time --
    # and swallowing it leaves the caller guessing.
    git_said: str = ""
    # Which checkout this report is about, and what is on it. Every tool here
    # takes `repo` and is therefore always right about it; a shell in the same
    # session is not, and a repository with worktrees has several checkouts of
    # one history side by side. Saying it back means a caller reading `git show
    # HEAD:file` from the wrong directory has this server's own answer next to
    # its own, instead of two numbers it has no way to tell apart. Cost one
    # `rev-parse`; the alternative was a rebase abandoned on a misreading.
    worktree: str = ""
    branch: str = ""
    # What an observed check said here. See `CheckResult`, and `check_halts` on
    # rebase_start for why a check would be observed rather than enforced.
    check: CheckResult | None = None


def status(repo: str = ".") -> StatusReport:
    """Report what `repo` is currently doing.

    Call this before amending, continuing or resolving. `operation` names what
    is in progress -- a rebase, cherry-pick, revert or merge, or "unknown" for a
    conflicted index nothing recorded -- and `head_is_replaying_commit`
    distinguishes a stop where the commit was applied from one where it
    conflicted part-way, which git's own output does not.

    `state` is "conflicted" for every operation that left unmerged paths, so
    that one check answers the question whatever produced them.

    `finished` is set when a rebase this server started has ended and
    rebase_finish has not checked it yet, which "not_rebasing" on its own cannot
    say -- and the state a caller most needs telling about, since the branch is
    rewritten and nothing has verified it.
    """
    git = _git(repo)
    return _report(read_state(git), git=git)


@dataclass(frozen=True)
class UnitReport:
    """One region both sides edited, as what each side did to the base.

    `base_range` is None where the base file does not contain the block's text,
    so the region has no position to report. Its two diffs still say what each
    side did, which is what the region is read for.
    """

    base_range: tuple[int, int] | None
    branch_so_far_diff: str
    replaying_diff: str
    # One sentence for what each side did. A block wrapped in an `if` and
    # reindented is a large diff and a small change; the diff does not say which.
    branch_so_far_summary: str = ""
    replaying_summary: str = ""
    # The commits behind the branch's lines here. The replayed commit states its
    # intent in its message; this is the nearest the other side has to one.
    branch_so_far_commits: tuple[CommitInfo, ...] = ()


@dataclass(frozen=True)
class FileReport:
    path: str
    units: tuple[UnitReport, ...]
    # True when the sides share no ancestor for this path, so there are no units
    # and the whole texts below are the answer. They are always included in that
    # case, regardless of include_full_sides, because nothing else is on offer.
    no_common_base: bool = False
    # Every block is both sides inserting at the same point. Nothing says which
    # order was meant, so composing refuses -- but `take="both"` answers it.
    both_inserted: bool = False
    # "branch" or "replaying" for a path that side deleted while the other
    # modified it. There are no units: what is being decided is whether the path
    # lives, and `take` names the side whose answer to that to stage.
    deleted_by: str | None = None
    base: str | None = None
    branch_so_far: str | None = None
    replaying: str | None = None
    # Everything the replayed commit did to this file, not only the contested
    # region. Wanted often enough in real use to be worth not leaving the tool
    # for: what a commit did elsewhere in a file is how you tell whether the
    # region in front of you is the whole of its intent.
    replaying_file_diff: str | None = None


@dataclass(frozen=True)
class ConflictReport:
    replaying: CommitInfo | None
    replaying_body: str
    files: tuple[FileReport, ...]
    guidance: str


@dataclass(frozen=True)
class RepeatedLine:
    """A line the resolution has more copies of than either side did."""

    line: str
    in_resolution: int
    in_branch: int
    in_replaying: int


@dataclass(frozen=True)
class ResolveReport:
    path: str
    still_conflicted: tuple[str, ...]
    guidance: str
    # Lines the staged text has more copies of than either side had. Reported
    # rather than refused: taking both sides legitimately doubles a line the two
    # of them share, and a resolution is the caller's decision. But a resolution
    # that replaced more than the contested region and re-added lines already
    # merged below it looks exactly like this, and nothing else downstream
    # objects -- the markers are gone, the file parses, and a route registered
    # twice or an import repeated is legal code that reaches a commit.
    repeated: tuple[RepeatedLine, ...] = ()
    # True when what was staged is the path's removal rather than any text. Said
    # out loud because every other resolution leaves a file behind, and a caller
    # that asked for a side without knowing that side had deleted it should hear
    # about it here rather than notice at the next `git status`.
    deleted: bool = False


def conflicts(
    repo: str = ".",
    context: int = 3,
    include_full_sides: bool = False,
    include_file_diffs: bool = False,
) -> ConflictReport:
    """Report each conflict as what the two sides did, rather than as markers.

    Works on any conflict git has recorded, not only a rebase this server
    started: a cherry-pick, a revert, a merge, a rebase begun by hand, or a
    stash that popped into one. All of them leave the same three stages in the
    index, which is what this reads.

    Per contested region you get two diffs from the common base: `branch_so_far`
    is what is already here, `replaying` is what is being applied over it -- the
    replayed or cherry-picked commit, the revert, or the branch being merged in.
    Read them as two intents and compose them -- "wrap this block in an `if`"
    plus "swap this call" is usually just both.

    Regions only one side changed are not listed: git merged those already.

    `context` is how many unchanged lines to show around each change. Raise it
    when the region is hard to place -- whether the lines above already do what
    the incoming side is adding.

    `include_file_diffs` adds everything the incoming side did to each file, not
    only the contested part, which is how you tell whether the region in front
    of you is the whole of its intent. `include_full_sides` gives the three
    whole texts when even that is not enough.
    """
    git = _git(repo)
    state = read_state(git)
    if not isinstance(state, (Conflicted, Applying)):
        return ConflictReport(
            replaying=None,
            replaying_body="",
            files=(),
            guidance="Nothing is conflicted.",
        )
    incoming = _incoming(state)
    files = tuple(
        _file_report(
            read_conflict(git, path, context, _branch_base(git)),
            include_full_sides,
            _incoming_file_diff(git, state, path) if include_file_diffs else None,
        )
        for path in state.unmerged
    )
    return ConflictReport(
        replaying=_info(incoming) if incoming else None,
        replaying_body=(
            git.out("log", "-1", "--format=%B", incoming.sha).strip() if incoming else ""
        ),
        files=files,
        guidance=_conflict_guidance(files, state),
    )


def _incoming(state: Conflicted | Applying) -> Commit | None:
    """The side being applied, whatever is applying it.

    A rebase always names it; a merge, cherry-pick or revert names it too, in a
    ref of its own; and a conflict nothing recorded has no name for it at all,
    which is the case the None is for.
    """
    return state.replaying if isinstance(state, Conflicted) else state.incoming


def _incoming_file_diff(
    git: Git, state: Conflicted | Applying, path: str
) -> str:
    """Everything the incoming side did to one file, not only the contested part."""
    incoming = _incoming(state)
    if incoming is None:
        return ""
    if isinstance(state, Applying) and state.operation == "merge":
        # The incoming side of a merge is a branch, not a commit. What its tip
        # did on its own is rarely what is being merged in, so the comparison
        # runs from where the two sides parted.
        base = git.out("merge-base", "HEAD", incoming.sha)
        return git.out("diff", f"{base}..{incoming.sha}", "--", path)
    return git.out("show", "--format=", incoming.sha, "--", path)


def _branch_base(git: Git) -> str | None:
    """Where the branch this rebase is rewriting starts, if the session knows."""
    session = load_session(git)
    return session.base_sha if session else None


def _conflict_guidance(
    files: tuple[FileReport, ...], state: Conflicted | Applying
) -> str:
    # The field names read from a rebase, where they were built: what is here
    # already against what is being applied over it. Every other operation has
    # the same two sides, so the names hold and only what fills them changes.
    incoming = (
        "the replayed commit"
        if isinstance(state, Conflicted)
        else {
            "cherry-pick": "the cherry-picked commit",
            "revert": "the revert",
            "merge": "the branch being merged in",
            "unknown": "the incoming side",
        }[state.operation]
    )
    contested = sum(len(f.units) for f in files)
    lines = sum(
        (u.base_range[1] - u.base_range[0] + 1) if u.base_range else 0
        for f in files
        for u in f.units
    )
    extent = (
        f"{contested} region{'s' if contested != 1 else ''} contested"
        + (f", {lines} line{'s' if lines != 1 else ''} of the base in total" if lines else "")
        + ". Everything else in these files git merged already, so a resolution that "
        "rewrites more than the regions below is rewriting lines nobody disagreed "
        "about. "
    )
    advice = (
        extent
        + f"Each region lists what the branch did to the base and what {incoming} did "
        "to the same base -- `branch_so_far` is what is here already, `replaying` is "
        "what is being applied over it. branch_so_far_commits names the commits "
        "behind the first side, which is the nearest it has to a stated intent. "
        "Read both, then answer: "
        'resolve(path, take="both"/"branch"/"replaying") where that says '
        "it, or edit the file and call resolve(path) with no content. Those "
        "compose, and composing them is the cheap way through a region neither "
        "side gets right on its own: take the closer side first, which leaves a "
        "file with no markers in it, then edit that as ordinary text. "
        "Raise `context` if a region is hard to place."
    )
    appended = [f.path for f in files if f.both_inserted]
    if appended:
        advice += (
            f" Both sides inserted at the same point in {', '.join(appended)}, so"
            " nothing in the text says which order was meant. If it is two"
            ' independent additions, `resolve(path, take="both")` keeps the'
            " branch's first and the replayed commit's after."
        )
    removed = [(f.path, f.deleted_by) for f in files if f.deleted_by is not None]
    if removed:
        cases = ", ".join(
            f"{path} (gone from {'the branch' if side == 'branch' else incoming})"
            for path, side in removed
        )
        advice += (
            f" One side deleted a path the other modified: {cases}. Nothing composes "
            "here and no regions are listed: what is being decided is whether the path "
            'lives. `take` says either answer -- take="branch" and take="replaying" '
            "each stage what that side did, a deletion included. A deletion is often "
            "half of a rename the other side has not got, in which case what the "
            "incoming side did to the old path has to be reapplied to the new one: "
            "include_file_diffs shows it."
        )
    rootless = [f.path for f in files if f.no_common_base]
    if rootless:
        advice += (
            f" No common base for {', '.join(rootless)}: both sides introduced the "
            "file independently, so there is nothing to diff against and no "
            "regions are listed. The whole text of each side is included instead; "
            "decide between them, or write the combination you want."
        )
    unplaced = [f.path for f in files if any(u.base_range is None for u in f.units)]
    if unplaced:
        advice += (
            f" A region in {', '.join(unplaced)} has no base_range: its text is not "
            "in the base file, so there is no position to give and none is guessed "
            "at. Its two diffs still say what each side did; find it by that."
        )
    # Said here as well as in the status report because this is the tool a caller
    # reads at a conflict, and by the time it has resolved every region it is
    # about to continue. A warning it saw one call earlier is one it has already
    # scrolled past.
    if isinstance(state, Conflicted) and state.action_stop_lost:
        advice += (
            f" Note before continuing: this step is `{state.action}`, whose stop "
            "this conflict has spent -- git commits the resolution and moves on. "
            "`proceed` queues a break to give the stop back, so the commit can "
            "still be changed once it exists; `proceed(keep_stop=False)` takes "
            "git's behaviour, and then any change to this commit has to be "
            "staged alongside the resolutions rather than after them."
        )
    return advice


def _file_report(
    conflict: FileConflict, include_full_sides: bool, file_diff: str | None = None
) -> FileReport:
    return FileReport(
        path=conflict.path,
        units=tuple(
            UnitReport(
                base_range=unit.base_range,
                branch_so_far_diff=unit.branch_so_far_diff,
                replaying_diff=unit.replaying_diff,
                branch_so_far_summary=unit.branch_so_far_summary,
                replaying_summary=unit.replaying_summary,
                branch_so_far_commits=tuple(
                    CommitInfo(sha=c.sha, subject=c.subject)
                    for c in unit.branch_so_far_commits
                    if c.from_this_branch
                ),
            )
            for unit in conflict.units
        ),
        no_common_base=conflict.no_common_base,
        both_inserted=conflict.both_inserted,
        deleted_by=conflict.deleted_by,
        # With no common base there are no units, so withholding the texts would
        # leave the caller nothing at all.
        base=conflict.sides.base if include_full_sides else None,
        branch_so_far=conflict.sides.branch_so_far
        if include_full_sides or conflict.no_common_base
        else None,
        replaying=conflict.sides.replaying
        if include_full_sides or conflict.no_common_base
        else None,
        replaying_file_diff=file_diff,
    )


def resolve(
    path: str,
    content: str | None = None,
    repo: str = ".",
    take: str | None = None,
    allow_markers: bool = False,
) -> ResolveReport:
    """Stage the resolved content for one conflicted path.

    Three ways, in rough order of how much they cost to use:

    - `take="both"`, `"branch"` or `"replaying"` resolves every conflict block
      in the file the stated way. "both" keeps the branch's lines then the
      replayed commit's, which is what two insertions at the same point almost
      always mean. Cheapest, and it cannot introduce a typo.
    - no arguments stages what is already in the working tree, for a file large
      enough that sending it back costs more than editing it in place.
    - `content` writes the finished file and stages it.

    `take` names a side, not a text: where that side deleted the path, taking it
    stages the deletion. "both" has no meaning on such a path -- there is no
    text of one side to keep the other's beside -- and is refused rather than
    answered. Deleting the file yourself and calling this with no arguments
    stages the deletion too, which is what an absent file can only mean once the
    path is conflicted.

    Every route refuses content that still contains conflict markers. Staging
    one is how a commit ends up with `<<<<<<<` in it, and nothing downstream
    catches that.

    `allow_markers=True` stages it anyway, for the file whose markers are
    content: documentation showing what a conflict looks like, a fixture of
    git's own output. The check cannot tell that from a resolution abandoned
    half way -- both are marker lines in text a person wrote -- and it refuses
    on purpose when unsure, since a marker let through is a commit nobody can
    build. So the caller who knows says so.
    """
    git = _git(repo)
    if take is not None and content is not None:
        raise ValueError("take and content are two ways to say the same thing; pass one.")
    root, target = _contained(git.repo, path)
    # Inside the repository is not the same as inside the working tree. `.git`
    # holds the hooks, which run during the rebase this tool is driving, so a
    # write there is a write to something that executes. Git will not track a
    # path under `.git` either -- but it declines by ignoring it and exiting
    # zero, which is how such a write would otherwise go unmentioned.
    if ".git" in target.relative_to(root).parts:
        raise ValueError(
            f"{path} is under .git, which holds the repository itself rather than "
            "the work in it. Nothing there is a conflicted path, and writing to it "
            "can change what git does next."
        )
    # One side deleted the path, so the resolution is which of "gone" and "here"
    # to stage -- and one of those is not text. Composing blocks would answer it
    # with the empty string, which stages an empty file: a resolution nothing
    # downstream reports as wrong, since the path stops being conflicted either
    # way. So it is decided before any content is computed.
    deleted = deleted_side(git, path)
    if deleted is not None and (take is not None or (content is None and not target.is_file())):
        return _resolve_one_sided(git, path, take, deleted)

    if take is not None:
        content = take_side(git, path, take)

    from_disk = content is None
    if from_disk:
        if not target.is_file():
            raise ValueError(
                f"{path} is not in the working tree, so there is nothing to stage. "
                "Pass content to write it."
            )
        content = target.read_text()

    # Checked before anything is written, so a refusal leaves the file as it was
    # and the caller does not have to reconstruct what they sent.
    assert content is not None
    if has_markers(content) and not allow_markers:
        raise ValueError(
            f"{path} still contains conflict markers. Resolve them first: staging "
            "this would commit them. If they are content the file is meant to have "
            "-- documentation showing a conflict, a fixture of git's output -- pass "
            "allow_markers=True."
        )
    if not from_disk:
        target.write_text(content)
    # Before staging: `git add` collapses the index stages this reads the sides from.
    repeated = tuple(
        RepeatedLine(line, seen, in_branch, in_replaying)
        for line, seen, in_branch, in_replaying in repeated_lines(git, path, content)
    )
    git.run("add", "--", path)
    return _resolved(git, path, repeated=repeated)


def _resolve_one_sided(
    git: Git, path: str, take: str | None, deleted: str
) -> ResolveReport:
    """Answer a modify/delete: stage the path's removal, or the survivor's text.

    `take` of None means the caller deleted the file and called with no
    arguments, which on a conflicted path says the same as naming the side that
    deleted it. Anything the other side did to the text is in that side's stage,
    so keeping the path means staging what it says rather than the working file,
    which is one side's text and never says which.
    """
    kept = "replaying" if deleted == "branch" else "branch"
    if take == "both":
        raise ValueError(
            f'take="both" cannot answer {path}: {_side_name(deleted)} deleted it, so '
            "there are no two texts to keep in some order. The answers are "
            f'take="{deleted}", which stages the deletion, and take="{kept}", which '
            "keeps the file as that side has it."
        )
    if take is not None and take != deleted and take != kept:
        raise ValueError(f"take must be branch, replaying or both, not {take!r}")

    if take is None or take == deleted:
        # -f because the path is unmerged, which git otherwise refuses to remove;
        # it also covers the file already being gone, when the caller deleted it.
        git.run("rm", "-q", "-f", "--", path)
        return _resolved(git, path, deleted=True)

    _, target = _contained(git.repo, path)
    target.write_text(side_text(git, path, take))
    git.run("add", "--", path)
    return _resolved(git, path)


def _side_name(side: str) -> str:
    return "the branch" if side == "branch" else "the commit being replayed"


def _resolved(
    git: Git,
    path: str,
    deleted: bool = False,
    repeated: tuple[RepeatedLine, ...] = (),
) -> ResolveReport:
    """What is left to do, once one path has been answered."""
    remaining = tuple(git.lines("diff", "--name-only", "--diff-filter=U"))
    staged = "Staged the deletion of " if deleted else "Resolved "
    return ResolveReport(
        path=path,
        still_conflicted=remaining,
        deleted=deleted,
        repeated=repeated,
        guidance=_repeat_note(repeated) + (
            f"{staged}{path}. Still conflicted: {', '.join(remaining)}."
            if remaining
            else f"{staged}{path}; all paths resolved. Call proceed."
            if _carry_on_command(read_state(git)) is not None
            # A conflict nothing recorded has nothing to continue, and saying so
            # here saves the caller finding out from a refusal one call later.
            else f"{staged}{path}; all paths resolved. Nothing is mid-operation, so "
            "there is nothing to continue: commit them as you would any other change."
        ),
    )


def _repeat_note(repeated: tuple[RepeatedLine, ...]) -> str:
    """Say what the resolution has more of than either side, before saying what is left.

    First in the guidance rather than appended, because the caller reads this to
    decide whether to continue, and what is left to resolve is the sentence it is
    looking for -- anything after that gets skimmed.
    """
    if not repeated:
        return ""
    shown = ", ".join(
        f"{row.line.strip()[:60]!r} ({row.in_resolution}x here, "
        f"{row.in_branch}x branch, {row.in_replaying}x replaying)"
        for row in repeated[:3]
    )
    more = f" and {len(repeated) - 3} more" if len(repeated) > 3 else ""
    return (
        f"More copies than either side had: {shown}{more}. Expected if you took both "
        "sides and they share a line; otherwise the resolution reached past the "
        "contested region and re-added lines that were already merged. "
    )


@dataclass(frozen=True)
class TodoReport:
    remaining: tuple[str, ...]
    dropped: tuple[CommitInfo, ...]
    guidance: str


def rebase_todo(
    repo: str = ".", todo: list[str] | None = None, force: bool = False
) -> TodoReport:
    """Read the steps a running rebase has left, or replace them.

    Worth having because the need shows up mid-run: a `fixup` turns out to
    depend on a commit scheduled after it, and the fix is to move one line
    rather than to abandon thirty resolved conflicts and start again.

    Replacing the list can drop commits exactly as writing one badly can, so
    the same check applies: a commit in the remaining steps and not in the
    replacement is refused unless `force`. Lines that name no commit -- `exec`
    above all -- are counted too, and dropping every `exec` silently turns off
    the per-commit check, so that is called out rather than assumed.
    """
    git = _git(repo)
    # Specifically a rebase: a todo is a thing only a rebase has, and a
    # conflicted cherry-pick is emphatically not "no operation in progress".
    if not is_rebasing(read_state(git)):
        raise ValueError("No rebase in progress, so there are no steps left.")
    path = git.git_path("rebase-merge/git-rebase-todo")
    current = [
        line for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")
    ]
    if todo is None:
        return TodoReport(
            remaining=tuple(current),
            dropped=(),
            guidance=f"{len(current)} steps left. Pass todo to replace them.",
        )

    dropped = _dropped_steps(git, current, todo)
    if dropped and not force:
        raise ValueError(
            "Refusing to replace the steps: "
            + ", ".join(f"{c.sha[:9]} ({c.subject})" for c in dropped)
            + " would be dropped without a warning. Pass force to mean it."
        )
    lost_checks = sum(l.startswith("exec ") for l in current) - sum(
        l.startswith("exec ") for l in todo
    )
    path.write_text("\n".join(todo) + "\n")
    return TodoReport(
        remaining=tuple(todo),
        dropped=dropped,
        guidance=f"{len(todo)} steps now queued. Call proceed."
        + (f" {lost_checks} fewer exec steps, so less is checked." if lost_checks > 0 else ""),
    )


def _dropped_steps(git: Git, current: list[str], replacement: list[str]) -> tuple[CommitInfo, ...]:
    """Commits named in the remaining steps that the replacement leaves out."""

    def named(lines: list[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for line in lines:
            match = TODO_LINE.match(line)
            if match and match["action"].lower() not in DROPPING_ACTIONS:
                resolved = git.run(
                    "rev-parse", "--verify", "--quiet", f"{match['sha']}^{{commit}}", check=False
                ).stdout.strip()
                if resolved:
                    found[resolved] = match["sha"]
        return found

    missing = set(named(current)) - set(named(replacement))
    return tuple(_commit_info(git, sha) for sha in missing)


@dataclass(frozen=True)
class PreflightReport:
    base: str
    commits: tuple[CommitInfo, ...]
    dropped: tuple[CommitInfo, ...]
    unknown: tuple[str, ...]
    already_upstream: tuple[CommitInfo, ...]
    stray_fixups: tuple[CommitInfo, ...]
    blocking: tuple[str, ...]
    untracked_collisions: tuple[str, ...]
    safe_to_start: bool
    guidance: str


def rebase_preflight(
    base: str, repo: str = ".", todo: list[str] | None = None, onto: str | None = None
) -> PreflightReport:
    """Check what a rebase would do, without starting it or changing anything.

    Reports commits the todo would drop silently, commits it names that are not
    in the range, commits whose change is already in the base under a different
    sha, anything already in progress or uncommitted, and untracked files a
    replayed commit would collide with.

    `onto` where the rebase will be given one: it does not change which commits
    are replayed -- that is `base..HEAD` either way -- but it is the tree that
    gets checked out, so it decides which untracked files are in the way.
    """
    git = _git(repo)
    check = check_plan(git, base, todo)
    blocking = _blocking(git)
    collisions = _untracked_collisions(git, base, onto)

    problems = [*blocking, *check.problems]
    return PreflightReport(
        base=base,
        commits=tuple(_info(c) for c in check.commits),
        dropped=tuple(_info(c) for c in check.dropped),
        unknown=check.unknown,
        already_upstream=tuple(_info(c) for c in check.already_upstream),
        stray_fixups=tuple(_info(c) for c in check.stray_fixups),
        blocking=blocking,
        untracked_collisions=collisions,
        safe_to_start=not problems,
        guidance=(
            "Nothing found; safe to start."
            if not problems
            else "Not safe to start: " + "; ".join(problems)
        )
        + (
            f" Untracked files would be moved aside first: {', '.join(collisions)}."
            if collisions
            else ""
        ),
    )


def _blocking(git: Git) -> tuple[str, ...]:
    """Conditions that must be cleared before a rebase can start."""
    problems: list[str] = []
    state = read_state(git)
    if is_rebasing(state):
        problems.append("a rebase is already in progress")
    elif isinstance(state, Applying):
        problems.append(f"a {state.operation} is already in progress")
    if git.lines("status", "--porcelain", "--untracked-files=no"):
        problems.append("the working tree has uncommitted changes")
    return tuple(problems)


def _refs_into_range(git: Git, base: str) -> tuple[CarriedRef, ...]:
    """The branches `--update-refs` is expected to move, as they stand now.

    Every branch but the one checked out whose tip is a commit about to be
    replaced. Recorded so the finish can say whether each actually came along:
    git prints a line about it and exits zero either way, and a sibling left
    behind points into history that no longer exists -- which is the failure the
    option is asked for to prevent, and the one nothing would otherwise report.
    """
    in_range = set(git.lines("rev-list", f"{base}..HEAD"))
    if not in_range:
        return ()
    rebasing = git.run("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    current = rebasing.stdout.strip()
    found: list[CarriedRef] = []
    for line in git.lines("for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/"):
        name, _, sha = line.partition(" ")
        if name != current and sha in in_range:
            found.append(CarriedRef(ref=name, sha=sha))
    return tuple(found)


def _untracked_collisions(git: Git, base: str, onto: str | None = None) -> tuple[str, ...]:
    """Untracked files that checking out the range would refuse to overwrite.

    A rebase stops dead on these before doing anything, which is confusing when
    the file is unrelated scratch work that merely shares a name.

    Two different questions, and an explicit onto separates them: the tree that
    gets checked out is the landing place's, while the commits whose paths are
    also about to be written are `base..HEAD` -- the upstream's range. Reading
    both from one revision was right only while they were the same commit.
    """
    untracked = set(git.lines("ls-files", "--others", "--exclude-standard"))
    if not untracked:
        return ()
    known = set(git.lines("ls-tree", "-r", "--name-only", onto or base))
    known.update(git.lines("log", "--name-only", "--format=", f"{base}..HEAD"))
    return tuple(sorted(untracked & known))


@dataclass(frozen=True)
class StartReport:
    backup_ref: str
    stashed: tuple[str, ...]
    status: StatusReport
    guidance: str
    # Local files this rebase has already deleted by checking out a history that
    # does not track them. Reported here rather than only at the finish because
    # here is where it happened, and the sooner it is said the less of the run has
    # to be re-read to understand it. Last in the class because it has a default
    # and the three above do not. See `locals_the_rewrite_removed`.
    lost_locals: tuple[str, ...] = ()


def rebase_start(
    base: str,
    repo: str = ".",
    todo: list[str] | None = None,
    edit: list[str] | None = None,
    # After the todo, not beside `base` where it reads better: callers pass the
    # todo positionally, and moving it along is a silent change of meaning.
    onto: str | None = None,
    edit_every: bool = False,
    break_first: bool = False,
    autosquash: bool = False,
    update_refs: bool = False,
    check_command: str | None = None,
    check_edits_only: bool = False,
    check_halts: bool = True,
    auto_resolve: bool = False,
    force: bool = False,
) -> StartReport:
    """Begin a rebase onto `base`, and report where it stops.

    Refuses anything rebase_preflight called unsafe, unless `force`. Before
    starting it tags the current tip, so the result can be checked against it,
    and moves aside untracked files a replayed commit would collide with.

    `base` is the upstream: the commits replayed are `base..HEAD`. `onto` is
    where they land, and defaults to `base`, which is the ordinary rebase. Pass
    it when the two differ -- replaying a branch onto a rewritten version of the
    history it was cut from, above all, where the old upstream still says which
    commits are the branch's own and the new one no longer does. Naming the
    landing place as `base` instead would put every commit the two have not got
    in common into the range.

    `edit` is the short way to say "replay the whole range, stop at these": pass
    the commits to stop at and the todo is built here, every other commit
    picked. Prefer it to writing `todo` out by hand -- it cannot leave a commit
    out, and a hand-written list is exactly where one goes missing. `todo` is
    still there for anything that reorders, drops or squashes. Being a way of
    generating the todo, it cannot be combined with `autosquash` or
    `update_refs`, which are the others.

    `edit_every` stops at all of them instead of at named ones, which is what
    "rebase and check each commit" means; naming seven shas was a long way of
    saying it. `break_first` puts a stop in front of the first commit, with
    nothing of the branch applied -- where a baseline is measured, and the only
    place it can be. Both generate the todo, so the same exclusions apply, and
    that includes a `todo` of your own: they are ways of writing one, not
    modifiers on one. A hand-written todo needs no flag for this -- `break` is
    an ordinary step, so put the line where you want the stop.

    `autosquash` folds every `fixup!` and `squash!` in the range into the commit
    its subject names, which is the workflow `git commit --fixup` sets up. It
    cannot be combined with a todo, since it is a way of generating one.

    `update_refs` carries every other branch pointing into the range along with
    the rewrite, which is what a stack of branches on one another needs: without
    it the rebase moves only the branch checked out and strands its siblings on
    the commits it just replaced. Like autosquash it works by writing lines into
    the generated todo, so it cannot be combined with one of your own.

    `check_command` is run after every commit, which is the only thing that
    catches a step that applies cleanly but leaves the tree broken. Not after a
    `drop`, which creates no commit and so leaves the tree that was already
    checked. As a gate it is git's own `--exec`, so what it printed arrives in
    `git_said` rather than in a field: reaching the next stop is what says it
    passed. `check_edits_only` runs it after the commits you stop at instead of
    after all of them, which is what you want whenever the branch was not green
    at every commit to begin with -- most branches, since a budget or a fixture
    raised one commit after the code that needed it is red in between, and a
    per-commit check then halts the rebase on history that was already like that
    before you touched it.

    `check_halts=False` stops it being a gate at all: the command runs at each
    stop and its result comes back as `check` in the report, and nothing halts.
    That is the shape for a rebase somebody is watching -- the answer wanted at
    each stop is "what does the suite say here?", compared against a baseline,
    and a branch is rarely green at every commit of its own history. It is
    skipped at a conflicted stop, where the tree still holds markers.

    `auto_resolve` composes conflicts where the two sides touched different
    lines and carries on without stopping. Off by default: lines that do not
    overlap can still contradict each other -- one side adding a call, the other
    removing the helper it needs -- and a conflict resolved without being read
    has to be reviewed afterwards anyway. Use it when replaying a branch whose
    conflicts you already understand.
    """
    if autosquash and todo is not None:
        raise ValueError("autosquash generates the todo, so it cannot be given one.")
    if update_refs and todo is not None:
        # Silently dropping them would be the worst outcome: the rebase succeeds,
        # the caller is told nothing, and the sibling branches are left behind on
        # commits that no longer exist -- which is the very thing they asked to
        # avoid. Weaving `update-ref` lines into a caller's todo would mean
        # deciding where in their ordering each one belongs, which is theirs.
        raise ValueError(
            "update_refs writes update-ref lines into the generated todo, so a "
            "todo of your own would discard them. Put the update-ref lines in "
            "your todo, or start without one."
        )
    generating = edit is not None or edit_every or break_first
    if generating and todo is not None:
        raise ValueError(
            "edit, edit_every and break_first build the todo, so they cannot be "
            "combined with one. They are ways of writing a todo rather than "
            "modifiers on one: put the `edit` and `break` lines in yours -- "
            "`break` is an ordinary step and can go anywhere, including first."
        )
    if edit is not None and edit_every:
        raise ValueError(
            "edit_every stops at all of them, so naming some as well says two "
            "different things. Pass one."
        )
    if autosquash and generating:
        raise ValueError("autosquash generates the todo, so it cannot be given `edit`.")
    if update_refs and generating:
        # `edit` generates a todo and hands it over as one, so the update-ref
        # lines git would have written go the same way a caller's todo sends
        # them: nowhere, silently, leaving the sibling branches on commits that
        # no longer exist. Weaving them in here is possible -- unlike a hand-
        # written todo, this one's ordering is ours -- but it is a feature rather
        # than a merge, so for now the combination is refused rather than
        # half-honoured.
        raise ValueError(
            "update_refs writes update-ref lines into the generated todo, and "
            "`edit` generates that todo, so they would be discarded. Use a todo "
            "of your own with the update-ref lines in it, or drop `edit`."
        )
    if check_edits_only and not check_command:
        raise ValueError("check_edits_only says when to run check_command, which is unset.")
    if check_edits_only and not check_halts:
        raise ValueError(
            "check_edits_only says which steps get an exec line, and check_halts="
            "False writes none: the check runs at every stop instead. Pass one."
        )
    if not check_halts and not check_command:
        raise ValueError("check_halts says how to run check_command, which is unset.")
    git = _git(repo)
    if generating:
        todo = todo_stopping_at(
            git, base, edit, every=edit_every, break_first=break_first
        )
    preflight = rebase_preflight(base, repo, todo, onto)
    if not preflight.safe_to_start and not force:
        raise ValueError(f"Refusing to start. {preflight.guidance}")

    stashed, stash_ref = _stash(git, preflight.untracked_collisions)
    backup = record_backup(git)
    save_session(
        git,
        Session(
            backup_ref=backup.ref,
            backup_sha=backup.sha,
            backup_tree=backup.tree,
            base=onto or base,
            # Where the branch sits afterwards, which is the onto: every range
            # the finish check reads is `base_sha..HEAD`.
            base_sha=git.out("rev-parse", f"{onto or base}^{{commit}}"),
            # And where it sat before, which is the upstream. Only recorded when
            # the two differ, since otherwise the merge-base finds it anyway.
            upstream_sha=(
                git.out("rev-parse", f"{base}^{{commit}}") if onto is not None else ""
            ),
            stashed=stashed,
            stash_ref=stash_ref,
            check_command=check_command,
            check_halts=check_halts,
            carried=_refs_into_range(git, base) if update_refs else (),
        ),
    )

    args = ["rebase", "-i"]
    config: list[str] = ["-c", "core.editor=true", *RERERE]
    if todo is not None:
        # A supplied todo replaces whatever git generates, so --exec would be
        # discarded with it; the exec lines have to be woven in here instead.
        lines = _with_checks(
            todo, check_command if check_halts else None, check_edits_only
        )
        todo_file = git.git_path("rebase-mcp-todo")
        todo_file.write_text("\n".join(lines) + "\n")
        # Git runs this through a shell, so the path is quoted for one. A repo
        # named `re'po` used to make the editor command fail to parse, and the
        # rebase then silently did nothing with the todo.
        config += ["-c", f"sequence.editor=cp {shlex.quote(str(todo_file))}"]
    if onto is not None:
        args += ["--onto", onto]
    if todo is None:
        if autosquash:
            args.append("--autosquash")
        if update_refs:
            args.append("--update-refs")
        if check_command and check_halts:
            args += ["--exec", check_command]
    args.append(base)

    # Stopping at a conflict is an ordinary outcome that git reports as failure,
    # so the result is read from the rebase state instead -- but git's own words
    # about why it stopped are kept, because nothing else can reconstruct them.
    result = git.run(*config, *args, check=False)
    if not result.ok and not is_rebasing(read_state(git)):
        _withdraw_start(git, backup, stashed, stash_ref, result)
    stopped = _advance(git, _git_said(result), auto_resolve, _replayed(result))
    lost = locals_the_rewrite_removed(git, backup, stashed=stashed)
    return StartReport(
        backup_ref=backup.ref,
        stashed=stashed,
        lost_locals=lost,
        status=stopped,
        guidance=(
            # A rebase with no conflicts in it is over by the time this returns,
            # and saying "Started" and nothing else left the one report a caller
            # reads sounding like the work was still ahead of it. The finished
            # guidance already names the tag and the next call, so it is not
            # repeated here.
            "Ran to the end without stopping. "
            if stopped.finished is not None
            else f"Started. The tip beforehand is tagged {backup.ref}; "
            "rebase_finish checks the result against it. "
        )
        + _lost_locals_note(backup.ref, lost)
        + stopped.guidance,
    )


class StartMovedBranch(GitError):
    """Git failed after rewriting the branch, so the start was not withdrawn.

    Not a ValueError: nothing the caller passed was wrong, the repository simply
    did not end up where a start should leave it. The tip beforehand, the
    session and anything moved aside are the only record of where the branch was
    -- the one thing somebody needs when a rewrite goes wrong -- so none of it
    is taken back, and the failure says so.
    """

    def __init__(self, result: GitResult, backup: Backup, head: str) -> None:
        super().__init__(result)
        self.args = (
            f"{self}\n\nHEAD moved from {backup.sha[:9]} to {head[:9]} before git "
            "gave up, so the branch was rewritten. Nothing has been taken back: "
            f"the tip beforehand is tagged {backup.ref}, anything moved aside is "
            "still stashed, and rebase_finish checks what is here now against it.",
        )


def _withdraw_start(
    git: Git,
    backup: Backup,
    stashed: tuple[str, ...],
    stash_ref: str | None,
    result: GitResult,
) -> Never:
    """Take back a start git never made -- or refuse to, if it did make one.

    Git failing with no rebase in progress almost always means it never began:
    a dirty tree, or a todo it would not take. Then the right answer is to put
    back what was moved aside, clear the session that existed to check a
    result, and report git's own words rather than claim it started.

    Almost always is not always, and the difference is the whole reason this
    server exists. "No rebase in progress" also describes one that ran, rewrote
    the branch and then failed, and withdrawing the record there would delete
    the only note of where the branch had been -- at the one moment somebody
    needs it. So the tip is checked before anything is taken back, and if it
    moved, everything is kept and the report says so.

    Raises either way: a start that did not start is not a result.
    """
    head = git.out("rev-parse", "HEAD")
    if head != backup.sha:
        raise StartMovedBranch(result, backup, head)
    _unstash(git, stashed, stash_ref)
    clear_session(git)
    git.run("tag", "-d", backup.ref, check=False)
    raise GitError(result)


def _with_checks(
    todo: list[str], check_command: str | None, edits_only: bool = False
) -> list[str]:
    """Weave the check in after the commits it is meant to be run on.

    After every commit by default. `edits_only` narrows it to the steps that
    stop, which is the answer to a check that keeps failing on history rather
    than on the caller's work: a branch is rarely green at every commit -- a line
    budget raised one commit after the file that outgrew it is red in between --
    and a per-commit check then halts the rebase on a state that was already like
    that. Narrowing keeps the part that was wanted, "prove the commits I changed
    are sound", and drops the part that only rediscovers what the branch was.
    """
    if not check_command:
        return todo
    woven: list[str] = []
    for line in todo:
        woven.append(line)
        match = TODO_LINE.match(line)
        if not match:
            continue
        action = match["action"].lower()
        # A drop creates no commit, so the tree at the step after it is the tree
        # that was already checked. Running the suite again there says the same
        # thing at the same price, and a todo of mostly drops pays it repeatedly.
        if action in DROPPING_ACTIONS:
            continue
        if edits_only and action not in STOPPING_ACTIONS:
            continue
        woven.append(f"exec {check_command}")
    return woven


def _stash(git: Git, paths: tuple[str, ...]) -> tuple[tuple[str, ...], str | None]:
    """Move untracked files out of the way, remembering them for later.

    Only the ones that would actually collide: stashing anything else would be
    taking away work the caller did not ask us to touch.

    The entry's sha comes back with the paths, so a later restore can target
    that entry rather than the top of the stash stack -- which is what a stash
    made by the user meanwhile would otherwise be.
    """
    if not paths:
        return (), None
    git.run("stash", "push", "--include-untracked", "--quiet", "--", *paths)
    return paths, git.out("rev-parse", "stash@{0}")


@dataclass(frozen=True)
class AmendReport:
    before: CommitInfo
    after: CommitInfo
    guidance: str


def rebase_amend(
    repo: str = ".", message: str | None = None, stage_tracked: bool = False
) -> AmendReport:
    """Amend the commit this rebase has just applied.

    Refused at every other kind of stop. At a conflicted stop the commit being
    replayed has not been created yet, so HEAD is still the one before it and
    amending would fold two commits into one -- silently, and reported by git as
    success.

    `stage_tracked` stages modifications to files git already tracks. It will
    not add untracked files: those are never part of what a rebase is
    rewriting, and sweeping them in is how a stray binary or somebody's local
    notes end up in history.
    """
    git = _git(repo)
    state = read_state(git)
    # `unapplied` is refused here rather than left to the isinstance check: git's
    # own "you may amend" record survives the commit being taken back out, so the
    # state is the same one and the commit it names is no longer there.
    restored = _is_restored_stop(git, state)
    if not restored and (not isinstance(state, StoppedAfterApply) or state.unapplied):
        raise ValueError(_why_not_amendable(_report(state, git=git)))

    before = _info(state.head)
    if stage_tracked:
        # `git add -u`, never `-A`. The difference is untracked files, and
        # sweeping those into a commit is how a scratch file, a stray binary or
        # somebody's local notes end up in history -- silently, since the amend
        # reports success either way. Untracked files are never part of what a
        # rebase is rewriting, so this tool has no business staging them.
        git.run("add", "-u")
    args = ["commit", "--amend", "--no-verify"]
    args += ["-m", message] if message is not None else ["--no-edit"]
    git.run("-c", "core.editor=true", *args)
    # Recorded before the report, so the finish check can tell a branch whose
    # change moved because it was told to from one whose change moved because a
    # conflict went the wrong way. `applied` names the step rather than the
    # commit, so amending the same one twice stays one entry.
    session = load_session(git)
    applied = state.applied if isinstance(state, StoppedAfterApply) else before.sha
    if session is not None and applied and applied not in session.amended:
        save_session(git, replace(session, amended=(*session.amended, applied)))
    after = _commit_info(git, "HEAD")
    return AmendReport(
        before=before,
        after=after,
        guidance=f"Amended {before.sha[:9]} into {after.sha[:9]}. Call proceed.",
    )


@dataclass(frozen=True)
class SplitReport:
    """What was taken back out, and what is now in the tree to be committed."""

    unapplied: CommitInfo
    head: CommitInfo
    paths: tuple[str, ...]
    guidance: str


def rebase_split(repo: str = ".") -> SplitReport:
    """Take the commit this step just applied back out, keeping its changes.

    For turning one commit into several: its changes end up in the working tree,
    unstaged, with HEAD at the commit before it. Commit them in as many pieces as
    you like -- with ordinary git, which needs nothing this server guards -- then
    call proceed and the rest of the todo replays on top of them.

    Only at an `edit` stop, and only one holding a single commit. Part-way
    through a run of `fixup` or `squash` steps HEAD is the accumulation of them,
    and taking that apart is a different operation from splitting one commit.

    Nothing is lost if it goes wrong: the changes are in the tree, and abort
    still puts the branch back where it started.
    """
    git = _git(repo)
    state = read_state(git)
    if not isinstance(state, StoppedAfterApply) or state.unapplied:
        raise ValueError(_why_not_splittable(_report(state)))
    if state.fixups_pending:
        raise ValueError(
            f"Refusing to split: HEAD ({state.head.sha[:9]}) is a run of "
            f"{len(state.fixups_pending)} fixup or squash steps so far, not one "
            "commit. Let the run finish, then split the commit it produces at a "
            "later `edit` step."
        )
    # Uncommitted work would be indistinguishable from the commit's own changes
    # once both are sitting in the tree, and the caller is about to divide those
    # changes into commits by hand.
    dirty = tuple(git.lines("status", "--porcelain", "--untracked-files=no"))
    if dirty:
        raise ValueError(
            "Refusing to split: the working tree already has uncommitted changes "
            f"({', '.join(entry[3:] for entry in dirty)}), which would be mixed in "
            "with the commit's own once it is taken back out. Commit or stash them "
            "first."
        )
    parent = git.run("rev-parse", "--verify", "--quiet", f"{state.head.sha}^", check=False)
    if not parent.ok:
        raise ValueError(
            f"Refusing to split: {state.head.sha[:9]} is a root commit, so there is "
            "no commit before it to reset to. Its content is the whole of the branch "
            "at that point; a split has to be made by committing it differently."
        )

    unapplied = _info(state.head)
    # From the commit, not from the tree afterwards. A mixed reset leaves the
    # files a commit *added* untracked, so `git diff` lists only the ones it
    # modified -- and a caller told about half of what to commit will commit half.
    paths = tuple(git.lines("diff", "--name-only", f"{unapplied.sha}^", unapplied.sha))
    # Mixed on purpose: staged changes would let a `git commit` with no paths
    # sweep the whole commit back in, which is the opposite of splitting it.
    git.run("reset", "-q", "--mixed", "HEAD^")
    return SplitReport(
        unapplied=unapplied,
        head=_commit_info(git, "HEAD"),
        paths=paths,
        guidance=(
            f"Took {unapplied.sha[:9]} ({unapplied.subject!r}) back out. Its changes "
            f"are in the working tree, unstaged: {', '.join(paths)}. Commit them in "
            "pieces, then call proceed -- which refuses while anything is left "
            "uncommitted, since the rest of the todo would replay over it. Amending "
            "is refused until then: HEAD is the commit before the one that was "
            "applied."
        ),
    )


def _split_leftovers(
    git: Git, applied: str, staged: bool = False
) -> tuple[tuple[str, str], ...]:
    """What this step's commit changed that is not going into a commit, as (code, path).

    Untracked files are the reason this exists. A mixed reset -- how a commit is
    split -- leaves the files a commit *added* untracked, and `git rebase
    --continue` does not refuse to replay over an untracked file: it reports
    success, the change is not in the branch, and the file is still sitting in
    the tree looking like it had been dealt with. Measured, not assumed.

    Unstaged changes to tracked files count too: git refuses over those, but as
    "You must edit all merge conflicts", of a rebase with no conflict in it.

    Staged content only counts when `staged` is set, which is the split case.
    Everywhere else it is exactly what `--continue` is for: it is how every
    resolved conflict is committed, so counting it would refuse the normal flow.

    Scoped to the commit's own paths, so unrelated scratch work is not something
    this has an opinion about. Asked of the paths rather than of HEAD's position,
    because it has to keep holding once the first piece of a split is committed.
    """
    if not applied:
        return ()
    paths = git.lines("diff", "--name-only", f"{applied}^", applied)
    if not paths:
        return ()
    left: list[tuple[str, str]] = []
    for entry in git.lines("status", "--porcelain", "--untracked-files=all", "--", *paths):
        code, path = entry[:2], entry[3:]
        # X is the index against HEAD, Y the working tree against the index.
        if code == "??" or code[1] != " " or (staged and code[0] != " "):
            left.append((code, path))
    return tuple(left)


def _why_not_continuing(left: tuple[tuple[str, str], ...]) -> str:
    """Name what is being left behind, and what git would have done with it."""
    listed = ", ".join(path for _, path in left)
    dropped = [path for code, path in left if code == "??"]
    if dropped:
        return (
            f"Refusing to continue: {', '.join(dropped)} is untracked and this step's "
            "commit changed it -- the state a split leaves for a file the commit added. "
            "Git would carry on and report success, leaving the change out of the "
            "branch with the file still sitting in the tree. `git add` and commit it, "
            "or move it aside if it does not belong."
        )
    return (
        f"Refusing to continue: this step's commit changed {listed}, which is in the "
        "working tree and not in a commit. Commit it -- in as many pieces as you want, "
        "if this is a split -- or `git checkout` it away if it was meant to be dropped. "
        "Git refuses this too, as merge conflicts that need `git add`, of a rebase that "
        "has no conflict in it."
    )


def _why_not_splittable(report: StatusReport) -> str:
    """Say what is wrong and what to do instead, not just that it was refused."""
    if report.operation is None:
        return (
            "Refusing to split: nothing is in progress, so there is no step whose "
            f"commit this would be. HEAD is {report.head.sha[:9]} "
            f"({report.head.subject!r}); dividing that into several commits is an "
            "ordinary `git reset HEAD^`, which needs nothing this tool guards."
        )
    if report.unapplied:
        return (
            "Refusing to split: the commit this step applied has already been taken "
            "back out, and its changes are in the working tree. Commit them in "
            "pieces, then call proceed."
        )
    return (
        f"Refusing to split: the {report.operation} is {report.state}, and HEAD "
        f"({report.head.sha[:9]} {report.head.subject!r}) is not a commit this "
        f"step created. {report.guidance}"
    )


def _why_not_amendable(report: StatusReport) -> str:
    """Say what is wrong and what to do instead, not just that it was refused."""
    if report.operation is None:
        return (
            "Refusing to amend: nothing is in progress, so there is no step whose "
            f"commit this would be. HEAD is {report.head.sha[:9]} "
            f"({report.head.subject!r}); amending that is an ordinary "
            "`git commit --amend`, which needs nothing this tool guards."
        )
    return (
        f"Refusing to amend: the {report.operation} is {report.state}, and HEAD "
        f"({report.head.sha[:9]} {report.head.subject!r}) is not a commit this "
        f"step created. {report.guidance}"
    )


def proceed(
    repo: str = ".",
    auto_resolve: bool = False,
    message: str | None = None,
    keep_stop: bool = True,
) -> StatusReport:
    """Carry on with whatever is in progress, and report where it stops next.

    Calls the operation's own continue -- a cherry-pick is not finished by
    `git rebase --continue` -- so this is the one call whatever stopped.

    Refused while any path is still unmerged, which is the other way a marker
    reaches a commit.

    `message` is the message for the commit this continue is about to create,
    for a rebase stopped at a conflict. It is the only way to set one there: the
    commit does not exist yet, so `rebase_amend` is refused, and at a conflicted
    `edit` or `reword` there is no later stop -- so without this, a resolution
    that deserves a word in the message cannot have one. Anywhere else it is
    refused rather than ignored, and says where the message belongs instead.

    Comment lines are stripped, as they are from any commit message git takes
    from a file.

    `auto_resolve` behaves as it does in rebase_start, and is off for the same
    reason: deciding a conflict without reading it is not this tool's job.

    `keep_stop` gives back the stop a conflicted `edit` or `reword` spends on
    its conflict, by queueing a `break` in front of the remaining steps: the
    resolution is committed and the rebase stops again immediately, with that
    commit as HEAD and `rebase_amend` applying to it. On by default, because
    without it the caller has to have known to stage the whole of its change
    before continuing, and the one that did not gets no second chance. Pass
    False to continue straight past, which is git's own behaviour.
    """
    git = _git(repo)
    state = read_state(git)
    if message is not None:
        _set_pending_message(git, state, message)
    if isinstance(state, (Conflicted, Applying)) and state.unmerged:
        raise ValueError(
            "Refusing to continue: still unmerged: "
            f"{', '.join(state.unmerged)}. Stage each answer with resolve first."
        )
    if isinstance(state, StoppedAfterApply):
        left = _split_leftovers(git, state.applied, staged=state.unapplied)
        if left:
            raise ValueError(_why_not_continuing(left))
    command = _carry_on_command(state)
    if command is None:
        raise ValueError(_nothing_to_carry_on(git, state))
    _record_handwork(git, state)
    restored = _restore_lost_stop(git, state) if keep_stop else False
    # Stopping again on the next conflict is an ordinary outcome, not a failure,
    # so the exit status is read from the state rather than from git.
    result = git.run("-c", "core.editor=true", *RERERE, command, "--continue", check=False)
    report = _advance(git, _git_said(result), auto_resolve, _replayed(result))
    if not restored:
        return _forget_restored_stop(git, report)
    _remember_restored_stop(git, report)
    return replace(
        report, can_amend=True, guidance=_kept_stop_guidance(report)
    )


def _remember_restored_stop(git: Git, report: StatusReport) -> None:
    """Record which commit the restored stop is holding.

    Nothing in the repository says it. Git writes `rebase-merge/amend` at an
    `edit` stop and writes nothing at a `break`, so a later `status` reading the
    same stop would call it unamendable -- correctly for every other break, and
    wrongly for this one.

    Beside git's own rebase state rather than in the session, for two reasons:
    this server drives rebases it did not start, which have no session at all,
    and the fact is true of one stop of one rebase rather than of the run. It
    goes away when `rebase-merge/` does, which is exactly when it stops being
    true.
    """
    _restored_stop_file(git).write_text(report.head.sha + "\n")


def _forget_restored_stop(git: Git, report: StatusReport) -> StatusReport:
    """Clear the record once the rebase has moved off that stop.

    Left behind, it would make some later break -- or the same sha reached
    again -- read as amendable when the step behind it was somebody else's.
    """
    _restored_stop_file(git).unlink(missing_ok=True)
    return report


def _restored_stop_file(git: Git) -> Path:
    return git.git_path("rebase-merge/rebase-mcp-restored-stop")


def _is_restored_stop(git: Git | None, state: RebaseState) -> bool:
    """Whether this break is the one queued for a stop a conflict spent.

    By the commit, not by the fact of a break: a caller's own `break` reaches
    the same state, and HEAD there is the previous step's commit rather than
    one it asked to edit.
    """
    if git is None or not isinstance(state, StoppedWithoutApply):
        return False
    path = _restored_stop_file(git)
    return path.exists() and path.read_text().strip() == state.head.sha


def _restore_lost_stop(git: Git, state: RebaseState) -> bool:
    """Queue a `break` so an `edit` that conflicted still gets its stop.

    `edit` promises a stop at which the commit can be changed. When the commit
    conflicts, the stop is spent on the conflict instead: `--continue` commits
    the resolution and moves on, and the caller who did exactly what the
    amending advice said finds the commit went past unchanged. Warning about it
    was the first answer, and warning is what a caller reads *after* choosing
    the mode it is now stuck in.

    A `break` at the head of the remaining steps gives the stop back. Git
    commits the resolution, reaches the break immediately, and stops with HEAD
    on the commit that was just created -- which is the stop `edit` described,
    arriving one step later than it would have. `rebase_amend` works there.

    Before any leading `exec`, deliberately: the check then runs on the commit
    as amended rather than on the resolution as first staged, which is the
    question a per-commit check is being asked.

    Only for a rebase, and only for the actions that promised a stop. A
    conflicted `pick` promised nothing, and inserting a stop it did not ask for
    would be this tool inventing steps.
    """
    if not is_rebasing(state):
        return False
    # Not `isinstance(state, Conflicted)`: staging the resolution takes those
    # paths out of the unmerged list, so by the time a caller continues, the same
    # stop reads as StoppedWithoutApply. `_pending_commit` is the question that
    # survives that -- see its own note, which this is the third instance of.
    if not _pending_commit(git, state):
        return False
    action = getattr(state, "action", "").lower()
    if action not in STOPPING_ACTIONS:
        return False
    path = git.git_path("rebase-merge/git-rebase-todo")
    if not path.exists():
        return False
    path.write_text("break\n" + path.read_text())
    return True


def _kept_stop_guidance(report: StatusReport) -> str:
    """Say the stop is the one the conflict spent, so it is not read as a new one."""
    return (
        "Stopped at a break queued in place of the stop this step's conflict "
        "spent, so the commit it just created can still be changed: it is HEAD "
        "now, and rebase_amend applies to it. Call proceed when it is right. "
    ) + report.guidance


def _set_pending_message(git: Git, state: RebaseState, message: str) -> None:
    """Write the message the next commit of a conflicted rebase step will take.

    `rebase-merge/message` is where git keeps it, and where `--continue` reads it
    from. Verified against the alternative: overwriting `MERGE_MSG` instead is
    silently ignored by a rebase, which is the failure that would be hardest to
    notice -- the rebase succeeds and keeps the old message.

    Refused at every other stop, each for its own reason rather than a shared
    one, because the answer to "then how do I set it" differs:

    - A stop with the commit applied has `rebase_amend(message=...)`, which
      rewrites the commit that exists.
    - A `break` or a failing `exec` is creating no commit at all.
    - Another operation's conflict keeps its message somewhere else, and this has
      only been established for a rebase. Claiming it for a cherry-pick without
      having checked is how a message goes quietly missing.
    """
    if not _pending_commit(git, state):
        raise ValueError(
            "A message can only be set where a commit is about to be created from "
            f"a resolution, and this is {_report(state).state}. "
            + _why_not_this_message(state)
        )
    path = git.git_path("rebase-merge/message")
    path.write_text(message if message.endswith("\n") else message + "\n")


def _pending_commit(git: Git, state: RebaseState) -> bool:
    """Whether a rebase step is part-way through making a commit.

    Not `isinstance(state, Conflicted)`, which is the obvious answer and the
    wrong one: staging a resolution takes those paths out of the unmerged list,
    so the same stop reads as `StoppedWithoutApply` afterwards -- and staging the
    resolution is exactly what a caller has just done when it asks for this. The
    first version of this check refused every real use for that reason.

    Second time that has caught something. `_record_handwork` has the same note,
    and the shared lesson is that `Conflicted` describes an index, not a step: to
    ask about the step, read what it was doing.

    So the step's action decides, and a pending message has to be there for git
    to overwrite. A `break` or a failing `exec` has neither.
    """
    if isinstance(state, Conflicted):
        return True
    if not isinstance(state, StoppedWithoutApply):
        return False
    return (
        state.action.lower() in COMMITTING_ACTIONS
        and git.git_path("rebase-merge/message").exists()
    )


def _why_not_this_message(state: RebaseState) -> str:
    if isinstance(state, StoppedAfterApply):
        return (
            "The commit exists here, so rebase_amend(message=...) is what rewrites "
            "it."
        )
    if isinstance(state, StoppedWithoutApply):
        return f"`{state.action}` creates no commit, so there is no message to set."
    if isinstance(state, Conflicted):  # unreachable: those are accepted above
        return ""
    if isinstance(state, Applying):
        return (
            f"A {state.operation} keeps its pending message elsewhere, and only a "
            "rebase's has been established here; setting the wrong file would "
            "leave the old message in place without saying so. Commit the "
            "resolution yourself with the message you want."
        )
    return "Nothing is mid-operation, so there is no pending commit to name."


def _carry_on_command(state: RebaseState) -> str | None:
    """The git command that carries this operation on, if one can.

    The operation names its own command in every case -- `git cherry-pick
    --continue` finishes a cherry-pick and nothing else does -- so the mapping
    is the identity, and the only real question is whether there is one at all.
    """
    if is_rebasing(state):
        return "rebase"
    if isinstance(state, Applying) and state.operation != "unknown":
        return state.operation
    return None


def _nothing_to_carry_on(git: Git, state: RebaseState, verb: str = "continue") -> str:
    """Why there is nothing to carry on, in the terms of what actually happened.

    A rebase that ran to its end leaves nothing to continue, which is the same
    absence as never having started one -- and reported in those words it reads
    as the state having been lost, at the moment a branch has just been
    rewritten and nobody has checked it. The session says which it is, so it is
    asked rather than the caller left to find out from `status`.
    """
    if isinstance(state, Applying):  # the unknown operation: nothing owns it
        return (
            f"Nothing to {verb}: the conflict came from something that left no "
            "record of itself -- a stash popped into one, or `checkout -m` -- so "
            "there is no operation to finish. Resolve the paths and commit as usual."
        )
    finished = _finished(git)
    if finished is not None and finished.branch_moved:
        return (
            f"Nothing to {verb}: the rebase ran to the end. The branch now has "
            f"{plural(finished.rewritten, 'commit')} on {finished.base}, unchecked. "
            "Call rebase_finish."
        )
    if finished is not None:
        return (
            f"Nothing to {verb}: the rebase is over and the branch is exactly where "
            "it started, so it was abandoned outside this server or had nothing to "
            "change. Call rebase_finish to close it out."
        )
    return f"Nothing in progress: no rebase, cherry-pick, revert or merge to {verb}."


def _commit_info(git: Git, revision: str) -> CommitInfo:
    sha, _, subject = git.out("log", "-1", "--format=%H%n%s", revision).partition("\n")
    return CommitInfo(sha=sha, subject=subject)


# Progress ticks and git's generic advice add length without adding meaning; the
# sentence explaining the stop is what is worth keeping.
NOISE = re.compile(r"^(Rebasing \(\d+/\d+\)|hint:|\s*$)")

# Git's own sentence for a conflict it resolved from its recorded memory. This
# server turns rerere on (see RERERE) and leaves autoUpdate off precisely
# because a replay "is a guess from an earlier context and should be looked
# at" -- and then said so only inside git's passthrough text, while its own
# far milder auto-compositions got a field of their own. Parsing a message is
# not this project's habit, but the alternative is asking git for something it
# does not report; a wording change degrades this to silence, never to a wrong
# answer.
REPLAYED = re.compile(r"^Resolved '(.+)' using previous resolution\.$", re.M)


def _replayed(result: GitResult) -> tuple[str, ...]:
    """Paths git resolved from a resolution it recorded earlier."""
    return tuple(REPLAYED.findall(result.stderr + result.stdout))


def _git_said(result: GitResult, limit: int = 1200) -> str:
    """The part of git's output that explains itself."""
    lines = [
        line.rstrip()
        for line in (result.stderr + result.stdout).splitlines()
        if not NOISE.match(line)
    ]
    said = "\n".join(lines).strip()
    return said if len(said) <= limit else said[:limit] + "\n[...]"


def _git(repo: str) -> Git:
    path = Path(repo).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    return Git(path)


def _contained(repo: Path, path: str) -> tuple[Path, Path]:
    """The repository's root and the path, both resolved, one inside the other.

    The containment check only holds if the repository it is tested against is
    resolved too. `_git` hands over a resolved path today, but a repository
    reached through a symlink and compared against an unresolved one would have
    every path in it refused as though it were outside.
    """
    root = repo.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"{path} is not inside {repo}")
    return root, target


def _info(commit: Commit) -> CommitInfo:
    return CommitInfo(sha=commit.sha, subject=commit.subject)


# A rebase of any size stops many times; the cap only exists so a bug cannot
# spin forever. Reaching it means something is wrong, not that a branch is long.
AUTO_STEPS = 200


def _advance(git: Git, git_said: str, auto_resolve: bool,
             replayed: tuple[str, ...] = ()) -> StatusReport:
    """Read where the rebase stopped, composing the decidable conflicts on the way.

    Composing is opt-in. Where the two sides edited different lines there is
    usually one answer both would recognise, but "usually" is doing work there:
    lines that do not overlap can still contradict each other, and this cannot
    tell. So it happens only when asked for.
    """
    resolved: list[str] = []

    def stopped(state: RebaseState) -> StatusReport:
        """The report for a stop the caller is about to be handed.

        One place, so an observed check runs exactly once per call and cannot be
        forgotten at one of the four ways out below.
        """
        return _report(state, git_said, tuple(resolved), git=git,
                       replayed=replayed, check=_observed_check(git, state))

    for _ in range(AUTO_STEPS):
        state = read_state(git)
        _record_handwork(git, state)
        command = _carry_on_command(state)
        if not isinstance(state, (Conflicted, Applying)) or not auto_resolve:
            return stopped(state)
        if not state.unmerged or command is None:
            return stopped(state)

        composed = {path: auto_resolve_file(git, path) for path in state.unmerged}
        if any(text is None for text in composed.values()):
            return stopped(state)

        for path, text in composed.items():
            assert text is not None
            (git.repo / path).write_text(text)
            git.run("add", "--", path)
            resolved.append(path)
        result = git.run("-c", "core.editor=true", *RERERE, command, "--continue", check=False)
        git_said = _git_said(result)
        replayed = replayed + _replayed(result)
    return stopped(read_state(git))


def _conflicted_guidance(state: Conflicted) -> str:
    """What HEAD is here, and -- the expensive half -- whether it stops again.

    The first half is about amending and was always right. What it left unsaid is
    that at a conflicted `edit` or `reword` this stop *is* the one the action
    promised: continue, and git commits the resolution and goes on to the next
    step with the commit unchanged. A caller can follow the amending advice to
    the letter, resolve, continue, and lose the edit -- which is exactly how it
    was found, at the cost of replaying a 63-commit branch a second time.

    So the warning goes where the decision is made, and says what to do instead
    rather than only what not to expect: stage the change now, alongside the
    resolution, because `--continue` commits everything staged.
    """
    said = (
        f"Stopped part-way through applying {state.replaying.sha[:9]} "
        f"({state.replaying.subject}). That commit does not exist yet, so HEAD is "
        "still the one before it and amending would rewrite the wrong commit. "
        "Read the conflicted paths with `conflicts`, which reports each region "
        "as what the two sides did rather than as markers, stage each answer "
        "with `resolve`, then continue. `rebase_compare` says whether what has "
        "been replayed so far still matches the originals."
    )
    if not state.action_stop_lost:
        return said
    intended = "change in this commit" if state.action == "edit" else "reword"
    return said + (
        f" This conflict has spent the stop `{state.action}` promised: git commits "
        "the resolution and moves to the next step. `proceed` gives that stop back, "
        "by queueing a break -- the rebase stops again immediately with the new "
        "commit as HEAD, and rebase_amend applies to it there, so the "
        f"{intended} can wait until you have seen what the resolution made. "
        "`proceed(keep_stop=False)` continues straight past instead, in which case "
        "everything has to be staged now, since `--continue` commits what is "
        "staged, and `proceed(message=...)` is the only way to set the message."
    )


def _observed_check(git: Git, state: RebaseState) -> CheckResult | None:
    """Run the check at this stop and report what it said, without acting on it.

    The other half of `check_command`. As `--exec` it is a gate: a non-zero exit
    halts the rebase, which is what you want when nobody is watching. It is the
    wrong shape when somebody is: a branch is rarely green at every commit of its
    own history -- a budget raised one commit after the file that outgrew it, a
    helper used one commit before it is defined -- and a gate then stops on that
    rather than on anything the rebase did. Recovering means rewriting the todo,
    so the feature goes unused on the one workflow that most wants it.

    Observed instead, the same command answers the question actually being asked
    at each stop: what does the suite say here? The caller compares it against the
    baseline and decides. Nothing halts.

    Skipped at a conflicted stop. The working tree still holds markers there, so
    the answer would be about the conflict rather than about the commit, and a
    long suite run would be spent saying so.
    """
    session = load_session(git)
    if session is None or not session.check_command or session.check_halts:
        return None
    if isinstance(state, (Conflicted, Applying)) and state.unmerged:
        return None
    # Through a shell, which is how git's own `exec` runs it: the same string has
    # to mean the same thing whichever half of `check_command` is in use.
    finished = subprocess.run(
        session.check_command,
        shell=True,
        cwd=git.repo,
        capture_output=True,
        text=True,
    )
    said = (finished.stdout + finished.stderr).strip()
    return CheckResult(
        command=session.check_command,
        ok=finished.returncode == 0,
        output=said[-CHECK_OUTPUT:] if len(said) > CHECK_OUTPUT else said,
    )


def _record_handwork(git: Git, state: RebaseState) -> None:
    """Note that this continue is about to put a decision into a commit.

    Two of them, and the finish check had a record of neither:

    - A step that conflicted will commit a resolution, and a resolution is a
      decision. Composing two sides is not the same text as either side, so the
      branch's contribution legitimately moves.
    - Continuing from an `edit` stop with something staged folds it into the
      commit. That is the path this server recommends over `rebase_amend` --
      "staging it and calling proceed does that" -- and it was the one that left
      no trace, so the finish check read the tool's own advice as damage.

    The conflict is recorded when the state is *seen*, not when it is continued
    from, because staging a resolution takes the path out of the unmerged list:
    by the time `proceed` runs, the same stop reads as `StoppedWithoutApply` and
    nothing left says a conflict happened there. Called from the top of
    `_advance`, which is where every stop a caller can act on is observed.

    The staged case has to be read at the continue instead, since at the moment
    the stop is reported nothing is staged yet. Written before git runs rather
    than after, so a note is never lost to a process that dies between
    committing and recording; the cost is a note for a continue git then
    refused, which is the harmless direction -- it can only make the finish
    check more forgiving, and the refusals are guarded before this is reached.

    A missing session means a rebase this server did not start, which has nothing
    to check against and so nothing to record for.
    """
    session = load_session(git)
    if session is None:
        return
    if isinstance(state, Conflicted):
        if state.replaying.sha in session.resolved:
            return
        save_session(git, replace(session, resolved=(*session.resolved, state.replaying.sha)))
        return
    if not isinstance(state, StoppedAfterApply) or not state.applied:
        return
    # Nothing staged means an ordinary continue, which changes no content: at
    # this stop the commit has just been created, so the index matches HEAD until
    # somebody puts something in it.
    if git.succeeds("diff", "--cached", "--quiet"):
        return
    if state.applied in session.amended:
        return
    save_session(git, replace(session, amended=(*session.amended, state.applied)))


def _after_apply_guidance(state: StoppedAfterApply) -> str:
    """What HEAD is at this stop, which decides whether amending means anything.

    Three different stops share it, and the difference is not in what git prints:
    the ordinary one, a fixup run mid-accumulation, and a commit that has been
    unapplied to be split. Only the first is one where `--amend` rewrites the
    commit the caller has in mind.
    """
    if state.unapplied:
        return (
            f"Stopped at `{state.action}`, and {state.applied[:9]} has since been "
            "taken back out: HEAD is the commit before it and its changes are in the "
            "working tree. That is the state a split leaves. Commit them in as many "
            "pieces as you want, then call proceed. Amending here would rewrite the "
            "commit before the one this step applied, so it is refused."
        )
    if state.fixups_pending:
        return (
            f"Stopped at `{state.action}`. Git will amend HEAD "
            f"({state.head.sha[:9]}), which is a run of "
            f"{len(state.fixups_pending)} fixup or squash steps so far, not "
            f"{state.replaying.sha[:9]} on its own. Its message is still "
            "git's template and is rewritten when the run ends, so ignore "
            "the subject above."
        )
    # The sha the caller asked for and the one they now have, when the rebase had
    # to rewrite it. Saying both keeps `replaying` in the report from reading as
    # a commit that failed to apply.
    rewritten = (
        f" (applied as {state.applied[:9]}, since its parent moved)"
        if state.applied and state.applied != state.replaying.sha
        else ""
    )
    return (
        f"Stopped at `{state.action}` with {state.replaying.sha[:9]} applied"
        f"{rewritten}. HEAD is that commit, so amending it is safe. Staging "
        "changes and calling proceed folds them into it, with no separate amend "
        "step: that is what `git rebase --continue` does here. rebase_amend is "
        "for changing the message, for staging tracked files in the same call, "
        "or for seeing the commit before and after."
    )


def _outside_guidance(state: Applying) -> str:
    """Say what is going on, when it is not a rebase doing it."""
    if not state.unmerged:
        return (
            f"A {state.operation} is in progress with nothing unmerged: the conflicts "
            "are resolved and staged, and it has still to be told to commit them. "
            "Call proceed."
        )
    count = plural(len(state.unmerged), "path")
    next_step = (
        " Call conflicts to read them, resolve to stage each answer, then "
        "proceed."
    )
    if state.operation == "unknown":
        return (
            f"{count} conflicted, from something that left no record of itself -- a "
            "stash popped into a conflict, or `checkout -m`. The stages are in the "
            "index either way, so the regions read the same as any other conflict. "
            "Call conflicts to read them and resolve to stage each answer; there "
            "is nothing to continue afterwards, since nothing is mid-operation."
        )
    if state.incoming is None:
        return f"A {state.operation} left {count} conflicted." + next_step
    return (
        f"A {state.operation} of {state.incoming.sha[:9]} ({state.incoming.subject}) "
        f"left {count} conflicted. Nothing has been committed yet." + next_step
    )


def _finished(git: Git) -> FinishedRebase | None:
    """The rebase that has just ended, when one has.

    A session outliving the rebase that wrote it is the signal: rebase_start
    writes it, and finishing, aborting and withdrawing a failed start all clear
    it, so a session sitting beside a repository with no rebase running means one
    ran to the end and nothing has checked it.

    Read here rather than only in the tools that drive a rebase, so that a bare
    status call answers it too. The session is kept in the git directory
    precisely because the server can be restarted mid-rebase, and the caller who
    comes back afterwards has no other way to find out what happened.
    """
    session = load_session(git)
    if session is None:
        return None
    # A range against a base that is no longer resolvable would fail, and a
    # status call that raises is worse than one that reports the count as zero:
    # the point of the call is to find out where things stand.
    range_ = f"{session.base_sha}..HEAD"
    rewritten = len(git.lines("rev-list", range_)) if git.succeeds("rev-parse", range_) else 0
    return FinishedRebase(
        rewritten=rewritten,
        base=session.base,
        backup_ref=session.backup_ref,
        branch_moved=git.out("rev-parse", "HEAD") != session.backup_sha,
    )


def _finished_guidance(finished: FinishedRebase | None) -> str:
    if finished is None:
        return "No rebase in progress."
    if not finished.branch_moved:
        return (
            "No rebase in progress, and the branch is exactly where it started. "
            "Either it was abandoned outside this server, or the rebase had "
            f"nothing to change. The tip is still tagged {finished.backup_ref}; "
            "abort has nothing left to undo."
        )
    return (
        f"Rebase finished: {plural(finished.rewritten, 'commit')} between "
        f"{finished.base} and HEAD, and nothing is in progress. The branch is "
        "rewritten but unchecked -- call rebase_finish to compare what is here "
        f"against {finished.backup_ref} and to restore anything moved aside."
    )


def _report(
    state: RebaseState,
    git_said: str = "",
    auto_resolved: tuple[str, ...] = (),
    git: Git | None = None,
    replayed: tuple[str, ...] = (),
    check: CheckResult | None = None,
) -> StatusReport:
    """The state as a report, stamped with the checkout it is about.

    The stamp is applied here rather than in each `case` so that a state added
    later cannot arrive without it -- the same reason `assert_never` guards the
    end of the match.  It needs `git`, and the callers that pass none want the
    bare state name and nothing else.
    """
    report = _state_report(state, git_said, auto_resolved, git, replayed)
    if check is not None:
        report = replace(report, check=check)
    if git is None:
        return report
    worktree, branch = git.where()
    return replace(report, worktree=worktree, branch=branch)


def _state_report(
    state: RebaseState,
    git_said: str = "",
    auto_resolved: tuple[str, ...] = (),
    git: Git | None = None,
    replayed: tuple[str, ...] = (),
) -> StatusReport:
    match state:
        case NotRebasing():
            # Without `git` this is the bare state name, which is all the callers
            # that pass no repository want it for.
            finished = _finished(git) if git is not None else None
            return StatusReport(
                git_said=git_said,
                auto_resolved=auto_resolved,
                replayed_resolutions=replayed,
                state="not_rebasing",
                head=_info(state.head),
                head_is_replaying_commit=False,
                can_amend=False,
                guidance=_finished_guidance(finished),
                finished=finished,
            )
        case Conflicted():
            return StatusReport(
                git_said=git_said,
                auto_resolved=auto_resolved,
                replayed_resolutions=replayed,
                state="conflicted",
                operation="rebase",
                head=_info(state.head),
                head_is_replaying_commit=False,
                can_amend=False,
                action_stop_lost=state.action_stop_lost,
                guidance=_conflicted_guidance(state),
                step=StepInfo(state.step.index, state.step.total),
                action=state.action,
                replaying=_info(state.replaying),
                conflicted_files=state.unmerged,
            )
        case Applying():
            return StatusReport(
                git_said=git_said,
                auto_resolved=auto_resolved,
                replayed_resolutions=replayed,
                state="conflicted" if state.unmerged else "applying",
                operation=state.operation,
                head=_info(state.head),
                head_is_replaying_commit=False,
                can_amend=False,
                guidance=_outside_guidance(state),
                replaying=_info(state.incoming) if state.incoming else None,
                conflicted_files=state.unmerged,
            )
        case StoppedAfterApply():
            return StatusReport(
                git_said=git_said,
                auto_resolved=auto_resolved,
                replayed_resolutions=replayed,
                state="stopped_after_apply",
                operation="rebase",
                head=_info(state.head),
                head_is_replaying_commit=state.holds_applied_commit
                and not state.fixups_pending,
                can_amend=state.holds_applied_commit,
                unapplied=state.unapplied,
                guidance=_after_apply_guidance(state),
                step=StepInfo(state.step.index, state.step.total),
                action=state.action,
                replaying=_info(state.replaying),
            )
        case StoppedWithoutApply():
            restored = _is_restored_stop(git, state)
            return StatusReport(
                git_said=git_said,
                auto_resolved=auto_resolved,
                replayed_resolutions=replayed,
                state="stopped_without_apply",
                operation="rebase",
                head=_info(state.head),
                head_is_replaying_commit=False,
                can_amend=restored,
                guidance=(
                    "Stopped at a break queued in place of the stop the last "
                    "step's conflict spent. HEAD is the commit that step "
                    "created, so rebase_amend applies to it."
                    if restored
                    else f"Stopped at `{state.action}`, which applied nothing. HEAD is "
                    "whatever the previous step left, not a commit this step "
                    "created, so it is not the one to amend."
                ),
                step=StepInfo(state.step.index, state.step.total),
                action=state.action,
            )
        case _:
            # Reached only if a state is added without a branch here.
            assert_never(state)


@dataclass(frozen=True)
class RefCarry:
    """Whether a branch stacked on the rebased one came along with it.

    `after` is None for a ref that has since been deleted, which is not the same
    as one that stayed put and should not read as it.
    """

    ref: str
    before: str
    after: str | None
    moved: bool


@dataclass(frozen=True)
class FinishReport:
    ok: bool
    backup_ref: str
    branch_change: str | None
    commits_with_markers: tuple[str, ...]
    commits: tuple[CommitInfo, ...]
    restored: tuple[str, ...]
    guidance: str
    # True when the difference above is the same lines in a different order,
    # which a resolution that moves a block does and which is not damage.
    reordered_only: bool = False
    # True when the result has exactly the content it started with. Reported
    # because a branch whose diff-to-base changed and whose tree did not has lost
    # nothing: the change is in the base instead. That is what dropping a commit
    # already upstream looks like from here, and it is not damage.
    tree_identical: bool = False
    # Which commits account for a moved branch change, paired before and after.
    # `branch_change` says the branch's diff moved and names the paths; the
    # question after it is always which commit did that, and answering it meant
    # leaving the tool for `git diff`. Empty when nothing moved.
    changed_commits: tuple[CommitChange, ...] = ()
    # git's own commit-by-commit rendering of the same thing, behind
    # include_diff because on a long rebase it is very large.
    commit_detail: str | None = None
    # Each branch `update_refs` was asked to carry, and whether it did. Empty
    # when it was not asked for. Reported rather than refused: git does move
    # them, and the one report a caller reads should not have to be reconstructed
    # from merge-base by hand to know that a stack survived.
    carried_refs: tuple[RefCarry, ...] = ()
    # Local files that a trip through the backup tag would silently destroy,
    # because the tag still tracks them and this branch no longer does. Never a
    # problem: the rewrite did what it was asked. See `locals_the_backup_tracks`.
    fragile_locals: tuple[str, ...] = ()
    # And the ones it has already deleted, which is the same situation one step
    # later. Both halves are reported: still here means the backup tag will take
    # it, gone means the rewrite already has.
    lost_locals: tuple[str, ...] = ()


def _lost_locals_note(backup_ref: str, lost: Sequence[str]) -> str:
    """What to say about a local file the rewrite has already deleted.

    Shared by the start and the finish report so the advice cannot drift. Naming
    the command matters more than naming the cause: the content is in the backup
    tag and nowhere else, and this is the only place that knows both.
    """
    if not lost:
        return ""
    listed = ", ".join(lost)
    it = "it" if len(lost) == 1 else "them"
    return (
        f"{listed} {'was' if len(lost) == 1 else 'were'} tracked at {backup_ref} "
        f"and this history does not track {it}, so moving between the two deleted "
        f"the working {'copy' if len(lost) == 1 else 'copies'} -- silently, since "
        f"{it} {'is' if len(lost) == 1 else 'are'} ignored here. Nothing else "
        f"holds {it}: restore with `git show {backup_ref}:<path> > <path>`. "
    )


def _how_much_moved(change: Change) -> str:
    """State the size, so `allow_change` is a judgement rather than a formality.

    Whoever passes it already knows something changed -- they changed it. What
    they cannot tell from "the branch no longer makes the same change" is whether
    the amount fits what they did, and a table underneath is read as evidence of
    the thing already agreed to rather than as a quantity to check.

    Found the hard way: a one-line fix to one commit produced nine moved lines,
    because the resolution that carried it re-added three lines that were already
    merged. The table said so. The sentence did not, and the sentence is what got
    read.
    """
    return (
        f" The difference is {change.lines} line{'' if change.lines == 1 else 's'} "
        f"across {change.paths} file{'' if change.paths == 1 else 's'}; if that is "
        "more than what you changed, the extra is the part to look at"
    )


def _why_the_change_might_be_meant(session: Session, unchanged_tree: bool) -> str:
    """The reading of a moved branch change that fits what this run actually did.

    Three of them, and getting this wrong is expensive in one direction only: a
    report that says "something was lost" when nothing was teaches the caller
    that the check cries wolf, and the next real one gets waved through.

    Amending is the case that was missing, and it is not a corner -- it is what
    an `edit` stop is *for*, and this server hands out `rebase_amend` to do it.
    Every rebase that used one ended here being told its work looked like
    damage, with a hard reset as the only exit offered.

    Resolving was the case missing after that, and it is not a corner either: it
    is what a conflicted rebase *is*. A resolution is a decision, and composing
    two sides gives text that is neither, so the contribution moves and nothing
    has gone wrong. Amending is offered first because it is the narrower claim --
    a named commit was deliberately rewritten -- and a rebase that did both is
    better explained by that than by the conflicts it also had.
    """
    if session.amended:
        count = len(session.amended)
        return (
            f", which is what amending does: {count} "
            f"commit{'' if count == 1 else 's'} "
            f"({', '.join(sha[:9] for sha in session.amended)}) "
            f"{'was' if count == 1 else 'were'} amended during this rebase, on "
            "purpose. Read the difference below and confirm it is only what you "
            "changed, then pass allow_change=true"
        )
    if session.resolved:
        count = len(session.resolved)
        return (
            f", which is what resolving does: {count} "
            f"commit{'' if count == 1 else 's'} "
            f"({', '.join(sha[:9] for sha in session.resolved)}) "
            f"{'was' if count == 1 else 'were'} continued from a conflict, and a "
            "resolution is a decision -- composing two sides gives text that is "
            "neither of them. Read the difference below and confirm it is the "
            "resolutions you made, then pass allow_change=true"
        )
    if unchanged_tree:
        return (
            f", though its content is identical to {session.backup_ref}: nothing "
            "was lost, only the branch's own share of it changed. That is what "
            "dropping a commit already in the new base looks like, and what "
            "moving one commit's work into another does. Pass allow_change=true "
            "if that was the intent"
        )
    return (
        ", so something was lost or resolved wrongly -- or was changed on "
        "purpose by hand, which this run has no record of. Read the difference "
        "below; pass allow_change=true only if all of it is yours"
    )


def _carried_now(git: Git, recorded: Sequence[CarriedRef]) -> tuple[RefCarry, ...]:
    """Where each branch that was to be carried points now."""
    found: list[RefCarry] = []
    for ref in recorded:
        current = git.run("rev-parse", "--verify", "--quiet", f"{ref.ref}^{{commit}}", check=False)
        after = current.stdout.strip() or None
        found.append(
            RefCarry(ref=ref.ref, before=ref.sha, after=after, moved=after not in (None, ref.sha))
        )
    return tuple(found)


def _what_happened_to_the_stack(carried: Sequence[RefCarry]) -> str:
    """Whether the branches stacked on this one came along, in one sentence."""
    if not carried:
        return ""
    left = [entry.ref for entry in carried if not entry.moved]
    if not left:
        named = ", ".join(entry.ref for entry in carried)
        return f" Carried along: {named}. "
    # Worth saying plainly: a ref still on its old sha points into history that
    # was just replaced, and nothing else in this report would mention it.
    return (
        f" Left behind on the commits that were replaced: {', '.join(left)}"
        " -- still pointing at history this rebase rewrote. "
    )


def rebase_finish(
    repo: str = ".",
    allow_change: bool = False,
    allow_markers: bool = False,
    include_diff: bool = False,
) -> FinishReport:
    """Check the finished rebase against the tip it started from, and tidy up.

    What must stay the same is the change the branch makes to its base -- not
    the resulting tree, which legitimately changes when the rebase also moves
    onto newer upstream work. A difference here is usually a report of damage: a
    commit dropped from the todo, or a conflict resolved the wrong way.

    Usually, because the same difference is what a deliberate redistribution
    looks like: a commit dropped because its content is already in the new base,
    or one commit's work divided into others. `tree_identical` separates the two
    -- the content being exactly what it was means nothing was lost, only that
    the branch's own share of it changed -- and `allow_change` accepts it.

    Every rewritten commit is also scanned for conflict markers, since one
    committed part-way and tidied up later still leaves a commit nobody can
    build. `allow_markers=True` for the rebase that legitimately brings such a
    line in -- a file documenting what a conflict looks like, a fixture of
    git's output. They are still named in the report and in the guidance:
    waiving a check is not the same as hiding what it found.

    When the branch's change did move, `branch_change` names the paths whose
    contribution moved and by how many lines -- what actually differs, not a
    diff between the two tips, which on a rebase onto newer upstream work is
    mostly the new base's own commits. `changed_commits` then names the commits
    that account for it, paired before and after: dropped, added, or the same
    commit with different content, since the question after "which files" is
    always which commit changed them. `include_diff=True` adds git's own
    commit-by-commit rendering, which is large on a long rebase and so is not
    sent unasked.

    A rebase started with `update_refs` also reports, per branch it was asked to
    carry, whether that branch actually moved -- git prints a line about it and
    exits zero whether or not it did, and a sibling left behind points into
    history this rebase replaced. Reported rather than refused, since git does
    move them; the point is that confirming it should not mean reconstructing the
    stack from merge-base by hand.

    The backup tag is kept either way; deleting the only record of where the
    branch was is not this tool's decision to make.
    """
    git = _git(repo)
    state = read_state(git)
    if not isinstance(state, NotRebasing):
        raise ValueError(
            f"Refusing to finish: the rebase is still {_report(state).state}. "
            "Continue or abort it first."
        )
    session = load_session(git)
    if session is None:
        raise ValueError("No rebase recorded by this server; nothing to check against.")

    # `upstream_sha` is set only for a rebase given an explicit onto, where the
    # merge-base of the old tip and the landing place is not where the branch's
    # own contribution began.
    fork = session.upstream_sha or None
    change = branch_change(git, session.backup, session.base_sha, fork=fork)
    difference = change.summary if change else None
    unchanged_tree = bool(change) and same_tree(git, session.backup)
    # Only when something moved: pairing every commit costs a range-diff over
    # the whole branch, and there is nothing to attribute when nothing changed.
    comparison = (
        compare_commits(git, session.backup, session.base_sha, fork=fork)
        if change
        else None
    )
    marker_hits = commits_with_markers(git, f"{session.base_sha}..HEAD")
    carried = _carried_now(git, session.carried)
    fragile = locals_the_backup_tracks(git, session.backup)
    lost = locals_the_rewrite_removed(git, session.backup, stashed=session.stashed)
    problems: list[str] = []
    if change and not change.reordered_only and not allow_change:
        problems.append(
            "the branch no longer makes the same change to its base"
            + _why_the_change_might_be_meant(session, unchanged_tree)
            + _how_much_moved(change)
            + f":\n{change.summary}"
        )
    named_markers = ", ".join(f"{hit.sha[:9]} ({hit.subject})" for hit in marker_hits)
    if marker_hits and not allow_markers:
        problems.append("conflict markers were committed in " + named_markers)
    # Said even when the rebase passes, because it only passed by being told to
    # allow it, and a report that reads "checks out" would leave the caller
    # believing the scan found nothing.
    waived = f"Conflict markers are committed in {named_markers}, allowed. " if (
        marker_hits and allow_markers
    ) else ""
    # Said whether or not the rest of the check passed, and never as a problem:
    # the rewrite did what it was asked. What is worth knowing is that the tag
    # this report keeps recommending is now dangerous to check out, and only this
    # report knows both the tag and which paths it would take with it.
    fragile_note = (
        f"{', '.join(fragile)} {'is' if len(fragile) == 1 else 'are'} local now "
        f"and {session.backup_ref} still tracks {'it' if len(fragile) == 1 else 'them'}: "
        f"checking that tag out overwrites {'it' if len(fragile) == 1 else 'them'} and "
        "coming back deletes it, silently, since it is ignored. Copy it aside "
        "before using the tag to compare. "
        if fragile
        else ""
    )

    restored = (
        _unstash(git, session.stashed, session.stash_ref) if not problems else ()
    )
    if not problems:
        clear_session(git)

    return FinishReport(
        ok=not problems,
        backup_ref=session.backup_ref,
        branch_change=difference,
        reordered_only=bool(change and change.reordered_only),
        changed_commits=comparison.changes if comparison else (),
        commit_detail=comparison.detail if comparison and include_diff else None,
        tree_identical=unchanged_tree,
        fragile_locals=fragile,
        lost_locals=lost,
        commits_with_markers=tuple(hit.sha for hit in marker_hits),
        commits=tuple(
            _commit_info(git, sha)
            for sha in git.lines("rev-list", "--reverse", f"{session.base_sha}..HEAD")
        ),
        carried_refs=carried,
        restored=restored,
        guidance=(
            "Rebase checks out"
            + (
                ", with the same lines in a different order than before -- a moved "
                "block rather than a loss. "
                if change and change.reordered_only
                else ". "
            )
            + waived
            + _what_happened_to_the_stack(carried)
            + _lost_locals_note(session.backup_ref, lost)
            + fragile_note
            + "Anything moved aside was restored. The tip before the rebase is "
            f"still tagged {session.backup_ref}."
            if not problems
            else "Not finished: "
            + "; ".join(problems)
            + f". The branch before the rebase is at {session.backup_ref}; "
            "`git reset --hard` to it to undo. "
            + _what_happened_to_the_stack(carried)
            + _lost_locals_note(session.backup_ref, lost)
            + fragile_note
        ),
    )


@dataclass(frozen=True)
class CompareReport:
    """How the commits replayed so far compare with the ones they came from."""

    replayed: int
    total: int
    changed: tuple[CommitChange, ...]
    # Commits of the original range this rebase has not reached yet. They pair as
    # "dropped" because they are genuinely not there -- but they are not lost,
    # they are pending, and calling that a difference mid-run would make the
    # report unreadable at every stop but the last.
    pending: tuple[str, ...]
    detail: str | None
    guidance: str


def rebase_compare(repo: str = ".", include_diff: bool = False) -> CompareReport:
    """Pair the commits replayed so far against the originals, mid-rebase.

    What `rebase_finish` does at the end, available while the rebase is still
    running -- because the question "was that commit replayed faithfully?" is
    asked at every stop, not once. A test suite cannot answer it: it reports the
    state of the branch, which is the branch's own business, and says nothing
    about whether this replay matches what it was replaying. Every stop of a real
    seven-commit rebase had `git range-diff` built by hand for exactly this, which
    is the tool asking for something it already knows how to do.

    `changed` is the commits whose content differs, paired before and after. An
    empty list is the answer worth wanting: every commit reached so far carries
    the same patch it did. `pending` is the tail not yet replayed, kept separate
    so it does not read as loss.

    A commit altered by more than `range-diff`'s creation threshold cannot be
    paired with its original, and comes back as a drop and an add of the same
    subject rather than one "changed". That reading is passed on rather than
    tidied: collapsing such a pair would also collapse a genuine drop and add
    that happened to share a subject, which is the one this check exists to see.

    Read-only, and safe at any stop -- including a conflicted one, where the
    commit part-way through is simply not in the comparison yet.
    """
    git = _git(repo)
    session = load_session(git)
    if session is None:
        raise ValueError(
            "No rebase recorded by this server; there is nothing to compare "
            "against. rebase_finish clears the record once it has checked a "
            "finished rebase."
        )
    fork = session.upstream_sha or None
    original = git.lines(
        "rev-list", "--reverse", f"{fork or session.base_sha}..{session.backup_sha}"
    )
    replayed = git.lines("rev-list", "--reverse", f"{session.base_sha}..HEAD")
    pending = frozenset(original[len(replayed):])
    comparison = compare_commits(git, session.backup, session.base_sha, fork=fork)
    changed = tuple(
        change
        for change in comparison.changes
        if not (change.after is None and change.before in pending)
    )
    faithful = len(replayed) - len(changed)
    return CompareReport(
        replayed=len(replayed),
        total=len(original),
        changed=changed,
        pending=tuple(sorted(pending)),
        detail=comparison.detail if include_diff else None,
        guidance=(
            f"{len(replayed)} of {len(original)} replayed"
            + (f", {len(pending)} still to come" if pending else "")
            + ". "
            + (
                f"All {faithful} carry the patch they came from."
                if not changed
                else f"{len(changed)} differ: "
                + ", ".join(
                    f"{c.subject} ({c.status})" for c in changed
                )
                + ". That is damage unless you made it -- a resolution that "
                "composed two sides, or something staged into the commit on "
                "purpose."
            )
        ),
    )


@dataclass(frozen=True)
class AbortReport:
    head: CommitInfo
    restored: tuple[str, ...]
    guidance: str


def skip(repo: str = ".", auto_resolve: bool = False) -> StatusReport:
    """Drop the commit being applied and carry on.

    For a commit whose change is already in the base under a different sha, or
    one whose conflict resolves to "the branch already says this". Git offers it
    at every conflict; without it here the only way to take that offer is to
    reach past these tools and run git by hand, which is how a rebase ends up
    half driven from each side. Works for a cherry-pick and a revert too.

    Refused when nothing is being applied: skipping is a decision about a
    commit, and at a `break` or a failing `exec` there is no commit in question.
    A merge applies a branch rather than a commit, so git offers it nothing to
    skip with.
    """
    git = _git(repo)
    state = read_state(git)
    if isinstance(state, StoppedWithoutApply):
        raise ValueError(
            f"Refusing to skip: stopped at `{state.action}`, which is not replaying a "
            "commit, so there is nothing to skip. Continue instead."
        )
    if isinstance(state, Applying) and state.operation == "merge":
        raise ValueError(
            "Refusing to skip: a merge applies a whole branch, not a commit, so git "
            "offers nothing to skip it with. Resolve the conflicts, or abort."
        )
    command = _carry_on_command(state)
    if command is None:
        raise ValueError(_nothing_to_carry_on(git, state, "skip"))
    result = git.run("-c", "core.editor=true", *RERERE, command, "--skip", check=False)
    return _advance(git, _git_said(result), auto_resolve, _replayed(result))


def abort(repo: str = ".") -> AbortReport:
    """Abandon whatever is in progress and put back anything that was moved aside.

    Refused for a conflict nothing recorded -- a stash popped into one, say --
    because there is no operation to abort, and guessing at `reset` or
    `checkout` would throw away work this tool never put there.
    """
    git = _git(repo)
    state = read_state(git)
    command = _carry_on_command(state)
    if isinstance(state, Applying) and command is None:
        raise ValueError(
            "Refusing to abort: the conflict came from something that left no record "
            "of itself, so there is no operation to abandon and no way to tell what "
            "undoing it would discard. Resolve the paths, or undo it the way it was "
            "started."
        )
    if command is not None:
        git.run(command, "--abort", check=False)
    session = load_session(git)
    restored = _unstash(git, session.stashed, session.stash_ref) if session else ()
    if session:
        clear_session(git)
    return AbortReport(
        head=_commit_info(git, "HEAD"),
        restored=restored,
        guidance=f"{(command or 'rebase').capitalize()} abandoned."
        + (f" Restored: {', '.join(restored)}." if restored else ""),
    )


def _unstash(
    git: Git, stashed: tuple[str, ...], stash_ref: str | None = None
) -> tuple[str, ...]:
    """Put back what _stash moved, if it is still there to put back.

    Targeted at the entry that holds it, not at the top of the stash stack: a
    stash the user made meanwhile sits above it, and a positional pop would
    take that one and leave the moved-aside files hidden.
    """
    if not stashed:
        return ()
    if stash_ref is not None:
        entry = _stash_index(git, stash_ref)
        if entry is None:
            return ()  # gone; not our place to take someone else's stash
        ok = git.run("stash", "pop", f"stash@{{{entry}}}", check=False).ok
    else:
        ok = git.run("stash", "pop", check=False).ok
    return stashed if ok else ()


def _stash_index(git: Git, sha: str) -> int | None:
    """Where in the stash stack the entry with this sha sits."""
    count = len(git.lines("stash", "list"))
    for index in range(count):
        if git.out("rev-parse", f"stash@{{{index}}}") == sha:
            return index
    return None


def main() -> None:
    mcp.run()


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _rendered(fn: Callable[_P, _R]) -> Callable[_P, CallToolResult]:
    """Register a tool so its result carries a reading of itself as well as its fields.

    The fields go on in `structured_content`, unchanged and still schema-checked;
    the content block gets `render()`, which is what a person watching sees
    instead of a JSON dump of a rebase they are in the middle of.

    Applied at registration rather than to the function, so everything in-process
    -- the tests above all, and one tool calling another -- keeps getting the
    dataclass it asked for. `wraps` carries the annotations over, which is where
    the output schema comes from.
    """

    @wraps(fn)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> CallToolResult:
        report = fn(*args, **kwargs)
        dumped = TypeAdapter(type(report)).dump_python(report, mode="json")
        return CallToolResult(
            content=[TextContent(type="text", text=render(report))],
            structured_content=cast("dict[str, Any]", dumped),
        )

    return wrapper


# One call each rather than a loop: the tools have different signatures, and a
# sequence of them is a union no generic wrapper can be matched against.
mcp.tool(name="status")(_rendered(status))
mcp.tool(name="conflicts")(_rendered(conflicts))
mcp.tool(name="resolve")(_rendered(resolve))
mcp.tool(name="rebase_todo")(_rendered(rebase_todo))
mcp.tool(name="rebase_preflight")(_rendered(rebase_preflight))
mcp.tool(name="rebase_start")(_rendered(rebase_start))
mcp.tool(name="rebase_amend")(_rendered(rebase_amend))
mcp.tool(name="rebase_split")(_rendered(rebase_split))
mcp.tool(name="proceed")(_rendered(proceed))
mcp.tool(name="rebase_finish")(_rendered(rebase_finish))
mcp.tool(name="rebase_compare")(_rendered(rebase_compare))
mcp.tool(name="skip")(_rendered(skip))
mcp.tool(name="abort")(_rendered(abort))
