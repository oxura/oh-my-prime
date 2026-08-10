from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SKILLS_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SKILLS_ROOT.parents[2]
sys.path.insert(0, str(REPO_ROOT / "prime-agent-runtime" / "src"))
sys.path.insert(0, str(SKILLS_ROOT / "workspace" / "src"))
sys.path.insert(0, str(SKILLS_ROOT / "prove" / "src"))
sys.path.insert(0, str(SKILLS_ROOT / "prime" / "src"))

from prime import (  # noqa: E402
    ExplorationStartError,
    ExplorationTimeout,
    NoVerifiedCandidate,
    ProofTreeRuntime,
)
from prove import ProofRuntime, Requirement, command  # noqa: E402
from rlm import RLMSpawnHandle  # noqa: E402
from workspace import WorkspaceManager  # noqa: E402


class FakeRlm:
    def __init__(self, on_spawn=None, *, fail_after: int | None = None, running: bool = False) -> None:
        self.on_spawn = on_spawn
        self.fail_after = fail_after
        self.running = running
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.children: dict[str, SimpleNamespace] = {}

    async def __call__(self, prompt: str, **kwargs: object) -> RLMSpawnHandle:
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise RuntimeError("synthetic spawn failure")
        self.calls.append((prompt, kwargs))
        index = len(self.calls)
        child_id = f"sub-fake-{index}"
        cwd = Path(str(kwargs["cwd"])).resolve()
        if self.on_spawn is not None:
            self.on_spawn(prompt, cwd, index)
        self.children[child_id] = SimpleNamespace(
            rlm_child_id=child_id,
            status="running" if self.running else "completed",
        )
        return RLMSpawnHandle(
            rlm_child_id=child_id,
            name=str(kwargs["name"]),
            session_dir=cwd.parent / f"session-{index}",
            model=str(kwargs.get("model") or "fake/model"),
            cwd=cwd,
        )

    async def list_subagents(self):
        return list(self.children.values())

    async def delete_subagent(self, target: str):
        return self.children.pop(target, None)


