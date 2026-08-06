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
| `rebase_conflicts` | Each contested region as two diffs, plus the replayed commit's message. Only the ones that needed asking about. |
| `rebase_resolve` | Stages a resolution: `take="both"`/`"branch"`/`"replaying"`, edited in place, or written inline. Refuses markers. |
| `rebase_amend` | Amends — only where `HEAD` really is this step's commit. |
| `rebase_continue` | Carries on. Refuses while anything is unmerged. |
| `rebase_todo` | The steps left, and replaces them. Refuses to drop a commit. |
| `rebase_finish` | Checks the branch still makes the same change to its base, and scans every commit for markers. |
| `rebase_abort` | Abandons the rebase and puts back what was moved aside. |

`rebase_start` takes a `check_command`, run after every commit. It is the only
thing that catches a step which applies cleanly and still leaves the tree
broken -- a resolution that drops a line, say, so the file no longer parses.
Use it.

## Use it

```bash
uv tool install --from . git-rebase-mcp     # or: uv sync, for development
```

In Claude Code, `.mcp.json`:

```json
{
  "mcpServers": {
    "git-rebase": { "command": "git-rebase-mcp" }
  }
}
```

Every tool takes a `repo` argument, defaulting to the working directory.

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
