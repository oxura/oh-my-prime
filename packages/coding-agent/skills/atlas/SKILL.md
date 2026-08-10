---
name: atlas
description: Build and query a durable semantic graph of repository files, symbols, imports, calls, references, inheritance, and parse diagnostics. Use before broad code changes, when locating exact symbols and callers, and when compiling minimal task context.
---

# Code Atlas

Code Atlas turns a repository into a queryable semantic graph instead of a bag
of text chunks. The `atlas` module is preloaded in IPython:

```python
report = await atlas.build(".")
print(report.indexed_files, report.symbols, report.edges)

target = await atlas.symbol("AuthService.rotateToken")
callers = await atlas.references(target, kinds=["calls"])
dependencies = await atlas.outgoing(target)
```

## Indexed nodes

- every Git-tracked regular file, with language, SHA-256 content hash, size, and
  parser status;
- Python functions, async functions, methods, and classes from the standard
  Python AST;
- TypeScript/JavaScript functions, variables, classes, interfaces, types,
  enums, methods, constructors, and properties from the project's TypeScript
  compiler API;
- stable symbol keys, qualified names, source spans, export status, and
  signature hashes.

## Semantic edges

- `imports` between modules/files;
- `calls` resolved through the TypeScript type checker or Python local symbols;
- `references` to declarations;
- `extends` and `implements` inheritance edges.

TypeScript extraction uses the project's own authenticated dependency-free
compiler installation, so its module resolution and `tsconfig.json` match the
project. If the compiler API is unavailable, Atlas uses an explicit low-
confidence fallback scanner and marks affected files `fallback`; it never
presents fallback edges as fully resolved AST evidence.

## Context Compiler

Compile a minimal, task-specific capsule instead of sending a repository dump:

```python
capsule = await atlas.compile_context(
    "Fix refresh-token rotation without changing the public API",
    contract=acceptance_contract,
    roots=["AuthService.rotateToken"],
    paths=["tests/auth/"],
    token_budget=18_000,
)
child_context = capsule.render()
```

The compiler combines explicit roots, task and contract terms, dirty tracked
files, callers, callees, references, and import neighbors. It ranks those
signals, merges overlapping source spans, and packs the strongest excerpts
under the hard token budget. Every item carries:

- its repository-relative source and exact line range;
- source-file and excerpt SHA-256 hashes;
- filesystem update time;
- the reason it was included and its relation to the task;
- the stable semantic symbol keys represented by the excerpt.

The capsule records aggregated unrelated-file exclusions and explicit
budget-trimmed excerpts. `capsule.render()` includes the task, acceptance
contract, evidence metadata, source excerpts, and exclusions in one child-ready
document.

Capsules are persisted atomically outside the repository. Their IDs attest the
full payload, and loading rejects a changed payload or a capsule from another
repository:

```python
same = await atlas.load_capsule(capsule.id)
status = await atlas.capsule_freshness(same)
assert status.fresh
```

Compilation checks the indexed commit and content hashes first. By default,
stale tracked sources trigger an automatic graph rebuild. Pass
`auto_refresh=False` when a stale graph must fail closed instead.

## Impact Analysis and hash-gated patches

Analyze the exact unified diff before editing the worktree:

```python
report = await atlas.impact(proposed_diff, max_depth=3)
print(report.render())

await atlas.require_fresh_impact(report)
result = await atlas.apply_impact(report)
```

Impact Analysis validates that the patch applies, maps old-side hunk ranges to
changed declarations, and traverses incoming calls, references, inheritance,
and file/import edges. Reports separate:

- directly changed and transitively impacted symbols;
- touched public API declarations;
- affected source, test, documentation, configuration, and migration files;
- unresolved static targets and explicit analysis limitations;
- a deterministic risk score derived from API surface, dependents, deletions,
  missing connected tests, migrations, configuration, and unresolved targets.

The report and exact patch are persisted atomically and content-attested.
`impact_freshness()` checks the repository HEAD, Atlas snapshot, every target
file hash, and patch applicability. `apply_impact()` repeats those checks under
a repository-scoped lock immediately before `git apply`, then records resulting
content hashes. A stale report fails without writing.

Only text patches are admitted. Binary, symlink, and submodule patches are
rejected. Static impact is evidence, not proof of reflective, generated,
data-driven, or runtime-only dependencies; those limitations remain visible in
the report.

## Persistence and correctness

The graph is stored outside the repository in a repository-keyed SQLite
database. Builds collect all records first and replace the visible graph in one
`BEGIN IMMEDIATE` transaction, so readers see either the previous complete
index or the next complete index, never a half-built graph. Git-ignored files,
symlinks, binary/invalid UTF-8 source, and source files larger than 2 MiB are not
parsed as code.

`BuildReport.changed_files` and `removed_files` compare content hashes with the
previous index. Use `atlas.stats()` to inspect the indexed revision and graph
cardinality, and `atlas.freshness()` to compare it with the current tracked
worktree. An index rebuilt from deliberate dirty changes remains fresh until
those bytes change again.

## Query API

- `await atlas.files(query="", language=None, limit=50)`
- `await atlas.symbols(query="", kind=None, limit=50)`
- `await atlas.symbol(query)` — requires one unambiguous match
- `await atlas.references(symbol, kinds=(...), limit=200)`
- `await atlas.outgoing(symbol, kinds=(), limit=200)`
- `await atlas.freshness()`
- `await atlas.compile_context(task, contract=..., roots=..., paths=...)`
- `await atlas.load_capsule(capsule_id)`
- `await atlas.capsule_freshness(capsule)`
- `await atlas.impact(proposed_diff, max_depth=3)`
- `await atlas.load_impact(report_id)`
- `await atlas.impact_freshness(report)`
- `await atlas.require_fresh_impact(report)`
- `await atlas.apply_impact(report)`

Build or refresh the graph before relying on direct graph queries for a
load-bearing refactor. `compile_context()` performs that freshness check
automatically.
