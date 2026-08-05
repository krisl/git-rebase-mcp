# git-rebase-mcp

An MCP server that makes driving a `git rebase` safe for an agent.

It exists because a rebase can corrupt history in ways git does not report. Three
that happened, in one session, on one branch:

- **Amending at a conflicted `edit` stop.** `edit` normally stops with `HEAD` on
  the commit just applied, but when it stops *because of a conflict* `HEAD` is
  still the previous commit. `git commit --amend` there silently folds two
  commits into one. Nothing in git's output distinguishes the two situations.
- **A hand-written todo list that dropped three commits.** They vanished without
  a warning.
- **Staging a file that still contained conflict markers.** Two commits shipped
  `<<<<<<<` into the tree.

So the server does two things:

1. **Refuses the unsafe operation** rather than documenting it. `rebase_amend`
   is not callable at a conflicted stop.
2. **Presents a conflict as two intents instead of marker soup** — the diff from
   the merge base to the branch so far, and the diff from the merge base to the
   commit being replayed. "Wrap this loop in `if current:`" against "replace
   `delta` with `delta_count(...)`" composes in seconds; three near-identical
   marker blocks differing by indentation and one token does not.

It also records the tip before starting and asserts at the end that the final
tree is unchanged, which is what caught all three errors above.

## Prior art

The conflict view is [DiffDiff](https://github.com/krisl/DiffDiff)'s idea. It
shows the same two diffs in vim, and its documented wishlist — commit messages
as labels, conflict counts, resolve-with-ours/theirs — anticipates most of this
tool surface. This server is that insight delivered to an agent instead of a
buffer, wrapped in the rebase state machine.

The difference in mechanism: DiffDiff parses conflict markers because it works
inside a buffer. A server does not have to. Git keeps all three sides in the
index as stages 1, 2 and 3, so the payload is read with `git show :1:path`
rather than reconstructed from text.

## Status

Early. See [docs/plan.md](docs/plan.md) for the design and
[docs/decisions/](docs/decisions/) for why it is Python.

## Development

```bash
uv sync
uv run pytest
```
