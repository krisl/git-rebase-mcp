"""The MCP tools.

Thin wiring over the modules that do the work. The one thing it adds is that
every reply says what is safe to do next, at the point where a caller would
otherwise have to infer it -- which is where the mistakes happen.

Internally the rebase state is a closed union so that unsafe operations can be
made unrepresentable. On the wire it is flattened into one shape with a
discriminator, because a stable schema is easier for a caller to rely on.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, assert_never

from mcp.server.mcpserver import MCPServer

from .conflicts import FileConflict, read_conflict
from .git import Git
from .invariants import (
    Session,
    clear_session,
    commits_with_markers,
    has_markers,
    load_session,
    record_backup,
    save_session,
    tree_change,
)
from .plan import TODO_LINE, check_plan
from .state import (
    Commit,
    Conflicted,
    NotRebasing,
    RebaseState,
    StoppedAfterApply,
    StoppedWithoutApply,
    read_state,
)

mcp = MCPServer(
    "git-rebase",
    instructions=(
        "Drives an interactive git rebase safely. Call rebase_status before "
        "acting: it reports whether the commit being replayed has actually been "
        "created yet, which decides whether amending would rewrite the commit "
        "you mean or the one before it."
    ),
)

StateName = Literal[
    "not_rebasing", "conflicted", "stopped_after_apply", "stopped_without_apply"
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
    step: StepInfo | None = None
    action: str | None = None
    replaying: CommitInfo | None = None
    conflicted_files: tuple[str, ...] = ()


@mcp.tool()
def rebase_status(repo: str = ".") -> StatusReport:
    """Report what the rebase in `repo` is currently doing.

    Call this before amending, continuing or resolving. In particular
    `head_is_replaying_commit` distinguishes a stop where the commit was applied
    from one where it conflicted part-way, which git's own output does not.
    """
    return _report(read_state(_git(repo)))


@dataclass(frozen=True)
class UnitReport:
    """One region both sides edited, as what each side did to the base."""

    base_range: tuple[int, int]
    branch_so_far_diff: str
    replaying_diff: str


@dataclass(frozen=True)
class FileReport:
    path: str
    units: tuple[UnitReport, ...]
    # True when the sides share no ancestor for this path, so there are no units
    # and the whole texts below are the answer. They are always included in that
    # case, regardless of include_full_sides, because nothing else is on offer.
    no_common_base: bool = False
    base: str | None = None
    branch_so_far: str | None = None
    replaying: str | None = None


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
def rebase_conflicts(repo: str = ".", include_full_sides: bool = False) -> ConflictReport:
    """Report each conflict as what the two sides did, rather than as markers.

    Per contested region you get two diffs from the common base: one for the
    branch built so far, one for the commit being replayed. Read them as two
    intents and compose them -- "wrap this block in an `if`" plus "swap this
    call" is usually just both.

    Regions only one side changed are not listed: git merged those already.
    Set `include_full_sides` to also get the three whole texts.
    """
    git = _git(repo)
    state = read_state(git)
    if not isinstance(state, Conflicted):
        return ConflictReport(
            replaying=None,
            replaying_body="",
            files=(),
            guidance="Nothing is conflicted.",
        )
    files = tuple(
        _file_report(read_conflict(git, path), include_full_sides) for path in state.unmerged
    )
    return ConflictReport(
        replaying=_info(state.replaying),
        replaying_body=git.out("log", "-1", "--format=%B", state.replaying.sha).strip(),
        files=files,
        guidance=_conflict_guidance(files),
    )


def _conflict_guidance(files: tuple[FileReport, ...]) -> str:
    advice = (
        "Each region lists what the branch did to the base and what the replayed "
        "commit did to the same base. The replayed commit's message states its "
        "intent; reapply that intent on top of what the branch already has. Then "
        "call rebase_resolve with the finished file."
    )
    rootless = [f.path for f in files if f.no_common_base]
    if rootless:
        advice += (
            f" No common base for {', '.join(rootless)}: both sides introduced the "
            "file independently, so there is nothing to diff against and no "
            "regions are listed. The whole text of each side is included instead; "
            "decide between them, or write the combination you want."
        )
    return advice


def _file_report(conflict: FileConflict, include_full_sides: bool) -> FileReport:
    return FileReport(
        path=conflict.path,
        units=tuple(
            UnitReport(
                base_range=unit.base_range,
                branch_so_far_diff=unit.branch_so_far_diff,
                replaying_diff=unit.replaying_diff,
            )
            for unit in conflict.units
        ),
        no_common_base=conflict.no_common_base,
        # With no common base there are no units, so withholding the texts would
        # leave the caller nothing at all.
        base=conflict.sides.base if include_full_sides else None,
        branch_so_far=conflict.sides.branch_so_far
        if include_full_sides or conflict.no_common_base
        else None,
        replaying=conflict.sides.replaying
        if include_full_sides or conflict.no_common_base
        else None,
    )


@mcp.tool()
def rebase_resolve(path: str, content: str, repo: str = ".") -> ResolveReport:
    """Write the resolved content for one conflicted path and stage it.

    Refuses content that still contains conflict markers. Staging one is how a
    commit ends up with `<<<<<<<` in it, which nothing downstream catches.
    """
    git = _git(repo)
    if has_markers(content):
        raise ValueError(
            f"{path} still contains conflict markers. Resolve them first: staging "
            "this would commit them."
        )
    target = git.repo / path
    if not target.parent.is_dir():
        raise ValueError(f"{path} is not inside {git.repo}")
    target.write_text(content)
    git.run("add", "--", path)

    remaining = tuple(git.lines("diff", "--name-only", "--diff-filter=U"))
    return ResolveReport(
        path=path,
        still_conflicted=remaining,
        guidance=(
            "All paths resolved; call rebase_continue."
            if not remaining
            else f"Still conflicted: {', '.join(remaining)}."
        ),
    )


@dataclass(frozen=True)
class PreflightReport:
    base: str
    commits: tuple[CommitInfo, ...]
    dropped: tuple[CommitInfo, ...]
    unknown: tuple[str, ...]
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
    in the range, anything already in progress or uncommitted, and untracked
    files a replayed commit would collide with.
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
    if not isinstance(read_state(git), NotRebasing):
        problems.append("a rebase is already in progress")
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
    check_command: str | None = None,
    force: bool = False,
) -> StartReport:
    """Begin a rebase onto `base`, and report where it stops.

    Refuses anything rebase_preflight called unsafe, unless `force`. Before
    starting it tags the current tip, so the result can be checked against it,
    and moves aside untracked files a replayed commit would collide with.

    `check_command` is run after every commit, which is the only thing that
    catches a step that applies cleanly but leaves the tree broken.
    """
    git = _git(repo)
    preflight = rebase_preflight(base, repo, todo)
    if not preflight.safe_to_start and not force:
        raise ValueError(f"Refusing to start. {preflight.guidance}")

    stashed = _stash(git, preflight.untracked_collisions)
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
            check_command=check_command,
        ),
    )

    args = ["rebase", "-i"]
    config: list[str] = ["-c", "core.editor=true"]
    if todo is not None:
        # A supplied todo replaces whatever git generates, so --exec would be
        # discarded with it; the exec lines have to be woven in here instead.
        lines = _with_checks(todo, check_command)
        todo_file = git.git_path("rebase-mcp-todo")
        todo_file.write_text("\n".join(lines) + "\n")
        config += ["-c", f"sequence.editor=cp '{todo_file}'"]
    elif check_command:
        args += ["--exec", check_command]
    args.append(base)

    # Stopping at a conflict is an ordinary outcome that git reports as failure,
    # so the result is read from the rebase state instead.
    git.run(*config, *args, check=False)
    status = _report(read_state(git))
    return StartReport(
        backup_ref=backup.ref,
        stashed=stashed,
        status=status,
        guidance=(
            f"Started. The tip beforehand is tagged {backup.ref}; rebase_finish "
            "checks the result against it. " + status.guidance
        ),
    )


def _with_checks(todo: list[str], check_command: str | None) -> list[str]:
    if not check_command:
        return todo
    woven: list[str] = []
    for line in todo:
        woven.append(line)
        if TODO_LINE.match(line):
            woven.append(f"exec {check_command}")
    return woven


def _stash(git: Git, paths: tuple[str, ...]) -> tuple[str, ...]:
    """Move untracked files out of the way, remembering them for later.

    Only the ones that would actually collide: stashing anything else would be
    taking away work the caller did not ask us to touch.
    """
    if not paths:
        return ()
    git.run("stash", "push", "--include-untracked", "--quiet", "--", *paths)
    return paths


@dataclass(frozen=True)
class AmendReport:
    before: CommitInfo
    after: CommitInfo
    guidance: str


@mcp.tool()
def rebase_amend(
    repo: str = ".", message: str | None = None, stage_all: bool = False
) -> AmendReport:
    """Amend the commit this rebase has just applied.

    Refused at every other kind of stop. At a conflicted stop the commit being
    replayed has not been created yet, so HEAD is still the one before it and
    amending would fold two commits into one -- silently, and reported by git as
    success.
    """
    git = _git(repo)
    state = read_state(git)
    if not isinstance(state, StoppedAfterApply):
        raise ValueError(_why_not_amendable(_report(state)))

    before = _info(state.head)
    if stage_all:
        git.run("add", "-A")
    args = ["commit", "--amend", "--no-verify"]
    args += ["-m", message] if message is not None else ["--no-edit"]
    git.run("-c", "core.editor=true", *args)
    after = _commit_info(git, "HEAD")
    return AmendReport(
        before=before,
        after=after,
        guidance=f"Amended {before.sha[:9]} into {after.sha[:9]}. Call rebase_continue.",
    )


def _why_not_amendable(report: StatusReport) -> str:
    """Say what is wrong and what to do instead, not just that it was refused."""
    return (
        f"Refusing to amend: the rebase is {report.state}, and HEAD "
        f"({report.head.sha[:9]} {report.head.subject!r}) is not a commit this "
        f"step created. {report.guidance}"
    )


@mcp.tool()
def rebase_continue(repo: str = ".") -> StatusReport:
    """Carry on with the rebase, and report where it stops next.

    Refused while any path is still unmerged, which is the other way a marker
    reaches a commit.
    """
    git = _git(repo)
    state = read_state(git)
    if isinstance(state, Conflicted):
        raise ValueError(
            "Refusing to continue: still unmerged: "
            f"{', '.join(state.unmerged)}. Resolve them with rebase_resolve first."
        )
    if isinstance(state, NotRebasing):
        raise ValueError("No rebase in progress.")
    # Stopping again on the next conflict is an ordinary outcome, not a failure,
    # so the exit status is read from the state rather than from git.
    git.run("-c", "core.editor=true", "rebase", "--continue", check=False)
    return _report(read_state(git))


def _commit_info(git: Git, revision: str) -> CommitInfo:
    sha, _, subject = git.out("log", "-1", "--format=%H%n%s", revision).partition("\n")
    return CommitInfo(sha=sha, subject=subject)


def _git(repo: str) -> Git:
    path = Path(repo).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    return Git(path)


def _info(commit: Commit) -> CommitInfo:
    return CommitInfo(sha=commit.sha, subject=commit.subject)


def _report(state: RebaseState) -> StatusReport:
    match state:
        case NotRebasing():
            return StatusReport(
                state="not_rebasing",
                head=_info(state.head),
                head_is_replaying_commit=False,
                can_amend=False,
                guidance="No rebase in progress.",
            )
        case Conflicted():
            return StatusReport(
                state="conflicted",
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
        case StoppedAfterApply():
            same = state.head.sha == state.replaying.sha
            return StatusReport(
                state="stopped_after_apply",
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
                state="stopped_without_apply",
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
    tree_change: str | None
    commits_with_markers: tuple[str, ...]
    commits: tuple[CommitInfo, ...]
    restored: tuple[str, ...]
    guidance: str


@mcp.tool()
def rebase_finish(repo: str = ".", allow_tree_change: bool = False) -> FinishReport:
    """Check the finished rebase against the tip it started from, and tidy up.

    Reordering commits must not change the end result, so a difference here is
    a report of damage: a commit dropped from the todo, or two folded together.
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

    difference = tree_change(git, session.backup)
    marker_hits = commits_with_markers(git, f"{session.base_sha}..HEAD")
    problems: list[str] = []
    if difference and not allow_tree_change:
        problems.append(
            "the tree at the tip is not what it was before the rebase, so "
            f"something was lost or merged:\n{difference}"
        )
    if marker_hits:
        problems.append(
            "conflict markers were committed in "
            + ", ".join(f"{hit.sha[:9]} ({hit.subject})" for hit in marker_hits)
        )

    restored = _unstash(git, session.stashed) if not problems else ()
    if not problems:
        clear_session(git)

    return FinishReport(
        ok=not problems,
        backup_ref=session.backup_ref,
        tree_change=difference,
        commits_with_markers=tuple(hit.sha for hit in marker_hits),
        commits=tuple(
            _commit_info(git, sha)
            for sha in git.lines("rev-list", "--reverse", f"{session.base_sha}..HEAD")
        ),
        restored=restored,
        guidance=(
            f"Rebase checks out. Anything moved aside was restored. The tip before "
            f"the rebase is still tagged {session.backup_ref}."
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
def rebase_abort(repo: str = ".") -> AbortReport:
    """Abandon the rebase and put back anything that was moved aside."""
    git = _git(repo)
    if not isinstance(read_state(git), NotRebasing):
        git.run("rebase", "--abort", check=False)
    session = load_session(git)
    restored = _unstash(git, session.stashed) if session else ()
    if session:
        clear_session(git)
    return AbortReport(
        head=_commit_info(git, "HEAD"),
        restored=restored,
        guidance="Rebase abandoned."
        + (f" Restored: {', '.join(restored)}." if restored else ""),
    )


def _unstash(git: Git, stashed: tuple[str, ...]) -> tuple[str, ...]:
    """Put back what _stash moved, if it is still there to put back."""
    if not stashed:
        return ()
    return stashed if git.run("stash", "pop", check=False).ok else ()


def main() -> None:
    mcp.run()
