---
name: workspace
description: Create isolated Git worktrees for competing implementations, inspect immutable candidate diffs, promote only an unchanged verified snapshot, and discard losing branches. Use whenever agents must edit independently or a patch needs verifier-gated promotion.
---

# Isolated Workspace Manager

Run speculative implementations outside the user's worktree. The `workspace`
module is preloaded in IPython:

```python
candidate = await workspace.fork(name="root-cause")
print(candidate.path)

snapshot = await workspace.diff(candidate)
print(snapshot.files)
print(snapshot.patch_sha256)

# Verify exactly this snapshot, then gate promotion with its digest.
result = await workspace.promote(
    candidate,
    expected_patch_sha256=snapshot.patch_sha256,
)
await workspace.discard(candidate)
```

## Invariants

- `fork()` requires a clean Git worktree. Uncommitted source changes are never
  silently omitted from a candidate.
- Each candidate is a real Git worktree on an `omp/workspace/*` branch, stored
  under the user's Oh My Prime state directory rather than inside the project.
- `diff()` captures tracked, staged, committed-since-fork, deleted, renamed, and
  non-ignored untracked files. It returns the binary-safe patch, changed paths,
  and a SHA-256 digest.
- `promote()` requires the target to be clean and still at the candidate's exact
  base commit. Pass the verified `patch_sha256`; mutation after verification is
  rejected. The applied patch remains unstaged in the target worktree.
- Promotion patches and manifests are persisted as audit artifacts. A promoted
  candidate remains available until explicitly discarded.
- `discard()` is destructive by design: it force-removes only the managed
  candidate worktree and its private `omp/workspace/*` branch.

## API

- `await workspace.fork(repo=".", name=None, base="HEAD") -> Workspace`
- `await workspace.get(handle_or_id, repo=".") -> Workspace`
- `await workspace.list(repo=".") -> list[Workspace]`
- `await workspace.diff(handle_or_id, repo=".") -> WorkspaceDiff`
- `await workspace.promote(handle_or_id, repo=None, expected_patch_sha256=None) -> PromotionResult`
- `await workspace.discard(handle_or_id, repo=".") -> Workspace`

Give a child agent the candidate path explicitly and require all reads, edits,
and commands to stay inside it. Never let two implementers share a candidate.
Do not promote a snapshot until independent verification has completed.
