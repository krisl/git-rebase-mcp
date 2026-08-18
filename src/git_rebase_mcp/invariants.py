"""Checks that catch a rebase going wrong when git itself reports nothing.

Rewriting history can lose commits, fold two into one, or commit a file that
still has conflict markers in it, and in every case git exits zero and says
nothing. The only reliable defence is to record what the branch looked like
before starting and to compare afterwards.

That is the whole idea, with one correction learned from using it: what must
stay the same is not the tree but *the change the branch makes to its base*. A
rebase that also moves the branch onto newer upstream work changes the tree by
definition, and reporting that as damage cries wolf on the most ordinary rebase
there is. Comparing the branch's own diff holds in both cases.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import cast
from datetime import datetime, timezone

from .git import Git

# What git writes at the start of a conflict block. No trailing space is
# required: a marker someone has half-resolved by hand is `<<<<<<<` with the
# label stripped, and it has to be refused as loudly as git's own spelling.
# `=======` alone stays out on purpose -- it is a markdown heading underline,
# and a markdown file would trip the scan in every commit of every rebase.
MARKER_PREFIXES = ("<<<<<<<", "|||||||", ">>>>>>>")

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


@dataclass(frozen=True)
class Change:
    """A difference in what the branch does to its base."""

    # True when every changed line is still there, in a different order or
    # place. A resolution that puts a block somewhere else does that, and it is
    # not damage; reporting it as damage is how a check stops being believed.
    reordered_only: bool
    # The paths whose contribution moved and by how many lines -- what differs,
    # rather than a diff between the two tips, which on a rebase onto newer
    # upstream work is mostly the new base's own commits.
    summary: str


def branch_change(
    git: Git, backup: Backup, base: str, revision: str = "HEAD", fork: str | None = None
) -> Change | None:
    """How the branch's own contribution differs from before, or None.

    A rebase must not alter what the branch does to its base. Comparing that,
    rather than the resulting tree, is what makes the check hold when the rebase
    also moves onto newer upstream work -- where the tree changes for a
    perfectly good reason and only the branch's own diff should not.

    `fork` says where the branch's *old* contribution starts, for the rebase that
    was given an explicit onto. Then the two sides are measured from different
    places on purpose -- `upstream..old tip` against `onto..new tip` -- and the
    merge-base guessed at below is the wrong one: with an onto that shares only
    distant history it reaches back past the upstream and calls every commit
    between them part of the branch's own change.
    """
    fork = fork or _fork_point(git, backup.sha, base)
    if _contribution(git, fork, backup.sha) == _contribution(git, base, revision):
        return None
    before = _changed_lines(git, fork, backup.sha)
    after = _changed_lines(git, base, revision)
    return Change(
        reordered_only=before == after,
        summary=_moved_contribution(before, after),
    )


def _moved_contribution(
    before: dict[str, list[str]], after: dict[str, list[str]]
) -> str:
    """Which files the branch's contribution differs on, and by how many lines.

    The difference, not `git diff <old tip> <new tip>`, which is what this used
    to hand over. On a rebase that also moves onto newer upstream work that diff
    is mostly upstream's own commits: a real session resolved two files and was
    given thirty-three to read, of which thirty-one were the new base's and not
    its own doing. The check above already compares the right two things -- each
    side's contribution to its own base -- and only the evidence was taken from
    somewhere else, which is the worst place for a check to be loose. A caller
    who cannot see what moved has to reconstruct this by hand to decide whether
    to allow it, and the one who does not reconstruct it waves it through.

    Counted as lines present in one side's contribution and not the other's, per
    path, so a file both sides change in the same way stays out. Multiplicity is
    kept: the same line changed twice before and once after is a difference.
    """
    rows: list[tuple[str, str]] = []
    for path in sorted(set(before) | set(after)):
        was, now = Counter(before.get(path, ())), Counter(after.get(path, ()))
        only_before, only_after = sum((was - now).values()), sum((now - was).values())
        if not only_before and not only_after:
            continue
        counts = [
            *([f"{only_before} before only"] if only_before else []),
            *([f"{only_after} after only"] if only_after else []),
        ]
        rows.append((path, ", ".join(counts)))
    if not rows:
        # patch-ids disagreed and the lines did not, which is a reordering. The
        # caller is told that in `reordered_only`; saying "0 files" here instead
        # of naming it reads as the report having nothing to show.
        return "The same lines, in a different order or place.\n"
    width = max(len(path) for path, _ in rows)
    listed = "".join(f" {path.ljust(width)} | {counts}\n" for path, counts in rows)
    return f"{listed} {len(rows)} file{'' if len(rows) == 1 else 's'} whose contribution moved\n"


def same_tree(git: Git, backup: Backup, revision: str = "HEAD") -> bool:
    """Whether the result has exactly the content it started with.

    Not the check -- the tree changes for a good reason whenever the rebase also
    moves onto newer upstream work -- but the thing worth saying alongside it
    when the branch's own diff has changed. A branch whose diff differs and whose
    tree does not has lost nothing: the change is in the new base instead, which
    is what dropping an already-upstream commit looks like from here, and what
    redistributing one commit's work into others looks like too.
    """
    return git.out("rev-parse", f"{revision}^{{tree}}") == backup.tree


@dataclass(frozen=True)
class CommitChange:
    """One commit that differs between the branch before the rebase and after.

    `before` and `after` are the two shas of the same commit, either being None
    when it exists on only one side. The subject is how a person names it.
    """

    subject: str
    before: str | None
    after: str | None
    status: str  # "changed", "dropped" or "added"


@dataclass(frozen=True)
class BranchComparison:
    """The branch before and after, paired up commit by commit."""

    changes: tuple[CommitChange, ...]
    # git's own rendering, which shows what changed inside each commit. Large
    # on a long rebase, so it is passed on only when asked for.
    detail: str


# A range-diff summary line: `3:  bb4d412 ! 3:  000e026 adds d`, with `-` and a
# row of dashes standing in for the side a commit is missing from.
RANGE_DIFF_LINE = re.compile(
    r"^\s*(?:\d+|-):\s+(?P<before>[0-9a-f]+|-+)\s+"
    r"(?P<mark>[=!<>])\s+"
    r"(?:\d+|-):\s+(?P<after>[0-9a-f]+|-+)\s+(?P<subject>.*)$"
)

STATUS = {"!": "changed", "<": "dropped", ">": "added"}


def compare_commits(
    git: Git, backup: Backup, base: str, revision: str = "HEAD", fork: str | None = None
) -> BranchComparison:
    """Pair the branch's commits before and after, and name the ones that moved.

    `branch_change` answers whether the branch still makes the same change; the
    next question is always which commit accounts for the difference, and until
    now that meant leaving the tool for `git diff`. A stat cannot answer it: it
    says three files changed, not which commit changed them.

    The pairing is `git range-diff`, which exists for exactly this comparison
    and matches commits across a rewrite by content rather than by position. It
    also sees a reworded commit, which the patch-id check by design cannot --
    the message is not part of the change a branch makes to its base.

    `fork` as in `branch_change`: where the old side starts, when an explicit
    onto means it is not the merge-base.
    """
    fork = fork or _fork_point(git, backup.sha, base)
    found = git.run(
        "range-diff", f"{fork}..{backup.sha}", f"{base}..{revision}", check=False
    )
    if not found.ok:
        return BranchComparison(changes=(), detail="")
    changes: list[CommitChange] = []
    for line in found.stdout.splitlines():
        match = RANGE_DIFF_LINE.match(line)
        if match is None or match["mark"] == "=":
            continue
        changes.append(
            CommitChange(
                subject=match["subject"].strip(),
                before=_full(git, match["before"]),
                after=_full(git, match["after"]),
                status=STATUS[match["mark"]],
            )
        )
    return BranchComparison(changes=tuple(changes), detail=found.stdout)


def _full(git: Git, abbreviated: str) -> str | None:
    """A range-diff sha at the length the rest of the report uses.

    range-diff abbreviates, and a caller should not have to know that one field
    of one report is shorter than every other sha it is given. A row of dashes
    is how it spells the side a commit is missing from.
    """
    if abbreviated.startswith("-"):
        return None
    found = git.run(
        "rev-parse", "--verify", "--quiet", f"{abbreviated}^{{commit}}", check=False
    )
    return found.stdout.strip() or abbreviated


def _changed_lines(git: Git, base: str, tip: str) -> dict[str, list[str]]:
    """Every line the branch adds or removes, per file, order discarded.

    Two rebases that produce the same lines in a different order agree on this
    and disagree on the patch-id, which is exactly the distinction wanted.

    `--- a/f` and `+++ b/f` are headers only outside a hunk. A removed line
    whose content starts with `-- ` renders as `--- `, and an added line whose
    content starts with `++ ` renders as `+++ `, so a header can only be told
    from a body line by where it sits.

    The `---` side is kept for the file the diff deletes, whose `+++` side is
    `/dev/null`. Reading the path off `+++` alone files every deletion under
    that name, which collides two deleted files into one entry and names neither
    of them -- invisible while this only fed an equality check, and wrong the
    moment the difference is reported per path.
    """
    per_file: dict[str, list[str]] = {}
    path = ""
    removed = ""
    in_hunk = False
    for line in git.run("diff", base, tip).stdout.splitlines():
        if line.startswith("diff --git ") or line.startswith("@@"):
            in_hunk = line.startswith("@@")
            continue
        if not in_hunk and (line.startswith("--- ") or line.startswith("+++ ")):
            if line.startswith("--- "):
                removed = line[6:] if line.startswith("--- a/") else line[4:]
            else:
                added = line[6:] if line.startswith("+++ b/") else line[4:]
                path = removed if added == "/dev/null" else added
            continue
        if line[:1] in "+-" and path:
            per_file.setdefault(path, []).append(line)
    return {path: sorted(lines) for path, lines in per_file.items()}


def _contribution(git: Git, base: str, tip: str) -> str:
    """A stable identity for the diff `base..tip`, independent of context lines."""
    patch = git.run("diff", base, tip).stdout
    return git.run("patch-id", "--stable", stdin=patch).stdout.split(" ")[0].strip()


def _fork_point(git: Git, tip: str, base: str) -> str:
    """Where the branch left the base it was on before the rebase."""
    return git.out("merge-base", tip, base)


def commits_with_markers(git: Git, revision_range: str) -> list[MarkerHit]:
    """Commits in the range that brought a conflict marker to a path.

    Every commit rather than only the tip, because a marker committed part-way
    through and tidied up later still leaves a commit nobody can build.

    A path that already had one is not this rebase's doing, and reporting it is
    worse than saying nothing. Some files contain marker lines legitimately --
    a fixture of git's merge output, documentation quoting one -- and a check
    that scans whole trees flags those in every commit, of every rebase of that
    repository, for as long as the file exists. This project's own test suite
    is such a file: the warning fired on both commits of the rebase that found
    this, neither of which had touched it. A warning nobody can act on is one
    nobody reads, which costs more than the check was ever worth.
    """
    hits: list[MarkerHit] = []
    for sha in git.lines("rev-list", revision_range):
        present = _marker_paths(git, sha)
        if not present:
            continue
        # Only the candidates are re-scanned, so the parent costs nothing on the
        # ordinary commit, which has no markers anywhere to begin with.
        introduced = present - _marker_paths(git, f"{sha}^", sorted(present))
        if introduced:
            hits.append(
                MarkerHit(
                    sha=sha,
                    subject=git.out("log", "-1", "--format=%s", sha),
                    paths=tuple(sorted(introduced)),
                )
            )
    return hits


def _marker_paths(git: Git, revision: str, limit: Sequence[str] = ()) -> set[str]:
    """Paths in one commit's tree holding a conflict marker, at or below `limit`."""
    patterns: list[str] = []
    for regex in MARKER_REGEXES:
        patterns += ["-e", regex]
    scope = ["--", *limit] if limit else []
    found = git.run("grep", "-l", "-E", *patterns, revision, *scope, check=False)
    # Non-zero is also how grep reports matching nothing, and how a root commit
    # reports having no parent to compare against. Both mean the same here.
    if not found.ok:
        return set()
    return {line.split(":", 1)[1] for line in found.stdout.splitlines() if ":" in line}


