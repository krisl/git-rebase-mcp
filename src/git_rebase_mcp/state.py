"""What a rebase is currently doing, as a closed set of states.

The distinction this module exists for: when a rebase stops at an `edit` step it
normally leaves HEAD on the commit just applied, but when it stops *because that
step conflicted* HEAD is still the previous commit. Amending in the second case
silently folds two commits into one, and nothing in git's own output tells the
two apart.

So the states are separate types rather than fields on one object, and the
operations that are only safe in one of them accept only that type.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .git import Git

# Todo actions that apply a commit, and so leave HEAD on it once they succeed.
# Anything else that stops (`break`, a failing `exec`) leaves HEAD wherever the
# previous step left it, which is not a commit this rebase created.
APPLYING_ACTIONS = frozenset(
    {"p", "pick", "r", "reword", "e", "edit", "s", "squash", "f", "fixup", "m", "merge"}
)


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str


@dataclass(frozen=True)
class Step:
    index: int
    total: int


@dataclass(frozen=True)
class NotRebasing:
    """No rebase in progress."""

    head: Commit


@dataclass(frozen=True)
class Conflicted:
    """Stopped part-way through applying a commit.

    HEAD is still the previous commit: the one being replayed has not been
    created yet. Amending here would rewrite the wrong commit.
    """

    step: Step
    action: str
    replaying: Commit
    head: Commit
    unmerged: tuple[str, ...]


@dataclass(frozen=True)
class StoppedAfterApply:
    """Stopped at an `edit` or `reword` after the commit was applied.

    HEAD is that commit, so amending it is what the caller means.
    """

    step: Step
    action: str
    replaying: Commit
    head: Commit


@dataclass(frozen=True)
class StoppedWithoutApply:
    """Stopped at a `break`, or by a failing `exec`.

    No commit is part-way through, but HEAD is not a commit this step created
    either, so amending it would rewrite something the caller did not name.
    """

    step: Step
    action: str
    head: Commit


RebaseState = NotRebasing | Conflicted | StoppedAfterApply | StoppedWithoutApply


class UnsupportedRebase(Exception):
    """An `am`-based rebase, which this server does not drive."""


def read_state(git: Git) -> RebaseState:
    directory = _git_path(git, "rebase-merge")
    if not directory.is_dir():
        if _git_path(git, "rebase-apply").is_dir():
            raise UnsupportedRebase(
                "this is an am-based rebase (git rebase --apply); "
                "only interactive/merge rebases are supported"
            )
        return NotRebasing(head=_commit(git, "HEAD"))

    step = Step(index=_number(directory / "msgnum"), total=_number(directory / "end"))
    action = _last_action(directory / "done")
    head = _commit(git, "HEAD")
    unmerged = tuple(git.lines("diff", "--name-only", "--diff-filter=U"))

    if unmerged:
        return Conflicted(
            step=step,
            action=action,
            replaying=_commit(git, "REBASE_HEAD"),
            head=head,
            unmerged=unmerged,
        )
    if action in APPLYING_ACTIONS:
        return StoppedAfterApply(
            step=step, action=action, replaying=_commit(git, "REBASE_HEAD"), head=head
        )
    return StoppedWithoutApply(step=step, action=action, head=head)


def _git_path(git: Git, name: str) -> Path:
    """Resolve a path inside the git directory.

    Asking git rather than assuming `.git/` keeps this working in worktrees and
    submodules, where the git directory is somewhere else entirely.
    """
    path = Path(git.out("rev-parse", "--git-path", name))
    return path if path.is_absolute() else git.repo / path


def _commit(git: Git, revision: str) -> Commit:
    sha, _, subject = git.out("log", "-1", "--format=%H%n%s", revision).partition("\n")
    return Commit(sha=sha, subject=subject)


def _number(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def _last_action(done: Path) -> str:
    """The verb of the most recent todo line git carried out."""
    try:
        entries = [line for line in done.read_text().splitlines() if line.strip()]
    except OSError:
        return ""
    return entries[-1].split(maxsplit=1)[0] if entries else ""
