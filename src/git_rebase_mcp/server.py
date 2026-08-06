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
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Never, assert_never

from mcp.server.mcpserver import MCPServer

from .conflicts import FileConflict, auto_resolve_file, plural, read_conflict, take_side
from .git import Git, GitError, GitResult
from .invariants import (
    Backup,
    Session,
    clear_session,
    commits_with_markers,
    has_markers,
    load_session,
    record_backup,
    save_session,
    branch_change,
)
from .plan import DROPPING_ACTIONS, TODO_LINE, check_plan
from .state import (
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
class StatusReport:
    state: StateName
    head: CommitInfo
    head_is_replaying_commit: bool
    can_amend: bool
    guidance: str
    # What left the index in this state: "rebase", "cherry-pick", "revert",
    # "merge", or "unknown" for a conflict nothing recorded -- a stash popped
    # into one, say. `state` stays "conflicted" for all of them, because what a
    # caller does first is the same in every case, and a second state name would
    # only be a way for a caller checking the old one to miss a real conflict.
    operation: Operation | None = None
    step: StepInfo | None = None
    action: str | None = None
    replaying: CommitInfo | None = None
    conflicted_files: tuple[str, ...] = ()
    # Paths this server composed and staged without asking, because the two
    # sides edited different lines. Named rather than left silent: an automatic
    # resolution is still a resolution, and worth a look.
    auto_resolved: tuple[str, ...] = ()
    # What git printed when it stopped, when this report follows a git command.
    # Git often explains a stop in a way nothing else can reconstruct -- "the
    # previous cherry-pick is now empty" being the one that cost the most time --
    # and swallowing it leaves the caller guessing.
    git_said: str = ""


@mcp.tool()
def status(repo: str = ".") -> StatusReport:
    """Report what `repo` is currently doing.

    Call this before amending, continuing or resolving. `operation` names what
    is in progress -- a rebase, cherry-pick, revert or merge, or "unknown" for a
    conflicted index nothing recorded -- and `head_is_replaying_commit`
    distinguishes a stop where the commit was applied from one where it
    conflicted part-way, which git's own output does not.

    `state` is "conflicted" for every operation that left unmerged paths, so
    that one check answers the question whatever produced them.
    """
    return _report(read_state(_git(repo)))


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
class ResolveReport:
    path: str
    still_conflicted: tuple[str, ...]
    guidance: str


@mcp.tool()
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
    advice = (
        f"Each region lists what the branch did to the base and what {incoming} did "
        "to the same base -- `branch_so_far` is what is here already, `replaying` is "
        "what is being applied over it. branch_so_far_commits names the commits "
        "behind the first side, which is the nearest it has to a stated intent. "
        "Read both, then answer: "
        'resolve(path, take="both"/"branch"/"replaying") where that says '
        "it, or edit the file and call resolve(path) with no content. "
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


@mcp.tool()
def resolve(
    path: str, content: str | None = None, repo: str = ".", take: str | None = None
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

    Every route refuses content that still contains conflict markers. Staging
    one is how a commit ends up with `<<<<<<<` in it, and nothing downstream
    catches that.
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
    if has_markers(content):
        raise ValueError(
            f"{path} still contains conflict markers. Resolve them first: staging "
            "this would commit them."
        )
    if not from_disk:
        target.write_text(content)
    git.run("add", "--", path)

    remaining = tuple(git.lines("diff", "--name-only", "--diff-filter=U"))
    return ResolveReport(
        path=path,
        still_conflicted=remaining,
        guidance=(
            f"Still conflicted: {', '.join(remaining)}."
            if remaining
            else "All paths resolved; call proceed."
            if _carry_on_command(read_state(git)) is not None
            # A conflict nothing recorded has nothing to continue, and saying so
            # here saves the caller finding out from a refusal one call later.
            else "All paths resolved. Nothing is mid-operation, so there is nothing "
            "to continue: commit them as you would any other change."
        ),
    )


@dataclass(frozen=True)
class TodoReport:
    remaining: tuple[str, ...]
    dropped: tuple[CommitInfo, ...]
    guidance: str


@mcp.tool()
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


@mcp.tool()
def rebase_preflight(
    base: str, repo: str = ".", todo: list[str] | None = None
) -> PreflightReport:
    """Check what a rebase would do, without starting it or changing anything.

    Reports commits the todo would drop silently, commits it names that are not
    in the range, commits whose change is already in the base under a different
    sha, anything already in progress or uncommitted, and untracked files a
    replayed commit would collide with.
    """
    git = _git(repo)
    check = check_plan(git, base, todo)
    blocking = _blocking(git)
    collisions = _untracked_collisions(git, base)

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


def _untracked_collisions(git: Git, base: str) -> tuple[str, ...]:
    """Untracked files that checking out the range would refuse to overwrite.

    A rebase stops dead on these before doing anything, which is confusing when
    the file is unrelated scratch work that merely shares a name.
    """
    untracked = set(git.lines("ls-files", "--others", "--exclude-standard"))
    if not untracked:
        return ()
    known = set(git.lines("ls-tree", "-r", "--name-only", base))
    known.update(git.lines("log", "--name-only", "--format=", f"{base}..HEAD"))
    return tuple(sorted(untracked & known))


@dataclass(frozen=True)
class StartReport:
    backup_ref: str
    stashed: tuple[str, ...]
    status: StatusReport
    guidance: str


@mcp.tool()
def rebase_start(
    base: str,
    repo: str = ".",
    todo: list[str] | None = None,
    autosquash: bool = False,
    check_command: str | None = None,
    auto_resolve: bool = False,
    force: bool = False,
) -> StartReport:
    """Begin a rebase onto `base`, and report where it stops.

    Refuses anything rebase_preflight called unsafe, unless `force`. Before
    starting it tags the current tip, so the result can be checked against it,
    and moves aside untracked files a replayed commit would collide with.

    `autosquash` folds every `fixup!` and `squash!` in the range into the commit
    its subject names, which is the workflow `git commit --fixup` sets up. It
    cannot be combined with a todo, since it is a way of generating one.

    `check_command` is run after every commit, which is the only thing that
    catches a step that applies cleanly but leaves the tree broken.

    `auto_resolve` composes conflicts where the two sides touched different
    lines and carries on without stopping. Off by default: lines that do not
    overlap can still contradict each other -- one side adding a call, the other
    removing the helper it needs -- and a conflict resolved without being read
    has to be reviewed afterwards anyway. Use it when replaying a branch whose
    conflicts you already understand.
    """
    if autosquash and todo is not None:
        raise ValueError("autosquash generates the todo, so it cannot be given one.")
    git = _git(repo)
    preflight = rebase_preflight(base, repo, todo)
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
            base=base,
            base_sha=git.out("rev-parse", f"{base}^{{commit}}"),
            stashed=stashed,
            stash_ref=stash_ref,
            check_command=check_command,
        ),
    )

    args = ["rebase", "-i"]
    config: list[str] = ["-c", "core.editor=true", *RERERE]
    if todo is not None:
        # A supplied todo replaces whatever git generates, so --exec would be
        # discarded with it; the exec lines have to be woven in here instead.
        lines = _with_checks(todo, check_command)
        todo_file = git.git_path("rebase-mcp-todo")
        todo_file.write_text("\n".join(lines) + "\n")
        # Git runs this through a shell, so the path is quoted for one. A repo
        # named `re'po` used to make the editor command fail to parse, and the
        # rebase then silently did nothing with the todo.
        config += ["-c", f"sequence.editor=cp {shlex.quote(str(todo_file))}"]
    else:
        if autosquash:
            args.append("--autosquash")
        if check_command:
            args += ["--exec", check_command]
    args.append(base)

    # Stopping at a conflict is an ordinary outcome that git reports as failure,
    # so the result is read from the rebase state instead -- but git's own words
    # about why it stopped are kept, because nothing else can reconstruct them.
    result = git.run(*config, *args, check=False)
    if not result.ok and not is_rebasing(read_state(git)):
        _withdraw_start(git, backup, stashed, stash_ref, result)
    stopped = _advance(git, _git_said(result), auto_resolve)
    return StartReport(
        backup_ref=backup.ref,
        stashed=stashed,
        status=stopped,
        guidance=(
            f"Started. The tip beforehand is tagged {backup.ref}; rebase_finish "
            "checks the result against it. " + stopped.guidance
        ),
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
        raise ValueError(
            f"{GitError(result)}\n\nHEAD moved from {backup.sha[:9]} to {head[:9]} "
            "before git gave up, so the branch was rewritten. Nothing has been "
            f"taken back: the tip beforehand is tagged {backup.ref}, anything "
            "moved aside is still stashed, and rebase_finish checks what is here "
            "now against it."
        )
    _unstash(git, stashed, stash_ref)
    clear_session(git)
    git.run("tag", "-d", backup.ref, check=False)
    raise GitError(result)


def _with_checks(todo: list[str], check_command: str | None) -> list[str]:
    if not check_command:
        return todo
    woven: list[str] = []
    for line in todo:
        woven.append(line)
        if TODO_LINE.match(line):
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


@mcp.tool()
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
    if not isinstance(state, StoppedAfterApply):
        raise ValueError(_why_not_amendable(_report(state)))

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
    after = _commit_info(git, "HEAD")
    return AmendReport(
        before=before,
        after=after,
        guidance=f"Amended {before.sha[:9]} into {after.sha[:9]}. Call proceed.",
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


@mcp.tool()
def proceed(repo: str = ".", auto_resolve: bool = False) -> StatusReport:
    """Carry on with whatever is in progress, and report where it stops next.

    Calls the operation's own continue -- a cherry-pick is not finished by
    `git rebase --continue` -- so this is the one call whatever stopped.

    Refused while any path is still unmerged, which is the other way a marker
    reaches a commit.

    `auto_resolve` behaves as it does in rebase_start, and is off for the same
    reason: deciding a conflict without reading it is not this tool's job.
    """
    git = _git(repo)
    state = read_state(git)
    if isinstance(state, (Conflicted, Applying)) and state.unmerged:
        raise ValueError(
            "Refusing to continue: still unmerged: "
            f"{', '.join(state.unmerged)}. Stage each answer with resolve first."
        )
    command = _carry_on_command(state)
    if command is None:
        raise ValueError(_nothing_to_carry_on(state))
    # Stopping again on the next conflict is an ordinary outcome, not a failure,
    # so the exit status is read from the state rather than from git.
    result = git.run("-c", "core.editor=true", *RERERE, command, "--continue", check=False)
    return _advance(git, _git_said(result), auto_resolve)


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


def _nothing_to_carry_on(state: RebaseState, verb: str = "continue") -> str:
    if isinstance(state, Applying):  # the unknown operation: nothing owns it
        return (
            f"Nothing to {verb}: the conflict came from something that left no "
            "record of itself -- a stash popped into one, or `checkout -m` -- so "
            "there is no operation to finish. Resolve the paths and commit as usual."
        )
    return f"Nothing in progress: no rebase, cherry-pick, revert or merge to {verb}."


def _commit_info(git: Git, revision: str) -> CommitInfo:
    sha, _, subject = git.out("log", "-1", "--format=%H%n%s", revision).partition("\n")
    return CommitInfo(sha=sha, subject=subject)


# Progress ticks and git's generic advice add length without adding meaning; the
# sentence explaining the stop is what is worth keeping.
NOISE = re.compile(r"^(Rebasing \(\d+/\d+\)|hint:|\s*$)")


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


def _advance(git: Git, git_said: str, auto_resolve: bool) -> StatusReport:
    """Read where the rebase stopped, composing the decidable conflicts on the way.

    Composing is opt-in. Where the two sides edited different lines there is
    usually one answer both would recognise, but "usually" is doing work there:
    lines that do not overlap can still contradict each other, and this cannot
    tell. So it happens only when asked for.
    """
    resolved: list[str] = []
    for _ in range(AUTO_STEPS):
        state = read_state(git)
        command = _carry_on_command(state)
        if not isinstance(state, (Conflicted, Applying)) or not auto_resolve:
            return _report(state, git_said, tuple(resolved))
        if not state.unmerged or command is None:
            return _report(state, git_said, tuple(resolved))

        composed = {path: auto_resolve_file(git, path) for path in state.unmerged}
        if any(text is None for text in composed.values()):
            return _report(state, git_said, tuple(resolved))

        for path, text in composed.items():
            assert text is not None
            (git.repo / path).write_text(text)
            git.run("add", "--", path)
            resolved.append(path)
        result = git.run("-c", "core.editor=true", *RERERE, command, "--continue", check=False)
        git_said = _git_said(result)
    return _report(read_state(git), git_said, tuple(resolved))


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


def _report(
    state: RebaseState, git_said: str = "", auto_resolved: tuple[str, ...] = ()
) -> StatusReport:
    match state:
        case NotRebasing():
            return StatusReport(
                git_said=git_said,
                auto_resolved=auto_resolved,
                state="not_rebasing",
                head=_info(state.head),
                head_is_replaying_commit=False,
                can_amend=False,
                guidance="No rebase in progress.",
            )
        case Conflicted():
            return StatusReport(
                git_said=git_said,
                auto_resolved=auto_resolved,
                state="conflicted",
                operation="rebase",
                head=_info(state.head),
                head_is_replaying_commit=False,
                can_amend=False,
                guidance=(
                    f"Stopped part-way through applying {state.replaying.sha[:9]} "
                    f"({state.replaying.subject}). That commit does not exist yet, so "
                    "HEAD is still the one before it and amending would rewrite the "
                    "wrong commit. Resolve the conflicted paths, then continue."
                ),
                step=StepInfo(state.step.index, state.step.total),
                action=state.action,
                replaying=_info(state.replaying),
                conflicted_files=state.unmerged,
            )
        case Applying():
            return StatusReport(
                git_said=git_said,
                auto_resolved=auto_resolved,
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
            same = state.head.sha == state.replaying.sha
            return StatusReport(
                git_said=git_said,
                auto_resolved=auto_resolved,
                state="stopped_after_apply",
                operation="rebase",
                head=_info(state.head),
                head_is_replaying_commit=same,
                can_amend=True,
                guidance=(
                    f"Stopped at `{state.action}` with {state.replaying.sha[:9]} "
                    "applied. HEAD is that commit, so amending it is safe."
                    if same
                    else (
                        f"Stopped at `{state.action}`. Git will amend HEAD "
                        f"({state.head.sha[:9]}), which is a run of "
                        f"{len(state.fixups_pending)} fixup or squash steps so far, not "
                        f"{state.replaying.sha[:9]} on its own. Its message is still "
                        "git's template and is rewritten when the run ends, so ignore "
                        "the subject above."
                    )
                ),
                step=StepInfo(state.step.index, state.step.total),
                action=state.action,
                replaying=_info(state.replaying),
            )
        case StoppedWithoutApply():
            return StatusReport(
                git_said=git_said,
                auto_resolved=auto_resolved,
                state="stopped_without_apply",
                operation="rebase",
                head=_info(state.head),
                head_is_replaying_commit=False,
                can_amend=False,
                guidance=(
                    f"Stopped at `{state.action}`, which applied nothing. HEAD is "
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


@mcp.tool()
def rebase_finish(repo: str = ".", allow_change: bool = False) -> FinishReport:
    """Check the finished rebase against the tip it started from, and tidy up.

    What must stay the same is the change the branch makes to its base -- not
    the resulting tree, which legitimately changes when the rebase also moves
    onto newer upstream work. A difference here is a report of damage: a commit
    dropped from the todo, or a conflict resolved the wrong way.
    Every rewritten commit is also scanned for conflict markers, since one
    committed part-way and tidied up later still leaves a commit nobody can
    build.

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

    change = branch_change(git, session.backup, session.base_sha)
    difference = change.summary if change else None
    marker_hits = commits_with_markers(git, f"{session.base_sha}..HEAD")
    problems: list[str] = []
    if change and not change.reordered_only and not allow_change:
        problems.append(
            "the branch no longer makes the same change to its base, so "
            f"something was lost or resolved wrongly:\n{change.summary}"
        )
    if marker_hits:
        problems.append(
            "conflict markers were committed in "
            + ", ".join(f"{hit.sha[:9]} ({hit.subject})" for hit in marker_hits)
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
        commits_with_markers=tuple(hit.sha for hit in marker_hits),
        commits=tuple(
            _commit_info(git, sha)
            for sha in git.lines("rev-list", "--reverse", f"{session.base_sha}..HEAD")
        ),
        restored=restored,
        guidance=(
            "Rebase checks out"
            + (
                ", with the same lines in a different order than before -- a moved "
                "block rather than a loss. "
                if change and change.reordered_only
                else ". "
            )
            + "Anything moved aside was restored. The tip before the rebase is "
            f"still tagged {session.backup_ref}."
            if not problems
            else "Not finished: "
            + "; ".join(problems)
            + f". The branch before the rebase is at {session.backup_ref}; "
            "`git reset --hard` to it to undo."
        ),
    )


@dataclass(frozen=True)
class AbortReport:
    head: CommitInfo
    restored: tuple[str, ...]
    guidance: str


@mcp.tool()
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
        raise ValueError(_nothing_to_carry_on(state, "skip"))
    result = git.run("-c", "core.editor=true", *RERERE, command, "--skip", check=False)
    return _advance(git, _git_said(result), auto_resolve)


@mcp.tool()
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
