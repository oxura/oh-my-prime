---
name: prove
description: Turn a coding goal into a commit-bound acceptance contract, run deterministic and before/after reproducer gates inside isolated workspaces, persist an evidence ledger, and permit promotion only for the exact verified snapshot. Use for every speculative candidate and before claiming implementation success.
---

# Verifier Fabric

Verification is a separate phase from implementation. Define observable claims
before spawning candidates, then run the same contract against each candidate.
The `prove` and `workspace` modules are preloaded in IPython.

```python
contract = await prove.contract(
    "Prevent duplicate queue claims",
    requirements=[
        prove.Requirement("NO-DUP", "A job is never owned by two workers"),
        prove.Requirement("NO-LOSS", "Every queued job remains claimable"),
    ],
    invariants=["FIFO order is preserved"],
    constraints=["Existing public API remains compatible"],
    gates=[
        prove.reproducer(
            ["python", "tests/reproduce_duplicate_claim.py"],
            id="duplicate-reproducer",
            proves=["NO-DUP"],
            timeout_seconds=120,
        ),
        prove.command(
            ["npm", "run", "check"],
            id="repository-check",
            proves=["NO-LOSS", "INV-001", "CON-001"],
            timeout_seconds=900,
        ),
    ],
)

candidate = await workspace.fork(name="root-cause")
report = await prove.run(candidate, contract)
report.require_verified()
await prove.promote(candidate, report)
```

## Contract rules

- Contracts bind the goal, requirements, gates, repository identity, and exact
  base commit into a SHA-256 digest. Creation requires a clean source worktree.
- String requirements receive `REQ-001`, `REQ-002`, and so on. With no explicit
  requirements, the goal becomes `GOAL`. Invariants and constraints become hard
  requirements named `INV-001` and `CON-001`.
- Gates must identify which requirement IDs they prove. Omitting `proves` means
  the gate covers every requirement in the contract.
- With no explicit gates, a repository `npm run check` script is discovered as
  a default hard gate. Without a deterministic gate, verification is
  `incomplete`, never `verified`.
- Commands are argument arrays executed directly, never shell strings. This
  avoids shell interpolation and makes the recorded command exact.

## Gate types

- `prove.command(argv, id=..., ...)` runs only in the candidate. The default
  expectation is exit code zero.
- `prove.reproducer(argv, id=..., ...)` forks a fresh baseline workspace. By
  default the command must fail on the unchanged base and pass in the candidate.
  This proves the bug existed before the patch and no longer reproduces after it.
- Set `required=False` only for informative checks. Every hard requirement still
  needs at least one passing gate, and at least one required gate must exist.

## Evidence ledger

Each command records argv, resolved cwd, expectation, exit code, timeout state,
duration, timestamps, bounded output previews, full stdout/stderr artifacts,
and SHA-256 hashes. Reports distinguish `verified`, `failed`, and `incomplete`.

Verification snapshots the candidate before and after all gates. Any non-ignored
candidate mutation during verification invalidates the run. Promotion reloads
the persisted report, requires `status == "verified"`, checks the workspace ID,
and passes the attested patch digest to hash-gated workspace promotion.

Never treat model review as a hard gate. Use it only after deterministic gates.
Never call `prove.promote()` for a failed or incomplete report.
