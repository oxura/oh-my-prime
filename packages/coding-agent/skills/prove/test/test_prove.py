from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


SKILLS_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS_ROOT / "workspace" / "src"))
sys.path.insert(0, str(SKILLS_ROOT / "prove" / "src"))

from prove import (  # noqa: E402
    ContractError,
    ProofRuntime,
    Requirement,
    VerificationError,
    command,
    reproducer,
)
from workspace import WorkspaceManager  # noqa: E402


class ProofRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "--initial-branch=main")
        self._git("config", "user.name", "Oh My Prime Test")
        self._git("config", "user.email", "test@oh-my-prime.local")
        self._git("config", "core.autocrlf", "false")
        (self.repo / "app.txt").write_text("broken\n", encoding="utf-8")
        self._git("add", "app.txt")
        self._git("commit", "-m", "base")
        self.workspaces = WorkspaceManager(self.root / "workspace-state")
        self.runtime = ProofRuntime(
            self.root / "proof-state",
            workspace_manager=self.workspaces,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ("git", "-C", str(self.repo), *args),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout.rstrip("\n")

    @staticmethod
    def _fixed_gate(*, gate_id: str = "fixed", required: bool = True):
        return command(
            (
                sys.executable,
                "-c",
                "from pathlib import Path; raise SystemExit(Path('app.txt').read_text() != 'fixed\\n')",
            ),
            id=gate_id,
            required=required,
            proves=("FIX",),
            timeout_seconds=5,
        )

    async def test_verified_report_persists_evidence_and_gates_promotion(self) -> None:
        contract = await self.runtime.contract(
            "Fix the application state",
            requirements=(Requirement("FIX", "app.txt contains the fixed state"),),
            gates=(self._fixed_gate(),),
            repo=self.repo,
        )
        candidate = await self.workspaces.fork(self.repo, name="verified")
        (candidate.path / "app.txt").write_text("fixed\n", encoding="utf-8")

        report = await self.runtime.run(candidate, contract)

        self.assertTrue(report.verified)
        self.assertEqual(report.require_verified(), report)
        self.assertEqual(report.requirement_results[0].status, "proved")
        evidence = report.gate_results[0].candidate
        self.assertTrue(evidence.passed)
        self.assertTrue(evidence.stdout_path.is_file())
        self.assertTrue(evidence.stderr_path.is_file())
        self.assertEqual(len(evidence.stdout_sha256), 64)
        self.assertTrue(report.report_path.is_file())

        result = await self.runtime.promote(candidate, report)
        self.assertEqual(result.patch_sha256, report.patch_sha256)
        self.assertEqual((self.repo / "app.txt").read_text(encoding="utf-8"), "fixed\n")

    async def test_reproducer_requires_failure_before_and_success_after(self) -> None:
        (self.repo / "reproduce.py").write_text(
            "from pathlib import Path\nraise SystemExit(Path('app.txt').read_text() != 'fixed\\n')\n",
            encoding="utf-8",
        )
        self._git("add", "reproduce.py")
        self._git("commit", "-m", "add reproducer")
        contract = await self.runtime.contract(
            "Prove the regression transition",
            requirements=(Requirement("FIX", "the reproducer no longer fails"),),
            gates=(
                reproducer(
                    (sys.executable, "reproduce.py"),
                    id="regression",
                    proves=("FIX",),
                    timeout_seconds=5,
                ),
            ),
            repo=self.repo,
        )
        candidate = await self.workspaces.fork(self.repo, name="regression")
        (candidate.path / "app.txt").write_text("fixed\n", encoding="utf-8")

        report = await self.runtime.run(candidate, contract)

        gate = report.gate_results[0]
        self.assertTrue(report.verified)
        self.assertIsNotNone(gate.baseline)
        self.assertNotEqual(gate.baseline.exit_code, 0)
        self.assertEqual(gate.candidate.exit_code, 0)

    async def test_contract_without_deterministic_gate_is_incomplete(self) -> None:
        contract = await self.runtime.contract(
            "Change app.txt",
            requirements=(Requirement("FIX", "app.txt changes"),),
            repo=self.repo,
        )
        candidate = await self.workspaces.fork(self.repo, name="unproved")
        (candidate.path / "app.txt").write_text("fixed\n", encoding="utf-8")

        report = await self.runtime.run(candidate, contract)

        self.assertEqual(report.status, "incomplete")
        self.assertEqual(report.requirement_results[0].status, "unproved")

    async def test_required_gate_failure_fails_verification(self) -> None:
        contract = await self.runtime.contract(
            "Fix app.txt",
            requirements=(Requirement("FIX", "app.txt is fixed"),),
            gates=(self._fixed_gate(),),
            repo=self.repo,
        )
        candidate = await self.workspaces.fork(self.repo, name="failed")
        (candidate.path / "app.txt").write_text("still broken\n", encoding="utf-8")

        report = await self.runtime.run(candidate, contract)

        self.assertEqual(report.status, "failed")
        self.assertIn("exited with code", report.gate_results[0].failure)

    async def test_candidate_mutation_during_gate_invalidates_evidence(self) -> None:
        mutating_gate = command(
            (
                sys.executable,
                "-c",
                "from pathlib import Path; Path('generated.txt').write_text('side effect\\n')",
            ),
            id="mutating",
            proves=("FIX",),
            timeout_seconds=5,
        )
        contract = await self.runtime.contract(
            "Fix app.txt without verifier side effects",
            requirements=(Requirement("FIX", "app.txt is fixed"),),
            gates=(mutating_gate,),
            repo=self.repo,
        )
        candidate = await self.workspaces.fork(self.repo, name="mutated")
        (candidate.path / "app.txt").write_text("fixed\n", encoding="utf-8")

        report = await self.runtime.run(candidate, contract)

        self.assertEqual(report.status, "failed")
        self.assertIn("candidate changed during verification", report.limitations[0])

    async def test_tampered_ledger_cannot_promote(self) -> None:
        contract = await self.runtime.contract(
            "Fix app.txt",
            requirements=(Requirement("FIX", "app.txt is fixed"),),
            gates=(self._fixed_gate(),),
            repo=self.repo,
        )
        candidate = await self.workspaces.fork(self.repo, name="tampered")
        (candidate.path / "app.txt").write_text("fixed\n", encoding="utf-8")
        report = await self.runtime.run(candidate, contract)
        payload = json.loads(report.report_path.read_text(encoding="utf-8"))
        payload["status"] = "failed"
        report.report_path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(VerificationError, "differs from its persisted"):
            await self.runtime.promote(candidate, report)

        self.assertEqual((self.repo / "app.txt").read_text(encoding="utf-8"), "broken\n")

    async def test_contract_digest_rejects_in_memory_tampering(self) -> None:
        contract = await self.runtime.contract(
            "Fix app.txt",
            requirements=(Requirement("FIX", "app.txt is fixed"),),
            gates=(self._fixed_gate(),),
            repo=self.repo,
        )
        candidate = await self.workspaces.fork(self.repo, name="contract-tamper")
        (candidate.path / "app.txt").write_text("fixed\n", encoding="utf-8")

        with self.assertRaisesRegex(ContractError, "digest mismatch"):
            await self.runtime.run(candidate, replace(contract, goal="different goal"))

    async def test_timeout_is_recorded_as_failed_evidence(self) -> None:
        timeout_gate = command(
            (sys.executable, "-c", "import time; time.sleep(10)"),
            id="timeout",
            proves=("FIX",),
            timeout_seconds=0.05,
        )
        contract = await self.runtime.contract(
            "Bound verifier execution",
            requirements=(Requirement("FIX", "verification is bounded"),),
            gates=(timeout_gate,),
            repo=self.repo,
        )
        candidate = await self.workspaces.fork(self.repo, name="timeout")
        (candidate.path / "app.txt").write_text("fixed\n", encoding="utf-8")

        report = await self.runtime.run(candidate, contract)

        self.assertEqual(report.status, "failed")
        self.assertTrue(report.gate_results[0].candidate.timed_out)


if __name__ == "__main__":
    unittest.main()
