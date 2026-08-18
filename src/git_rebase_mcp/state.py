"""What the repository is currently doing, as a closed set of states.

The distinction this module exists for: when a rebase stops at an `edit` step it
normally leaves HEAD on the commit just applied, but when it stops *because that
step conflicted* HEAD is still the previous commit. Amending in the second case
silently folds two commits into one, and nothing in git's own output tells the
two apart.

So the states are separate types rather than fields on one object, and the
operations that are only safe in one of them accept only that type.

A rebase is not the only thing that leaves a conflicted index, and for a while
this module reported everything else as "no rebase in progress" -- which callers
above it turned into "nothing is conflicted", said of a repository with unmerged
paths sitting in the index. A false negative on the one question this server
exists to answer. So a conflict from a cherry-pick, a revert, a merge, or from
something that left no record of itself at all, is a state of its own rather
than an absence.

The other half of that first distinction took longer to notice, because it is
not about which commit HEAD is -- it is about whether there will be a second
stop at all. See `STOPPING_ACTIONS`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .git import Git

Operation = Literal["rebase", "cherry-pick", "revert", "merge", "unknown"]

# The ref git writes for the commit it was applying, per operation. A conflicted
# index does not say what produced it, and the answer decides which command
# carries on from here -- `git merge --continue` cannot finish a cherry-pick.
# `unknown` is not in here on purpose: a stash that popped into a conflict, or a
# `checkout -m`, leaves stages in the index and no record of where they came
# from, and the honest report of that is the regions themselves.
INCOMING_REFS: tuple[tuple[Operation, str], ...] = (
    ("cherry-pick", "CHERRY_PICK_HEAD"),
    ("revert", "REVERT_HEAD"),
    ("merge", "MERGE_HEAD"),
)

# Todo actions that hand the caller a stop once the commit has been applied.
#
# When one of these *conflicts* instead, the stop it promised is the conflict
# itself. `--continue` commits the resolution and carries straight on to the
# next step: there is no second stop at which to make the change the action was
# asked for. Measured, on git 2.x, in a repository built for the question -- a
# conflicted `edit` committed the resolution and went directly to the following
# step, and a conflicted `reword` finished the whole rebase still carrying the
# original message.
#
# Nothing in git's output says so, and the shape of the mistake is quiet: the
# caller resolves, continues, and the commit they meant to change goes past
# unchanged. Found by losing an entire replay of a 63-commit branch to it.
#
# A conflicted `pick` spends no stop, because a `pick` never promised one. That
# is why this is a set of actions rather than a property of conflicts.
STOPPING_ACTIONS = frozenset({"edit", "reword"})


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

    @property
    def action_stop_lost(self) -> bool:
        """Whether this conflict has spent the stop the action promised.

        True at a conflicted `edit` or `reword`: continuing commits and moves
        on, so any change to this commit has to be staged before continuing
        rather than after. See `STOPPING_ACTIONS`.
        """
        return self.action in STOPPING_ACTIONS


@dataclass(frozen=True)
class StoppedAfterApply:
    """Stopped with a commit in hand that `--amend` would rewrite.

    Git names it in `.git/rebase-merge/amend`, which is where this comes from.

    `applied` is the sha git recorded there: the commit this step created. It is
    not the commit being replayed whenever the rebase had to rewrite it -- which
    is most of the time, since a commit whose parent moved gets a new sha -- so
    comparing HEAD against `replaying` answers a different question than it
    looks like it answers, and answers it "no" on an ordinary stop.

    HEAD is not always the commit this step created either:

    - Part-way through a run of `fixup` or `squash` steps it is the accumulation
      of them so far, and its message is still the raw template git assembles
      and cleans up at the end of the run. `fixups_pending` says when that is
      the case, so nothing reports the template as though it were the subject.
    - `unapplied` is set when the commit has been taken back out with a mixed
      reset, leaving its changes in the working tree. That is how a commit is
      split, and git leaves its `amend` record in place through it, so nothing
      but this comparison distinguishes it from a stop where amending is safe.
    """

    step: Step
    action: str
    replaying: Commit
    head: Commit
    fixups_pending: tuple[str, ...] = ()
    applied: str = ""
    # HEAD is the commit this step created, or an amendment of it: the commit
    # `--amend` would rewrite is the one the caller means.
    holds_applied_commit: bool = True
    # HEAD is that commit's parent, so amending would rewrite the commit before
    # the one this step applied.
    unapplied: bool = False


@dataclass(frozen=True)
class Applying:
    """Something other than a rebase is part-way through applying a commit.

    Its own type rather than a flag on `Conflicted`, because what is safe here
    is different in kind: there is no todo and no step, nothing has been half
    created, and the command that carries on is named after the operation. What
    it shares with a rebase conflict is the only part that matters for reading
    one -- the three stages in the index, which is how git records every
    conflict, whatever made it.

    `unmerged` can be empty: a cherry-pick whose conflicts have all been staged
    is still in progress, and still needs its own `--continue` to commit. That
    was reported as "no rebase in progress" before this type existed, which left
    a caller who had done everything right with nowhere to go.

    `incoming` is what was being applied, and is None when nothing recorded it.
    """

    operation: Operation
    incoming: Commit | None
    head: Commit
    unmerged: tuple[str, ...]


@dataclass(frozen=True)
class StoppedWithoutApply:
    """Stopped at a `break`, or by a failing `exec`.

    No commit is part-way through, but HEAD is not a commit this step created
    either, so amending it would rewrite something the caller did not name.
    """

    step: Step
    action: str
    head: Commit


RebaseState = NotRebasing | Conflicted | Applying | StoppedAfterApply | StoppedWithoutApply

# The states a rebase specifically is in. A conflicted cherry-pick is a conflict
# but not a rebase, and the tools that read `.git/rebase-merge/` need the
# difference: without it they answer a question about a file that is not there.
REBASING = (Conflicted, StoppedAfterApply, StoppedWithoutApply)


def is_rebasing(state: RebaseState) -> bool:
    return isinstance(state, REBASING)


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
        return _outside_rebase(git)

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
        applied = _read(directory / "amend")
        return StoppedAfterApply(
            step=step,
            action=action,
            replaying=_commit(git, "REBASE_HEAD"),
            head=head,
            fixups_pending=tuple(_read(directory / "current-fixups").splitlines()),
            applied=applied,
            holds_applied_commit=_same_place(git, head.sha, applied),
            unapplied=bool(applied) and head.sha == _parent(git, applied),
        )
    return StoppedWithoutApply(step=step, action=action, head=head)


def _same_place(git: Git, head: str, applied: str) -> bool:
    """Whether HEAD sits where the commit this step created sat.

    Their parents, not their shas: an amend replaces the commit with a sibling,
    and a caller who amends twice means the same commit both times. Without a
    record to compare against -- an older git, or a file this server cannot read
    -- the answer is the ordinary case, which is what the stop is for.
    """
    if not applied:
        return True
    return _parent(git, head) == _parent(git, applied)


def _parent(git: Git, revision: str) -> str:
    """The first parent's sha, or "" for a root commit."""
    result = git.run("rev-parse", "--verify", "--quiet", f"{revision}^", check=False)
    return result.stdout.strip() if result.ok else ""


def _outside_rebase(git: Git) -> RebaseState:
    """What the repository is doing when no rebase is.

    The ref is asked about before the index, because an operation with every
    conflict already staged is still in progress and still needs finishing --
    whereas a conflicted index nothing claims is the last thing to check, and
    the one that must not come back as a clean tree.
    """
    unmerged = tuple(git.lines("diff", "--name-only", "--diff-filter=U"))
    for operation, ref in INCOMING_REFS:
        if git.succeeds("rev-parse", "--verify", "--quiet", ref):
            return Applying(
                operation=operation,
                incoming=_commit(git, ref),
                head=_commit(git, "HEAD"),
                unmerged=unmerged,
            )
    if unmerged:
        return Applying(
            operation="unknown", incoming=None, head=_commit(git, "HEAD"), unmerged=unmerged
        )
    return NotRebasing(head=_commit(git, "HEAD"))


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