def has_markers(text: str) -> bool:
    """Whether resolved content still contains a conflict marker."""
    return any(line.startswith(MARKER_PREFIXES) for line in text.splitlines())


def locals_the_backup_tracks(git: Git, backup: Backup, revision: str = "HEAD") -> tuple[str, ...]:
    """Local files that a trip through the backup tag would destroy.

    A rewrite that stops tracking a file -- `git rm --cached`, plus a line in
    .gitignore -- leaves a working copy that is now the only one there is. The
    backup tag still tracks it, at the content it had before. So:

        git checkout <backup-tag>   # writes the tracked version over the local one
        git checkout <branch>       # the branch does not track it: git deletes it

    Both steps are silent, and the second is silent *because* the file is
    ignored: git refuses to clobber an untracked file, and makes an exception for
    an ignored one. A file this rewrite has just started ignoring is exactly the
    exception.

    Nothing here causes that. What makes it worth reporting is that this server
    hands out the tag and recommends comparing against it -- and the file it
    destroys is the kind that holds machine-local configuration, which is
    unrecoverable and whose absence surfaces somewhere else entirely. It cost an
    hour: three tests failing on a missing package, an editor serving nothing,
    and no path back from either symptom to a `git checkout` two steps earlier.

    Reported forward, before any of it has happened, because that is the only
    moment the warning can still be acted on. The four conditions together are
    what keep it quiet on an ordinary rebase: a path the branch deliberately
    deleted is not on disk, one that is still tracked is not at risk, and one
    that is untracked without being ignored makes git refuse rather than clobber.
    """
    before = set(git.lines("ls-tree", "-r", "--name-only", backup.sha))
    if not before:
        return ()
    tracked_now = set(git.lines("ls-tree", "-r", "--name-only", revision))
    candidates = sorted(
        path
        for path in before - tracked_now
        if (git.repo / path).is_file() and path not in set(git.lines("ls-files"))
    )
    return _ignored(git, candidates) if candidates else ()


