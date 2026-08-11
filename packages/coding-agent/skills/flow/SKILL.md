---
name: flow
description: Build and run durable dependency-aware task graphs with typed content-addressed artifacts, resource coordination, retries, budgets, quorum joins, cancellation, and restart recovery. Use for multi-stage recursive work that must survive process failure and preserve machine-readable intermediate results.
---

# Durable Flow Runtime

Use Flow when work has real dependencies or must resume after a kernel restart.
The `flow` module is preloaded in IPython:

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
    "Audit the current architecture",
    prompt="Publish a machine-readable architecture map.",
    route="fast",
    produces=(
        flow.ArtifactSpec(
            "architecture",
            value_type="object",
            required_keys=("modules", "risks"),
        ),
    ),
)

design = await run.task(
    "Design the migration",
    prompt="Use the architecture artifact and publish a migration plan.",
    route="deep",
    requires=(audit.id,),
    consumes=("architecture",),
    produces=(flow.ArtifactSpec("plan", required_keys=("steps",)),),
)

implementations = await run.map(
    "Implement modules",
    ["auth", "billing", "queue"],
    prompt=lambda module: f"Implement the verified plan for {module}.",
    worker="implement-module",
    route="code",
    requires=(design.id,),
    resources=lambda module: (f"module:{module}",),
    produces=(flow.ArtifactSpec("patch", required_keys=("module", "files")),),
)

integration = await run.task(
    "Integrate verified modules",
    prompt="Integrate every successful module patch and publish the result.",
    route="deep",
    requires=tuple(task.id for task in implementations),
    quorum="all",
    consumes=("patch",),
    produces=(flow.ArtifactSpec("integration", required_keys=("status",)),),
)

result = await run.run()
print(result.status, result.total_attempts, result.total_tokens)
```

## Execution invariants

- The graph is persisted before execution and after every task transition.
  `await flow.load(run.id)` resets interrupted `running` attempts to `pending`
  but never reruns a `succeeded` task.
- Dependencies are task IDs. `quorum="all"`, `"any"`, or a positive integer
  determines when a join may run. A join whose quorum is impossible becomes
  `blocked` instead of deadlocking the scheduler.
- Tasks with an overlapping resource name never execute concurrently. Independent
  tasks run up to `max_parallel`.
- `TaskPolicy` bounds attempts, each attempt's timeout, and retry backoff. Flow
  budgets independently bound total attempts, failures, provider-reported model
  tokens, and wall time. A token budget fails closed when child usage is absent;
  exhaustion is persisted before the flow stops.
- Cancellation settles direct RLM children before retry or completion. An
  unreachable child is persisted as unsettled and fails the flow rather than
  risking duplicate work. No completed output is inferred from prose or a child
  exit status.

## Typed artifact blackboard

Each declared output is validated against its `ArtifactSpec`, encoded as
canonical JSON, size-limited, and staged under a private attempt directory.
Content-addressed artifacts become queryable only after a manifest and the
successful task transition commit. Artifact metadata binds the flow, producer
task, name, type, path, digest, and creation time. Every read revalidates both
metadata and payload.

```python
artifacts = await run.artifacts(name="patch")
patch = await run.blackboard.get(artifacts[0].id)
```

The default RLM executor grants each child only the selected repository and its
private attempt directory, then supplies an exact output path and JSON contract.
A successful child must atomically write every declared output there; a terminal
child without a valid file is a failed attempt. Tests and local programs may
inject a `TaskExecutor` implementation that returns `flow.TaskExecution`
directly.

## Recovery and idempotency

```python
recovered = await flow.load(run.id, repo=".")
status = await recovered.status()
result = await recovered.run()
```

Retries create new attempts for incomplete tasks only. Recovery first reconciles
the durable child ID, terminal status, usage, and deletion state; it never starts
a replacement while the prior child may still be live. Published artifacts are
immutable and content-addressed. Use stable task IDs when rebuilding a graph;
duplicate IDs are rejected rather than silently creating duplicate work.
