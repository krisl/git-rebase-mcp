# git-rebase MCP server

## Context

Rebasing a 21-commit branch into 11 clean commits took most of a session and produced three errors, each of which silently corrupted history:

1. **Amending at a conflicted `edit` stop.** `edit` normally stops with HEAD on the applied commit, but when it stops *because of a conflict* HEAD is still the previous commit. `git commit --amend` there folded two commits into one. Nothing in git's output distinguishes the two situations.
2. **A hand-authored todo that dropped three commits.** They vanished with no warning.
3. **Staging and committing a file that still contained conflict markers.** Two commits shipped `<<<<<<<` into the tree.

None were detected by git. All three were caught by an ad-hoc harness: tag the tip first, then assert at the end that the branch still makes the same change to its base. That harness is the thing worth productising.

A second, separate cost was conflict *presentation*. Reordering commits means a later fix's context no longer matches, so git asks you to merge text when what you want is to compose two intents. Reading three near-identical marker blocks differing in indentation and one token is slow and error-prone; reading "base→ours: wrap loop in `if current:`" against "base→theirs: replace `delta` with `delta_count(...)`" is immediate.

**Prior art, and the direct model for this:** [DiffDiff](https://github.com/krisl/DiffDiff) already does exactly that for vim — parse a conflict block, write the three sides to temp files, show `diff -u base ours` and `diff -u base theirs`, with an optional direct `ours→theirs` toggle. Its documented ideas list (commit messages as labels, conflict counts, resolve-with-ours/theirs) is close to this tool surface. This server is DiffDiff's insight, delivered to an agent instead of a buffer, wrapped in the rebase state machine and the invariant harness.

**Goal:** an agent driving a rebase should be unable to make the three errors above, and should see conflicts as composable intents rather than marker soup.

## Design principles

1. **Refuse, don't advise.** I understood the amend rule and broke it anyway. A tool that returns an error is worth more than any documentation.
2. **Never parse conflict markers.** Git already holds the three sides in the index as stages 1/2/3 (`git show :1:path`, `:2:`, `:3:`). Marker parsing is what DiffDiff must do inside a buffer; a server has the structured source. My hand-written `take_ours`/`take_theirs` helpers were solving a solved problem, and one of them silently truncated a file.
3. **Role labels, not ours/theirs.** In a rebase these are inverted relative to merge: "ours" is the branch built so far, "theirs" is the commit being replayed. Label them `branch_so_far` and `replaying` so the footgun cannot fire.
4. **Hunk-scoped payloads.** File-scoped `base→theirs` is just `git show <sha> -- <file>`, which the agent already has. The new information is *which lines the two intents collide on*.
5. **Shell out to git.** Not pygit2. The value of this server is that its behaviour matches what a human would get by hand; a libgit2 divergence would undermine that.

## Tooling

Python 3.12+, `uv`, official `mcp` SDK (FastMCP), distributed via `uvx`. Rationale: mature MCP SDK, `py-tree-sitter` + `tree-sitter-language-pack` give phase 2 prebuilt grammars with no grammar compilation, and scratch-repo integration tests are most ergonomic in pytest. `difflib` in the stdlib covers hunk computation.

## Tool surface (v1: rebase only)

**Lifecycle**

| Tool | Behaviour |
| --- | --- |
| `rebase_preflight` | Dry run. Validates the todo (dropped commits, unresolvable `fixup!` targets), reports which commits get rewritten, creates the backup tag, records the baseline tree hash, enables `rerere.enabled` and `rebase.missingCommitsCheck=error` for the run, stashes untracked files that a replayed commit would collide with. Starts nothing. |
| `rebase_start` | Runs the rebase non-interactively with the supplied todo (`GIT_SEQUENCE_EDITOR`) or `--autosquash`. Takes an optional `check_command` wired to `--exec`. Refuses on a dirty index. |
| `rebase_status` | The typed state below. |
| `rebase_continue` | Refuses while any path is unmerged. |
| `rebase_amend` | **Refuses unless `head_is_replaying_commit`.** The single most valuable tool in the server. |
| `rebase_abort` | Aborts, restores the stash, reports what was restored. |
| `rebase_finish` | Asserts the invariants and reports. |

**Conflicts**

| Tool | Behaviour |
| --- | --- |
| `rebase_conflicts` | All conflicted files, each split into collision units (below). |
| `rebase_resolve` | Writes resolved content, **refuses if markers remain**, stages it. |

### The status payload

```jsonc
{
  "in_progress": true,
  "step": { "index": 7, "total": 20 },
  "action": "edit",
  "replaying": { "sha": "182a3b4", "subject": "Report the census even when..." },
  "head":      { "sha": "67f30d8", "subject": "Track test counts and..." },
  "head_is_replaying_commit": false,   // <- false here means: DO NOT amend
  "conflicted": true,
  "conflicted_files": [".github/scripts/ci_metrics.py"],
  "backup_ref": "refs/tags/rebase-backup/2026-08-05T18-22",
  "stashed": ["pytest.ini"],
  "todo_remaining": [ /* ... */ ]
}
```

`head_is_replaying_commit` is derived simply: if any path is unmerged, the commit has not been applied, so HEAD is still the previous one. That one boolean prevents error #1.

### The conflict payload

Per **collision unit** — a group of hunks whose base line ranges overlap:

```jsonc
{
  "file": ".github/scripts/ci_metrics.py",
  "unit": { "index": 1, "total": 3 },
  "base_range": [654, 698],
  "branch_so_far_diff": "@@ -654,44 +654,50 @@\n+    if current:\n...",
  "replaying_diff":     "@@ -654,44 +665,50 @@\n-                delta_count(...)\n+                delta,\n...",
  "replaying_commit":   { "sha": "182a3b4", "subject": "...", "body": "..." },
  "direct_diff": null,          // ours->theirs, on request (DiffDiff's toggle)
  "sides": { "base": "...", "branch_so_far": "...", "replaying": "..." }
}
```

**Algorithm.** Read the three blobs from index stages 1/2/3. Compute `base→branch_so_far` and `base→replaying` with `difflib.SequenceMatcher` opcodes. Group opcodes from both diffs whose base ranges overlap or abut into collision units. Render each side's opcodes for that unit as a unified diff. Full-file `sides` stay available as a fallback but are not the primary view.

This is also what makes the not-yet-landed-symbol case self-describing: because `branch_so_far` **is** the replayed state, anything a later commit hasn't introduced yet shows up as a deletion in `base→branch_so_far`. No separate symbol-availability signal is needed — verified against this session's `delta_count` conflicts, where the call site sat inside the conflicting hunk as both a `-` and a `+` line.

### Invariants checked by `rebase_finish`

- `git diff <backup_ref> HEAD` is empty, unless the caller passes `allow_tree_change` with a reason. Catches errors #1 and #2.
- No commit in the rewritten range contains conflict markers — scan every tree, not just the tip. Catches error #3.
- `check_command` passed at every commit (via `--exec` during the run).
- Reports the before/after commit list so the caller can see what was squashed into what.

## Project layout

```
  pyproject.toml              # uv; mcp[cli]; pytest
  README.md                   # includes credit to DiffDiff as prior art
  src/git_rebase_mcp/
    server.py                 # FastMCP tool definitions, thin
    git.py                    # subprocess wrapper; typed errors
    state.py                  # .git/rebase-merge/* + REBASE_HEAD -> status payload
    conflicts.py              # index stages -> collision units
    invariants.py             # backup tag, tree assert, marker scan, stash bookkeeping
  test/
    conftest.py               # scratch-repo fixtures
    test_state.py
    test_conflicts.py
    test_invariants.py
    test_regressions.py       # the three failure modes, as tests
```

## Verification

The three errors from this session are the specification. Each becomes a test built on a scratch repo:

1. **Conflicted `edit` stop.** Construct a rebase where an `edit` step conflicts. Assert `rebase_status().head_is_replaying_commit is False` and that `rebase_amend()` raises rather than folding two commits.
2. **Dropped commits.** Hand `rebase_preflight` a todo missing commits from the range. Assert it errors and names them, and that nothing has been started.
3. **Markers committed.** Call `rebase_resolve` with content still containing `<<<<<<<`; assert refusal. Separately, force a marker-bearing commit into a scratch branch and assert `rebase_finish` reports it.

Plus:

4. **Golden replay.** Build a branch shaped like this session's: a feature series, then fix commits authored later against those features. Autosquash them. Assert the branch's contribution is unchanged and that every commit passes its own tests — the end-to-end property the whole server exists to guarantee.
5. **Conflict payload correctness.** Reconstruct the `delta_count` collision from this session as a fixture; assert the two diffs isolate "wrap in `if current:`" from "replace `delta` with `delta_count(...)`", and that the collision unit contains both.
6. **Manual smoke.** Point a Claude Code session at a scratch repo and drive a real reorder end to end through the tools only.

## Phasing

- **v1 — rebase. Done.** Everything above. Smallest surface that would have prevented all three errors.
- **v2 — structural conflicts.** tree-sitter (`tree-sitter-language-pack`) to classify each collision unit as node-disjoint or overlapping, and auto-resolve the disjoint ones so they never surface. Most of this session's conflicts were end-of-file test appends and pure reindents, which are node-disjoint. Must be a lossless CST, not a Python `ast`: these files are comment-heavy and several conflicts *were* comment blocks, and any reformatting would alter the branch's contribution and trip the check.
- **later — history surgery.** `split_commit` by hunk (the one operation that forced hand-written Python this session, because `git add -p` is interactive), `absorb` (route a fix to the commit that introduced the line), bisect driving.

## Out of scope

Project-specific rules — changelog gates, commit-message linting, per-repo file policies. The server enforces git-level invariants only; `check_command` is the single hook, and it is a test runner, not a policy engine.