class ProofTreeRuntimeTest(unittest.IsolatedAsyncioTestCase):
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
        self.proofs = ProofRuntime(
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

    async def _contract(self):
        return await self.proofs.contract(
            "Fix app.txt",
            requirements=(Requirement("FIX", "app.txt contains fixed"),),
            gates=(
                command(
                    (
                        sys.executable,
                        "-c",
                        "from pathlib import Path; raise SystemExit(Path('app.txt').read_text() != 'fixed\\n')",
                    ),
                    id="fixed",
                    proves=("FIX",),
                    timeout_seconds=5,
                ),
            ),
            repo=self.repo,
        )

    async def test_explores_isolated_strategies_and_promotes_smallest_verified_patch(self) -> None:
        def implement(prompt: str, cwd: Path, _index: int) -> None:
            if "Strategy (root-cause)" in prompt:
                (cwd / "app.txt").write_text("fixed\n", encoding="utf-8")
                (cwd / "analysis.txt").write_text("extra but valid\n", encoding="utf-8")
            elif "Strategy (minimal-fix)" in prompt:
                (cwd / "app.txt").write_text("fixed\n", encoding="utf-8")
            else:
                (cwd / "app.txt").write_text("wrong\n", encoding="utf-8")

        fake = FakeRlm(implement)
        runtime = ProofTreeRuntime(
            self.root / "tree-state",
            workspace_manager=self.workspaces,
            proof_runtime=self.proofs,
            rlm_client=fake,
        )
        contract = await self._contract()

        run = await runtime.explore("Fix app.txt", contract=contract, candidates=3)
        winner = await run.best_verified(max_parallel=2)

        self.assertEqual(len({str(candidate.workspace.path) for candidate in run.candidates}), 3)
        self.assertTrue(all(Path(str(kwargs["cwd"])).is_dir() for _, kwargs in fake.calls))
        self.assertEqual(winner.candidate.strategy.name, "minimal-fix")
        self.assertTrue(winner.report.verified)
        self.assertEqual(winner.score.files_changed, 1)

        promoted = await winner.promote()
        self.assertEqual((self.repo / "app.txt").read_text(encoding="utf-8"), "fixed\n")
        self.assertEqual(len(promoted.discarded_candidate_ids), 2)
        self.assertEqual(promoted.cleanup_failures, ())
        self.assertTrue(winner.candidate.workspace.path.exists())

    async def test_never_selects_least_bad_when_every_candidate_fails(self) -> None:
        def implement(_prompt: str, cwd: Path, index: int) -> None:
            (cwd / "app.txt").write_text(f"wrong-{index}\n", encoding="utf-8")

        fake = FakeRlm(implement)
        runtime = ProofTreeRuntime(
            self.root / "tree-state",
            workspace_manager=self.workspaces,
            proof_runtime=self.proofs,
            rlm_client=fake,
        )
        contract = await self._contract()
        run = await runtime.explore("Fix app.txt", contract=contract, candidates=2)

        with self.assertRaisesRegex(NoVerifiedCandidate, "no verified candidate"):
            await run.best_verified()

        self.assertEqual((self.repo / "app.txt").read_text(encoding="utf-8"), "broken\n")
        self.assertTrue(all(candidate.workspace.path.exists() for candidate in run.candidates))

    async def test_recovers_run_and_reexecutes_verification(self) -> None:
        def implement(_prompt: str, cwd: Path, _index: int) -> None:
            (cwd / "app.txt").write_text("fixed\n", encoding="utf-8")

        fake = FakeRlm(implement)
        runtime = ProofTreeRuntime(
            self.root / "tree-state",
            workspace_manager=self.workspaces,
            proof_runtime=self.proofs,
            rlm_client=fake,
        )
        contract = await self._contract()
        original = await runtime.explore("Fix app.txt", contract=contract, candidates=2)

        recovered = await runtime.load(original.id, repo=self.repo)
        winner = await recovered.best_verified()

        self.assertEqual(recovered.id, original.id)
        self.assertEqual(
            [candidate.child_id for candidate in recovered.candidates],
            [candidate.child_id for candidate in original.candidates],
        )
        self.assertTrue(winner.report.verified)

    async def test_partial_spawn_is_durable_and_recoverable(self) -> None:
        fake = FakeRlm(
            lambda _prompt, cwd, _index: (cwd / "app.txt").write_text("fixed\n", encoding="utf-8"),
            fail_after=1,
        )
        runtime = ProofTreeRuntime(
            self.root / "tree-state",
            workspace_manager=self.workspaces,
            proof_runtime=self.proofs,
            rlm_client=fake,
        )
        contract = await self._contract()

        with self.assertRaises(ExplorationStartError) as raised:
            await runtime.explore("Fix app.txt", contract=contract, candidates=2)

        recovered = await runtime.load(raised.exception.run_id, repo=self.repo)
        self.assertEqual(recovered.state, "start_error")
        self.assertEqual(len(recovered.candidates), 1)
        self.assertEqual((await recovered.statuses())[recovered.candidates[0].id], "completed")

    async def test_wait_timeout_preserves_running_candidates(self) -> None:
        fake = FakeRlm(
            lambda _prompt, cwd, _index: (cwd / "app.txt").write_text("fixed\n", encoding="utf-8"),
            running=True,
        )
        runtime = ProofTreeRuntime(
            self.root / "tree-state",
            workspace_manager=self.workspaces,
            proof_runtime=self.proofs,
            rlm_client=fake,
        )
        contract = await self._contract()
        run = await runtime.explore("Fix app.txt", contract=contract, candidates=2)

        with self.assertRaisesRegex(ExplorationTimeout, "still running"):
            await run.wait(timeout_seconds=0.02, poll_interval_seconds=0.005)

        self.assertTrue(all(candidate.workspace.path.exists() for candidate in run.candidates))


if __name__ == "__main__":
    unittest.main()
