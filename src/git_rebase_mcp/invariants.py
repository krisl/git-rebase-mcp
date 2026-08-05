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

import json
import re
from dataclasses import asdict, dataclass
from typing import cast
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


@dataclass(frozen=True)
class Session:
    """What one run of the server set up, so a later call can undo or check it.

    Kept in the git directory rather than in memory: it has to survive the
    server being restarted mid-rebase, and it is what someone reads by hand if
    everything else fails.
    """

    backup_ref: str
    backup_sha: str
    backup_tree: str
    base: str
    # Resolved when the rebase started. The spelling above is kept for reporting,
    # but a relative name like HEAD~2 points somewhere else once history has
    # been rewritten, so it cannot be used to name the range afterwards.
    base_sha: str
    stashed: tuple[str, ...] = ()
    check_command: str | None = None

    @property
    def backup(self) -> Backup:
        return Backup(ref=self.backup_ref, sha=self.backup_sha, tree=self.backup_tree)


def _session_file(git: Git):
    return git.git_path("rebase-mcp.json")


def save_session(git: Git, session: Session) -> None:
    _session_file(git).write_text(json.dumps(asdict(session), indent=1) + "\n")


def load_session(git: Git) -> Session | None:
    """Read the session back, treating anything unreadable as absent.

    A file left by a different version must not stop the server working.
    """
    try:
        parsed: object = json.loads(_session_file(git).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    raw = cast("dict[str, object]", parsed)

    stashed_raw = raw.get("stashed", ())
    stashed: tuple[str, ...] = (
        tuple(str(path) for path in cast("list[object]", stashed_raw))
        if isinstance(stashed_raw, list)
        else ()
    )
    command = raw.get("check_command")
    try:
        return Session(
            backup_ref=str(raw["backup_ref"]),
            backup_sha=str(raw["backup_sha"]),
            backup_tree=str(raw["backup_tree"]),
            base=str(raw["base"]),
            base_sha=str(raw["base_sha"]),
            stashed=stashed,
            check_command=str(command) if command is not None else None,
        )
    except KeyError:
        return None


def clear_session(git: Git) -> None:
    _session_file(git).unlink(missing_ok=True)
