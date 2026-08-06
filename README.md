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

**Offers the resolution rather than making it.** `rebase_resolve` takes
`take="both" | "branch" | "replaying"` for the cases the two diffs make obvious,
so answering costs one call instead of sending a whole file back. `auto_resolve`
will compose conflicts where the two sides touched different lines and carry on
without stopping, but it is **off by default**: lines that do not overlap can
still contradict each other — one side adding a call, the other removing the
helper it needs — and a conflict resolved without being read has to be reviewed
afterwards anyway.

**Records where the branch was, and checks the result against it.** What must
stay the same is the change the branch makes to its base -- not the resulting
tree, which changes for good reason when the rebase also moves onto newer
upstream work. A difference is a report of damage. This is what caught all
three errors above.

## Tools

| Tool | |
| --- | --- |
| `rebase_preflight` | What a rebase would do. Changes nothing. Names commits a todo would drop. |
| `rebase_start` | Tags the tip, moves aside colliding untracked files, begins. `autosquash` folds `fixup!` commits in. |
| `rebase_status` | Typed state, and whether `HEAD` is the commit being replayed. |
| `rebase_conflicts` | Each contested region as two diffs, headed by the definition it sits in, plus the replayed commit's message. `context=` for more surrounding lines, `include_file_diffs=` for everything the replayed commit did to each file. |
| `rebase_resolve` | Stages a resolution: `take="both"`/`"branch"`/`"replaying"`, edited in place, or written inline. Refuses markers. |
| `rebase_amend` | Amends — only where `HEAD` really is this step's commit. |
| `rebase_continue` | Carries on. Refuses while anything is unmerged. |
| `rebase_skip` | Drops the commit being replayed — for one already in the base. |
| `rebase_todo` | The steps left, and replaces them. Refuses to drop a commit. |
| `rebase_finish` | Checks the branch still makes the same change to its base, and scans every commit for markers. |
| `rebase_abort` | Abandons the rebase and puts back what was moved aside. |

`rebase_start` takes a `check_command`, run after every commit. It is the only
thing that catches a step which applies cleanly and still leaves the tree
broken -- a resolution that drops a line, say, so the file no longer parses.
Use it.

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
