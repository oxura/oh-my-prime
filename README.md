<h1 align="center">Oh My Prime</h1>

<p align="center">
  Verified Recursive Agent Runtime
</p>

<p align="center">
  <a href="packages/coding-agent/docs/index.md">Documentation</a> &bull;
  <a href="packages/coding-agent/docs/rlm.md">RLM runtime</a> &bull;
  <a href="https://github.com/PrimeIntellect-ai/prime-agent">Upstream Prime Agent</a>
</p>

<p align="center">
  <a href="https://github.com/oxura/oh-my-prime/actions/workflows/ci.yml">
    <img src="https://github.com/oxura/oh-my-prime/actions/workflows/ci.yml/badge.svg" alt="CI" />
  </a>
</p>

Oh My Prime is a fork of [Prime Agent](https://github.com/PrimeIntellect-ai/prime-agent) built around one loop:

```text
Explore -> Prove -> Promote -> Learn
```

The inherited Prime Agent core provides a persistent IPython control plane, recursive agents, daemon-backed sessions, direct agent messaging, persistent goals, and a continual harness. Oh My Prime adds a verification layer that explores competing implementations in isolated workspaces, runs deterministic acceptance contracts, promotes only the exact verified patch, and learns only from evidence that remains valid.

This is not a collection of extra model tools. The new capabilities are Python modules inside the same persistent control plane:

- **ProofTree:** runs deliberately different strategies in independent Git worktrees, verifies every candidate against one immutable contract, and never selects a least-bad failure.
- **Verifier Fabric:** binds requirements and reproducer or command gates to the repository revision, records deterministic evidence, and gates promotion on the exact candidate patch hash.
- **Isolated Workspace Manager:** creates durable candidate worktrees, produces binary-safe patch snapshots, detects stale branches, and applies only an attested patch.
- **Model Mesh:** routes semantic roles such as `fast`, `code`, `deep`, and `review` using authenticated capabilities, cost, local verified outcomes, and maker/checker independence.
- **Code Atlas and Context Compiler:** maintain a content-addressed semantic graph, compile budget-bounded context with provenance, report transitive impact, and reject stale source snapshots.
- **Evolution Lab:** keeps hypotheses separate from verified knowledge and requires replay, shadow evaluation, and transactional promotion before changing active harness memory.
- **Flow Runtime:** persists dependency-aware task graphs with typed content-addressed artifacts, retries, budgets, resource locks, quorum joins, cancellation, and restart recovery.
- **Capability Sandbox:** gives every RLM child an immutable, non-widening filesystem, network, secret, and process manifest. Supported hosts enforce it through Sandbox Runtime and OS primitives; child startup fails closed when enforcement cannot be established.

## Quick Start

Requirements: Node.js 22 or newer, npm 11.10 or newer, Git, and Python 3.10 or newer.

Linux child sandboxes also require `ripgrep`, `bubblewrap`, and `socat` from the system package manager (for example, `sudo apt install ripgrep bubblewrap socat` or `sudo dnf install ripgrep bubblewrap socat`). macOS uses the built-in sandbox runtime.

```bash
git clone https://github.com/oxura/oh-my-prime.git
cd oh-my-prime
npm install
./ompr
```

Release installs expose the `ompr` command. The repository launcher is `./ompr`; inherited `prime-agent` configuration paths remain unchanged. On first launch, use `/login` to configure a subscription or API-key provider.

## ProofTree Example

`prime`, `prove`, and `workspace` are preloaded in the persistent IPython kernel:

```python
contract = await prove.contract(
    "Prevent duplicate queue claims",
    requirements=[
        prove.Requirement("NO-DUP", "A job is never owned by two workers"),
        prove.Requirement("NO-LOSS", "Every queued job remains claimable"),
    ],
    gates=[
        prove.reproducer(
            ["python", "tests/reproduce_duplicate_claim.py"],
            id="duplicate-reproducer",
            proves=["NO-DUP"],
        ),
        prove.command(
            ["npm", "run", "check"],
            id="repository-check",
            proves=["NO-LOSS"],
        ),
    ],
)

run = await prime.explore(
    "Prevent duplicate queue claims",
    contract=contract,
    candidates=4,
    strategies=["root-cause", "minimal-fix", "adversarial", "concurrency-first"],
)

await run.wait(timeout_seconds=3600)
winner = await run.best_verified(max_parallel=2)
result = await winner.promote()
```

Every candidate receives a separate complete session workspace. The checker receives the contract, immutable diff, command output, and evidence ledger rather than the implementer's confidence. If no candidate is fully verified, `best_verified()` raises and preserves the branches for diagnosis.

## Durable Orchestration

Use Flow when recursive work has dependencies or must survive a restart:

```python
run = await flow.create(
    "Migrate the service safely",
    budget=flow.FlowBudget(
        max_attempts=20,
        max_failures=4,
        max_tokens=250_000,
        wall_time_seconds=3600,
    ),
    max_parallel=4,
)

audit = await run.task(
    "Audit the architecture",
    route="fast",
    produces=(flow.ArtifactSpec("architecture", required_keys=("modules", "risks")),),
)
design = await run.task(
    "Design the migration",
    route="deep",
    requires=(audit.id,),
    consumes=("architecture",),
    produces=(flow.ArtifactSpec("plan", required_keys=("steps",)),),
)

result = await run.run()
```

Flow treats terminal model output as untrusted until every declared artifact exists and validates. Succeeded tasks are never rerun during recovery.

## Security Boundary

The top-level session still executes model-generated Python and project commands with the current user's permissions. Capability manifests apply to RLM child kernels; host-side extensions and custom tools remain outside that boundary. Use trusted repositories and extensions, inspect promoted patches, and use a stronger external sandbox for hostile host-side code.

The default child manifest grants read and write access only to the selected child workspace, denies outbound domains and inherited secrets, limits the process tree, and expires the child after a bounded wall time. Nested children may narrow that manifest but cannot widen it.

## Documentation

- [Quickstart](packages/coding-agent/docs/quickstart.md)
- [Usage and CLI reference](packages/coding-agent/docs/usage.md)
- [RLM programming model and capability manifests](packages/coding-agent/docs/rlm.md)
- [Isolated Workspace Manager](packages/coding-agent/skills/workspace/SKILL.md)
- [Verifier Fabric](packages/coding-agent/skills/prove/SKILL.md)
- [ProofTree](packages/coding-agent/skills/prime/SKILL.md)
- [Model Mesh](packages/coding-agent/skills/models/SKILL.md)
- [Code Atlas](packages/coding-agent/skills/atlas/SKILL.md)
- [Evolution Lab](packages/coding-agent/skills/evolve/SKILL.md)
- [Flow Runtime](packages/coding-agent/skills/flow/SKILL.md)
- [Long-running agents](packages/coding-agent/docs/long-running-agents.md)
- [Architecture](packages/coding-agent/docs/architecture.md)
- [Development](packages/coding-agent/docs/development.md)

## Acknowledgements

Oh My Prime is based on [Prime Agent](https://github.com/PrimeIntellect-ai/prime-agent), which is built on [`pi`](https://github.com/earendil-works/pi). The upstream projects remain the source of the persistent IPython, recursive-agent, daemon, session, and terminal foundations.

## License

Oh My Prime is released under the [MIT License](LICENSE).
