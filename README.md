# git-rebase-mcp

An MCP server that makes driving a `git rebase` safe for an agent.

It exists because a rebase can corrupt history in ways git does not report.
Three that happened, in one session, on one branch:

- **Amending at a conflicted `edit` stop.** `edit` normally stops with `HEAD` on
  the commit just applied, but when it stops *because of a conflict* `HEAD` is
  still the previous commit. `git commit --amend` there silently folds two
  commits into one. Nothing in git's output distinguishes the two situations.
- **A hand-written todo list that dropped three commits.** They vanished without
  a warning.
- **Staging a file that still contained conflict markers.** Two commits shipped
  `<<<<<<<` into the tree.

Every one exited zero and reported success.

## What it does about it

**Refuses the unsafe operation** rather than documenting it. `rebase_amend` is
not callable at a conflicted stop, and the refusal says why and what to do
instead:

> Refusing to amend: the rebase is conflicted, and HEAD (2c806c04b 'base') is
> not a commit this step created. Stopped part-way through applying 87f3d0fd4
> (third). That commit does not exist yet, so HEAD is still the one before it
> and amending would rewrite the wrong commit. Resolve the conflicted paths,
> then continue.

**Says when a stop is the last one.** An `edit` step stops for you once its
commit applies — unless it conflicts, in which case *the conflict is that stop*.
`--continue` commits the resolution and moves to the next step, and the commit
you meant to change goes past unchanged. Git says nothing, and the advice above
is true and beside the point: a caller can follow it exactly, resolve, continue,
and lose the edit. So `action_stop_lost` says so and the guidance says what to do
instead — stage the change now, with the resolution, because `--continue` commits
everything staged. A conflicted `reword` is worse and gets the same warning: it
finishes the whole rebase still carrying the original message, and reports
success.

**Shows a conflict as two intents, not as marker soup.** Per contested region
it reports what each side did to the common base:

```
──── branch so far ────          ──── replaying: "Use delta_count" ────
 def counts(packages):            def counts(packages):
-    if packages:                     if packages:
-        rows = []                        rows = []
-        for package in packages:         for package in packages:
+    rows = []                   -            rows.append(delta(package))
+    for package in packages:    +            rows.append(delta_count(package))
     return rows                      return rows
```

"The branch has not replayed the wrap yet" against "the fix swaps the call, and
leaves the wrap alone". Composing those needs no reasoning about which of three
interleaved blocks belongs to whom.

Each side also gets a sentence — "adds 1 line and reindents or moves 3 lines"
against "adds 1 line and removes 1 line" — because a block wrapped in an `if`
produces a diff the size of the block and a change of one line, and the diff
alone does not say which you are looking at.

Each region is headed by the definition it sits in — `@@ -662,13 +662,12 @@ def
counts_render(self, ctx):` — worked out by git's own funcname driver for the
language, of which it ships twenty-five. Git applies one only where a repository
asked for it in `.gitattributes`, and most have not; its fallback then recognises
a definition at column 0 only, which in any language whose definitions nest names
the class every time and the method never. So this server picks the driver, and
picks nothing else: the patterns stay git's, and a language it has never heard of
is named as well as one it has.

Both sides get to state their intent. The replayed commit has its message; the
branch so far is an accumulation with no message, so each region names the
commits behind its lines — which is the nearest equivalent, and is left empty
rather than guessed when the lines predate the rebase.

**Offers the resolution rather than making it.** `resolve` takes
`take="both" | "branch" | "replaying"` for the cases the two diffs make obvious,
so answering costs one call instead of sending a whole file back. `auto_resolve`
will compose conflicts where the two sides touched different lines and carry on
without stopping, but it is **off by default**: lines that do not overlap can
still contradict each other — one side adding a call, the other removing the
helper it needs — and a conflict resolved without being read has to be reviewed
afterwards anyway.

