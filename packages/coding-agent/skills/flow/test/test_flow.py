from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

SKILLS_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SKILLS_ROOT.parents[2]
sys.path.insert(0, str(REPO_ROOT / "prime-agent-runtime" / "src"))
sys.path.insert(0, str(SKILLS_ROOT / "flow" / "src"))

from flow import (
    ArtifactSpec,
    ArtifactValidationError,
    FlowBudget,
    FlowConflict,
    FlowError,
    FlowRuntime,
    RlmTaskExecutor,
    TaskExecution,
    TaskExecutionError,
    TaskPolicy,
)


class RecordingExecutor:
    def __init__(self) -> None:
        self.attempts: dict[str, list[int]] = {}
        self.active_resources: set[str] = set()
        self.resource_overlap = False
        self.context_inputs: dict[str, object] = {}

    async def execute(self, context):
        self.attempts.setdefault(context.spec.id, []).append(context.attempt)
        resources = set(context.spec.resources)
        if resources & self.active_resources:
            self.resource_overlap = True
        self.active_resources.update(resources)
        try:
            await asyncio.sleep(0.02)
            if context.spec.id == "flaky" and context.attempt == 1:
                raise TaskExecutionError("transient failure")
            self.context_inputs[context.spec.id] = dict(context.inputs)
            outputs = {}
            for spec in context.spec.produces:
                if spec.name == "piece":
                    outputs[spec.name] = {"task": context.spec.id}
                else:
                    outputs[spec.name] = {"inputs": dict(context.inputs)}
            return TaskExecution(outputs=outputs, tokens=context.attempt)
        finally:
            self.active_resources.difference_update(resources)


class FlowRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    async def test_dag_retries_resources_and_typed_artifacts(self) -> None:
        executor = RecordingExecutor()
        runtime = FlowRuntime(self.state, executor=executor, max_parallel=3)
        run = await runtime.create("Build a verified result", repo=self.repo)
        flaky = await run.task(
            "Flaky producer",
            id="flaky",
            prompt="Produce the first piece.",
            policy=TaskPolicy(max_attempts=2, backoff_seconds=0),
            produces=(ArtifactSpec("piece", required_keys=("task",)),),
            resources=("database",),
        )
        steady = await run.task(
            "Steady producer",
            id="steady",
            prompt="Produce the second piece.",
            produces=(ArtifactSpec("piece", required_keys=("task",)),),
            resources=("database",),
        )
        joined = await run.task(
            "Join pieces",
            id="join",
            prompt="Join both typed pieces.",
            requires=(flaky.id, steady.id),
            consumes=("piece",),
            produces=(ArtifactSpec("result", required_keys=("inputs",)),),
        )

        result = await run.run()

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(executor.attempts[flaky.id], [1, 2])
        self.assertEqual(executor.attempts[steady.id], [1])
        self.assertEqual(executor.attempts[joined.id], [1])
        self.assertFalse(executor.resource_overlap)
        self.assertEqual(len(executor.context_inputs[joined.id]["piece"]), 2)
        self.assertEqual(result.total_attempts, 4)
        self.assertEqual(result.total_failures, 1)
        self.assertEqual(result.total_tokens, 4)
        self.assertEqual(len(result.artifacts), 3)
        record, value = await run.blackboard.get(result.artifacts[-1].id)
        self.assertEqual(record.name, "result")
        self.assertIn("piece", value["inputs"])

    async def test_quorum_progress_and_impossible_join_blocking(self) -> None:
        class QuorumExecutor:
            async def execute(self, context):
                if context.spec.id == "bad":
                    raise TaskExecutionError("permanent failure")
                return TaskExecution(outputs={})

        runtime = FlowRuntime(self.state, executor=QuorumExecutor())
        run = await runtime.create("Exercise quorum", repo=self.repo)
        bad = await run.task("Bad", id="bad", prompt="Fail.")
        good = await run.task("Good", id="good", prompt="Pass.")
        any_join = await run.task(
            "Any join",
            id="any",
            prompt="Run after one success.",
            requires=(bad.id, good.id),
            quorum="any",
        )
        all_join = await run.task(
            "All join",
            id="all",
            prompt="Require both successes.",
            requires=(bad.id, good.id),
            quorum="all",
        )

        result = await run.run()
        statuses = {task.spec.id: task.status for task in result.tasks}

        self.assertEqual(result.status, "failed")
        self.assertEqual(statuses[any_join.id], "succeeded")
        self.assertEqual(statuses[all_join.id], "blocked")
        self.assertEqual(statuses[bad.id], "failed")

    async def test_restart_preserves_attempts_and_never_reruns_success(self) -> None:
        executor = RecordingExecutor()
        runtime = FlowRuntime(self.state, executor=executor)
        run = await runtime.create("Recover interrupted work", repo=self.repo)
        done = await run.task("Done", id="done", prompt="Already done.")
        pending = await run.task(
            "Interrupted",
            id="interrupted",
            prompt="Resume once.",
            policy=TaskPolicy(max_attempts=2),
        )
        record = await run.status()
        now = record.created_at
        staged_tasks = tuple(
            replace(
                task,
                status="succeeded",
                attempts=1,
                started_at=now,
                finished_at=now,
            )
            if task.spec.id == done.id
            else replace(task, status="running", attempts=1, started_at=now)
            for task in record.tasks
        )
        runtime.store.update(
            replace(
                record,
                status="running",
                tasks=staged_tasks,
                started_at=now,
                total_attempts=2,
            ),
            expected_revision=record.revision,
        )

        recovered_runtime = FlowRuntime(self.state, executor=executor)
        recovered = await recovered_runtime.load(run.id, repo=self.repo)
        recovered_record = await recovered.status()
        recovered_tasks = {task.spec.id: task for task in recovered_record.tasks}
        self.assertEqual(recovered_tasks[pending.id].attempts, 1)
        self.assertEqual(recovered_tasks[pending.id].status, "pending")
        self.assertEqual(recovered_tasks[done.id].status, "succeeded")

        result = await recovered.run()
        self.assertEqual(result.status, "succeeded")
        self.assertNotIn(done.id, executor.attempts)
        self.assertEqual(executor.attempts[pending.id], [2])
        self.assertEqual(result.total_attempts, 3)

    async def test_cross_runtime_lease_prevents_live_recovery(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class SlowExecutor:
            async def execute(self, context):
                started.set()
                await release.wait()
                return TaskExecution(outputs={})

        runtime = FlowRuntime(self.state, executor=SlowExecutor())
        run = await runtime.create("Lease live work", repo=self.repo)
        await run.task("Slow", id="slow", prompt="Wait.")
        active = asyncio.create_task(run.run())
        await asyncio.wait_for(started.wait(), timeout=2)

        other = FlowRuntime(self.state, executor=SlowExecutor())
        with self.assertRaisesRegex(FlowConflict, "leased"):
            await other.load(run.id, repo=self.repo)

        release.set()
        self.assertEqual((await active).status, "succeeded")

    async def test_cancellation_settles_executor_and_persists_terminal_state(
        self,
    ) -> None:
        started = asyncio.Event()
        settled = asyncio.Event()

        class CancelExecutor:
            async def execute(self, context):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    settled.set()

        runtime = FlowRuntime(self.state, executor=CancelExecutor())
        run = await runtime.create("Cancel live work", repo=self.repo)
        await run.task("Long", id="long", prompt="Wait forever.")
        active = asyncio.create_task(run.run())
        await asyncio.wait_for(started.wait(), timeout=2)

        cancelled = await run.cancel()

        self.assertEqual(cancelled.status, "cancelled")
        self.assertTrue(settled.is_set())
        self.assertEqual((await active).status, "cancelled")
        self.assertEqual((await run.status()).tasks[0].status, "cancelled")

    async def test_default_rlm_executor_uses_route_cwd_and_atomic_output(self) -> None:
        calls: list[dict[str, object]] = []
        deleted: list[str] = []
        children: list[SimpleNamespace] = []

        async def spawn(prompt: str, **kwargs):
            calls.append({"prompt": prompt, **kwargs})
            output_path = Path(prompt.rsplit("OUTPUT_PATH=", 1)[1].strip())
            temporary = output_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"answer": {"ok": True}}), encoding="utf-8")
            os.replace(temporary, output_path)
            child = SimpleNamespace(
                rlm_child_id="child-1",
                session_name=kwargs["name"],
                status="completed",
            )
            children[:] = [child]
            return SimpleNamespace(rlm_child_id="child-1")

        async def list_children():
            return list(children)

        async def delete_child(child_id: str):
            deleted.append(child_id)
            children.clear()

        executor = RlmTaskExecutor(
            spawn=spawn,
            list_children=list_children,
            delete_child=delete_child,
            poll_seconds=0.001,
        )
        runtime = FlowRuntime(self.state, executor=executor)
        run = await runtime.create("Run a routed child", repo=self.repo)
        await run.task(
            "Child task",
            id="child-task",
            prompt="Publish an answer.",
            route="deep",
            worker="architecture",
            produces=(ArtifactSpec("answer", required_keys=("ok",)),),
        )

        result = await run.run()

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(calls[0]["route"], "deep")
        self.assertEqual(calls[0]["task_type"], "architecture")
        self.assertEqual(calls[0]["cwd"], str(self.repo.resolve()))
        prompt = calls[0]["prompt"]
        self.assertIsInstance(prompt, str)
        output_dir = Path(prompt.rsplit("OUTPUT_PATH=", 1)[1].strip()).parent
        self.assertEqual(
            calls[0]["capabilities"],
            {
                "filesystem": {
                    "read": [str(self.repo.resolve()), str(output_dir)],
                    "write": [str(self.repo.resolve()), str(output_dir)],
                }
            },
        )
        self.assertEqual(deleted, ["child-1"])

    async def test_default_executor_enforces_measured_token_budget(self) -> None:
        children: list[SimpleNamespace] = []

        async def spawn(prompt: str, **kwargs):
            output_path = Path(prompt.rsplit("OUTPUT_PATH=", 1)[1].strip())
            output_path.write_text("{}", encoding="utf-8")
            child = SimpleNamespace(
                rlm_child_id="budget-child",
                session_name=kwargs["name"],
                status="completed",
                usage_tokens=7,
            )
            children[:] = [child]
            return SimpleNamespace(rlm_child_id=child.rlm_child_id)

        async def list_children():
            return list(children)

        async def delete_child(child_id: str):
            children[:] = [
                child for child in children if child.rlm_child_id != child_id
            ]

        runtime = FlowRuntime(
            self.state,
            executor=RlmTaskExecutor(
                spawn=spawn,
                list_children=list_children,
                delete_child=delete_child,
                poll_seconds=0.001,
            ),
        )
        run = await runtime.create(
            "Enforce child tokens",
            repo=self.repo,
            budget=FlowBudget(max_tokens=6),
        )
        await run.task("Spend tokens", id="spend", prompt="Complete once.")

        result = await run.run()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.total_tokens, 7)
        self.assertEqual(result.total_attempts, 1)
        self.assertEqual(result.tasks[0].status, "succeeded")
        self.assertEqual(result.tasks[0].child_id, "budget-child")
        self.assertIsNotNone(result.tasks[0].child_name)
        self.assertEqual(result.tasks[0].child_status, "completed")
        self.assertTrue(result.tasks[0].child_settled)

    async def test_default_executor_fails_closed_without_trusted_usage(self) -> None:
        children: list[SimpleNamespace] = []
        spawn_count = 0

        async def spawn(prompt: str, **kwargs):
            nonlocal spawn_count
            spawn_count += 1
            output_path = Path(prompt.rsplit("OUTPUT_PATH=", 1)[1].strip())
            output_path.write_text("{}", encoding="utf-8")
            child = SimpleNamespace(
                rlm_child_id="unmeasured-child",
                session_name=kwargs["name"],
                status="completed",
                usage_tokens=None,
            )
            children[:] = [child]
            return SimpleNamespace(rlm_child_id=child.rlm_child_id)

        async def list_children():
            return list(children)

        async def delete_child(child_id: str):
            children.clear()

        runtime = FlowRuntime(
            self.state,
            executor=RlmTaskExecutor(
                spawn=spawn,
                list_children=list_children,
                delete_child=delete_child,
                poll_seconds=0.001,
            ),
        )
        run = await runtime.create(
            "Require measured usage",
            repo=self.repo,
            budget=FlowBudget(max_tokens=100),
        )
        await run.task(
            "Unmeasured",
            id="unmeasured",
            prompt="Complete once.",
            policy=TaskPolicy(max_attempts=3, backoff_seconds=0),
        )

        result = await run.run()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.tasks[0].status, "failed")
        self.assertIn("usage could not be measured", result.tasks[0].error)
        self.assertEqual(spawn_count, 1)

    async def test_orphan_recovery_settles_before_retry(self) -> None:
        events: list[str] = []
        children = [
            SimpleNamespace(
                rlm_child_id="old-child",
                session_name="persisted-child",
                status="running",
                usage_tokens=3,
            )
        ]

        async def spawn(prompt: str, **kwargs):
            events.append("spawn")
            self.assertNotIn("old-child", [child.rlm_child_id for child in children])
            output_path = Path(prompt.rsplit("OUTPUT_PATH=", 1)[1].strip())
            output_path.write_text("{}", encoding="utf-8")
            child = SimpleNamespace(
                rlm_child_id="retry-child",
                session_name=kwargs["name"],
                status="completed",
                usage_tokens=2,
            )
            children[:] = [child]
            return SimpleNamespace(rlm_child_id=child.rlm_child_id)

        async def list_children():
            return list(children)

        async def delete_child(child_id: str):
            events.append(f"delete:{child_id}")
            children[:] = [
                child for child in children if child.rlm_child_id != child_id
            ]

        executor = RlmTaskExecutor(
            spawn=spawn,
            list_children=list_children,
            delete_child=delete_child,
            poll_seconds=0.001,
        )
        runtime = FlowRuntime(self.state, executor=executor)
        run = await runtime.create("Recover an orphan", repo=self.repo)
        await run.task(
            "Interrupted",
            id="interrupted",
            prompt="Retry only after settlement.",
            policy=TaskPolicy(max_attempts=2, backoff_seconds=0),
        )
        record = await run.status()
        task = replace(
            record.tasks[0],
            status="running",
            attempts=1,
            started_at=record.created_at,
            child_id="old-child",
            child_name="persisted-child",
            child_status="running",
            child_settled=False,
        )
        runtime.store.update(
            replace(
                record,
                status="running",
                tasks=(task,),
                started_at=record.created_at,
                total_attempts=1,
            ),
            expected_revision=record.revision,
        )

        recovered = await FlowRuntime(self.state, executor=executor).load(
            run.id, repo=self.repo
        )
        recovered_record = await recovered.status()
        self.assertEqual(recovered_record.tasks[0].status, "pending")
        self.assertEqual(events, ["delete:old-child"])

        result = await recovered.run()

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(events[:2], ["delete:old-child", "spawn"])
        self.assertEqual(result.total_tokens, 5)

    async def test_orphan_recovery_fails_when_settlement_is_uncertain(self) -> None:
        spawn_count = 0
        child = SimpleNamespace(
            rlm_child_id="stuck-child",
            session_name="stuck",
            status="running",
            usage_tokens=1,
        )

        async def spawn(prompt: str, **kwargs):
            nonlocal spawn_count
            spawn_count += 1

        async def list_children():
            return [child]

        async def delete_child(child_id: str):
            raise RuntimeError("daemon unavailable")

        executor = RlmTaskExecutor(
            spawn=spawn,
            list_children=list_children,
            delete_child=delete_child,
            poll_seconds=0.001,
        )
        runtime = FlowRuntime(self.state, executor=executor)
        run = await runtime.create("Fail closed on orphan", repo=self.repo)
        await run.task(
            "Stuck",
            id="stuck",
            prompt="Must not overlap.",
            policy=TaskPolicy(max_attempts=2),
        )
        record = await run.status()
        task = replace(
            record.tasks[0],
            status="running",
            attempts=1,
            started_at=record.created_at,
            child_id=child.rlm_child_id,
            child_name=child.session_name,
            child_status="running",
            child_settled=False,
        )
        runtime.store.update(
            replace(
                record,
                status="running",
                tasks=(task,),
                started_at=record.created_at,
                total_attempts=1,
            ),
            expected_revision=record.revision,
        )

        recovered = await FlowRuntime(self.state, executor=executor).load(
            run.id, repo=self.repo
        )
        recovered_record = await recovered.status()

        self.assertEqual(recovered_record.status, "failed")
        self.assertEqual(recovered_record.tasks[0].status, "failed")
        self.assertFalse(recovered_record.tasks[0].child_settled)
        self.assertEqual(spawn_count, 0)

    async def test_cleanup_failure_does_not_repeat_completed_side_effects(self) -> None:
        children: list[SimpleNamespace] = []
        side_effects = 0

        async def spawn(prompt: str, **kwargs):
            nonlocal side_effects
            side_effects += 1
            output_path = Path(prompt.rsplit("OUTPUT_PATH=", 1)[1].strip())
            output_path.write_text("{}", encoding="utf-8")
            child = SimpleNamespace(
                rlm_child_id="completed-child",
                session_name=kwargs["name"],
                status="completed",
                usage_tokens=4,
            )
            children[:] = [child]
            return SimpleNamespace(rlm_child_id=child.rlm_child_id)

        async def list_children():
            return list(children)

        async def delete_child(child_id: str):
            raise RuntimeError("cleanup unavailable")

        runtime = FlowRuntime(
            self.state,
            executor=RlmTaskExecutor(
                spawn=spawn,
                list_children=list_children,
                delete_child=delete_child,
                poll_seconds=0.001,
            ),
        )
        run = await runtime.create("Do a side effect once", repo=self.repo)
        await run.task(
            "Side effect",
            id="side-effect",
            prompt="Complete once.",
            policy=TaskPolicy(max_attempts=3, backoff_seconds=0),
        )

        first = await run.run()
        second = await run.run()

        self.assertEqual(first.status, "succeeded")
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(first.total_tokens, 4)
        self.assertEqual(side_effects, 1)

    async def test_cancellation_fails_while_child_may_remain_alive(self) -> None:
        admitted = asyncio.Event()
        child = SimpleNamespace(
            rlm_child_id="live-child",
            session_name="",
            status="running",
            usage_tokens=1,
        )

        async def spawn(prompt: str, **kwargs):
            child.session_name = kwargs["name"]
            admitted.set()
            return SimpleNamespace(rlm_child_id=child.rlm_child_id)

        async def list_children():
            return [child]

        async def delete_child(child_id: str):
            raise RuntimeError("child daemon unreachable")

        runtime = FlowRuntime(
            self.state,
            executor=RlmTaskExecutor(
                spawn=spawn,
                list_children=list_children,
                delete_child=delete_child,
                poll_seconds=0.001,
            ),
        )
        run = await runtime.create("Cancel safely", repo=self.repo)
        await run.task("Live child", id="live", prompt="Keep running.")
        active = asyncio.create_task(run.run())
        await asyncio.wait_for(admitted.wait(), timeout=2)

        cancelled = await run.cancel()

        self.assertEqual(cancelled.status, "failed")
        self.assertEqual(cancelled.tasks[0].status, "failed")
        self.assertFalse(cancelled.tasks[0].child_settled)
        self.assertFalse(active.done() and active.cancelled())
        self.assertEqual((await active).status, "failed")

    async def test_partial_artifact_stage_is_never_published(self) -> None:
        class TwoOutputExecutor:
            async def execute(self, context):
                return TaskExecution(
                    outputs={"first": {"ok": True}, "second": {"ok": True}}
                )

        runtime = FlowRuntime(self.state, executor=TwoOutputExecutor())
        original_stage = runtime.blackboard.stage
        staged = 0

        async def fail_second_stage(*args, **kwargs):
            nonlocal staged
            staged += 1
            if staged == 2:
                raise ArtifactValidationError("simulated partial write")
            return await original_stage(*args, **kwargs)

        runtime.blackboard.stage = fail_second_stage
        run = await runtime.create("Publish atomically", repo=self.repo)
        await run.task(
            "Two outputs",
            id="two",
            prompt="Produce both.",
            produces=(ArtifactSpec("first"), ArtifactSpec("second")),
            policy=TaskPolicy(max_attempts=1),
        )

        result = await run.run()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.artifacts, ())
        self.assertEqual(await run.artifacts(), ())
        self.assertEqual(await runtime.blackboard.query(run.id), ())

    async def test_store_failure_does_not_commit_staged_artifacts(self) -> None:
        class OutputExecutor:
            async def execute(self, context):
                return TaskExecution(outputs={"result": {"ok": True}})

        runtime = FlowRuntime(self.state, executor=OutputExecutor())
        run = await runtime.create("Reject store commit", repo=self.repo)
        await run.task(
            "Output",
            id="output",
            prompt="Produce one output.",
            produces=(ArtifactSpec("result"),),
        )
        original_update = runtime.store.update

        def fail_success(record, *, expected_revision=None):
            if any(task.status == "succeeded" for task in record.tasks):
                raise FlowError("simulated store failure")
            return original_update(record, expected_revision=expected_revision)

        runtime.store.update = fail_success

        with self.assertRaisesRegex(FlowError, "simulated store failure"):
            await run.run()
        self.assertEqual(await runtime.blackboard.query(run.id), ())
        self.assertEqual(await run.artifacts(), ())

    async def test_blackboard_and_store_fail_closed_on_tampering(self) -> None:
        runtime = FlowRuntime(self.state, executor=RecordingExecutor())
        artifact = await runtime.blackboard.publish(
            "flow-id",
            "task-id",
            ArtifactSpec("fact", required_keys=("value",)),
            {"value": 1},
        )
        target = runtime.blackboard.root / artifact.path
        target.write_text("{}", encoding="utf-8")
        with self.assertRaises(ArtifactValidationError):
            await runtime.blackboard.get(artifact.id)

        run = await runtime.create("Tamper the store", repo=self.repo)
        with sqlite3.connect(runtime.store.path) as connection:
            connection.execute(
                "UPDATE flows SET payload = ? WHERE id = ?",
                ("{}", run.id),
            )
            connection.commit()
        with self.assertRaises(FlowError):
            await run.status()

    async def test_model_validation_rejects_ambiguous_json_and_quorum(self) -> None:
        with self.assertRaises(ArtifactValidationError):
            ArtifactSpec("number", value_type="number").validate(True)
        with self.assertRaises(ValueError):
            TaskPolicy(max_attempts=True)
        runtime = FlowRuntime(self.state, executor=RecordingExecutor())
        run = await runtime.create("Reject invalid quorum", repo=self.repo)
        with self.assertRaises(ValueError):
            await run.task("Invalid", prompt="Invalid.", quorum=0)


if __name__ == "__main__":
    unittest.main()
