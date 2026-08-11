---
name: prime
description: "Run ProofTree speculative exploration: fork independent Git workspaces, assign deliberately different implementation strategies to recursive agents, apply one acceptance contract to every branch, select only a fully verified candidate, and promote its exact attested patch. Use for risky bugs, ambiguous implementations, concurrency work, and any task where one trajectory is not trustworthy enough."
---

# ProofTree

ProofTree implements the Oh My Prime loop:

```text
Explore -> Prove -> Promote
```

Create the acceptance contract before exploration. The `prime`, `prove`, and
`workspace` modules are preloaded in IPython:

```python
contract = await prove.contract(
    goal,
    requirements=[...],
    gates=[...],
    invariants=[...],
)

run = await prime.explore(
    goal,
    contract=contract,
    candidates=4,
    strategies=[
        "root-cause",
        "minimal-fix",
        "adversarial",
        "architectural",
    ],
)

# Child agents execute independently in their own complete session cwd.
await run.wait(timeout_seconds=3600)
winner = await run.best_verified(max_parallel=2)
print(winner.candidate.strategy.name, winner.score, winner.report.report_path)
result = await winner.promote()
```

`await run.promote_best()` combines wait, verification, selection, and promotion.

## Exploration invariants

- Candidate count is 2–8. Every candidate receives a separate managed Git
  worktree and a distinct strategy.
- The RLM host starts the child's complete session runtime in that candidate
  path; tools, IPython, shell commands, system prompt cwd, and session metadata
  all agree on the isolated workspace.
- Built-in strategies are `root-cause`, `minimal-fix`, `adversarial`,
  `architectural`, `test-first`, `compatibility-first`, `concurrency-first`, and
  `simplification`. Pass `prime.Strategy(name, instructions)` for a custom path.
- Candidate prompts include the immutable contract and forbid changing verifier
  inputs, switching branches, or touching the source worktree.
- `models` may be empty (inherit), one exact selector for every child, or one
  selector per candidate. Semantic Model Mesh routes supersede this low-level
  option when configured.

## Verification and selection

ProofTree ignores implementer confidence and final prose. It waits for terminal
child state, then independently runs the same Verifier Fabric contract in every
candidate. An errored/missing child or failed/incomplete report cannot win.

Among fully verified branches, the deterministic lower-is-better score is:

1. fewer failed optional gates;
2. fewer changed files;
3. fewer patch bytes;
4. lower verifier duration;
5. original candidate order as a stable tie-breaker.

If no branch is verified, `best_verified()` raises `NoVerifiedCandidate` and
leaves every workspace intact for diagnosis. It never promotes the least-bad
failure.

Promotion reloads the evidence ledger and applies only its attested patch hash.
By default losing workspaces are discarded after successful promotion; the
winner remains for audit until explicitly discarded.

## Recovery

Every run is persisted after each admitted child and lifecycle transition. The
Prime child registry and workspace manifests are durable too:

```python
run = await prime.load(run_id, repo=".")
statuses = await run.statuses()
winner = await run.best_verified()  # gates rerun after recovery
```

A partial spawn raises `ExplorationStartError` carrying `run_id`; recover that
run instead of starting duplicate children.