**Says when the question is whether a path lives at all.** A modify/delete does
not look like a conflict: git leaves the surviving side's text in the working
file with no markers in it, so nothing about the file says one side deleted it.
It is reported as that, with the side named, and `take` then means a side rather
than a text — the side that deleted the path stages the deletion. It used to
compose the file's blocks, of which there are none, and stage the empty string
that fell out: an empty file in the commit, the path no longer conflicted, and
nothing downstream with a reason to complain. The guidance also points out that
such a deletion is often half of a rename, in which case what the incoming side
did to the old path has to be reapplied to the new one.

**Splits a commit without being reached past.** `rebase_split` takes the commit
a step just applied back out, leaving its changes in the working tree to commit
as several. Doing that by hand — `git reset HEAD^` at an `edit` stop — is the one
move that leaves a rebase where `--amend` rewrites the commit *before* the one you
mean, and git's own record still says amending is safe through it. Now the state
says `unapplied`, amending is refused, and `proceed` refuses while any of the
commit is still outside a commit. That last one matters most for a file the
commit *added*: the reset leaves it untracked, and `git rebase --continue`
neither refuses nor picks it up. It reports success, and the change is simply
not in the branch.

**Records where the branch was, and checks the result against it.** What must
stay the same is the change the branch makes to its base -- not the resulting
tree, which changes for good reason when the rebase also moves onto newer
upstream work. A difference is usually a report of damage, and this is what
caught all three errors above. Usually, because a deliberate redistribution
looks the same from there — a commit dropped because the new base already has
it, or one commit's work moved into others — so the report says which:
`tree_identical` means the content is exactly what it started as, and nothing was
lost but the branch's own share of it.

## It is not only for rebases

A rebase is not the only thing that leaves three stages in the index. A
cherry-pick, a revert, a merge, a rebase you started by hand, a stash that
popped into a conflict — git records all of them the same way, which is what the
conflict view reads. So the tools work on any of them:

```
status:   state=conflicted operation=cherry-pick
          A cherry-pick of 47ba527eb (side change) left 1 path conflicted.
          Nothing has been committed yet. Call conflicts to read them…
proceed:  runs `git cherry-pick --continue`, because `git rebase --continue`
          does not finish a cherry-pick
```

`state` stays `conflicted` whatever produced it, so one check answers the
question; `operation` says what to expect. This was a **false negative** until
recently: anything that was not a rebase read as "no rebase in progress", which
`conflicts` reported as *"Nothing is conflicted."* — of a repository with
unmerged paths sitting in the index.

A conflict nothing recorded — the popped stash — is reported as
`operation="unknown"`, with its regions read exactly as any other. What it does
not get is a `proceed` or an `abort`, because there is no operation to finish
and no way to know what undoing it would discard.

The rebase-specific safety stays rebase-specific: the backup tag, the
branch-change check and the amend refusal are all about rewriting history, which
a cherry-pick is not doing.

## Tools

