"""Checks that catch a rebase going wrong when git itself reports nothing.

Rewriting history can lose commits, fold two into one, or commit a file that
still has conflict markers in it, and in every case git exits zero and says
nothing. The only reliable defence is to record what the branch looked like
before starting and to compare afterwards.

That is the whole idea: a rebase reorders commits, so the *tree* at the tip
should come out unchanged. When it does not, something was lost or merged that
should not have been.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .git import Git

# What git writes at the start of a conflict block. The label after the space is
# required on purpose: `=======` alone is a markdown heading underline, and
# `<<<<<<<` on its own turns up in prose about conflicts -- including in this
# project's own documentation.
MARKER_PREFIXES = ("<<<<<<< ", "||||||| ", ">>>>>>> ")

# Escaped, because `|` is alternation in extended regular expressions: the
# ancestor marker written literally reads as "empty or empty or ...", which
# matches every line of every file.
MARKER_REGEXES = tuple("^" + re.escape(prefix) for prefix in MARKER_PREFIXES)


@dataclass(frozen=True)
class Backup:
    """Where the branch was before the rewrite, and what it contained."""

    ref: str
    sha: str
    tree: str


@dataclass(frozen=True)
class MarkerHit:
    sha: str
    subject: str
    paths: tuple[str, ...]


def record_backup(git: Git, label: str | None = None) -> Backup:
    """Tag the current tip so the rewrite can be checked against it.

    A tag rather than a note in memory: it survives this process dying, and it
    is what someone recovers by hand if everything else fails.
    """
    stamp = label or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    ref = f"rebase-backup/{stamp}"
    sha = git.out("rev-parse", "HEAD")
    git.run("tag", "-f", ref, sha)
    return Backup(ref=ref, sha=sha, tree=git.out("rev-parse", f"{sha}^{{tree}}"))


def tree_change(git: Git, backup: Backup, revision: str = "HEAD") -> str | None:
    """A summary of how the tip's content differs from the backup, or None.

    Reordering commits must not change the end result, so anything here is a
    report of damage: a commit dropped from the todo, or two folded together by
    amending at the wrong moment.
    """
    if git.out("rev-parse", f"{revision}^{{tree}}") == backup.tree:
        return None
    return git.out("diff", "--stat", backup.sha, revision)


def commits_with_markers(git: Git, revision_range: str) -> list[MarkerHit]:
    """Commits in the range whose tree still contains conflict markers.

    Scanning every commit rather than only the tip, because a marker committed
    part-way through and tidied up later still leaves a commit nobody can build.
    """
    hits: list[MarkerHit] = []
    for sha in git.lines("rev-list", revision_range):
        patterns: list[str] = []
        for regex in MARKER_REGEXES:
            patterns += ["-e", regex]
        found = git.run("grep", "-l", "-E", *patterns, sha, check=False)
        if not found.ok:
            continue  # grep exits non-zero when it matches nothing
        paths = tuple(
            line.split(":", 1)[1] for line in found.stdout.splitlines() if ":" in line
        )
        if paths:
            hits.append(
                MarkerHit(sha=sha, subject=git.out("log", "-1", "--format=%s", sha), paths=paths)
            )
    return hits


def has_markers(text: str) -> bool:
    """Whether resolved content still contains a conflict marker."""
    return any(line.startswith(MARKER_PREFIXES) for line in text.splitlines())
