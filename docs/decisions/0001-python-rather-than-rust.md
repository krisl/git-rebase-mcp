# 0001 — Python rather than Rust

Status: accepted. A port stays open; see the closing note.

## The question

The server shells out to git, parses its output and the files under
`.git/rebase-merge/`, computes diffs, and serves MCP over stdio. Phase 2 adds
tree-sitter. Both languages can do all of that well, so the choice came down to
which risks matter for a tool whose entire value is being trusted.

## Where Rust genuinely wins

**Modelling the state machine.** This is the strongest argument, and it lands
directly on the bug that motivated the project. In Rust the rebase stop is an
enum:

```rust
enum Stop {
    Applied { head: Commit },
    Conflicted { replaying: Commit, head: Commit, unmerged: Vec<PathBuf> },
}
```

`amend` accepts `Stop::Applied` only, so amending at a conflicted stop is a
compile error rather than a runtime refusal. Unrepresentable beats rejected.

**Diff quality.** The `similar` crate implements patience and Myers diffs.
Python's stdlib `difflib.SequenceMatcher` uses an LCS variant that aligns badly
on repetitive code — a file of ninety near-identical test functions is its
pathological case, and that is precisely the file this server will be asked
about. Bad alignment means bad hunk boundaries, and hunk boundaries are the
core output.

**tree-sitter is native.** Written in Rust; grammars are crates; no FFI.

**Distribution.** A single static binary with no runtime dependency.

## Where Python genuinely wins

**The work is mostly subprocess and text handling.** Parsing `git` output,
reading `.git/rebase-merge/*`, assembling diffs. Python is faster to write and
easier to read for that, and there is no compute in the hot path.

**Testing ergonomics, which matter unusually much here.** The regression tests
*are* the specification, and each one builds a scratch repository with a
specific conflict topology. `pytest` with `tmp_path` and `subprocess` is about
as good as this gets; `tempfile` plus `assert_cmd` is workable but several times
the boilerplate for the same fixtures. A project whose value is its test suite
should be written where tests are cheapest.

**MCP SDK maturity.** The Python SDK is the most established binding.
Confidence in the Rust SDK's current state was lower at the time of writing,
and adopting a stack that cannot be vouched for is itself a risk.

**The tree-sitter advantage has largely evaporated.**
`tree-sitter-language-pack` ships prebuilt wheels for around a hundred
grammars, so the old "Rust avoids compiling grammars" argument no longer
applies.

**Ecosystem fit.** Across the author's projects: 4564 Python files, and no Rust
at all.

## The deciding factor

Maintenance surface. A tool that exists to be trusted has to stay readable to
whoever owns it. Introducing the only Rust in the ecosystem means that the first
time it misbehaves it is a black box, which defeats the purpose.

## Consequences

Two of Rust's advantages are worth recovering deliberately, and both are
requirements on the implementation rather than nice-to-haves:

1. **Model rebase state as a sealed union**, not an open dictionary — a
   discriminated dataclass hierarchy, `pyright --strict`, and `assert_never` in
   every match. That gives exhaustiveness at type-check time. It is not
   compile-time-unrepresentable, but the tools refuse at runtime regardless, so
   the practical gap is small.
2. **Use `patiencediff` rather than `difflib`** for collision units. This is
   not polish. On files with many similar blocks it is the difference between
   correct and misleading hunk boundaries.

## When to revisit

If this is ever distributed to people who do not have Python, the static-binary
argument wins. The server is on the order of 1000–1500 lines, so a port is
cheap, and it would be done already knowing what the thing should do.