| Tool | | Works on |
| --- | --- | --- |
| `rebase_preflight` | What a rebase would do. Changes nothing. Names commits a todo would drop. | rebase |
| `rebase_start` | Tags the tip, moves aside colliding untracked files, begins. `edit=[sha, ...]` builds the todo for you — those commits stop, the rest are picked — so the common case needs no list. `autosquash` folds `fixup!` commits in; `update_refs` carries every other branch pointing into the range along, which a stack of branches on one another needs. | rebase |
| `status` | Typed state, what operation is in progress, and whether `HEAD` is the commit being replayed. Reports a rebase that has ended and not been checked, rather than only that none is running. Every report also names the checkout it is about — `worktree` and `branch` — so an answer from here can be told apart from one a shell gave about a different worktree of the same repository, and `replayed_resolutions` names any conflict git answered from its recorded memory rather than fresh. | any |
| `conflicts` | Each contested region as two diffs, headed by the definition it sits in, plus the incoming commit's message. `context=` for more surrounding lines, `include_file_diffs=` for everything the incoming side did to each file. | any |
| `resolve` | Stages a resolution: `take="both"`/`"branch"`/`"replaying"`, edited in place, or written inline. Refuses markers, unless `allow_markers=` says the file is meant to have them. Where one side deleted the path, `take` names a side rather than a text, so taking that side stages the deletion.| any |
| `rebase_amend` | Amends — only where `HEAD` really is this step's commit. Not needed to fold a change in: staging it and calling `proceed` does that. | rebase |
| `rebase_split` | Takes this step's commit back out, changes left in the tree, to commit as several. | rebase |
| `proceed` | Carries on, by the operation's own `--continue`. Refuses while anything is unmerged, or while part of this step's commit is left outside a commit. | any |
| `skip` | Drops the commit being applied — for one already in the base. | rebase, cherry-pick, revert |
| `rebase_todo` | The steps left, and replaces them. Refuses to drop a commit. | rebase |
| `rebase_finish` | Checks the branch still makes the same change to its base, and names any commit that brought a conflict marker to a file. `tree_identical` says whether a difference is a redistribution or a loss, and a difference is read against what the run actually did — amending explains one, and so does resolving a conflict or staging something at an `edit` stop, so a rebase that did any of those is told which rather than told its own work looks like damage. When the change moved, `branch_change` names the paths whose contribution moved and by how many lines — the difference itself, not a diff between the two tips, which on a rebase onto newer upstream work is mostly the new base's own commits — and `changed_commits` names the commits that account for it — dropped, added, or altered — with `include_diff=` adding git's commit-by-commit rendering. `allow_change=`/`allow_markers=` waive either, and still report it. `fragile_locals` names any file the rewrite made local that the backup tag still tracks, because checking that tag out and coming back deletes it. A rebase started with `update_refs` also reports, per branch it was asked to carry, whether that branch actually moved — git exits zero either way, and a sibling left behind points into history the rebase replaced. | rebase started here |
| `abort` | Abandons the operation and puts back what was moved aside. | rebase, cherry-pick, revert, merge |

The prefix carries the distinction: `rebase_` is for the tools that only make
sense inside a rebase — a todo, an amend, a check against where the branch was —
and the bare names are for the ones that read or drive a conflict whatever
produced it. If a name has no prefix, it does not care how you got here.

`rebase_start` takes a `check_command`, run after every commit. It is the only
thing that catches a step which applies cleanly and still leaves the tree
broken -- a resolution that drops a line, say, so the file no longer parses.
Use it.

Add `check_edits_only=True` if the branch was not green at every commit to begin
with, which most are not: a budget or a fixture raised one commit after the code
that needed it is red in between, and a check after every commit then halts the
rebase on history that was already like that. Narrowing it to the commits you
stop at keeps the part that was wanted — prove the commits I changed are sound —
and drops the part that only rediscovers what the branch was.

## Install it

One stdio server, one command, no arguments and no environment. Put it on your
`PATH`:

```bash
uv tool install --from git+https://github.com/aaron-riact/git-rebase-mcp git-rebase-mcp
```

Then point your agent at `git-rebase-mcp`. Most harnesses take the same shape
and differ only in where the file lives and what the top-level key is called:

```json
{
  "mcpServers": {
    "git-rebase": { "command": "git-rebase-mcp" }
  }
}
```

| Harness | Where | Key |
| --- | --- | --- |
| **Claude Code** | `claude mcp add git-rebase --scope project -- git-rebase-mcp`, or `.mcp.json` in the repo | `mcpServers` |
| **Cursor** | `.cursor/mcp.json`, or `~/.cursor/mcp.json` for every project | `mcpServers` |
| **Gemini CLI** | `.gemini/settings.json`, or `~/.gemini/settings.json` | `mcpServers` |
| **Codex CLI** | `codex mcp add git-rebase -- git-rebase-mcp`, or `~/.codex/config.toml` | `[mcp_servers.git-rebase]` |
| **VS Code** (Copilot) | `.vscode/mcp.json` | `servers` |
| **Zed** | `~/.config/zed/settings.json` | `context_servers` |

