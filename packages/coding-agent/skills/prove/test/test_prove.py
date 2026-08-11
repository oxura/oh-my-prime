from __future__ import annotations

import hashlib
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

from prove import (
    ContractError,
    ProofRuntime,
    Requirement,
    VerificationError,
    command,
    reproducer,
)
from workspace import WorkspaceManager


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
            capture_output=True,
            text=True,
        )
        return result.stdout.rstrip("\n")

    @staticmethod
    def _rewrite_report(path: Path, payload: dict[str, object]) -> None:
        canonical = dict(payload)
        canonical.pop("digest", None)
        payload["digest"] = hashlib.sha256(
            json.dumps(
                canonical,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

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
        self.assertTrue(report.initial_diff.evidence_path.is_file())
        self.assertTrue(report.initial_diff.patch_path.is_file())
        self.assertEqual(report.patch_sha256, report.initial_diff.patch_sha256)
        self.assertEqual(len(evidence.stdout_sha256), 64)
        self.assertTrue(report.report_path.is_file())
        self.assertEqual(
            await self.runtime.load_report(report.id, repo=self.repo), report
        )

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

    async def test_no_change_report_cannot_be_forged_to_verified(self) -> None:
        always_passes = command(
            (sys.executable, "-c", "raise SystemExit(0)"),
            id="passes",
            proves=("FIX",),
            timeout_seconds=5,
        )
        contract = await self.runtime.contract(
            "Prove without changing the candidate",
            requirements=(Requirement("FIX", "the gate passes"),),
            gates=(always_passes,),
            repo=self.repo,
        )
        candidate = await self.workspaces.fork(self.repo, name="no-change")
        report = await self.runtime.run(candidate, contract)
        self.assertEqual(report.status, "incomplete")

        payload = json.loads(report.report_path.read_text(encoding="utf-8"))
        payload["status"] = "verified"
        self._rewrite_report(report.report_path, payload)

        with self.assertRaisesRegex(
            VerificationError, "status is inconsistent with its evidence"
        ):
            await self.runtime.load_report(report.id, repo=self.repo)

    async def test_instability_limitation_cannot_be_removed(self) -> None:
        mutating_gate = command(
            (
                sys.executable,
                "-c",
                "from pathlib import Path; Path('generated.txt').write_text('side effect\\n')",
            ),
            id="mutating-attestation",
            proves=("FIX",),
            timeout_seconds=5,
        )
        contract = await self.runtime.contract(
            "Reject verifier mutation",
            requirements=(Requirement("FIX", "the gate passes"),),
            gates=(mutating_gate,),
            repo=self.repo,
        )
        candidate = await self.workspaces.fork(self.repo, name="unstable-forgery")
        (candidate.path / "app.txt").write_text("fixed\n", encoding="utf-8")
        report = await self.runtime.run(candidate, contract)
        self.assertEqual(report.status, "failed")

        payload = json.loads(report.report_path.read_text(encoding="utf-8"))
        payload["status"] = "verified"
        payload["limitations"] = []
        self._rewrite_report(report.report_path, payload)

        with self.assertRaisesRegex(
            VerificationError, "limitations are inconsistent with its evidence"
        ):
            await self.runtime.load_report(report.id, repo=self.repo)

    async def test_patch_hash_and_file_count_are_recomputed_from_diff(self) -> None:
        contract = await self.runtime.contract(
            "Fix app.txt",
            requirements=(Requirement("FIX", "app.txt is fixed"),),
            gates=(self._fixed_gate(),),
            repo=self.repo,
        )
        candidate = await self.workspaces.fork(self.repo, name="summary-forgery")
        (candidate.path / "app.txt").write_text("fixed\n", encoding="utf-8")
        report = await self.runtime.run(candidate, contract)
        original = json.loads(report.report_path.read_text(encoding="utf-8"))

        for field in ("patch_sha256", "files_changed"):
            with self.subTest(field=field):
                payload = json.loads(json.dumps(original))
                initial_diff = payload["initial_diff"]
                if field == "patch_sha256":
                    payload[field] = "0" * 64
                    initial_diff[field] = "0" * 64
                else:
                    payload[field] += 1
                    initial_diff["files"].append(
                        {"status": "A", "path": "forged.txt", "previous_path": None}
                    )
                self._rewrite_report(report.report_path, payload)
                with self.assertRaisesRegex(
                    VerificationError, "diff attestation is inconsistent"
                ):
                    await self.runtime.load_report(report.id, repo=self.repo)

    async def test_report_load_rejects_symlinked_run_path_components(self) -> None:
        contract = await self.runtime.contract(
            "Fix app.txt",
            requirements=(Requirement("FIX", "app.txt is fixed"),),
            gates=(self._fixed_gate(),),
            repo=self.repo,
        )
        candidate = await self.workspaces.fork(self.repo, name="symlink-ledger")
        (candidate.path / "app.txt").write_text("fixed\n", encoding="utf-8")
        report = await self.runtime.run(candidate, contract)

        for component in (report.artifact_dir.parent, report.artifact_dir):
            with self.subTest(component=component.name):
                real = component.with_name(f"{component.name}-real")
                component.rename(real)
                component.symlink_to(real, target_is_directory=True)
                try:
                    with self.assertRaisesRegex(
                        VerificationError, "symlink path component"
                    ):
                        await self.runtime.load_report(report.id, repo=self.repo)
                finally:
                    component.unlink()
                    real.rename(component)

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
        with self.assertRaisesRegex(
            VerificationError, "status is inconsistent with its evidence"
        ):
            await self.runtime.load_report(report.id, repo=self.repo)

        self.assertEqual(
            (self.repo / "app.txt").read_text(encoding="utf-8"), "broken\n"
        )

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
