# Phase 2 — measured against two real rebases

## What this is for

The test is not "does the server work". It works: 138 tests, and across two real
rebases of a 21-commit branch it caught two defects that nothing else would have.
The test is whether it makes the work **faster and more confident**, and on the
first of those it is currently only half earning its place.

The evidence below is from driving the same branch twice, once by hand and once
through the tools.

## What already earns its place

| | Evidence |
| --- | --- |
| `rebase_finish` | Caught a stray line that made the test file a syntax error in 8 of 10 commits. Every commit looked fine; the rebase reported success. |
| `check_command` | Caught an incomplete resolution immediately -- a `fixup` whose test needed a commit scheduled later. Applies cleanly, looks right, only running it finds it. |
| `rebase_preflight` | Caught a dropped commit *and* an untracked-file collision in one call, both of which had already caused damage by hand. |
| auto-stash | Restored `pytest.ini` byte-identical, twice. Destroyed by hand on the first attempt. |
| `git_said` | Turned an unexplained stop into an instant diagnosis. |

The pattern: **refusing and checking earn their place; presenting competes with
bash and often loses.**

## What actually cost time

Counted from the second run, in rough order of cost.

1. **Mechanical conflicts resolved by hand.** Nine of the eleven conflicts were
   one of two shapes: "the branch has not reached these lines yet, and the
   replayed commit appends beside them", or "add one input and one env line to a
   YAML file". A `difflib` helper was written three times to do it. This is the
   single largest cost and it is entirely automatable.

2. **Re-resolving the same conflicts across runs.** The two runs hit identical
   conflicts. `rerere` would have replayed every one of them for free. The
   original plan called for enabling it; it was never implemented.

3. **Dropping to bash for literal text.** Even after the payload was fixed, the
   text needed to compose a resolution often had to come from `git show :2:path`.
   The tool that shows the conflict cannot yet apply the obvious composition.

4. **Discovering an ordering problem at step 31.** Fixed since, with
   `rebase_todo`, but it needed reaching past the tools to hand-edit
   `.git/rebase-merge/git-rebase-todo` first.

## What erodes confidence

**Both runs ended with `rebase_finish` reporting a difference that was
cosmetic** -- the same lines in a different order. The check cannot tell a
reordering from damage, so it cried wolf twice out of two. That is precisely the
failure mode this project exists to avoid, now present in its own headline
check.

## The work, in order

### 1. Enable rerere (small, immediate)

`rebase_start` sets `rerere.enabled` for the run and records whether it was
already on. Every re-run of a rebase then replays resolutions already made.
Cheap, and it directly addresses cost #2.

### 2. Resolve the decidable conflicts without asking (largest win)

Two cases need no language knowledge and cover most of what was done by hand:

- **Append-only.** One side adds lines past the end of the base; the other has
  not reached them. The union is the answer.
- **Disjoint insertions.** Both sides insert at different points in the base with
  no overlap.

Resolve these during `rebase_continue`, report them as resolved-automatically
rather than silently, and leave anything else to the caller. Guard it with the
existing `check_command`: an automatic resolution that fails the check should
stop the rebase, not be trusted.

This addresses cost #1 and is worth more than everything below it.

### 3. Make `rebase_finish` distinguish reordering from damage

Compare the branch's contribution as a multiset of changed lines per file as
well as by patch-id. Same lines in a different order should be reported as
"reordered, content identical" rather than as a difference. Restores the check's
credibility, which is the reason to have it at all.

### 4. Compose a resolution from the payload

`rebase_resolve(path, take="both" | "branch" | "replaying")` for the cases where
the composition is obvious from the two diffs. Removes the round trip through
`git show` (cost #3) for everything short-of-structural.

### 5. Structural conflicts, with tree-sitter

Only after 2--4. Node-disjoint edits -- a reindent beside a token change --
resolved without asking. Must be a lossless CST, not a Python `ast`: these files
are comment-heavy and several real conflicts *were* comment blocks. Reformatting
would alter the branch's contribution and trip the check in #3.

The reason this is last: (2) covers the common mechanical cases with no parsing
at all, and until that is done there is no evidence about what structural cases
remain.

## Not doing yet

`split_commit` and `absorb`. Both were on the original list; neither has come up
in real use. They stay speculative until something demands them.

## Result of the third run

Same 21-commit branch, same todo, driven again after items 1--4.

| | Second run | Third run |
| --- | --- | --- |
| Conflicts needing a hand-written resolution | 9 | **2** |
| Conflicts composed without asking | 0 | **4** (5 files) |
| Conflicts resolved by naming a side | 0 | **2** |
| False alarms from `rebase_finish` | 1 of 1 | **0** |

The two that still need a hand are the genuinely structural pair -- one side
wrapped a loop in an `if` and reindented it while the other swapped a call
inside it -- which is exactly the case the two-diff payload was built for, and
where a person should be looking anyway.

The run also fed back a fix. The first attempt composed only one conflict,
because every insertion was widened to a line for the overlap test and so an
insertion at the *boundary* of another edit read as a collision. That is the
common shape, not an edge case: it took the count from one to five.

## What would move it further

Only one thing is now worth measuring: a rule for two insertions at the same
point. Both cases in this run were "the branch appended tests and so did the
replayed commit", and `take="both"` was right both times. Applying it
automatically is tempting and wrong -- nothing in the text says which order was
meant -- but reporting the shape, so a caller can answer in one call instead of
inspecting first, would remove the last mechanical step.

Structural conflicts with tree-sitter remain last, and the case for them is now
weaker: after this, the only conflicts reaching a person are ones a person
should see.
