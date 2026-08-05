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
from dataclasses import dataclass

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
            "is already there. The branch has probably been merged already, or the "
            "base is wrong."
        ]
        if duplicated
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
            problems=tuple(upstream_problem + stray_problem),
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

    accounted = set(kept) | set(dropped_on_purpose)
    missing = tuple(commit for sha, commit in by_sha.items() if sha not in accounted)

    problems: list[str] = list(upstream_problem) + list(stray_problem)
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
        problems=tuple(problems),
    )


def _resolve(git: Git, revision: str) -> str | None:
    result = git.run("rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}", check=False)
    return result.stdout.strip() or None
