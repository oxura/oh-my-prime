from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT.parent / "workspace" / "src"))
sys.path.insert(0, str(SKILL_ROOT.parent / "prove" / "src"))
sys.path.insert(0, str(SKILL_ROOT / "src"))

from evolve import EvidenceError, EvidenceRef, EvolutionError, EvolutionLab
from prove import ProofRuntime, Requirement, command
from workspace import WorkspaceManager


class EvolutionMemoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "--initial-branch=main")
        self._git("config", "user.name", "Oh My Prime Test")
        self._git("config", "user.email", "test@oh-my-prime.local")
        (self.repo / "policy.txt").write_text("rollback required\n", encoding="utf-8")
        self._git("add", "policy.txt")
        self._git("commit", "-m", "fixture")
        self.now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
        self.workspaces = WorkspaceManager(self.root / "workspace-state")
        self.proof_runtime = ProofRuntime(
            self.root / "proof-state",
            workspace_manager=self.workspaces,
        )
        self.lab = EvolutionLab(
            self.root / "state",
            clock=lambda: self.now,
            proof_runtime=self.proof_runtime,
        )
        self.report_path = self.root / "observation.json"
        self._write_report(status="observed", passed=False)
        self._proof_report = None
        self._proof_counter = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ("git", "-C", str(self.repo), *args),
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.rstrip("\n")

    def _write_report(self, *, status: str, passed: bool) -> None:
        self.report_path.write_text(
            json.dumps(
                {
                    "id": "proof-verified",
                    "contract_id": "contract-migrations",
                    "contract_digest": "a" * 64,
                    "status": status,
                    "gate_results": [
                        {
                            "gate_id": "rollback",
                            "required": True,
                            "passed": passed,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    async def _verification_report(self, *, passes: bool = True):
        self._proof_counter += 1
        contract = await self.proof_runtime.contract(
            "Prove a reusable evolution lesson",
            requirements=(Requirement("LESSON", "the lesson passed verification"),),
            gates=(
                command(
                    (
                        sys.executable,
                        "-c",
                        f"raise SystemExit({0 if passes else 1})",
                    ),
                    id="lesson-proof",
                    proves=("LESSON",),
                ),
            ),
            repo=self.repo,
        )
        workspace = await self.workspaces.fork(
            self.repo,
            name=f"proof-{self._proof_counter}",
        )
        try:
            (workspace.path / "proof-change.txt").write_text(
                f"proof {self._proof_counter}\n",
                encoding="utf-8",
            )
            return await self.proof_runtime.run(workspace, contract)
        finally:
            await self.workspaces.discard(workspace, repo=self.repo)

    async def _verified_evidence(self) -> EvidenceRef:
        if self._proof_report is None:
            self._proof_report = await self._verification_report()
        return await self.lab.attest_verification(
            self._proof_report,
            repo=self.repo,
        )

    async def test_proposes_attested_memory_without_activating_harness(self) -> None:
        proof = await self._verified_evidence()
        candidate = await self.lab.propose_memory(
            "Database migrations require a rollback gate.",
            title="Migration rollback requirement",
            category="verified_knowledge",
            evidence=(proof,),
            confidence=0.92,
            scope="global",
            applies_to=("fixture", "migrations"),
            dependencies=("policy.txt",),
            metadata={"source_session": "session-123"},
            repo=self.repo,
        )

        self.assertEqual(candidate.status, "candidate")
        self.assertEqual(candidate.confirmations, 1)
        self.assertEqual(candidate.code_version, self._git("rev-parse", "HEAD"))
        self.assertEqual((await self.lab.get(candidate.id, repo=self.repo)), candidate)
        self.assertTrue((await self.lab.freshness(candidate)).fresh)
        self.assertEqual((await self.lab.stats(repo=self.repo)).candidates, 1)
        events = await self.lab.events(candidate.id, repo=self.repo)
        self.assertEqual(events[0].action, "propose")
        self.assertFalse((self.repo / "harness_state.json").exists())

    async def test_verified_categories_reject_unverified_or_forged_evidence(
        self,
    ) -> None:
        artifact = await self.lab.attest(self.report_path)
        with self.assertRaisesRegex(EvidenceError, "requires a verified"):
            await self.lab.propose_memory(
                "Unsupported fact",
                title="Unsupported",
                category="verified_knowledge",
                evidence=(artifact,),
                confidence=0.8,
                repo=self.repo,
            )

        forged = EvidenceRef(
            uri=artifact.uri,
            sha256=artifact.sha256,
            kind="artifact",
            verified=True,
            verifier="self",
            captured_at=artifact.captured_at,
        )
        with self.assertRaisesRegex(EvidenceError, "only persisted prove reports"):
            await self.lab.propose_memory(
                "Forged fact",
                title="Forged",
                category="known_error",
                evidence=(forged,),
                confidence=0.8,
                repo=self.repo,
            )

        with self.assertRaisesRegex(
            EvidenceError, "must be a VerificationReport returned by prove.run"
        ):
            await self.lab.attest_verification(self.report_path, repo=self.repo)

        failed_report = await self._verification_report(passes=False)
        with self.assertRaisesRegex(EvidenceError, "not 'verified'"):
            await self.lab.attest_verification(failed_report, repo=self.repo)

    async def test_activation_freshness_applies_the_active_confidence_floor(
        self,
    ) -> None:
        candidate = await self.lab.propose_memory(
            "A weakly supported memory must remain in shadow.",
            title="Weak activation candidate",
            category="verified_knowledge",
            evidence=(await self._verified_evidence(),),
            confidence=0.0,
            repo=self.repo,
        )

        current = await self.lab.freshness(candidate)
        prospective = await self.lab.freshness(candidate, for_activation=True)

        self.assertTrue(current.fresh)
        self.assertFalse(prospective.fresh)
        self.assertLess(prospective.effective_confidence, 0.5)
        self.assertIn(
            "effective confidence is below active threshold",
            prospective.reasons,
        )

    async def test_dependency_change_invalidates_candidate(self) -> None:
        candidate = await self.lab.propose_memory(
            "Rollback policy is required.",
            title="Rollback policy",
            category="verified_knowledge",
            evidence=(await self._verified_evidence(),),
            confidence=0.9,
            dependencies=("policy.txt",),
            repo=self.repo,
        )
        (self.repo / "policy.txt").write_text("policy changed\n", encoding="utf-8")

        freshness = await self.lab.freshness(candidate)
        self.assertFalse(freshness.fresh)
        self.assertEqual(freshness.changed_dependencies, ("policy.txt",))
        decay = await self.lab.decay(repo=self.repo)
        self.assertEqual(decay.invalidated_ids, (candidate.id,))
        self.assertEqual(
            (await self.lab.get(candidate.id, repo=self.repo)).status, "invalidated"
        )

    async def test_temporary_memory_expires_and_hypothesis_stays_distinct(self) -> None:
        observation = await self.lab.attest(self.report_path, kind="observation")
        temporary = await self.lab.propose_memory(
            "Provider quota is currently low.",
            title="Temporary provider quota",
            category="temporary_observation",
            evidence=(observation,),
            confidence=0.7,
            expires_at=self.now + timedelta(hours=2),
            repo=self.repo,
        )
        hypothesis = await self.lab.propose_memory(
            "A smaller context might reduce latency.",
            title="Context latency hypothesis",
            category="hypothesis",
            evidence=(observation,),
            confidence=0.6,
            repo=self.repo,
        )

        self.now += timedelta(days=2)
        report = await self.lab.decay(repo=self.repo)
        self.assertEqual(report.expired_ids, (temporary.id,))
        loaded_hypothesis = await self.lab.get(hypothesis.id, repo=self.repo)
        self.assertEqual(loaded_hypothesis.category, "hypothesis")
        self.assertEqual(loaded_hypothesis.status, "candidate")
        self.assertLess(
            (await self.lab.freshness(loaded_hypothesis)).effective_confidence,
            hypothesis.confidence,
        )

    async def test_verified_confirmation_and_contradiction_are_versioned(self) -> None:
        proof = await self._verified_evidence()
        candidate = await self.lab.propose_memory(
            "Potential queue invariant.",
            title="Queue invariant",
            category="hypothesis",
            evidence=(),
            confidence=0.6,
            repo=self.repo,
        )
        confirmed = await self.lab.confirm(
            candidate.id,
            (proof,),
            reason="verified stress run",
            repo=self.repo,
        )
        self.assertEqual(confirmed.revision, 2)
        self.assertEqual(confirmed.confirmations, 1)

        contradicted = await self.lab.contradict(
            candidate.id,
            (proof,),
            reason="verified counterexample",
            repo=self.repo,
        )
        self.assertEqual(contradicted.revision, 3)
        self.assertEqual(contradicted.status, "invalidated")
        self.assertEqual(
            [
                event.action
                for event in await self.lab.events(candidate.id, repo=self.repo)
            ],
            ["contradict", "confirm", "propose"],
        )

    async def test_store_rejects_payload_tampering(self) -> None:
        candidate = await self.lab.propose_memory(
            "A hypothesis",
            title="Hypothesis",
            category="hypothesis",
            confidence=0.5,
            repo=self.repo,
        )
        database = next((self.root / "state").glob("*.sqlite"))
        connection = sqlite3.connect(database)
        try:
            payload = json.loads(
                connection.execute(
                    "SELECT payload FROM candidates WHERE id = ?", (candidate.id,)
                ).fetchone()[0]
            )
            payload["claim"] = "tampered"
            connection.execute(
                "UPDATE candidates SET payload = ? WHERE id = ?",
                (json.dumps(payload), candidate.id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(EvolutionError, "digest mismatch"):
            await self.lab.get(candidate.id, repo=self.repo)


if __name__ == "__main__":
    unittest.main()
