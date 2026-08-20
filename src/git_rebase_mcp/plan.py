"""Checking a rebase todo before anything is rewritten.

A todo list is written by hand or generated, and git accepts it either way. A
commit simply left out of it is dropped: no warning, no error, and the only sign
is that the work is gone. Git can be told to object, with
`rebase.missingCommitsCheck`, but it is off by default and has to be set before
the rebase starts, which is exactly when nobody remembers.

So the todo is compared against the commits in the range before it is used.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .git import Git
from .state import Commit

# A todo line naming a commit: the action, then an abbreviated or full sha.
TODO_LINE = re.compile(r"^\s*(?P<action>[a-z-]+)\s+(?P<sha>[0-9a-f]{4,40})\b", re.IGNORECASE)

# Actions that leave the commit out of the result on purpose, so naming one is
# not the same as keeping it.
DROPPING_ACTIONS = frozenset({"d", "drop"})


@dataclass(frozen=True)
class PlanCheck:
    """What a todo would do to a range, before it does it."""

    commits: tuple[Commit, ...]
    named: tuple[Commit, ...]
    dropped: tuple[Commit, ...]
    deliberately_dropped: tuple[Commit, ...]
    unknown: tuple[str, ...]
    already_upstream: tuple[Commit, ...]
    stray_fixups: tuple[Commit, ...]
    problems: tuple[str, ...]
    # Pairs the todo replays in the opposite order to the history: (moved, was_before).
    # Not a problem -- reordering is what a todo is for -- but it is the moment a
    # commit stops sitting on something it used to sit on, and neither git nor a
    # conflict says so, because moving code that still merges cleanly is silent.
    reordered: tuple[tuple[Commit, Commit], ...] = ()

    @property
    def safe(self) -> bool:
        return not self.problems


def already_upstream(git: Git, base: str) -> frozenset[str]:
    """Commits in the range whose change is already in the base.

    A merged branch keeps its old commits: the merge brought in rewritten copies
    with different shas, so `base..HEAD` still lists every one of them and a
    rebase dutifully replays work that is already there. Every file then
    conflicts with itself, which reads as a catastrophe and is merely pointless.

    `--cherry-mark` answers this by patch-id rather than by sha, which is the
    only thing that can see through the rewrite.
    """
    marked = git.lines("rev-list", "--cherry-mark", "--right-only", f"{base}...HEAD")
    return frozenset(line[1:] for line in marked if line.startswith("="))


Relation = Literal["same", "ancestor", "descendant", "diverged"]


def base_relationship(git: Git, base: str) -> Relation:
    """Where `base` sits relative to HEAD.

    Asked because "the branch has probably been merged already, or the base is
    wrong" names two situations and leaves the caller to work out which, when
    git answers it directly.

    The one that costs most to guess at is `diverged`: a base that is neither
    ancestor nor descendant, carrying the same changes under other shas. That
    is what the tip before a rebase looks like, and this server's own backup
    tag is exactly that -- a copy of the branch rather than something to put
    the branch on.
    """
    head = git.out("rev-parse", "HEAD")
    there = git.out("rev-parse", f"{base}^{{commit}}")
    if there == head:
        return "same"
    if git.succeeds("merge-base", "--is-ancestor", there, head):
        return "ancestor"
    if git.succeeds("merge-base", "--is-ancestor", head, there):
        return "descendant"
    return "diverged"


def _why_duplicated(git: Git, base: str) -> str:
    """The relationship the duplicates come of, as a fact before a reading.

    "The branch has probably been merged already, or the base is wrong" gives
    two readings and no way to choose between them. Which of the two it is
    cannot be answered from the history -- an upstream that merged this branch
    and a backup tag of this branch are the same shape: diverged, same changes,
    other shas -- so both are still offered. What can be answered is the
    relationship, and that is the half that settles it: a base that is not an
    ancestor of the branch is not a base, whichever reading applies.

    Only a diverged base can get here. A duplicate needs a commit on the base
    side to match, which rules out an ancestor; and it needs a commit in
    `base..HEAD` to be marked, which rules out the base being HEAD or ahead of
    it. The other answer is kept for the caller who reaches it anyway, since a
    problem with no sentence after it is worse than a vague one.
    """
    if base_relationship(git, base) == "diverged":
        return (
            f"{base} is not an ancestor of this branch: the two have diverged, and "
            "it makes these same changes under other shas. The branch has probably "
            f"been merged already -- or {base} is a copy of the branch, which is "
            "what the tip before a rebase, or a backup tag, is, and not something "
            "to rebase onto."
        )
    return "The branch has probably been merged already, or the base is wrong."


def _nothing_between(git: Git, base: str) -> str:
    """Why the range is empty, which decides whether that is a problem at all.

    An empty range used to pass preflight as "Nothing found; safe to start",
    which is true of the checks and false of the question asked. A rebase with
    nothing to replay is not a safe rebase, it is a mistaken base.
    """
    relation = base_relationship(git, base)
    if relation == "same":
        return f"{base} is this branch's own tip."
    if relation == "descendant":
        return (
            f"{base} is ahead of this branch and already contains its commits, so "
            "there is nothing of the branch's own to put on top."
        )
    # An ancestor with an empty range is HEAD under another name, and a diverged
    # base always leaves something in the range. Neither reaches here.
    return f"{base} may not be the base that was meant."


def commits_in_range(git: Git, base: str) -> tuple[Commit, ...]:
    """The commits a rebase onto `base` would replay, oldest first."""
    return tuple(
        Commit(sha=sha, subject=subject)
        for sha, _, subject in (
            line.partition(" ")
            for line in git.lines("log", "--reverse", "--format=%H %s", f"{base}..HEAD")
        )
    )


FIXUP_SUBJECT = re.compile(r"^(fixup|squash)!\s+(?P<subject>.+)$")


def autosquash_targets(commits: tuple[Commit, ...]) -> dict[str, Commit | None]:
    """Which commit each `fixup!` or `squash!` in the range would fold into.

    A value of None means git will leave it where it is: the subject matches
    nothing in the range, usually because the target is already upstream or the
    subject was edited after the fixup was written. Git says nothing about that,
    and the commit then survives as a stray `fixup!` in the final history.
    """
    by_subject = {commit.subject: commit for commit in commits}
    targets: dict[str, Commit | None] = {}
    for commit in commits:
        found = FIXUP_SUBJECT.match(commit.subject)
        if found:
            targets[commit.sha] = by_subject.get(found["subject"])
    return targets


def check_plan(git: Git, base: str, todo: list[str] | None) -> PlanCheck:
    """Compare a todo against the commits it claims to cover.

    With no todo the caller means "keep them all, in order", which cannot drop
    anything, so only the range is reported.
    """
    commits = commits_in_range(git, base)
    stray = tuple(
        commit
        for commit in commits
        if commit.sha in autosquash_targets(commits) and autosquash_targets(commits)[commit.sha] is None
    )
    upstream = already_upstream(git, base)
    duplicated = tuple(commit for commit in commits if commit.sha in upstream)
    stray_problem = (
        [
            "these name a commit that is not in the range, so autosquash leaves them "
            "where they are and they survive into the final history: "
            + ", ".join(f"{c.sha[:9]} ({c.subject})" for c in stray)
        ]
        if stray
        else []
    )
    upstream_problem = (
        [
            f"{len(duplicated)} of {len(commits)} commits in the range are already in "
            f"{base} under different shas, so replaying them conflicts with work that "
            "is already there. "
            + _why_duplicated(git, base)
        ]
        if duplicated
        else []
    )
    empty_problem = (
        [
            f"there are no commits between {base} and HEAD, so a rebase onto it "
            "would replay nothing. " + _nothing_between(git, base)
        ]
        if not commits
        else []
    )

    if todo is None:
        return PlanCheck(
            commits=commits,
            named=commits,
            dropped=(),
            deliberately_dropped=(),
            unknown=(),
            already_upstream=duplicated,
            stray_fixups=stray,
            problems=tuple(empty_problem + upstream_problem + stray_problem),
        )

    by_sha = {commit.sha: commit for commit in commits}
    kept: dict[str, Commit] = {}
    dropped_on_purpose: dict[str, Commit] = {}
    unknown: list[str] = []

    for line in todo:
        match = TODO_LINE.match(line)
        if not match:
            continue
        resolved = _resolve(git, match["sha"]) or ""
        commit = by_sha.get(resolved)
        if commit is None:
            unknown.append(match["sha"])
        elif match["action"].lower() in DROPPING_ACTIONS:
            dropped_on_purpose[commit.sha] = commit
        else:
            kept[commit.sha] = commit

    order = {commit.sha: index for index, commit in enumerate(commits)}
    replayed = [sha for sha in kept if sha in order]
    reordered = tuple(
        (by_sha[later], by_sha[earlier])
        for position, later in enumerate(replayed)
        for earlier in replayed[position + 1:]
        if order[later] > order[earlier]
    )

    accounted = set(kept) | set(dropped_on_purpose)
    missing = tuple(commit for sha, commit in by_sha.items() if sha not in accounted)

    problems: list[str] = list(empty_problem) + list(upstream_problem) + list(stray_problem)
    if missing:
        problems.append(
            "the todo leaves out "
            + ", ".join(f"{c.sha[:9]} ({c.subject})" for c in missing)
            + " -- these would be dropped without a warning"
        )
    if unknown:
        problems.append("the todo names commits that are not in the range: " + ", ".join(unknown))

    return PlanCheck(
        commits=commits,
        named=tuple(kept.values()),
        dropped=missing,
        deliberately_dropped=tuple(dropped_on_purpose.values()),
        unknown=tuple(unknown),
        already_upstream=duplicated,
        stray_fixups=stray,
        reordered=reordered,
        problems=tuple(problems),
    )


def _resolve(git: Git, revision: str) -> str | None:
    result = git.run("rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}", check=False)
    return result.stdout.strip() or None


def todo_stopping_at(
    git: Git,
    base: str,
    stop_at: Sequence[str] | None = None,
    action: str = "edit",
    every: bool = False,
    break_first: bool = False,
) -> list[str]:
    """A todo that replays the whole range and stops where it is told to.

    `every` stops at all of them, which is what "rebase and check each commit"
    means and what naming them one by one was a long way of saying. `break_first`
    puts a `break` in front, so there is a stop with nothing of the branch
    applied yet -- where a baseline is measured, and the only place it can be.

    Both were missing on the first outing of this function: a seven-commit branch
    to be tested at every step still had its todo written out by hand, which is
    the thing this exists to avoid.

    The overwhelmingly common shape of a driven rebase, and the one that was
    most expensive to ask for. Marking 21 of 63 commits meant sending all 63
    lines, which is 12KB of JSON that has to be generated somewhere else and
    pasted in -- three times over on the rebase that prompted this, because
    every change of mind about which commits to stop at meant sending the whole
    list again.

    Building it here instead cannot drop a commit, which is the other half of
    the argument: a hand-written list of 63 lines is exactly where one goes
    missing silently, and `check_plan` exists because that is what happens.
    Every commit in the range is named by construction, so the only thing left
    to get wrong is which ones stop.

    A revision that is not in the range raises rather than being ignored: it is
    a typo or a stale sha, and honouring the rest of the request would give a
    rebase that runs to the end without ever stopping where it was asked to.
    """
    commits = commits_in_range(git, base)
    if every and stop_at:
        raise ValueError(
            "every stops at all of them, so naming some as well says two "
            "different things. Pass one."
        )
    if every:
        return (["break"] if break_first else []) + [
            f"{action} {commit.sha} {commit.subject}" for commit in commits
        ]
    wanted: dict[str, str] = {}
    for revision in stop_at or ():
        resolved = _resolve(git, revision)
        if resolved is None or all(commit.sha != resolved for commit in commits):
            raise ValueError(
                f"{revision} is not a commit in {base}..HEAD, so a todo built from "
                "it would replay the range without stopping there."
            )
        wanted[resolved] = revision
    return (["break"] if break_first else []) + [
        f"{action if commit.sha in wanted else 'pick'} {commit.sha} {commit.subject}"
        for commit in commits
    ]
