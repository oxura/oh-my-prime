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

## Persistence and correctness

The graph is stored outside the repository in a repository-keyed SQLite
Database. Builds collect all records first and replace the visible graph in one
`BEGIN IMMEDIATE` transaction, so readers see either the previous complete
index or the next complete index, never a half-built graph. Git-ignored files,
symlinks, binary/invalid UTF-8 source, and source files larger than 2 MiB are not
parsed as code.

`BuildReport.changed_files` and `removed_files` compare content hashes with the
previous index. Use `atlas.stats()` to inspect the indexed revision and graph
cardinality.

## Query API

- `await atlas.files(query="", language=None, limit=50)`
- `await atlas.symbols(query="", kind=None, limit=50)`
- `await atlas.symbol(query)` — requires one unambiguous match
- `await atlas.references(symbol, kinds=(...), limit=200)`
- `await atlas.outgoing(symbol, kinds=(), limit=200)`

Build or refresh the graph before relying on it for a load-bearing refactor.
Context capsules and stale-index checks are layered on this same content-
addressed graph.
