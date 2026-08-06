# 0002 — Git's funcname drivers rather than tree-sitter

Status: accepted. Supersedes the tree-sitter line in [phase 2](../phase-2.md).

## The question

A contested region is placed by what it sits inside. `@@ -662,13 +662,12 @@`
says where in a file, which is not the same thing; `def counts_render(self,
ctx):` is the answer a reader wants, and getting it was one of the two jobs
tree-sitter was going to be added for.

## Why not tree-sitter

The other job — saying what *kind* of change each side made — turned out not to
need a parser. Comparing lines with their indentation stripped separates "wrapped
a block in an `if` and reindented it" from "changed a call", which was the shape
that cost the most time in the runs this server was built from, and it is now a
sentence per side.

That leaves naming the construct, and tree-sitter answers it no more generally
than the alternatives do. A grammar is needed per language either way, so
"we cannot know what language this will be used on" is settled by *whose* list of
languages you adopt, not by how good the parser is. Tree-sitter's list is longer
and its answers are exact, at the cost of a compiled parser per language and a
dependency this server would otherwise not have.

## What was built instead

Git ships twenty-five funcname drivers — ada through tex — and already applies
them to its own hunk headers. What it does not do is choose one: `diff=python`
is something a repository states in `.gitattributes`, and most repositories never
do. Git's fallback then recognises a definition at column 0 only, which in any
language whose definitions nest names the class every time and the method never.

So the choice of driver is made here, from the file's extension, and nothing
else is: a marker is inserted at each region, git is asked to diff the file
against itself, and the funcname on each hunk header is the answer for the
region whose marker that hunk contains. A repository that has stated its own
driver — including a custom one it defined itself — is asked first and obeyed.

## Why that is the better trade

**The patterns are not ours.** An earlier attempt kept two regexes here, for
Python and Ruby, which is the version of this that does not scale: every
language nobody anticipated silently got the column-0 fallback. Kotlin is the
example that settled it — git's driver says `fun countsRender(ctx: Ctx):
List<Row> {` where the fallback says `class Report {`, and no regex written here
would ever have covered it.

**A wrong guess is harmless.** The table maps extensions to driver *names*.
Git ignores a name it does not recognise and falls back, so the failure mode of
a bad entry is the answer we would have had anyway — not a mislabelled region.

**It costs two subprocesses per conflicted file**, where the previous version
was pure in-process scanning. `read_conflict` already writes temp files and
shells out to `merge-file` per file, and to `blame` per region, so this is
within the order of what the module already spends.

## When to revisit

If naming the *chain* of enclosing constructs proves necessary — "in
`counts_render`, inside the `for` over `PACKAGES`" rather than just the
definition — git cannot answer that and tree-sitter can. Nothing measured so far
has asked for it.
