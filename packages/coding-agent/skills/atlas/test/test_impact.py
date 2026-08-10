from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "src"))

from atlas import CodeAtlas, ImpactAnalyzer, ImpactError, ImpactStale


class ImpactAnalyzerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "--initial-branch=main")
        self._git("config", "user.name", "Oh My Prime Test")
        self._git("config", "user.email", "test@oh-my-prime.local")
        (self.repo / ".gitignore").write_text("coverage/\n", encoding="utf-8")
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "__init__.py").write_text(
            "from .service import Service\n",
            encoding="utf-8",
        )
        (self.repo / "pkg" / "service.py").write_text(
            "class Service:\n    def run(self):\n        return 1\n",
            encoding="utf-8",
        )
        (self.repo / "app.py").write_text(
            "from pkg.service import Service\n\n"
            "def start():\n"
            "    return Service().run()\n",
            encoding="utf-8",
        )
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_service.py").write_text(
            "from pkg.service import Service\n\n"
            "def test_service():\n"
            "    assert Service().run() == 1\n",
            encoding="utf-8",
        )
        self._git("add", ".")
        self._git("commit", "-m", "fixture")
        self.atlas = CodeAtlas(self.root / "atlas-state")
        self.analyzer = ImpactAnalyzer(self.atlas)

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

    def _service_patch(self) -> str:
        service = self.repo / "pkg" / "service.py"
        original = service.read_text(encoding="utf-8")
        service.write_text(original.replace("return 1", "return 2"), encoding="utf-8")
        patch = self._git("diff", "--", "pkg/service.py")
        service.write_text(original, encoding="utf-8")
        return patch

    async def test_maps_changed_symbols_to_importers_and_tests(self) -> None:
        report = await self.analyzer.analyze(self._service_patch(), repo=self.repo)

        self.assertEqual(report.changes[0].path, "pkg/service.py")
        self.assertEqual(report.changes[0].status, "modified")
        self.assertTrue(
            any(item.qualified_name == "Service.run" for item in report.changed_symbols)
        )
        impacted_paths = {item.path for item in report.impacted_files}
        self.assertIn("app.py", impacted_paths)
        self.assertIn("pkg/__init__.py", impacted_paths)
        self.assertIn("tests/test_service.py", impacted_paths)
        self.assertEqual(report.tests, ("tests/test_service.py",))
        self.assertIn("Service", report.public_api_symbols)
        self.assertIn("Service.run", report.public_api_symbols)
        self.assertIn("Static impact cannot prove", report.render())

        loaded = await self.analyzer.load(report.id, repo=self.repo)
        self.assertEqual(loaded, report)
        self.assertTrue((await self.analyzer.freshness(loaded)).fresh)

        ignore_file = self.repo / ".gitignore"
        ignore_file.write_text("coverage/\ndist/\n", encoding="utf-8")
        graph_stale = await self.analyzer.freshness(loaded)
        self.assertFalse(graph_stale.fresh)
        self.assertTrue(graph_stale.graph_changed)
        self.assertEqual(graph_stale.changed_files, (".gitignore",))

    async def test_apply_hash_gates_then_applies_exact_attested_patch(self) -> None:
        report = await self.analyzer.analyze(self._service_patch(), repo=self.repo)
        result = await self.analyzer.apply(report)

        self.assertEqual(result.report_id, report.id)
        self.assertEqual(result.applied_files, ("pkg/service.py",))
        self.assertIn(
            "return 2", (self.repo / "pkg" / "service.py").read_text(encoding="utf-8")
        )
        self.assertIsNotNone(result.content_hashes["pkg/service.py"])
        self.assertFalse((await self.analyzer.freshness(report)).fresh)

    async def test_rejects_stale_target_before_apply(self) -> None:
        report = await self.analyzer.analyze(self._service_patch(), repo=self.repo)
        service = self.repo / "pkg" / "service.py"
        service.write_text(
            service.read_text(encoding="utf-8").replace("return 1", "return 3"),
            encoding="utf-8",
        )

        freshness = await self.analyzer.freshness(report)
        self.assertFalse(freshness.fresh)
        self.assertEqual(freshness.changed_files, ("pkg/service.py",))
        with self.assertRaisesRegex(ImpactStale, "file content hash changed"):
            await self.analyzer.apply(report)
        self.assertIn("return 3", service.read_text(encoding="utf-8"))

    async def test_accepts_reverse_git_diff_prefixes(self) -> None:
        service = self.repo / "pkg" / "service.py"
        service.write_text(
            service.read_text(encoding="utf-8").replace("return 1", "return 2"),
            encoding="utf-8",
        )
        reverse_patch = self._git("diff", "-R", "--", "pkg/service.py")

        report = await self.analyzer.analyze(reverse_patch, repo=self.repo)
        self.assertEqual(report.changes[0].path, "pkg/service.py")
        self.assertTrue((await self.analyzer.freshness(report)).fresh)

    async def test_rejects_tampered_persisted_patch(self) -> None:
        report = await self.analyzer.analyze(self._service_patch(), repo=self.repo)
        report.patch_path.write_text("tampered\n", encoding="utf-8")

        with self.assertRaisesRegex(ImpactError, "patch hash mismatch"):
            await self.analyzer.load(report.id, repo=self.repo)

    async def test_rejects_unsafe_file_modes(self) -> None:
        patch = (
            "diff --git a/link b/link\n"
            "new file mode 120000\n"
            "index 0000000..1de5659\n"
            "--- /dev/null\n"
            "+++ b/link\n"
            "@@ -0,0 +1 @@\n"
            "+target\n"
        )
        with self.assertRaisesRegex(ImpactError, "symlink"):
            await self.analyzer.analyze(patch, repo=self.repo)


if __name__ == "__main__":
    unittest.main()
