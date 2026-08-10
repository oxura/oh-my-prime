from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SKILLS_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SKILLS_ROOT.parents[2]
sys.path.insert(0, str(REPO_ROOT / "prime-agent-runtime" / "src"))
sys.path.insert(0, str(SKILLS_ROOT / "models" / "src"))

from models import ModelMesh, NoEligibleModel  # noqa: E402
from rlm import RLMModel, RLMModelCost  # noqa: E402


def make_model(
    selector: str,
    *,
    name: str | None = None,
    reasoning: bool = True,
    vision: bool = False,
    context_window: int = 128_000,
    input_cost: float = 1,
    output_cost: float = 5,
) -> RLMModel:
    provider, model_id = selector.split("/", 1)
    return RLMModel(
        provider=provider,
        id=model_id,
        name=name or model_id,
        selector=selector,
        reasoning=reasoning,
        input=("text", "image") if vision else ("text",),
        context_window=context_window,
        max_tokens=32_000,
        cost=RLMModelCost(
            input=input_cost,
            output=output_cost,
            cache_read=input_cost / 10,
            cache_write=input_cost,
        ),
    )


class ModelMeshTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.catalog = [
            make_model("cloud/flash-mini", reasoning=False, input_cost=0.1, output_cost=0.4),
            make_model("cloud/coder-pro", input_cost=3, output_cost=12),
            make_model("cloud/reviewer-pro", vision=True, context_window=256_000, input_cost=5, output_cost=20),
            make_model("local/ollama-coder", input_cost=0, output_cost=0),
        ]
        self.requests: list[tuple[str, int]] = []

        async def source(query: str, limit: int) -> list[RLMModel]:
            self.requests.append((query, limit))
            return list(self.catalog)

        self.source = source
        self.mesh = ModelMesh(self.root / "model-mesh.json", model_source=source)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    async def test_builtin_routes_filter_capabilities_and_cost(self) -> None:
        fast = await self.mesh.resolve("fast", task_type="scout")
        vision = await self.mesh.resolve("vision", task_type="ui")
        local = await self.mesh.resolve("private-local", task_type="private")
        long_context = await self.mesh.resolve("long-context", task_type="repository")

        self.assertEqual(fast.selector, "cloud/flash-mini")
        self.assertEqual(fast.effort, "low")
        self.assertEqual(vision.selector, "cloud/reviewer-pro")
        self.assertEqual(local.selector, "local/ollama-coder")
        self.assertGreaterEqual(long_context.model.context_window, 128_000)
        self.assertTrue(all(request == ("", 100) for request in self.requests))

    async def test_configured_globs_and_independence_exclusions_are_enforced(self) -> None:
        await self.mesh.configure(
            "security-review",
            candidates=("cloud/reviewer-*", "cloud/coder-*"),
            effort="xhigh",
            require_reasoning=True,
            min_context_window=100_000,
        )

        primary = await self.mesh.resolve("security-review", task_type="auth")
        checker = await self.mesh.resolve(
            "security-review",
            task_type="auth",
            independent_of=primary,
        )

        self.assertEqual(primary.selector, "cloud/reviewer-pro")
        self.assertEqual(primary.effort, "xhigh")
        self.assertEqual(checker.selector, "cloud/coder-pro")
        self.assertNotEqual(primary.selector, checker.selector)

    async def test_verified_history_changes_future_routing_and_persists(self) -> None:
        await self.mesh.configure(
            "learned",
            candidates=("cloud/coder-pro", "cloud/reviewer-pro"),
            effort="high",
            exploration_weight=0,
        )
        first = await self.mesh.resolve("learned", task_type="typescript")
        second = await self.mesh.resolve(
            "learned",
            task_type="typescript",
            independent_of=first,
        )
        for _ in range(5):
            await self.mesh.record(first, verified=False, duration_ms=1_000)
            await self.mesh.record(second, verified=True, duration_ms=800)

        reloaded = ModelMesh(self.root / "model-mesh.json", model_source=self.source)
        selected = await reloaded.resolve("learned", task_type="typescript")

        self.assertEqual(selected.selector, second.selector)
        self.assertEqual(selected.prior.trials, 5)
        self.assertEqual(selected.prior.verified, 5)
        self.assertEqual(selected.prior.mean_duration_ms, 800)

    async def test_missing_capability_never_falls_back_to_ineligible_model(self) -> None:
        self.catalog = [make_model("cloud/text-only", vision=False)]

        with self.assertRaisesRegex(NoEligibleModel, "no authenticated model satisfies route"):
            await self.mesh.resolve("vision")


if __name__ == "__main__":
    unittest.main()
