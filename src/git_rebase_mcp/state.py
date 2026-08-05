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
    """Stopped with a commit in hand that `--amend` would rewrite.

    Git names it in `.git/rebase-merge/amend`, which is where this comes from.

    HEAD is not always the commit being replayed. Part-way through a run of
    `fixup` or `squash` steps it is the accumulation of them so far, and its
    message is still the raw template git assembles and cleans up at the end of
    the run. `fixups_pending` says when that is the case, so nothing reports the
    template as though it were the commit's subject.
    """

    step: Step
    action: str
    replaying: Commit
    head: Commit
    fixups_pending: tuple[str, ...] = ()


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
    directory = git.git_path("rebase-merge")
    if not directory.is_dir():
        if git.git_path("rebase-apply").is_dir():
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
    # Git creates `amend` exactly at the stops where amending is meant, and
    # leaves it absent at a `break`, a failing `exec` and a conflict. Its
    # presence is the signal; its contents are the pre-amend sha, which git uses
    # to notice that you amended, so it stops matching HEAD after the first one.
    #
    # Reading this beats inferring from the todo action, which cannot tell a
    # `fixup` that stopped holding a commit from one that stopped without, and
    # got exactly that wrong on a real branch.
    if (directory / "amend").exists():
        return StoppedAfterApply(
            step=step,
            action=action,
            replaying=_commit(git, "REBASE_HEAD"),
            head=head,
            fixups_pending=tuple(_read(directory / "current-fixups").splitlines()),
        )
    return StoppedWithoutApply(step=step, action=action, head=head)


def _commit(git: Git, revision: str) -> Commit:
    sha, _, subject = git.out("log", "-1", "--format=%H%n%s", revision).partition("\n")
    return Commit(sha=sha, subject=subject)


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


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
