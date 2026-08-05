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
from .invariants import has_markers
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
        guidance=(
            "Each region lists what the branch did to the base and what the "
            "replayed commit did to the same base. The replayed commit's message "
            "states its intent; reapply that intent on top of what the branch "
            "already has. Then call rebase_resolve with the finished file."
        ),
    )


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
        base=conflict.sides.base if include_full_sides else None,
        branch_so_far=conflict.sides.branch_so_far if include_full_sides else None,
        replaying=conflict.sides.replaying if include_full_sides else None,
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
            return StatusReport(
                state="stopped_after_apply",
                head=_info(state.head),
                head_is_replaying_commit=True,
                can_amend=True,
                guidance=(
                    f"Stopped at `{state.action}` with {state.replaying.sha[:9]} "
                    "applied. HEAD is that commit, so amending it is safe."
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


def main() -> None:
    mcp.run()
