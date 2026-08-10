from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "src"))

from atlas import AtlasError, CodeAtlas  # noqa: E402


class CodeAtlasTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "--initial-branch=main")
        self._git("config", "user.name", "Oh My Prime Test")
        self._git("config", "user.email", "test@oh-my-prime.local")
        (self.repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        (self.repo / "package.json").write_text(
            '{"name":"atlas-fixture","private":true,"devDependencies":{"typescript":"*"}}\n',
            encoding="utf-8",
        )
        (self.repo / "tsconfig.json").write_text(
            '{"compilerOptions":{"module":"NodeNext","moduleResolution":"NodeNext","target":"ES2022"},"include":["src"]}\n',
            encoding="utf-8",
        )
        source_typescript = Path.cwd().parents[1] / "node_modules" / "typescript"
        if not source_typescript.is_dir():
            raise RuntimeError(f"TypeScript fixture dependency not found: {source_typescript}")
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules" / "typescript").symlink_to(
            source_typescript,
            target_is_directory=True,
        )
        (self.repo / "src").mkdir()
        (self.repo / "src" / "service.ts").write_text(
            "export function helper(): number { return 1; }\n"
            "export class Service {\n"
            "  run(): number { return helper(); }\n"
            "}\n",
            encoding="utf-8",
        )
        (self.repo / "src" / "main.ts").write_text(
            'import { Service } from "./service.js";\n'
            "export function start(): number { return new Service().run(); }\n",
            encoding="utf-8",
        )
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "pkg" / "worker.py").write_text(
            "def helper():\n"
            "    return 1\n\n"
            "class Worker:\n"
            "    def run(self):\n"
            "        return helper()\n",
            encoding="utf-8",
        )
        self._git(
            "add",
            ".gitignore",
            "package.json",
            "tsconfig.json",
            "src/service.ts",
            "src/main.ts",
            "pkg/__init__.py",
            "pkg/worker.py",
        )
        self._git("commit", "-m", "fixture")
        self.atlas = CodeAtlas(self.root / "atlas-state")

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

    async def test_builds_cross_language_symbols_calls_and_references(self) -> None:
        report = await self.atlas.build(self.repo)
        stats = await self.atlas.stats(self.repo)

        self.assertEqual(report.indexed_files, 7)
        self.assertEqual(report.changed_files, 7)
        self.assertEqual(report.parser_diagnostics, 0)
        self.assertGreater(report.symbols, 5)
        self.assertGreater(report.edges, 5)
        self.assertEqual(stats.files, report.indexed_files)
        self.assertEqual(stats.head_commit, self._git("rev-parse", "HEAD"))

        service_run = await self.atlas.symbol("Service.run", repo=self.repo)
        calls = await self.atlas.outgoing(service_run, kinds=("calls",), repo=self.repo)
        self.assertTrue(any(edge.target_name == "helper" for edge in calls))
        target_key = next(edge.target_symbol_key for edge in calls if edge.target_name == "helper")
        self.assertIsNotNone(target_key)

        helper = await self.atlas.symbol(target_key, repo=self.repo)
        references = await self.atlas.references(helper, kinds=("calls",), repo=self.repo)
        self.assertTrue(any(edge.source_symbol_key == service_run.key for edge in references))

        worker_run = await self.atlas.symbol("Worker.run", repo=self.repo)
        python_calls = await self.atlas.outgoing(worker_run, kinds=("calls",), repo=self.repo)
        self.assertTrue(any(edge.target_name == "helper" and edge.confidence == 1 for edge in python_calls))

    async def test_rebuild_reports_content_changes_and_removed_files(self) -> None:
        await self.atlas.build(self.repo)
        service = self.repo / "src" / "service.ts"
        service.write_text(service.read_text(encoding="utf-8") + "export const added = 2;\n", encoding="utf-8")

        changed = await self.atlas.build(self.repo)
        self.assertEqual(changed.changed_files, 1)
        self.assertEqual(changed.removed_files, 0)
        self.assertEqual((await self.atlas.symbol("added", repo=self.repo)).kind, "variable")

        self._git("rm", "pkg/worker.py")
        removed = await self.atlas.build(self.repo)
        self.assertEqual(removed.removed_files, 1)
        with self.assertRaisesRegex(AtlasError, "symbol not found"):
            await self.atlas.symbol("Worker.run", repo=self.repo)

    async def test_ambiguous_symbol_requires_qualified_query(self) -> None:
        await self.atlas.build(self.repo)

        with self.assertRaisesRegex(AtlasError, "ambiguous symbol"):
            await self.atlas.symbol("helper", repo=self.repo)


if __name__ == "__main__":
    unittest.main()
