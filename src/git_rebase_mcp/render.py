"""A reading of each report for the person watching.

Every tool here already answers precisely, in fields an agent consumes. What the
person over its shoulder gets is that JSON, and a rebase is exactly the operation
where they most want to know where it is: which step of how many, what is being
replayed, what is conflicted, whether the commit in hand is theirs to amend.

So each report is also rendered as a few lines of text, carried in the tool
result's content block while the fields go on in its structured content. Nothing
new is said or named here -- the vocabulary is git's (step, pick, edit, replaying,
HEAD, conflicted, todo, base, onto) and the server's own tool names -- because a
second vocabulary for the same facts is how the two accounts start disagreeing.

The report's own `guidance` is appended verbatim rather than summarised, for the
same reason: it is the sentence the agent is acting on, and the person watching is
best served by reading the one it read.
"""

from __future__ import annotations

from typing import Any

# Enough of a sha to identify a commit by eye against git's own output.
SHA = 9


def render(report: Any) -> str:
    """The report as text, or its guidance alone if nothing renders it."""
    renderer = _RENDERERS.get(type(report).__name__)
    body = renderer(report) if renderer else ""
    guidance = getattr(report, "guidance", "")
    # Blank line between them: the rendering is a table and the guidance is prose,
    # and run together the eye reads the first line of the prose as another row.
    return "\n\n".join(part for part in (body, guidance) if part)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _commit(commit: Any | None) -> str:
    return f"{commit.sha[:SHA]}  {commit.subject}" if commit is not None else "-"


def _rows(*pairs: tuple[str, str]) -> str:
    """Label/value lines, aligned, dropping the ones with nothing to say."""
    shown = [(label, value) for label, value in pairs if value]
    if not shown:
        return ""
    width = max(len(label) for label, _ in shown)
    return "\n".join(f"  {label.ljust(width)}  {value}" for label, value in shown)


def _listed(items: tuple[str, ...], limit: int = 6) -> str:
    if not items:
        return ""
    shown = ", ".join(items[:limit])
    return shown + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


def _status(report: Any) -> str:
    where = [report.operation or report.state]
    if report.step is not None:
        where.append(f"step {report.step.index}/{report.step.total}")
    if report.action:
        where.append(report.action)
    if report.branch:
        where.append(report.branch)
    head = _commit(report.head)
    rows = _rows(
        ("replaying", _commit(report.replaying) if report.replaying else ""),
        ("HEAD", head + ("  (the commit being replayed)" if report.head_is_replaying_commit else "")),
        ("conflicted", _listed(report.conflicted_files)),
        ("auto-resolved", _listed(report.auto_resolved)),
        ("replayed answers", _listed(report.replayed_resolutions)),
        ("amend", "rebase_amend applies to HEAD" if report.can_amend else ""),
        ("stop", "this step's only one -- stage changes with the resolution"
         if report.action_stop_lost else ""),
        ("check", f"{'passed' if report.check.ok else 'FAILED'}: {report.check.command}"
         if report.check else ""),
        ("finished", f"{_plural(report.finished.rewritten, 'commit')} over {report.finished.base}, "
         f"{'branch moved' if report.finished.branch_moved else 'branch unmoved'}, unchecked"
         if report.finished else ""),
    )
    return " · ".join(where) + ("\n" + rows if rows else "")


def _start(report: Any) -> str:
    rows = _rows(
        ("backup", report.backup_ref),
        ("stashed", _listed(report.stashed)),
        ("lost locals", _listed(report.lost_locals)),
    )
    return "rebase started" + ("\n" + rows if rows else "") + "\n" + _status(report.status)


def _preflight(report: Any) -> str:
    rows = _rows(
        ("base", report.base),
        ("commits", f"{_plural(len(report.commits), 'commit')} in range"),
        ("dropped", _listed(tuple(f"{c.sha[:SHA]} {c.subject}" for c in report.dropped), 4)),
        ("unknown", _listed(report.unknown)),
        ("already upstream", _listed(
            tuple(f"{c.sha[:SHA]} {c.subject}" for c in report.already_upstream), 4)),
        ("stray fixups", _listed(
            tuple(f"{c.sha[:SHA]} {c.subject}" for c in report.stray_fixups), 4)),
        ("blocking", _listed(report.blocking)),
        ("untracked", _listed(report.untracked_collisions)),
    )
    verdict = "safe to start" if report.safe_to_start else "NOT safe to start"
    return f"preflight · {verdict}" + ("\n" + rows if rows else "")


