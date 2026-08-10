from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "src"))

from atlas import AtlasStale, CapsuleError, CodeAtlas, ContextCompiler


class ContextCompilerTest(unittest.IsolatedAsyncioTestCase):
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
            '{"name":"context-fixture","private":true,"devDependencies":{"typescript":"*"}}\n',
            encoding="utf-8",
        )
        (self.repo / "tsconfig.json").write_text(
            '{"compilerOptions":{"module":"NodeNext","moduleResolution":"NodeNext","target":"ES2022"},"include":["src"]}\n',
            encoding="utf-8",
        )
        source_typescript = Path.cwd().parents[1] / "node_modules" / "typescript"
        if not source_typescript.is_dir():
            raise RuntimeError(
                f"TypeScript fixture dependency not found: {source_typescript}"
            )
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
        (self.repo / "src" / "unrelated.ts").write_text(
            "\n".join(
                f"export const unrelated{index} = {index};" for index in range(300)
            )
            + "\n",
            encoding="utf-8",
        )
        self._git("add", ".gitignore", "package.json", "tsconfig.json", "src")
        self._git("commit", "-m", "fixture")
        self.atlas = CodeAtlas(self.root / "atlas-state")
        self.compiler = ContextCompiler(self.atlas)

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

    async def test_compiles_semantic_callers_with_provenance_and_persists(self) -> None:
        contract = {"requirements": ["start must still call Service.run"]}
        capsule = await self.compiler.compile(
            "Change Service.run while preserving its start caller",
            contract=contract,
            roots=("Service.run",),
            token_budget=2_400,
            repo=self.repo,
        )

        sources = {item.source for item in capsule.items}
        self.assertIn("src/service.ts", sources)
        self.assertIn("src/main.ts", sources)
        self.assertLessEqual(capsule.estimated_tokens, capsule.token_budget)
        self.assertGreater(capsule.unrelated_files, 0)
        self.assertTrue(all(len(item.content_hash) == 64 for item in capsule.items))
        self.assertIn("start must still call Service.run", capsule.render())

        loaded = await self.compiler.load(capsule.id, repo=self.repo)
        self.assertEqual(loaded, capsule)
        self.assertTrue((await self.compiler.freshness(loaded)).fresh)

    async def test_marks_changed_sources_stale_and_auto_refreshes_graph(self) -> None:
        original = await self.compiler.compile(
            "Change Service.run",
            roots=("Service.run",),
            repo=self.repo,
        )
        service = self.repo / "src" / "service.ts"
        service.write_text(
            service.read_text(encoding="utf-8") + "export const changed = true;\n",
            encoding="utf-8",
        )

        stale = await self.compiler.freshness(original)
        self.assertFalse(stale.fresh)
        self.assertEqual(stale.changed_files, ("src/service.ts",))
        with self.assertRaisesRegex(AtlasStale, "Code Atlas is stale"):
            await self.compiler.compile(
                "Change Service.run",
                roots=("Service.run",),
                auto_refresh=False,
                repo=self.repo,
            )

        refreshed = await self.compiler.compile(
            "Change Service.run",
            roots=("Service.run",),
            repo=self.repo,
        )
        self.assertNotEqual(refreshed.id, original.id)
        self.assertTrue((await self.compiler.freshness(refreshed)).fresh)

        ignore_file = self.repo / ".gitignore"
        ignore_file.write_text(
            ignore_file.read_text(encoding="utf-8") + "coverage/\n", encoding="utf-8"
        )
        unrelated_stale = await self.compiler.freshness(refreshed)
        self.assertFalse(unrelated_stale.fresh)
        self.assertTrue(unrelated_stale.graph_changed)
        self.assertIn(".gitignore", unrelated_stale.changed_files)

    async def test_enforces_budget_and_rejects_tampered_capsule(self) -> None:
        capsule = await self.compiler.compile(
            "Inspect the service and caller",
            paths=("src/service.ts", "src/main.ts", "src/unrelated.ts"),
            token_budget=700,
            repo=self.repo,
        )
        self.assertLessEqual(capsule.estimated_tokens, 700)
        self.assertTrue(
            any(item.reason == "capsule token budget" for item in capsule.excluded)
        )

        capsule_files = list((self.root / "atlas-state" / "capsules").glob("**/*.json"))
        self.assertEqual(len(capsule_files), 1)
        payload = json.loads(capsule_files[0].read_text(encoding="utf-8"))
        payload["task"] = "tampered"
        capsule_files[0].write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(CapsuleError, "digest mismatch"):
            await self.compiler.load(capsule.id, repo=self.repo)


if __name__ == "__main__":
    unittest.main()
