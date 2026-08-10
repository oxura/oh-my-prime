---
name: evolve
description: Turn verified outcomes into evidence-backed continual harness memory candidates, track hypotheses separately, detect stale evidence and dependencies, decay confidence, and promote only replay-proven changes. Use after Verifier Fabric proves a reusable lesson or when reviewing existing learned behavior.
---

# Evolution Lab

Evolution Lab prevents one successful or failed trajectory from immediately
rewriting future agent behavior. It separates proposal from activation:

```text
trajectory -> candidate -> evidence validation -> replay -> shadow -> promotion
```

The `evolve` module is preloaded in IPython. Creating a candidate never mutates
active continual harness state.

## Proof-backed memory

Attest a successful Verifier Fabric report, then propose the smallest reusable
claim:

```python
proof = await evolve.attest_verification(verification_report)
candidate = await evolve.propose_memory(
    "Database migrations in this repository require a rollback gate.",
    title="Migration rollback requirement",
    category="verified_knowledge",
    evidence=[proof],
    confidence=0.92,
    scope="global",
    applies_to=["oh-my-prime", "database migrations"],
    dependencies=["packages/db/migrations.py"],
    metadata={"source_session": session_id},
)
```

`verified_knowledge` and `known_error` require a persisted `prove` report whose
status is `verified` and whose required gates all passed. Global candidates are
restricted to those two verified categories. A generic artifact hash cannot be
marked verified.

Use the four categories deliberately:

- `verified_knowledge` — a reusable claim established by deterministic gates;
- `known_error` — a reproduced failure or counterexample with verified evidence;
- `hypothesis` — a plausible idea that must not be presented as fact;
- `temporary_observation` — time-bounded state and therefore requires
  `expires_at`.

Every candidate records the claim, category, evidence URIs and hashes,
confidence, timestamps, scope, applicability, repository revision, and hashes
of explicitly related files. Candidate and event payloads are digest-attested
in a repository-keyed SQLite ledger outside the repository.

## Evidence and freshness

```python
status = await evolve.freshness(candidate)
print(status.fresh, status.effective_confidence, status.reasons)

more_proof = await evolve.attest_verification(second_report)
candidate = await evolve.confirm(
    candidate.id,
    [more_proof],
    reason="Independent replay confirms the rule",
)
```

Freshness re-hashes every evidence artifact and dependency. Missing or changed
evidence, changed dependencies, expiration, and terminal candidate states are
reported explicitly. Confidence decays by category and is adjusted by verified
confirmations and contradictions; hypotheses never silently become verified
knowledge.

Use `await evolve.decay()` to mark expired or stale candidates, and
`await evolve.invalidate(id, reason=...)` for explicit architectural or policy
invalidation. Every mutation uses an optimistic revision check and appends an
event with before/after digests.

## Query API

- `await evolve.attest(path)`
- `await evolve.attest_verification(report)`
- `await evolve.propose_memory(...)`
- `await evolve.get(candidate_id)`
- `await evolve.memories(status=..., category=...)`
- `await evolve.freshness(candidate)`
- `await evolve.confirm(candidate_id, evidence, reason=...)`
- `await evolve.contradict(candidate_id, evidence, reason=...)`
- `await evolve.invalidate(candidate_id, reason=...)`
- `await evolve.decay()`
- `await evolve.events(candidate_id)`

Replay suites, shadow evaluation, promotion, and rollback operate on these same
attested candidates. Do not copy candidate content into active harness memory
by hand; that bypasses the proof and rollback ledger.
