from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "src"))

from workspace import (  # noqa: E402
    WorkspaceDirtyError,
    WorkspaceManager,
    WorkspaceError,
    WorkspaceStaleError,
)


class WorkspaceManagerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "--initial-branch=main")
        self._git("config", "user.name", "Oh My Prime Test")
        self._git("config", "user.email", "test@oh-my-prime.local")
        self._git("config", "core.autocrlf", "false")
        (self.repo / "app.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "app.txt")
        self._git("commit", "-m", "base")
        self.manager = WorkspaceManager(self.root / "state")

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

    async def test_fork_isolates_and_snapshots_all_candidate_changes(self) -> None:
        candidate = await self.manager.fork(self.repo, name="root cause")
        (candidate.path / "app.txt").rename(candidate.path / "renamed.txt")
        (candidate.path / "new.txt").write_text("new\n", encoding="utf-8")

        snapshot = await self.manager.diff(candidate)

        self.assertEqual((self.repo / "app.txt").read_text(encoding="utf-8"), "base\n")
        self.assertFalse((self.repo / "new.txt").exists())
        self.assertFalse(snapshot.is_empty)
        self.assertEqual(len(snapshot.patch_sha256), 64)
        self.assertEqual({change.path for change in snapshot.files}, {"new.txt", "renamed.txt"})
        self.assertIn("diff --git", snapshot.text)

    async def test_promote_applies_only_the_verified_snapshot_and_keeps_audit_artifact(self) -> None:
        candidate = await self.manager.fork(self.repo, name="verified")
        (candidate.path / "app.txt").write_text("verified\n", encoding="utf-8")
        snapshot = await self.manager.diff(candidate)

        result = await self.manager.promote(
            candidate,
            expected_patch_sha256=snapshot.patch_sha256,
        )

        self.assertEqual((self.repo / "app.txt").read_text(encoding="utf-8"), "verified\n")
        self.assertEqual(result.patch_sha256, snapshot.patch_sha256)
        self.assertTrue(result.artifact_path.is_file())
        self.assertEqual(result.artifact_path.read_bytes(), snapshot.patch)
        self.assertEqual((await self.manager.get(candidate)).status, "promoted")
        self.assertIn(" M app.txt", self._git("status", "--short"))

        discarded = await self.manager.discard(candidate)
        self.assertEqual(discarded.status, "discarded")
        self.assertFalse(candidate.path.exists())

    async def test_promote_rolls_back_when_post_apply_verification_fails(self) -> None:
        candidate = await self.manager.fork(self.repo, name="rollback")
        (candidate.path / "app.txt").write_text("candidate\n", encoding="utf-8")
        snapshot = await self.manager.diff(candidate)

        with patch.object(
            self.manager,
            "_snapshot",
            AsyncMock(side_effect=[snapshot, WorkspaceError("verification failed")]),
        ):
            with self.assertRaisesRegex(WorkspaceError, "verification failed"):
                await self.manager.promote(
                    candidate,
                    expected_patch_sha256=snapshot.patch_sha256,
                )

        self.assertEqual((self.repo / "app.txt").read_text(encoding="utf-8"), "base\n")
        self.assertEqual(self._git("status", "--short"), "")
        self.assertEqual((await self.manager.get(candidate)).status, "active")

    async def test_promote_rejects_candidate_mutation_after_verification(self) -> None:
        candidate = await self.manager.fork(self.repo, name="mutable")
        (candidate.path / "app.txt").write_text("first\n", encoding="utf-8")
        snapshot = await self.manager.diff(candidate)
        (candidate.path / "app.txt").write_text("second\n", encoding="utf-8")

        with self.assertRaisesRegex(WorkspaceStaleError, "changed after verification"):
            await self.manager.promote(
                candidate,
                expected_patch_sha256=snapshot.patch_sha256,
            )

        self.assertEqual((self.repo / "app.txt").read_text(encoding="utf-8"), "base\n")
        self.assertEqual(self._git("status", "--short"), "")

    async def test_promote_rejects_target_that_moved_after_fork(self) -> None:
        candidate = await self.manager.fork(self.repo, name="stale")
        (candidate.path / "app.txt").write_text("candidate\n", encoding="utf-8")
        snapshot = await self.manager.diff(candidate)
        (self.repo / "main.txt").write_text("main moved\n", encoding="utf-8")
        self._git("add", "main.txt")
        self._git("commit", "-m", "move main")

        with self.assertRaisesRegex(WorkspaceStaleError, "target moved after fork"):
            await self.manager.promote(
                candidate,
                expected_patch_sha256=snapshot.patch_sha256,
            )

        self.assertEqual((self.repo / "app.txt").read_text(encoding="utf-8"), "base\n")

    async def test_fork_rejects_dirty_source_instead_of_losing_changes(self) -> None:
        (self.repo / "app.txt").write_text("dirty\n", encoding="utf-8")

        with self.assertRaisesRegex(WorkspaceDirtyError, "source worktree must be clean"):
            await self.manager.fork(self.repo)

        self.assertEqual(await self.manager.list(self.repo), [])


if __name__ == "__main__":
    unittest.main()