def _conflicts(report: Any) -> str:
    regions = sum(len(f.units) for f in report.files)
    head = f"conflicts · {_plural(len(report.files), 'file')} · {_plural(regions, 'region')}"
    if report.replaying is not None:
        head += f"\n  replaying  {_commit(report.replaying)}"
    lines = [head]
    for entry in report.files:
        lines.append(f"  {entry.path}")
        if entry.deleted_by:
            lines.append(f"    deleted by {entry.deleted_by}")
        for index, unit in enumerate(entry.units, start=1):
            where = (f"base {unit.base_range[0]}-{unit.base_range[1]}"
                     if unit.base_range else "not in the base")
            lines.append(f"    region {index}  {where}")
            lines.append(f"      branch_so_far  {unit.branch_so_far_summary}")
            lines.extend(_indented(unit.branch_so_far_diff))
            lines.append(f"      replaying      {unit.replaying_summary}")
            lines.extend(_indented(unit.replaying_diff))
    return "\n".join(lines)


def _indented(text: str, width: int = 8) -> list[str]:
    """Diff lines nested under their summary, without trailing whitespace.

    The diffs are already trimmed where they are built (long runs summarised,
    context kept), so this is bounded: it shows what each side did rather than
    only how much, which is what the region is read for.
    """
    pad = " " * width
    return [(pad + line) if line else "" for line in text.splitlines()]


def _resolve(report: Any) -> str:
    rows = _rows(
        ("staged", "the deletion" if report.deleted else report.path),
        ("still conflicted", _listed(report.still_conflicted) or "none"),
        *[(f"repeated {index}", f"{row.line.strip()[:60]!r}  "
           f"{row.in_resolution}x here, {row.in_branch}x branch, {row.in_replaying}x replaying")
          for index, row in enumerate(report.repeated[:3], start=1)],
        *[(f"dropped {index}", f"{row.line.strip()[:60]!r}  "
           f"gone here, {row.in_branch}x branch, {row.in_replaying}x replaying")
          for index, row in enumerate(report.dropped[:3], start=1)],
    )
    return "resolve" + ("\n" + rows if rows else "")


def _todo(report: Any) -> str:
    steps = "\n".join(f"    {line}" for line in report.remaining[:12])
    more = (f"\n    (+{len(report.remaining) - 12} more)"
            if len(report.remaining) > 12 else "")
    dropped = _listed(tuple(f"{c.sha[:SHA]} {c.subject}" for c in report.dropped), 4)
    return (f"todo · {_plural(len(report.remaining), 'step')} left"
            + (f"\n{steps}{more}" if steps else "")
            + (f"\n  dropped  {dropped}" if dropped else ""))


def _amend(report: Any) -> str:
    return "amend\n" + _rows(
        ("before", _commit(report.before)),
        ("after", _commit(report.after)),
        ("untracked beside", _listed(report.beside)),
    )


def _split(report: Any) -> str:
    return "split\n" + _rows(
        ("taken back out", _commit(report.unapplied)),
        ("HEAD", _commit(report.head)),
        ("in the working tree", _listed(report.paths)),
    )


def _compare(report: Any) -> str:
    rows = _rows(
        ("replayed", f"{report.replayed}/{report.total}"),
        ("changed", str(len(report.changed)) if report.changed else "none -- same patches"),
    )
    for change in report.changed[:6]:
        rows += f"\n    {change.status:<8} {change.subject}"
    return "compare" + ("\n" + rows if rows else "")


def _finish(report: Any) -> str:
    rows = _rows(
        ("verdict", "checks out" if report.ok else "NOT finished"),
        ("backup", report.backup_ref),
        ("commits", f"{_plural(len(report.commits), 'commit')} over the base"),
        ("branch change", "the same lines, reordered" if report.reordered_only
         else ("moved" if report.branch_change else "none")),
        ("markers committed", _listed(report.commits_with_markers)),
        ("restored", _listed(report.restored)),
    )
    if report.branch_change and not report.reordered_only:
        rows += "\n" + "\n".join(
            f"    {line}" for line in report.branch_change.rstrip("\n").splitlines()
        )
    return "finish" + ("\n" + rows if rows else "")


def _abort(report: Any) -> str:
    return "aborted\n" + _rows(
        ("HEAD", _commit(report.head)),
        ("restored", _listed(report.restored)),
        ("discarded", _listed(report.discarded)),
    )


_RENDERERS = {
    "StatusReport": _status,
    "StartReport": _start,
    "PreflightReport": _preflight,
    "ConflictReport": _conflicts,
    "ResolveReport": _resolve,
    "TodoReport": _todo,
    "AmendReport": _amend,
    "SplitReport": _split,
    "CompareReport": _compare,
    "FinishReport": _finish,
    "AbortReport": _abort,
}
