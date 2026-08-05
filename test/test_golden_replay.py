"""The whole loop, on a branch shaped like the one that motivated the project.

A feature series, then a fix written later against the tip. Squashing that fix
back into an earlier commit reorders history, and the fix no longer matches the
shape of the code at its new position -- which is where the conflicts that cost
the most time come from.

Driven here through the tools alone: start, read each conflict as two intents,
resolve, continue, and check the result against where the branch began.
"""

from __future__ import annotations

from git_rebase_mcp.server import (
    rebase_conflicts,
    rebase_continue,
    rebase_finish,
    rebase_start,
    rebase_resolve,
)

from scratch import Scratch

ORIGINAL = """def counts(packages):
    rows = []
    for package in packages:
        rows.append(delta(package))
    return rows
"""

# "Wrap the loop" indents the body under a guard.
WRAPPED = """def counts(packages):
    if packages:
        rows = []
        for package in packages:
            rows.append(delta(package))
    return rows
"""

# "Use delta_count" is written on top of the wrap, so it carries the indentation.
WRAPPED_AND_FIXED = WRAPPED.replace("delta(package)", "delta_count(package)")

# Squashed into "Add counts", the fix has to be reapplied without the wrap.
FIXED_ONLY = ORIGINAL.replace("delta(package)", "delta_count(package)")


def build(scratch: Scratch) -> tuple[str, str, str]:
    scratch.commit("base", readme="start\n")
    for message, content in (
        ("Add counts", ORIGINAL),
        ("Wrap the loop", WRAPPED),
        ("Use delta_count", WRAPPED_AND_FIXED),
    ):
        scratch.write("counts.py", content)
        scratch.commit(message)
    add, wrap, fix = (scratch.git.out("rev-parse", f"HEAD~{n}") for n in (2, 1, 0))
    return add, wrap, fix


def test_a_fix_is_squashed_back_into_its_target(scratch: Scratch) -> None:
    add, wrap, fix = build(scratch)
    repo = str(scratch.path)
    before = scratch.read("counts.py")

    started = rebase_start("HEAD~3", repo, [f"pick {add}", f"fixup {fix}", f"pick {wrap}"])
    assert started.status.state == "conflicted"

    # First conflict: the fix lands where the wrap has not happened yet.
    unit = rebase_conflicts(repo).files[0].units[0]
    assert "-    if packages:" in unit.branch_so_far_diff  # the branch has no wrap here
    assert "+            rows.append(delta_count(package))" in unit.replaying_diff
    changed = [line for line in unit.replaying_diff.splitlines() if line[:1] in "+-"]
    assert not any("if packages" in line for line in changed)  # the fix left the wrap alone

    rebase_resolve("counts.py", FIXED_ONLY, repo)
    after_first = rebase_continue(repo)

    # Second conflict: the wrap now lands on a body that already has the fix.
    assert after_first.state == "conflicted"
    unit = rebase_conflicts(repo).files[0].units[0]
    assert "delta_count" in unit.branch_so_far_diff
    assert "+    if packages:" in unit.replaying_diff

    rebase_resolve("counts.py", WRAPPED_AND_FIXED, repo)
    assert rebase_continue(repo).state == "not_rebasing"

    finished = rebase_finish(repo)
    assert finished.ok, finished.guidance
    assert finished.tree_change is None  # the end result is exactly as it was
    assert finished.commits_with_markers == ()
    assert [c.subject for c in finished.commits] == ["Add counts", "Wrap the loop"]
    assert scratch.read("counts.py") == before


def test_a_check_command_runs_at_every_commit(scratch: Scratch) -> None:
    """A step can apply cleanly and still leave the tree broken; only running
    something at each commit sees that."""
    scratch.commit("base", readme="start\n")
    scratch.write("counts.py", ORIGINAL)
    scratch.commit("Add counts")
    scratch.write("counts.py", FIXED_ONLY)
    scratch.commit("Use delta_count")

    repo = str(scratch.path)
    add, fix = scratch.git.out("rev-parse", "HEAD~1"), scratch.git.out("rev-parse", "HEAD")

    started = rebase_start(
        "HEAD~2", repo, [f"pick {add}", f"fixup {fix}"], check_command="test -f counts.py"
    )
    assert started.status.state == "not_rebasing"

    finished = rebase_finish(repo)
    assert finished.ok
    assert [c.subject for c in finished.commits] == ["Add counts"]
