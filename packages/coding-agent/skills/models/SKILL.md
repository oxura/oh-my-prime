---
name: models
description: Resolve semantic model routes such as fast, code, deep, review, vision, long-context, private-local, and max against authenticated model capabilities and verifier-backed local outcomes. Use instead of hard-coding provider/model IDs in reusable RLM orchestration.
---

# Model Mesh

Model Mesh decouples orchestration from provider model names. The bundled
`models` module registers itself as the resolver behind `rlm(..., route=...)`:

```python
child = await rlm(
    "Implement the verified candidate",
    route="code",
    task_type="typescript-refactor",
    effort="xhigh",  # explicit value overrides the route default
    cwd=str(candidate.path),
)
print(child.model, child.effort)
```

Reusable orchestration should name a route, not a vendor model. The host receives
only the exact authenticated selector chosen by Model Mesh.

## Built-in routes

- `fast` — low effort, favors low-cost mini/flash/haiku-class models.
- `code` — high effort, favors coding-specialized models.
- `deep` — xhigh effort and reasoning capability.
- `review` — high effort and reasoning capability.
- `vision` — requires image input.
- `long-context` — requires at least 128k context.
- `private-local` — requires a model identified as local; configure exact
  selectors when a custom local provider name is not recognizable.
- `max` — maximum effort, reasoning required, low exploration weight.

Inspect or override policies durably:

```python
await models.routes()
await models.configure(
    "code",
    candidates=[
        "openai-codex/*",
        "anthropic/claude-*",
    ],
    effort="high",
    exploration_weight=0.15,
)
selection = await models.resolve("code", task_type="typescript-refactor")
```

Candidate patterns use case-insensitive glob matching against exact
`provider/model` selectors. Explicit order contributes to ranking but does not
silence verified outcome history.

## Scheduler inputs

Resolution considers:

- authenticated availability;
- reasoning, vision, context-window, and local-only requirements;
- explicit route priority;
- route/model-name affinity;
- token price for cost-sensitive routes;
- a Bayesian verified-success posterior;
- a bounded exploration bonus for under-sampled models.

No model self-rating enters the score.

## Capability learning

Only objective verifier outcomes should update routing policy:

```python
selection = await models.resolve("code", task_type="queue-concurrency")
# Spawn with selection.selector, implement, then run Verifier Fabric.
await models.record(
    selection,
    verified=report.verified,
    duration_ms=report.duration_ms,
)
```

Outcomes are keyed by route, task type, and exact selector. Model changes do not
require changing RLM programs, and a newly authenticated model receives an
exploration opportunity without immediately displacing proven models.

## Maker/checker separation

Resolve a pair before starting work. The default fails closed unless the checker
uses both a different exact model and a different provider:

```python
pair = await models.pair(
    maker_route="code",
    checker_route="review",
    task_type="queue-concurrency",
)

maker = await rlm(task, model=pair.maker.selector, effort=pair.maker.effort)
# After implementation, give the checker only the goal, contract, diff, and evidence.
checker = await rlm(review_task, model=pair.checker.selector, effort=pair.checker.effort)
```

For dynamic routing, a spawn handle is accepted directly:

```python
maker = await rlm(task, route="code")
checker = await rlm(
    review_task,
    route="review",
    independent_of=maker,
    different_provider=True,
)
```

`different_provider=True` excludes the maker's entire provider, reducing shared
provider-cache and model-family bias. It never silently relaxes when no
independent authenticated model exists. Set it to `False` explicitly only when
different exact models within one provider are an acceptable compromise.
