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

## Replay, shadow, promotion, rollback

Replay cases are deterministic command contracts. Evolution runs every case
against baseline and candidate harness snapshots in separate Git workspaces:

```python
suite = await evolve.create_replay_suite(
    "migration lessons",
    [
        evolve.ReplayCase(
            id="rollback-case",
            title="agent preserves the rollback gate",
            argv=("python", "replays/check_migration_behavior.py"),
            stdout_contains=("rollback verified",),
            timeout_seconds=120,
        ),
    ],
)
replay = await evolve.run_replay(candidate.id, suite)
shadow_candidate = await evolve.begin_shadow(candidate.id, replay)

shadow = await evolve.run_replay(
    candidate.id,
    suite,
    phase="shadow",
)
await evolve.evaluate_shadow(candidate.id, shadow)
promotion = await evolve.promote(candidate.id, replay, shadow)
```

The source repository must be clean when replay workspaces are forked, when a
shadow report is admitted, and again at promotion. Commands execute without a
shell in isolated workspaces with a small allowlist of non-secret environment
variables, bounded time and output, and process-tree cleanup on timeout or
cancellation. Exit code and stdout assertions determine each result. The report
preserves stdout/stderr paths, hashes, previews, scores, improvements,
regressions, candidate revision, and suite digest; every later load revalidates
the suite, output files, and harness snapshots.

`begin_shadow()` writes only an isolated shadow harness; active state remains
untouched. A failed or regressing shadow report rejects the candidate and
removes that shadow. Promotion requires:

- verified knowledge or a verified known error;
- current evidence and dependency hashes;
- an attested replay report with improvement and no regressions;
- a separate attested shadow report with improvement and no regressions;
- unchanged candidate lineage and unchanged target harness entry.

Promotion binds the candidate, replay suite, repository revision, shadow
snapshot, and exact admitted harness path. It writes a durable transaction
journal before changing either the harness or SQLite candidate ledger. Restart
recovery treats the ledger as authoritative, completes committed transitions,
and restores uncommitted harness changes without touching unrelated entries.
Cross-language harness locks carry a live heartbeat and reject ownership or
symlink changes.

`await evolve.rollback(candidate.id, reason=...)` restores the exact
pre-promotion entry or removes the new one. Rollback rejects later edits rather
than clobbering them. `await evolve.maintain()` revalidates the original
Verifier Fabric gate artifacts plus both replay ledgers and automatically rolls
back active memory whose proof, dependencies, expiry, or effective confidence
became stale. It continues independent rollbacks and reports conflicts after
the safe transitions finish. `await evolve.recover()` explicitly reconciles
interrupted journals; every lifecycle operation also recovers its candidate
before proceeding.

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
- `await evolve.create_replay_suite(name, cases)`
- `await evolve.run_replay(candidate_id, suite, phase=...)`
- `await evolve.load_replay(report_id)`
- `await evolve.begin_shadow(candidate_id, replay_report)`
- `await evolve.evaluate_shadow(candidate_id, shadow_report)`
- `await evolve.promote(candidate_id, replay_report, shadow_report)`
- `await evolve.rollback(candidate_id, reason=...)`
- `await evolve.recover()`
- `await evolve.maintain()`

Do not copy candidate content into active harness memory by hand; that bypasses
the replay proof and rollback ledger.