def _ignored(git: Git, paths: Sequence[str]) -> tuple[str, ...]:
    """Which of `paths` the current ignore rules match.

    `--no-index` so the answer is about the rules rather than about the index:
    without it git declines to answer for anything it tracks, and these paths are
    asked about precisely because tracking is what they have just lost. Exit
    status 1 is how it reports matching nothing.
    """
    found = git.run("check-ignore", "--no-index", "--", *paths, check=False)
    if not found.ok:
        return ()
    return tuple(line for line in found.stdout.splitlines() if line)


@dataclass(frozen=True)
class CarriedRef:
    """A branch `--update-refs` is expected to move, and where it pointed before.

    Recorded before the rewrite because afterwards there is nothing left to read
    it from: a ref that was carried no longer holds its old sha, and one that was
    left behind is indistinguishable from one that was never in the range.
    """

    ref: str
    sha: str


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
    # Where the branch sat before, when a rebase was given an explicit onto and
    # so `base_sha` names where it lands instead. Empty for the ordinary rebase,
    # where the two are the same commit and the merge-base finds it anyway. It is
    # what the finish check measures the old contribution from: see the `fork`
    # argument of `branch_change`.
    upstream_sha: str = ""
    stashed: tuple[str, ...] = ()
    # The stash entry holding the moved-aside files, so restoring targets that
    # entry rather than the top of the stash stack -- which is what a stash the
    # user made meanwhile would otherwise be.
    stash_ref: str | None = None
    check_command: str | None = None
    # The commits amended at an `edit` stop, by the sha git recorded in
    # `rebase-merge/amend` -- which names the *step*, and so stays put when the
    # same commit is amended twice. Kept because amending is how an `edit` stop
    # is used, and it changes the branch's own share of the change on purpose:
    # without this the finish check reports the tool's primary workflow as
    # "something was lost or resolved wrongly".
    amended: tuple[str, ...] = ()
    # The commits whose step was continued from a conflict, by the sha being
    # replayed. A resolution is a decision, and a decision legitimately changes
    # what the branch contributes: composing two sides is not the same text as
    # either of them. Without this, a rebase that resolved anything and did not
    # also amend had one reading left at the finish -- "something was lost or
    # resolved wrongly" -- which is the wrong half of the sentence to leave a
    # caller holding.
    resolved: tuple[str, ...] = ()
    # The branches `update_refs` was asked to carry, as they stood before the
    # rewrite. Empty when it was not asked for, so nothing is claimed about a
    # rebase that never promised to move them.
    carried: tuple[CarriedRef, ...] = ()

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
    stash_ref = raw.get("stash_ref")
    amended_raw = raw.get("amended", ())
    amended: tuple[str, ...] = (
        tuple(str(sha) for sha in cast("list[object]", amended_raw))
        if isinstance(amended_raw, list)
        else ()
    )
    resolved_raw = raw.get("resolved", ())
    resolved: tuple[str, ...] = (
        tuple(str(sha) for sha in cast("list[object]", resolved_raw))
        if isinstance(resolved_raw, list)
        else ()
    )
    try:
        return Session(
            backup_ref=str(raw["backup_ref"]),
            backup_sha=str(raw["backup_sha"]),
            backup_tree=str(raw["backup_tree"]),
            base=str(raw["base"]),
            base_sha=str(raw["base_sha"]),
            upstream_sha=str(raw.get("upstream_sha") or ""),
            stashed=stashed,
            stash_ref=str(stash_ref) if stash_ref is not None else None,
            check_command=str(command) if command is not None else None,
            amended=amended,
            resolved=resolved,
            carried=_carried(raw.get("carried", ())),
        )
    except KeyError:
        return None


def _carried(raw: object) -> tuple[CarriedRef, ...]:
    """The recorded refs, skipping any entry a different version wrote oddly."""
    if not isinstance(raw, list):
        return ()
    found: list[CarriedRef] = []
    for entry in cast("list[object]", raw):
        if not isinstance(entry, dict):
            continue
        fields = cast("dict[str, object]", entry)
        ref, sha = fields.get("ref"), fields.get("sha")
        if ref is not None and sha is not None:
            found.append(CarriedRef(ref=str(ref), sha=str(sha)))
    return tuple(found)


def clear_session(git: Git) -> None:
    _session_file(git).unlink(missing_ok=True)