The two that are not JSON-with-`mcpServers`:

```toml
# ~/.codex/config.toml
[mcp_servers.git-rebase]
command = "git-rebase-mcp"
```

```json
// .vscode/mcp.json — "servers", not "mcpServers"
{ "servers": { "git-rebase": { "type": "stdio", "command": "git-rebase-mcp" } } }
```

If the server fails to start in an editor launched from a desktop icon rather
than a shell, it is `PATH`: those processes do not read your shell profile, so
`~/.local/bin` is missing. Give the absolute path — `which git-rebase-mcp` — as
the `command`.

Every tool takes a `repo` argument, defaulting to the working directory, so one
installation serves every repository you work in.

For development, clone it and `uv sync`; `uv tool install --from . git-rebase-mcp`
installs the working tree instead of the published remote.

## Prior art

The conflict view is [DiffDiff](https://github.com/krisl/DiffDiff)'s idea. It
shows the same two diffs in vim, and its documented wishlist — commit messages
as labels, conflict counts, resolve-with-ours/theirs — anticipates most of this
tool surface. This server is that insight delivered to an agent instead of a
buffer, wrapped in the rebase state machine.

The regions are DiffDiff's too, and that matters more than it looks. Git's merge
has already decided which parts of a file could not be reconciled, and marked
exactly those; DiffDiff diffs the three sides of one such block and never sees
the rest of the file. Working the regions out independently -- diffing whole
sides against the whole base and intersecting -- re-derives that decision badly:
two independent diffs cannot know what a merge could reconcile, so a block one
side has not reached yet gets fused with a one-line change beside it, and one
side ends up with nothing to say. This server made that mistake first and
measured it: 4556 characters for one conflict, of which one side was 104 lines
of unchanged context. Asking git for the blocks instead brought the same
conflict to 289.

## Design notes

- [docs/plan.md](docs/plan.md) — the design, and what is deliberately left out.
- [docs/decisions/0001-python-rather-than-rust.md](docs/decisions/0001-python-rather-than-rust.md)
  — including the two things Rust would have done better, and how each is
  recovered here.
- [docs/decisions/0002-git-s-funcname-drivers-rather-than-tree-sitter.md](docs/decisions/0002-git-s-funcname-drivers-rather-than-tree-sitter.md)
  — why naming what a region sits inside did not need a parser, and why the
  language patterns are git's rather than this server's.

Rebase state is a closed union of four types rather than fields on one object,
so `rebase_amend` accepts one type instead of testing a set of conditions that
could drift apart from the states. `patiencediff` rather than `difflib`, because
the default matcher pairs up the wrong blocks in files that repeat — a test file
being the obvious case — and those hunk boundaries are the main output.

## Status

The rebase tools are complete and tested, and have driven the same 21-commit
branch twice. They caught two defects nothing else would have — a syntax error
committed into 8 of 10 commits, and a `fixup` whose test depended on a commit
scheduled after it.

A third run, tidying a branch whose TypeScript conversion had been split from
the feature that prompted it, produced the four fixes above: `take` staging an
empty file where the branch had renamed one away, an `edit` stop reported as "a
run of 0 fixup or squash steps", no way to divide a commit without reaching past
the tools, and a deliberately dropped commit called damage. Each was found by
using the thing, and each is now a test.

What they do not yet do is save much time: nine of eleven conflicts in the last
run were mechanical shapes resolved by a hand-written script. [Phase 2](docs/phase-2.md)
is planned against that measurement rather than against a feature list, and says
how to tell whether it worked.

## Development

```bash
uv sync
uv run pytest       # builds real repositories and runs real git against them
uv run pyright
```

Tests use scratch repositories rather than mocks. The point of the server is
that it agrees with git, so mocking git would test nothing worth testing.
