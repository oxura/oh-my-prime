from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT.parent / "workspace" / "src"))
sys.path.insert(0, str(SKILL_ROOT.parent / "prove" / "src"))
sys.path.insert(0, str(SKILL_ROOT / "src"))
import evolve
from evolve import EvolutionError, EvolutionLab, PromotionRejected
from evolve._models import ReplayCase
from evolve._promotion import PromotionRuntime
from evolve._replay import ReplayRuntime
from evolve._store import EvolutionStore
from prove import ProofRuntime, Requirement, command
from workspace import WorkspaceManager


class ReplayRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "--initial-branch=main")
        self._git("config", "user.name", "Oh My Prime Test")
        self._git("config", "user.email", "test@oh-my-prime.local")
        (self.repo / "check_memory.py").write_text(
            "import json, os, pathlib, sys\n"
            "print('secret ' + os.environ.get('EVOLUTION_TEST_SECRET', 'missing'))\n"
            "root = pathlib.Path(os.environ['RLM_HARNESS_STATE_DIR'])\n"
            "state = json.loads((root / 'harness_state.json').read_text())\n"
            "entries = state['entries']['memory'].values()\n"
            "entry = next((item for item in entries if item['metadata'].get('evolution_status') == 'shadow'), None)\n"
            f"block = pathlib.Path({str(self.root / 'reject-shadow')!r})\n"
            "if entry and not block.exists():\n"
            "    print('rule active')\n"
            "    raise SystemExit(0)\n"
            "print('rule missing')\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        self._git("add", "check_memory.py")
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
        self.runtime = ReplayRuntime(self.lab, self.workspaces)
        self.promotion = PromotionRuntime(self.lab, self.runtime)
        self.harness_dir = self.root / "harness"
        self.proof_path: Path | None = None
        self._proof_report = None

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

    def _descendant_case(
        self,
        case_id: str,
        *,
        child_delay: float,
        timeout: float,
    ) -> tuple[ReplayCase, Path, Path]:
        ready = self.root / f"{case_id}.ready"
        marker = self.root / f"{case_id}.survived"
        child_code = (
            "import pathlib,time;"
            f"time.sleep({child_delay!r});"
            f"pathlib.Path({str(marker)!r}).write_text("
            "'survived',encoding='utf-8')"
        )
        parent_code = (
            "import pathlib,subprocess,sys,time;"
            f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
            f"pathlib.Path({str(ready)!r}).write_text("
            "str(child.pid),encoding='utf-8');"
            "time.sleep(60)"
        )
        return (
            ReplayCase(
                id=case_id,
                title="process tree cleanup",
                argv=(sys.executable, "-c", parent_code),
                timeout_seconds=timeout,
            ),
            ready,
            marker,
        )

    @staticmethod
    async def _wait_for_path(path: Path) -> None:
        while not path.exists():
            await asyncio.sleep(0.01)

    async def _verified_evidence(self):
        if self._proof_report is None:
            contract = await self.proof_runtime.contract(
                "Prove a reusable queue lesson",
                requirements=(Requirement("QUEUE", "queue behavior is verified"),),
                gates=(
                    command(
                        (sys.executable, "-c", "print('queue proof')"),
                        id="queue-proof",
                        proves=("QUEUE",),
                    ),
                ),
                repo=self.repo,
            )
            workspace = await self.workspaces.fork(
                self.repo,
                name="queue-proof",
            )
            try:
                (workspace.path / "proof-change.txt").write_text(
                    "verified\n",
                    encoding="utf-8",
                )
                self._proof_report = await self.proof_runtime.run(
                    workspace,
                    contract,
                )
            finally:
                await self.workspaces.discard(workspace, repo=self.repo)
            self.proof_path = self._proof_report.report_path
        return await self.lab.attest_verification(
            self._proof_report,
            repo=self.repo,
        )

    async def _candidate(
        self,
        target_id: str = "queue_rule",
        *,
        confidence: float = 0.9,
        expires_at: datetime | None = None,
    ):
        proof = await self._verified_evidence()
        return await self.lab.propose_memory(
            f"{target_id} claims require a stress gate.",
            title=f"{target_id} stress rule",
            target_id=target_id,
            category="verified_knowledge",
            evidence=(proof,),
            confidence=confidence,
            expires_at=expires_at,
            repo=self.repo,
        )

    async def _suite(self, name: str = "queue memory replay"):
        return await self.runtime.create_suite(
            name,
            (
                ReplayCase(
                    id="queue-memory",
                    title="candidate memory is visible",
                    argv=(sys.executable, "check_memory.py"),
                    stdout_contains=("rule active",),
                ),
            ),
            repo=self.repo,
        )

    async def _prepare_promotion(
        self,
        name: str,
        target_id: str = "queue_rule",
        *,
        confidence: float = 0.9,
    ):
        candidate = await self._candidate(target_id, confidence=confidence)
        suite = await self._suite(name)
        replay = await self.runtime.run(
            candidate.id,
            suite,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        await self.promotion.begin_shadow(
            candidate.id,
            replay,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        shadow = await self.runtime.run(
            candidate.id,
            suite,
            phase="shadow",
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        await self.promotion.evaluate_shadow(candidate.id, shadow, repo=self.repo)
        return candidate, replay, shadow

    async def _promote_candidate(self, name: str, *, confidence: float = 0.9):
        candidate, replay, shadow = await self._prepare_promotion(
            name, confidence=confidence
        )
        promotion = await self.promotion.promote(
            candidate.id,
            replay,
            shadow,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        return candidate, promotion

    async def _stage_partial_promotion(self, name: str):
        candidate, replay, shadow = await self._prepare_promotion(name)
        candidate = await self.lab.get(candidate.id, repo=self.repo)
        _, common_dir, _ = await self.lab._repo(self.repo)
        state_path = (self.harness_dir / "harness_state.json").absolute()
        current = self.runtime._load_harness(state_path)
        before = current["entries"]["memory"].get(candidate.target_id)
        timestamp = self.lab._now_text()
        after = self.runtime._harness_entry(
            candidate,
            before,
            status="active",
            timestamp=timestamp,
        )
        result = self.promotion._promotion_result(
            candidate,
            replay,
            shadow,
            state_path,
            before,
            after,
            timestamp,
        )
        result_path = self.promotion._promotion_path(common_dir, result.id).absolute()
        journal_path = self.promotion._journal_path(common_dir, candidate.id)
        journal = self.promotion._journal_payload(
            operation="promote",
            candidate=candidate,
            state_path=state_path,
            before_entry=before,
            after_entry=after,
            record_path=result_path,
            record_payload=self.promotion._promotion_payload(result),
        )
        changed = json.loads(json.dumps(current))
        changed["entries"]["memory"][candidate.target_id] = after
        await asyncio.to_thread(
            self.promotion._write_json_atomic, journal_path, journal
        )
        await asyncio.to_thread(
            self.promotion._write_json_atomic,
            result_path,
            self.promotion._promotion_payload(result),
        )
        await asyncio.to_thread(self.promotion._write_json_atomic, state_path, changed)
        return (
            candidate,
            result,
            journal_path,
            result_path,
            state_path,
            current,
            changed,
        )

    async def test_replay_compares_isolated_baseline_and_candidate(self) -> None:
        candidate = await self._candidate()
        suite = await self._suite()
        previous_secret = os.environ.get("EVOLUTION_TEST_SECRET")
        os.environ["EVOLUTION_TEST_SECRET"] = "must-not-leak"
        try:
            report = await self.runtime.run(
                candidate.id,
                suite,
                harness_state_dir=self.harness_dir,
                repo=self.repo,
            )
        finally:
            if previous_secret is None:
                os.environ.pop("EVOLUTION_TEST_SECRET", None)
            else:
                os.environ["EVOLUTION_TEST_SECRET"] = previous_secret

        self.assertEqual(report.status, "passed")
        self.assertEqual(report.improvements, ("queue-memory",))
        self.assertEqual(report.regressions, ())
        self.assertFalse(report.case_results[0].baseline.passed)
        self.assertTrue(report.case_results[0].candidate.passed)
        for evidence in (
            report.case_results[0].baseline,
            report.case_results[0].candidate,
        ):
            self.assertIn("secret missing", evidence.stdout_preview)
            self.assertNotIn("must-not-leak", evidence.stdout_preview)
        self.assertEqual(
            await self.runtime.load_report(report.id, repo=self.repo), report
        )
        self.assertEqual(await self.runtime.load_suite(suite.id, repo=self.repo), suite)
        self.assertEqual(
            len(self._git("worktree", "list", "--porcelain").split("worktree ")) - 1, 1
        )

    async def test_equal_outcome_does_not_claim_improvement(self) -> None:
        candidate = await self._candidate()
        suite = await self.runtime.create_suite(
            "no improvement",
            (
                ReplayCase(
                    id="always-passes",
                    title="same outcome",
                    argv=(sys.executable, "-c", "print('ok')"),
                    stdout_contains=("ok",),
                ),
            ),
            repo=self.repo,
        )

        report = await self.runtime.run(
            candidate.id,
            suite,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.improvements, ())
        self.assertEqual(report.regressions, ())

    async def test_replay_rejects_suite_object_from_another_repository(
        self,
    ) -> None:
        candidate = await self._candidate()
        foreign_repo = self.root / "foreign-repo"
        foreign_repo.mkdir()
        for arguments in (
            ("init", "--initial-branch=main"),
            ("config", "user.name", "Oh My Prime Test"),
            ("config", "user.email", "test@oh-my-prime.local"),
        ):
            await asyncio.to_thread(
                subprocess.run,
                ("git", "-C", str(foreign_repo), *arguments),
                check=True,
                capture_output=True,
                text=True,
            )
        (foreign_repo / "fixture.py").write_text("value = 1\n", encoding="utf-8")
        await asyncio.to_thread(
            subprocess.run,
            ("git", "-C", str(foreign_repo), "add", "fixture.py"),
            check=True,
            capture_output=True,
            text=True,
        )
        await asyncio.to_thread(
            subprocess.run,
            ("git", "-C", str(foreign_repo), "commit", "-m", "fixture"),
            check=True,
            capture_output=True,
            text=True,
        )
        foreign_suite = await self.runtime.create_suite(
            "foreign replay",
            (
                ReplayCase(
                    id="foreign",
                    title="foreign suite",
                    argv=(sys.executable, "-c", "raise SystemExit(0)"),
                ),
            ),
            repo=foreign_repo,
        )
        _, common_dir, _ = await self.lab._repo(self.repo)
        report_root = self.runtime._report_root(common_dir)
        fork = AsyncMock(side_effect=AssertionError("workspace forked"))

        with (
            patch.object(self.workspaces, "fork", fork),
            self.assertRaisesRegex(EvolutionError, "replay suite is invalid"),
        ):
            await self.runtime.run(
                candidate.id,
                foreign_suite,
                harness_state_dir=self.harness_dir,
                repo=self.repo,
            )

        fork.assert_not_awaited()
        self.assertFalse(report_root.exists())

    async def test_replay_rejects_changed_persisted_suite_object(self) -> None:
        candidate = await self._candidate()
        suite = await self._suite("persisted suite")
        changed = replace(suite, name="changed suite", digest="")
        changed = replace(changed, digest=self.runtime._suite_digest(changed))
        _, common_dir, _ = await self.lab._repo(self.repo)
        report_root = self.runtime._report_root(common_dir)
        fork = AsyncMock(side_effect=AssertionError("workspace forked"))

        with (
            patch.object(self.workspaces, "fork", fork),
            self.assertRaisesRegex(EvolutionError, "does not match persisted suite"),
        ):
            await self.runtime.run(
                candidate.id,
                changed,
                harness_state_dir=self.harness_dir,
                repo=self.repo,
            )

        fork.assert_not_awaited()
        self.assertFalse(report_root.exists())

    async def test_suite_ledger_never_follows_a_symlinked_root(self) -> None:
        suite = await self._suite("symlinked suite ledger")
        _, common_dir, _ = await self.lab._repo(self.repo)
        suite_root = self.runtime._suite_path(common_dir, suite.id).parent
        victim = self.root / "suite-ledger-victim"
        suite_root.rename(victim)
        suite_root.symlink_to(victim, target_is_directory=True)

        with self.assertRaisesRegex(
            EvolutionError, "managed replay path uses a symlink"
        ):
            await self.runtime.load_suite(suite.id, repo=self.repo)

        existing = {path.name for path in victim.iterdir()}
        with self.assertRaisesRegex(
            EvolutionError, "managed replay path uses a symlink"
        ):
            await self._suite("blocked suite ledger write")
        self.assertEqual({path.name for path in victim.iterdir()}, existing)

    async def test_report_ledger_never_follows_a_symlinked_root(self) -> None:
        candidate = await self._candidate()
        suite = await self._suite("symlinked report ledger")
        report = await self.runtime.run(
            candidate.id,
            suite,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        report_root = report.artifact_dir.parent
        victim = self.root / "report-ledger-victim"
        report_root.rename(victim)
        report_root.symlink_to(victim, target_is_directory=True)

        with self.assertRaisesRegex(
            EvolutionError, "managed replay path uses a symlink"
        ):
            await self.runtime.load_report(report.id, repo=self.repo)

        other_suite = await self._suite("blocked report ledger write")
        existing = {path.name for path in victim.iterdir()}
        fork = AsyncMock(side_effect=AssertionError("workspace forked"))
        with (
            patch.object(self.workspaces, "fork", fork),
            self.assertRaisesRegex(
                EvolutionError, "managed replay path uses a symlink"
            ),
        ):
            await self.runtime.run(
                candidate.id,
                other_suite,
                harness_state_dir=self.harness_dir,
                repo=self.repo,
            )
        fork.assert_not_awaited()
        self.assertEqual({path.name for path in victim.iterdir()}, existing)

    async def test_report_load_rejects_a_symlinked_artifact_subdirectory(
        self,
    ) -> None:
        candidate = await self._candidate()
        suite = await self._suite("symlinked report artifact")
        report = await self.runtime.run(
            candidate.id,
            suite,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        harness_root = report.artifact_dir / "harness-baseline"
        victim = self.root / "report-harness-victim"
        harness_root.rename(victim)
        harness_root.symlink_to(victim, target_is_directory=True)

        with self.assertRaisesRegex(
            EvolutionError, "managed replay path uses a symlink"
        ):
            await self.runtime.load_report(report.id, repo=self.repo)

    async def test_suite_load_binds_payload_id_to_requested_ledger_id(self) -> None:
        suite = await self._suite("copied suite payload")
        _, common_dir, _ = await self.lab._repo(self.repo)
        source = self.runtime._suite_path(common_dir, suite.id)
        requested_id = "suite-" + (
            "0" * 24 if suite.id != "suite-" + "0" * 24 else "1" * 24
        )
        shutil.copyfile(
            source,
            self.runtime._suite_path(common_dir, requested_id),
        )

        with self.assertRaisesRegex(
            EvolutionError, "suite id does not match ledger path"
        ):
            await self.runtime.load_suite(requested_id, repo=self.repo)

    async def test_report_load_binds_recomputed_payload_to_requested_id(
        self,
    ) -> None:
        candidate = await self._candidate()
        suite = await self._suite("copied report payload")
        report = await self.runtime.run(
            candidate.id,
            suite,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        requested_id = "replay-" + (
            "0" * 24 if report.id != "replay-" + "0" * 24 else "1" * 24
        )
        copied_dir = report.artifact_dir.parent / requested_id
        shutil.copytree(report.artifact_dir, copied_dir)
        copied_path = copied_dir / "report.json"
        payload = json.loads(copied_path.read_text(encoding="utf-8"))
        payload["artifact_dir"] = str(copied_dir)
        payload["report_path"] = str(copied_path)
        for result in payload["case_results"]:
            for variant in ("baseline", "candidate"):
                evidence = result[variant]
                for field in ("stdout_path", "stderr_path"):
                    evidence[field] = str(copied_dir / Path(evidence[field]).name)
        payload["digest"] = None
        payload["digest"] = hashlib.sha256(self.runtime._canonical(payload)).hexdigest()
        copied_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            EvolutionError, "report id does not match ledger path"
        ):
            await self.runtime.load_report(requested_id, repo=self.repo)

    @unittest.skipIf(os.name == "nt", "process-group assertion requires POSIX")
    async def test_timed_out_case_terminates_its_process_tree(self) -> None:
        case, ready, marker = self._descendant_case(
            "timeout-cleanup",
            child_delay=1.5,
            timeout=0.75,
        )
        artifact_dir = self.lab.state_root / "timeout-artifacts"
        artifact_dir.mkdir(parents=True)

        evidence = await self.runtime._run_case(
            case,
            self.repo,
            self.harness_dir,
            artifact_dir,
            "baseline",
            "candidate-timeout",
            "local",
        )

        self.assertTrue(evidence.timed_out)
        self.assertTrue(ready.is_file())
        await asyncio.sleep(0.9)
        self.assertFalse(marker.exists())

    @unittest.skipIf(os.name == "nt", "process-group assertion requires POSIX")
    async def test_cancelled_case_terminates_its_process_tree(self) -> None:
        case, ready, marker = self._descendant_case(
            "cancel-cleanup",
            child_delay=0.5,
            timeout=10,
        )
        artifact_dir = self.lab.state_root / "cancel-artifacts"
        artifact_dir.mkdir(parents=True)
        replay = asyncio.create_task(
            self.runtime._run_case(
                case,
                self.repo,
                self.harness_dir,
                artifact_dir,
                "candidate",
                "candidate-cancel",
                "local",
            )
        )
        try:
            await asyncio.wait_for(self._wait_for_path(ready), timeout=2)
            replay.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await replay
        finally:
            if not replay.done():
                replay.cancel()
                await asyncio.gather(replay, return_exceptions=True)

        await asyncio.sleep(0.6)
        self.assertFalse(marker.exists())

    async def test_report_load_rejects_changed_output(self) -> None:
        candidate = await self._candidate()
        suite = await self._suite("tamper replay")
        report = await self.runtime.run(
            candidate.id,
            suite,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        report.case_results[0].candidate.stdout_path.write_text(
            "tampered", encoding="utf-8"
        )

        with self.assertRaisesRegex(EvolutionError, "stdout evidence is invalid"):
            await self.runtime.load_report(report.id, repo=self.repo)

    async def test_promotes_only_after_replay_and_shadow_then_rolls_back(self) -> None:
        candidate = await self._candidate()
        suite = await self._suite("promotion replay")
        replay = await self.runtime.run(
            candidate.id,
            suite,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        shadow_candidate = await self.promotion.begin_shadow(
            candidate.id,
            replay,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        self.assertEqual(shadow_candidate.status, "shadow")
        self.assertFalse((self.harness_dir / "harness_state.json").exists())

        shadow = await self.runtime.run(
            candidate.id,
            suite,
            phase="shadow",
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        evaluated = await self.promotion.evaluate_shadow(
            candidate.id, shadow, repo=self.repo
        )
        self.assertEqual(evaluated.status, "shadow")
        promotion = await self.promotion.promote(
            candidate.id,
            replay,
            shadow,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )

        state = json.loads(
            (self.harness_dir / "harness_state.json").read_text(encoding="utf-8")
        )
        entry = state["entries"]["memory"]["queue_rule"]
        self.assertEqual(entry["metadata"]["evolution_status"], "active")
        self.assertEqual(
            (await self.lab.get(candidate.id, repo=self.repo)).status, "active"
        )
        self.assertEqual(promotion.before_entry, None)

        rollback = await self.promotion.rollback(
            candidate.id,
            reason="verified regression",
            repo=self.repo,
        )
        restored = json.loads(
            (self.harness_dir / "harness_state.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("queue_rule", restored["entries"]["memory"])
        self.assertEqual(
            (await self.lab.get(candidate.id, repo=self.repo)).status, "rolled_back"
        )
        self.assertEqual(rollback.promotion_id, promotion.id)

    async def test_recovery_aborts_uncommitted_partial_promotion(self) -> None:
        (
            candidate,
            _,
            journal_path,
            result_path,
            state_path,
            _,
            _,
        ) = await self._stage_partial_promotion("partial promotion recovery")
        staged = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIn(candidate.target_id, staged["entries"]["memory"])

        recovered = await self.promotion.recover(repo=self.repo)

        self.assertEqual(recovered, (candidate.id,))
        restored = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotIn(candidate.target_id, restored["entries"]["memory"])
        self.assertFalse(journal_path.exists())
        self.assertFalse(result_path.exists())
        self.assertEqual(
            (await self.lab.get(candidate.id, repo=self.repo)).status,
            "shadow",
        )

    async def test_recovery_aborts_uncommitted_partial_rollback(self) -> None:
        candidate, promotion = await self._promote_candidate(
            "partial rollback recovery"
        )
        active, _ = await self.lab._candidate_and_store(candidate.id, self.repo)
        _, common_dir, _ = await self.lab._repo(self.repo)
        state_path = promotion.harness_state_path
        current = self.runtime._load_harness(state_path)
        restored = json.loads(json.dumps(current))
        records = restored["entries"]["memory"]
        if promotion.before_entry is None:
            records.pop(active.target_id, None)
        else:
            records[active.target_id] = promotion.before_entry
        timestamp = self.lab._now_text()
        rollback = self.promotion._rollback_result(
            active,
            promotion,
            timestamp,
            "simulated interrupted rollback",
        )
        rollback_path = self.promotion._rollback_path(
            common_dir, rollback.id
        ).absolute()
        journal_path = self.promotion._journal_path(common_dir, active.id)
        journal = self.promotion._journal_payload(
            operation="rollback",
            candidate=active,
            state_path=state_path,
            before_entry=promotion.after_entry,
            after_entry=promotion.before_entry,
            record_path=rollback_path,
            record_payload=self.promotion._rollback_payload(rollback),
        )
        await asyncio.to_thread(
            self.promotion._write_json_atomic, journal_path, journal
        )
        await asyncio.to_thread(
            self.promotion._write_json_atomic,
            rollback_path,
            self.promotion._rollback_payload(rollback),
        )
        await asyncio.to_thread(self.promotion._write_json_atomic, state_path, restored)

        recovered = await self.promotion.recover(repo=self.repo)

        self.assertEqual(recovered, (active.id,))
        recovered_state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            recovered_state["entries"]["memory"][active.target_id],
            promotion.after_entry,
        )
        self.assertFalse(journal_path.exists())
        self.assertFalse(rollback_path.exists())
        self.assertEqual(
            (await self.lab.get(active.id, repo=self.repo)).status,
            "active",
        )

    async def test_recovery_never_follows_a_symlinked_journal_root(self) -> None:
        (
            candidate,
            _,
            journal_path,
            _,
            _,
            _,
            _,
        ) = await self._stage_partial_promotion("symlinked journal recovery")
        journal_root = journal_path.parent
        victim = self.root / "journal-victim"
        journal_root.rename(victim)
        journal_root.symlink_to(victim, target_is_directory=True)

        with self.assertRaisesRegex(PromotionRejected, "uses a symlink"):
            await self.promotion.recover(repo=self.repo)

        self.assertTrue((victim / journal_path.name).is_file())
        self.assertEqual(
            (await self.lab.get(candidate.id, repo=self.repo)).status,
            "shadow",
        )

    async def test_cancelled_promotion_settles_the_ledger_before_recovery(
        self,
    ) -> None:
        candidate, replay, shadow = await self._prepare_promotion(
            "cancelled promotion recovery"
        )
        started = threading.Event()
        release = threading.Event()
        original_update = EvolutionStore.update

        def delayed_update(store, value, *, action, reason, evidence=()):
            if action == "promote":
                started.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("test did not release promotion update")
            return original_update(
                store,
                value,
                action=action,
                reason=reason,
                evidence=evidence,
            )

        with patch.object(EvolutionStore, "update", delayed_update):
            task = asyncio.create_task(
                self.promotion.promote(
                    candidate.id,
                    replay,
                    shadow,
                    harness_state_dir=self.harness_dir,
                    repo=self.repo,
                )
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        active = await self.lab.get(candidate.id, repo=self.repo)
        self.assertEqual(active.status, "active")
        recovered = await self.promotion.recover(repo=self.repo)
        self.assertEqual(recovered, (candidate.id,))
        state = json.loads(
            (self.harness_dir / "harness_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            state["entries"]["memory"][candidate.target_id]["metadata"][
                "evolution_status"
            ],
            "active",
        )
        shadow_path = Path(active.metadata["shadow"]["shadow_state_path"])
        self.assertFalse(shadow_path.parent.exists())

    async def test_maintenance_rolls_back_stale_active_memory(self) -> None:
        candidate, _ = await self._promote_candidate("maintenance replay")
        self.assertIsNotNone(self.proof_path)
        self.proof_path.unlink()

        rolled_back = await self.promotion.maintain(repo=self.repo)
        self.assertEqual(rolled_back, (candidate.id,))
        state = json.loads(
            (self.harness_dir / "harness_state.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("queue_rule", state["entries"]["memory"])
        self.assertEqual(
            (await self.lab.get(candidate.id, repo=self.repo)).status,
            "rolled_back",
        )

    async def test_maintenance_revalidates_proof_gate_artifacts(self) -> None:
        candidate, _ = await self._promote_candidate("proof artifact replay")
        self.assertIsNotNone(self._proof_report)
        self._proof_report.gate_results[0].candidate.stdout_path.unlink()

        rolled_back = await self.promotion.maintain(repo=self.repo)

        self.assertEqual(rolled_back, (candidate.id,))
        self.assertEqual(
            (await self.lab.get(candidate.id, repo=self.repo)).status,
            "rolled_back",
        )

    async def test_maintenance_revalidates_replay_output_artifacts(self) -> None:
        candidate, replay, shadow = await self._prepare_promotion(
            "replay artifact maintenance"
        )
        await self.promotion.promote(
            candidate.id,
            replay,
            shadow,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        replay.case_results[0].candidate.stdout_path.unlink()

        rolled_back = await self.promotion.maintain(repo=self.repo)

        self.assertEqual(rolled_back, (candidate.id,))
        self.assertEqual(
            (await self.lab.get(candidate.id, repo=self.repo)).status,
            "rolled_back",
        )

    async def test_maintenance_rechecks_staleness_under_the_harness_lock(
        self,
    ) -> None:
        candidate, _ = await self._promote_candidate("maintenance freshness race")

        with (
            patch.object(
                self.promotion,
                "_candidate_staleness",
                AsyncMock(side_effect=[("stale snapshot",), ()]),
            ),
            self.assertRaisesRegex(PromotionRejected, "candidate is no longer stale"),
        ):
            await self.promotion.maintain(repo=self.repo)

        self.assertEqual(
            (await self.lab.get(candidate.id, repo=self.repo)).status,
            "active",
        )
        state = json.loads(
            (self.harness_dir / "harness_state.json").read_text(encoding="utf-8")
        )
        self.assertIn(candidate.target_id, state["entries"]["memory"])

    async def test_maintenance_continues_after_an_independent_conflict(
        self,
    ) -> None:
        prepared = [
            await self._prepare_promotion(
                "first maintenance conflict", "maintenance_one"
            ),
            await self._prepare_promotion(
                "second maintenance conflict", "maintenance_two"
            ),
        ]
        for candidate, replay, shadow in prepared:
            await self.promotion.promote(
                candidate.id,
                replay,
                shadow,
                harness_state_dir=self.harness_dir,
                repo=self.repo,
            )
        conflicted = prepared[0][0]
        recoverable = prepared[1][0]
        state_path = self.harness_dir / "harness_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["entries"]["memory"][conflicted.target_id]["content"] = (
            "independent user edit"
        )
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.assertIsNotNone(self._proof_report)
        self._proof_report.gate_results[0].candidate.stdout_path.unlink()

        with self.assertRaisesRegex(
            PromotionRejected, "maintenance rollback conflicts"
        ):
            await self.promotion.maintain(repo=self.repo)

        self.assertEqual(
            (await self.lab.get(conflicted.id, repo=self.repo)).status,
            "active",
        )
        self.assertEqual(
            (await self.lab.get(recoverable.id, repo=self.repo)).status,
            "rolled_back",
        )

    async def test_verified_contradiction_uses_effective_confidence_for_rollback(
        self,
    ) -> None:
        candidate, _ = await self._promote_candidate(
            "effective confidence rollback",
            confidence=0.8,
        )

        with (
            patch.object(evolve, "_default_lab", self.lab),
            patch.object(evolve, "_default_promotion", self.promotion),
        ):
            contradicted = await evolve.contradict(
                candidate.id,
                (await self._verified_evidence(),),
                reason="verified contradiction",
                repo=self.repo,
            )

        self.assertGreater(contradicted.confidence, 0.5)
        self.assertLess(
            (await self.lab.freshness(contradicted)).effective_confidence,
            0.5,
        )
        self.assertEqual(contradicted.status, "rolled_back")
        state = json.loads(
            (self.harness_dir / "harness_state.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("queue_rule", state["entries"]["memory"])

    async def test_invalidate_removes_symlinked_shadow_without_following_it(
        self,
    ) -> None:
        candidate = await self._candidate("invalidated_shadow")
        suite = await self._suite("invalidate shadow cleanup")
        replay = await self.runtime.run(
            candidate.id,
            suite,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        shadow = await self.promotion.begin_shadow(
            candidate.id,
            replay,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        shadow_dir = Path(shadow.metadata["shadow"]["shadow_state_path"]).parent
        victim = self.root / "shadow-cleanup-victim"
        victim.mkdir()
        marker = victim / "keep.txt"
        marker.write_text("keep\n", encoding="utf-8")
        shutil.rmtree(shadow_dir)
        shadow_dir.symlink_to(victim, target_is_directory=True)

        with (
            patch.object(evolve, "_default_lab", self.lab),
            patch.object(evolve, "_default_promotion", self.promotion),
        ):
            invalidated = await evolve.invalidate(
                candidate.id,
                reason="candidate withdrawn",
                repo=self.repo,
            )

        self.assertEqual(invalidated.status, "invalidated")
        self.assertFalse(shadow_dir.exists())
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")

    async def test_decay_removes_expired_shadow_directory(self) -> None:
        candidate = await self._candidate(
            "expired_shadow",
            expires_at=self.now + timedelta(hours=1),
        )
        suite = await self._suite("expired shadow cleanup")
        replay = await self.runtime.run(
            candidate.id,
            suite,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        shadow = await self.promotion.begin_shadow(
            candidate.id,
            replay,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        shadow_dir = Path(shadow.metadata["shadow"]["shadow_state_path"]).parent
        self.assertTrue(shadow_dir.is_dir())
        self.now += timedelta(hours=2)

        with (
            patch.object(evolve, "_default_lab", self.lab),
            patch.object(evolve, "_default_promotion", self.promotion),
        ):
            report = await evolve.decay(repo=self.repo)

        self.assertEqual(report.expired_ids, (candidate.id,))
        self.assertFalse(shadow_dir.exists())
        self.assertEqual(
            (await self.lab.get(candidate.id, repo=self.repo)).status,
            "expired",
        )

    async def test_rollback_rejects_later_harness_edits(self) -> None:
        candidate, _ = await self._promote_candidate("rollback conflict replay")
        state_path = self.harness_dir / "harness_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["entries"]["memory"]["queue_rule"]["content"] = "user changed this entry"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaisesRegex(PromotionRejected, "changed after promotion"):
            await self.promotion.rollback(
                candidate.id,
                reason="must not clobber",
                repo=self.repo,
            )
        preserved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            preserved["entries"]["memory"]["queue_rule"]["content"],
            "user changed this entry",
        )
        self.assertEqual(
            (await self.lab.get(candidate.id, repo=self.repo)).status,
            "active",
        )

    async def test_rollback_restores_the_exact_prior_entry(self) -> None:
        prior_entry = {
            "id": "queue_rule",
            "kind": "memory",
            "title": "Earlier queue rule",
            "content": "Keep the earlier behavior.",
            "path": "legacy",
            "scope": "local",
            "reference": {"source": "manual"},
            "arguments": {"mode": "strict"},
            "metadata": {"owner": "user"},
            "source": "refine",
            "created_at": "2026-08-01T00:00:00+00:00",
            "updated_at": "2026-08-02T00:00:00+00:00",
            "version": 7,
        }
        self.harness_dir.mkdir(parents=True)
        state_path = self.harness_dir / "harness_state.json"
        state_path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "entries": {
                        "prompt": {},
                        "memory": {"queue_rule": prior_entry},
                        "skill": {},
                        "subagent": {},
                    },
                    "refinements": [],
                }
            ),
            encoding="utf-8",
        )

        candidate, promotion = await self._promote_candidate("restore replay")
        self.assertEqual(promotion.before_entry, prior_entry)
        rollback = await self.promotion.rollback(
            candidate.id,
            reason="restore verified predecessor",
            repo=self.repo,
        )

        restored = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(restored["entries"]["memory"]["queue_rule"], prior_entry)
        self.assertEqual(rollback.restored_entry, prior_entry)

    async def test_shadow_must_reuse_the_admitted_suite(self) -> None:
        candidate = await self._candidate()
        admitted_suite = await self._suite("admitted suite")
        replay = await self.runtime.run(
            candidate.id,
            admitted_suite,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        await self.promotion.begin_shadow(
            candidate.id,
            replay,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        substitute_suite = await self._suite("substitute suite")
        shadow = await self.runtime.run(
            candidate.id,
            substitute_suite,
            phase="shadow",
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )

        with self.assertRaisesRegex(PromotionRejected, "admitted replay suite"):
            await self.promotion.evaluate_shadow(
                candidate.id,
                shadow,
                repo=self.repo,
            )

    async def test_shadow_replay_preserves_unrelated_live_harness_changes(
        self,
    ) -> None:
        marker = self.root / "require-live-entry"
        case_code = (
            "import json, os, pathlib\n"
            "root = pathlib.Path(os.environ['RLM_HARNESS_STATE_DIR'])\n"
            "records = json.loads((root / 'harness_state.json').read_text())"
            "['entries']['memory']\n"
            f"require_live = pathlib.Path({str(marker)!r}).exists()\n"
            "passed = 'queue_rule' in records and "
            "(not require_live or 'unrelated_live' in records)\n"
            "print('combined delta visible' if passed else 'combined delta missing')\n"
            "raise SystemExit(0 if passed else 1)\n"
        )
        candidate = await self._candidate()
        suite = await self.runtime.create_suite(
            "shadow live baseline delta",
            (
                ReplayCase(
                    id="live-baseline-delta",
                    title="candidate delta preserves current live memory",
                    argv=(sys.executable, "-c", case_code),
                    stdout_contains=("combined delta visible",),
                ),
            ),
            repo=self.repo,
        )
        replay = await self.runtime.run(
            candidate.id,
            suite,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        admitted = await self.promotion.begin_shadow(
            candidate.id,
            replay,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        state_path = self.harness_dir / "harness_state.json"
        live = self.runtime._load_harness(state_path)
        live["entries"]["memory"]["unrelated_live"] = {
            "id": "unrelated_live",
            "kind": "memory",
            "title": "Concurrent live memory",
            "content": "This entry arrived after shadow admission.",
            "metadata": {"owner": "another-session"},
        }
        self.promotion._write_json_atomic(state_path, live)
        marker.touch()

        shadow = await self.runtime.run(
            candidate.id,
            suite,
            phase="shadow",
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )

        self.assertEqual(shadow.status, "passed")
        self.assertEqual(shadow.improvements, ("live-baseline-delta",))
        baseline_snapshot = self.runtime._load_harness(
            shadow.artifact_dir / "harness-baseline" / "harness_state.json"
        )
        candidate_snapshot = self.runtime._load_harness(
            shadow.artifact_dir / "harness-candidate" / "harness_state.json"
        )
        admitted_snapshot = self.runtime._load_harness(
            Path(admitted.metadata["shadow"]["shadow_state_path"])
        )
        self.assertEqual(
            candidate_snapshot["entries"]["memory"]["queue_rule"],
            admitted_snapshot["entries"]["memory"]["queue_rule"],
        )
        candidate_snapshot["entries"]["memory"].pop("queue_rule")
        baseline_snapshot["entries"]["memory"].pop("queue_rule", None)
        self.assertEqual(candidate_snapshot, baseline_snapshot)

    async def test_shadow_run_rejects_a_tampered_shadow_harness(self) -> None:
        candidate = await self._candidate()
        suite = await self._suite("shadow tamper suite")
        replay = await self.runtime.run(
            candidate.id,
            suite,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        shadow_candidate = await self.promotion.begin_shadow(
            candidate.id,
            replay,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        shadow_path = Path(shadow_candidate.metadata["shadow"]["shadow_state_path"])
        shadow_path.write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(EvolutionError, "shadow harness hash mismatch"):
            await self.runtime.run(
                candidate.id,
                suite,
                phase="shadow",
                harness_state_dir=self.harness_dir,
                repo=self.repo,
            )
        self.assertFalse((self.harness_dir / "harness_state.json").exists())

    async def test_shadow_admission_rejects_a_symlinked_harness_state(
        self,
    ) -> None:
        candidate = await self._candidate()
        suite = await self._suite("admitted harness symlink replay")
        replay = await self.runtime.run(
            candidate.id,
            suite,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        self.harness_dir.mkdir(parents=True)
        victim = self.root / "untrusted-harness.json"
        victim.write_text("{}\n", encoding="utf-8")
        (self.harness_dir / "harness_state.json").symlink_to(victim)

        with self.assertRaisesRegex(
            PromotionRejected, "admitted harness state is a symlink"
        ):
            await self.promotion.begin_shadow(
                candidate.id,
                replay,
                harness_state_dir=self.harness_dir,
                repo=self.repo,
            )

    async def test_promotion_never_follows_a_shadow_directory_symlink(self) -> None:
        candidate, replay, shadow = await self._prepare_promotion(
            "shadow symlink replay"
        )
        evaluated = await self.lab.get(candidate.id, repo=self.repo)
        shadow_path = Path(evaluated.metadata["shadow"]["shadow_state_path"])
        shadow_dir = shadow_path.parent
        victim = self.root / "victim"
        victim.mkdir()
        marker = victim / "keep.txt"
        marker.write_text("keep\n", encoding="utf-8")
        shutil.rmtree(shadow_dir)
        shadow_dir.symlink_to(victim, target_is_directory=True)

        with self.assertRaisesRegex(PromotionRejected, "shadow path is a symlink"):
            await self.promotion.promote(
                candidate.id,
                replay,
                shadow,
                harness_state_dir=self.harness_dir,
                repo=self.repo,
            )

        self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")
        self.assertFalse((self.harness_dir / "harness_state.json").exists())
        self.assertEqual(
            (await self.lab.get(candidate.id, repo=self.repo)).status,
            "shadow",
        )

    async def test_promotion_rejects_candidate_changes_after_shadow(self) -> None:
        candidate, replay, shadow = await self._prepare_promotion("lineage replay")
        proof = await self._verified_evidence()
        await self.lab.confirm(
            candidate.id,
            (proof,),
            reason="late evidence changes the candidate",
            repo=self.repo,
        )

        with self.assertRaisesRegex(PromotionRejected, "changed after shadow"):
            await self.promotion.promote(
                candidate.id,
                replay,
                shadow,
                harness_state_dir=self.harness_dir,
                repo=self.repo,
            )

    async def test_shadow_evaluation_rejects_changes_after_admission(self) -> None:
        candidate = await self._candidate()
        suite = await self._suite("admission lineage replay")
        replay = await self.runtime.run(
            candidate.id,
            suite,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        await self.promotion.begin_shadow(
            candidate.id,
            replay,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        proof = await self._verified_evidence()
        await self.lab.confirm(
            candidate.id,
            (proof,),
            reason="late evidence after admission",
            repo=self.repo,
        )
        shadow = await self.runtime.run(
            candidate.id,
            suite,
            phase="shadow",
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )

        with self.assertRaisesRegex(
            PromotionRejected, "changed after shadow admission"
        ):
            await self.promotion.evaluate_shadow(
                candidate.id,
                shadow,
                repo=self.repo,
            )

    async def test_promotion_is_bound_to_the_admitted_harness(self) -> None:
        candidate, replay, shadow = await self._prepare_promotion(
            "bound harness replay"
        )
        other_harness = self.root / "other-harness"

        with self.assertRaisesRegex(
            PromotionRejected, "differs from the admitted shadow harness"
        ):
            await self.promotion.promote(
                candidate.id,
                replay,
                shadow,
                harness_state_dir=other_harness,
                repo=self.repo,
            )

        self.assertFalse((other_harness / "harness_state.json").exists())
        self.assertEqual(
            (await self.lab.get(candidate.id, repo=self.repo)).status,
            "shadow",
        )

    async def test_shadow_evaluation_requires_a_clean_worktree(self) -> None:
        candidate = await self._candidate()
        suite = await self._suite("dirty shadow replay")
        replay = await self.runtime.run(
            candidate.id,
            suite,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        await self.promotion.begin_shadow(
            candidate.id,
            replay,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        shadow = await self.runtime.run(
            candidate.id,
            suite,
            phase="shadow",
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        (self.repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")

        with self.assertRaisesRegex(PromotionRejected, "worktree must be clean"):
            await self.promotion.evaluate_shadow(
                candidate.id,
                shadow,
                repo=self.repo,
            )

    async def test_promotion_requires_a_clean_worktree(self) -> None:
        candidate, replay, shadow = await self._prepare_promotion(
            "dirty promotion replay"
        )
        (self.repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")

        with self.assertRaisesRegex(PromotionRejected, "worktree must be clean"):
            await self.promotion.promote(
                candidate.id,
                replay,
                shadow,
                harness_state_dir=self.harness_dir,
                repo=self.repo,
            )

    async def test_promotion_rejects_head_advance_while_waiting_for_lock(
        self,
    ) -> None:
        candidate, replay, shadow = await self._prepare_promotion(
            "promotion lock revision race"
        )

        @asynccontextmanager
        async def advancing_lock(_path: Path):
            (self.repo / "advanced.txt").write_text("advanced\n", encoding="utf-8")
            self._git("add", "advanced.txt")
            self._git("commit", "-m", "advance while promotion waits")
            yield

        with (
            patch.object(self.promotion, "_lock", advancing_lock),
            self.assertRaisesRegex(
                PromotionRejected,
                "repository changed while waiting for the promotion lock",
            ),
        ):
            await self.promotion.promote(
                candidate.id,
                replay,
                shadow,
                harness_state_dir=self.harness_dir,
                repo=self.repo,
            )

        self.assertFalse((self.harness_dir / "harness_state.json").exists())
        self.assertEqual(
            (await self.lab.get(candidate.id, repo=self.repo)).status,
            "shadow",
        )

    async def test_promotion_rejects_low_effective_confidence(self) -> None:
        candidate, replay, shadow = await self._prepare_promotion(
            "low confidence promotion",
            confidence=0.0,
        )

        with self.assertRaisesRegex(
            PromotionRejected,
            "effective confidence is below active threshold",
        ):
            await self.promotion.promote(
                candidate.id,
                replay,
                shadow,
                harness_state_dir=self.harness_dir,
                repo=self.repo,
            )

        self.assertFalse((self.harness_dir / "harness_state.json").exists())
        self.assertEqual(
            (await self.lab.get(candidate.id, repo=self.repo)).status,
            "shadow",
        )

    async def test_replay_rejects_repository_changes_after_candidate_creation(
        self,
    ) -> None:
        candidate = await self._candidate()
        suite = await self._suite("source revision replay")
        (self.repo / "later.py").write_text("value = 2\n", encoding="utf-8")
        self._git("add", "later.py")
        self._git("commit", "-m", "change source")

        with self.assertRaisesRegex(
            EvolutionError, "repository changed after candidate creation"
        ):
            await self.runtime.run(
                candidate.id,
                suite,
                harness_state_dir=self.harness_dir,
                repo=self.repo,
            )

    async def test_concurrent_promotions_preserve_both_entries(self) -> None:
        first, first_replay, first_shadow = await self._prepare_promotion(
            "first concurrent replay",
            "queue_rule_one",
        )
        second, second_replay, second_shadow = await self._prepare_promotion(
            "second concurrent replay",
            "queue_rule_two",
        )

        promotions = await asyncio.wait_for(
            asyncio.gather(
                self.promotion.promote(
                    first.id,
                    first_replay,
                    first_shadow,
                    harness_state_dir=self.harness_dir,
                    repo=self.repo,
                ),
                self.promotion.promote(
                    second.id,
                    second_replay,
                    second_shadow,
                    harness_state_dir=self.harness_dir,
                    repo=self.repo,
                ),
            ),
            timeout=10,
        )

        self.assertEqual(len(promotions), 2)
        state = json.loads(
            (self.harness_dir / "harness_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(state["entries"]["memory"]),
            {"queue_rule_one", "queue_rule_two"},
        )

    async def test_failed_shadow_is_rejected_without_touching_harness(self) -> None:
        candidate = await self._candidate()
        replay_suite = await self._suite("admission replay")
        replay = await self.runtime.run(
            candidate.id,
            replay_suite,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        shadow_candidate = await self.promotion.begin_shadow(
            candidate.id,
            replay,
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )
        shadow_path = Path(shadow_candidate.metadata["shadow"]["shadow_state_path"])
        (self.root / "reject-shadow").touch()
        shadow = await self.runtime.run(
            candidate.id,
            replay_suite,
            phase="shadow",
            harness_state_dir=self.harness_dir,
            repo=self.repo,
        )

        rejected = await self.promotion.evaluate_shadow(
            candidate.id, shadow, repo=self.repo
        )
        self.assertEqual(rejected.status, "rejected")
        self.assertFalse((self.harness_dir / "harness_state.json").exists())
        self.assertFalse(shadow_path.exists())
        with self.assertRaisesRegex(PromotionRejected, "must be in shadow"):
            await self.promotion.promote(
                candidate.id,
                replay,
                shadow,
                harness_state_dir=self.harness_dir,
                repo=self.repo,
            )


if __name__ == "__main__":
    unittest.main()
