from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from pathlib import Path

from prove import ProofError

from ._memory import EvolutionLab
from ._models import (
    EvidenceRef,
    EvolutionError,
    MemoryCandidate,
    PromotionRejected,
    PromotionResult,
    ReplayReport,
    RollbackResult,
)
from ._replay import ReplayRuntime
from ._store import EvolutionStore

_JOURNAL_VERSION = 1


class PromotionRuntime:
    """Gate harness activation on independent replay and shadow reports."""

    def __init__(self, lab: EvolutionLab, replay: ReplayRuntime) -> None:
        self.lab = lab
        self.replay = replay

    async def begin_shadow(
        self,
        candidate_id: str,
        replay_report: ReplayReport | str,
        *,
        harness_state_dir: str | os.PathLike[str] | None = None,
        repo: str | os.PathLike[str] = ".",
    ) -> MemoryCandidate:
        await self._recover_candidate(candidate_id, repo)
        candidate, _ = await self.lab._candidate_and_store(candidate_id, repo)
        report = await self._attested_report(replay_report, repo)
        _, common_dir, _ = await self.lab._repo(repo)
        shadow_dir = self._shadow_dir(common_dir, candidate.id)
        self._require_managed_path(shadow_dir, "shadow state")
        admission_lock = shadow_dir.parent / f".{candidate.id}.admission.lock"
        async with self._lock(admission_lock):
            candidate, store = await self.lab._candidate_and_store(candidate_id, repo)
            if candidate.status != "candidate":
                raise PromotionRejected(
                    f"candidate must be pending, got {candidate.status!r}"
                )
            self._require_report(report, candidate, phase="replay")
            freshness = await self.lab.freshness(candidate)
            if not freshness.fresh:
                raise PromotionRejected(
                    f"candidate is stale: {'; '.join(freshness.reasons)}"
                )
            await self._require_clean_repo(candidate.repo_root)
            _, _, current_revision = await self.lab._repo(repo)
            if current_revision != report.source_revision:
                raise PromotionRejected("repository changed after replay evaluation")
            source_dir = self.replay._harness_dir(candidate, harness_state_dir)
            source_path = source_dir / "harness_state.json"
            if source_path.is_symlink():
                raise PromotionRejected("admitted harness state is a symlink")
            harness_lock = source_path.with_name(f".{source_path.name}.evolution.lock")
            async with self._lock(harness_lock):
                baseline = await asyncio.to_thread(
                    self.replay._load_harness, source_path
                )
                before = baseline["entries"]["memory"].get(candidate.target_id)
                before_digest = self._entry_digest(before)
                if shadow_dir.is_symlink():
                    await asyncio.to_thread(shadow_dir.unlink)
                elif shadow_dir.exists():
                    await asyncio.to_thread(self._remove_tree, shadow_dir)
                timestamp = self.lab._now_text()
                evidence = self._report_evidence(report)
                shadow = json.loads(json.dumps(baseline))
                shadow["entries"]["memory"][candidate.target_id] = (
                    self.replay._harness_entry(
                        candidate,
                        before,
                        status="shadow",
                        timestamp=timestamp,
                    )
                )
                shadow_state_path = shadow_dir / "harness_state.json"
                await asyncio.to_thread(
                    self._write_json_atomic,
                    shadow_state_path,
                    shadow,
                )
                shadow_state_sha256 = await asyncio.to_thread(
                    ReplayRuntime._sha256, shadow_state_path
                )
                metadata = {
                    **candidate.metadata,
                    "shadow": {
                        "harness_state_path": str(source_path.resolve()),
                        "shadow_state_path": str(shadow_state_path.resolve()),
                        "shadow_state_sha256": shadow_state_sha256,
                        "replay_report_id": report.id,
                        "replay_report_digest": report.digest,
                        "source_candidate_digest": candidate.digest,
                        "source_candidate_revision": candidate.revision,
                        "baseline_entry_digest": before_digest,
                        "started_at": timestamp,
                    },
                }
                updated = replace(
                    candidate,
                    revision=candidate.revision + 1,
                    status="shadow",
                    metadata=metadata,
                    updated_at=timestamp,
                )
                try:
                    return await self._settled_store_update(
                        store,
                        updated,
                        action="begin_shadow",
                        reason=f"replay {report.id} passed without regressions",
                        evidence=(evidence,),
                    )
                except BaseException:
                    try:
                        latest = await asyncio.to_thread(store.get, candidate.id)
                    except (EvolutionError, OSError, ValueError, sqlite3.Error):
                        latest = None
                    expected_shadow = updated.metadata.get("shadow")
                    latest_shadow = (
                        latest.metadata.get("shadow") if latest is not None else None
                    )
                    committed = (
                        latest is not None
                        and latest.status in {"shadow", "active"}
                        and isinstance(expected_shadow, dict)
                        and isinstance(latest_shadow, dict)
                        and latest_shadow.get("shadow_state_path")
                        == expected_shadow.get("shadow_state_path")
                        and latest_shadow.get("source_candidate_digest")
                        == expected_shadow.get("source_candidate_digest")
                    )
                    if latest is not None and not committed:
                        await asyncio.to_thread(self._remove_tree, shadow_dir)
                    raise

    async def evaluate_shadow(
        self,
        candidate_id: str,
        shadow_report: ReplayReport | str,
        *,
        repo: str | os.PathLike[str] = ".",
    ) -> MemoryCandidate:
        await self._recover_candidate(candidate_id, repo)
        candidate, store = await self.lab._candidate_and_store(candidate_id, repo)
        if candidate.status != "shadow":
            raise PromotionRejected(
                f"candidate must be in shadow, got {candidate.status!r}"
            )
        report = await self._attested_report(shadow_report, repo)
        if report.phase != "shadow" or report.candidate_id != candidate.id:
            raise PromotionRejected("shadow report does not target this candidate")
        if (
            report.candidate_digest != candidate.digest
            or report.candidate_revision != candidate.revision
        ):
            raise PromotionRejected("shadow report targets a stale candidate revision")
        shadow_metadata = self._shadow_metadata(candidate)
        replay_report_id = shadow_metadata.get("replay_report_id")
        if not isinstance(replay_report_id, str):
            raise PromotionRejected("candidate shadow replay metadata is invalid")
        replay = await self._attested_report(replay_report_id, repo)
        if (
            report.suite_digest != replay.suite_digest
            or report.source_revision != replay.source_revision
        ):
            raise PromotionRejected(
                "shadow report must use the admitted replay suite and source revision"
            )
        source_digest = shadow_metadata.get("source_candidate_digest")
        source_revision = shadow_metadata.get("source_candidate_revision")
        if (
            replay.candidate_digest != source_digest
            or replay.candidate_revision != source_revision
            or not isinstance(source_revision, int)
            or candidate.revision != source_revision + 1
        ):
            raise PromotionRejected("candidate changed after shadow admission")
        _, common_dir, current_revision = await self.lab._repo(repo)
        if current_revision != report.source_revision:
            raise PromotionRejected("repository changed after shadow evaluation")
        await self._require_clean_repo(candidate.repo_root)
        self._shadow_dir_from_candidate(candidate, common_dir)
        if report.status == "passed" and not report.regressions:
            metadata = {
                **candidate.metadata,
                "shadow": {
                    **self._shadow_metadata(candidate),
                    "shadow_report_id": report.id,
                    "shadow_report_digest": report.digest,
                    "evaluated_candidate_digest": candidate.digest,
                    "evaluated_candidate_revision": candidate.revision,
                    "evaluated_at": self.lab._now_text(),
                },
            }
            updated = replace(
                candidate,
                revision=candidate.revision + 1,
                metadata=metadata,
                updated_at=self.lab._now_text(),
            )
            return await self._settled_store_update(
                store,
                updated,
                action="shadow_passed",
                reason=f"shadow replay {report.id} passed",
                evidence=(self._report_evidence(report),),
            )
        updated = replace(
            candidate,
            revision=candidate.revision + 1,
            status="rejected",
            metadata={
                **candidate.metadata,
                "shadow": {
                    **self._shadow_metadata(candidate),
                    "shadow_report_id": report.id,
                    "shadow_report_digest": report.digest,
                    "rejected_at": self.lab._now_text(),
                },
            },
            updated_at=self.lab._now_text(),
        )
        try:
            result = await self._settled_store_update(
                store,
                updated,
                action="shadow_rejected",
                reason=f"shadow replay {report.id} status={report.status}; regressions={','.join(report.regressions)}",
                evidence=(self._report_evidence(report),),
            )
        except asyncio.CancelledError:
            latest = await asyncio.to_thread(store.get, candidate.id)
            if latest.status == "rejected" and latest.metadata.get(
                "shadow"
            ) == updated.metadata.get("shadow"):
                await self.cleanup_shadow(latest, repo=repo)
            raise
        await self.cleanup_shadow(result, repo=repo)
        return result

    async def promote(
        self,
        candidate_id: str,
        replay_report: ReplayReport | str,
        shadow_report: ReplayReport | str,
        *,
        harness_state_dir: str | os.PathLike[str] | None = None,
        repo: str | os.PathLike[str] = ".",
    ) -> PromotionResult:
        await self._recover_candidate(candidate_id, repo)
        candidate, _ = await self.lab._candidate_and_store(candidate_id, repo)
        if candidate.status != "shadow":
            raise PromotionRejected(
                f"candidate must be in shadow, got {candidate.status!r}"
            )
        if candidate.category not in {"verified_knowledge", "known_error"}:
            raise PromotionRejected(f"{candidate.category} cannot become active memory")
        replay = await self._attested_report(replay_report, repo)
        shadow = await self._attested_report(shadow_report, repo)
        self._require_promotion_lineage(candidate, replay, shadow)
        freshness = await self.lab.freshness(candidate, for_activation=True)
        if not freshness.fresh:
            raise PromotionRejected(
                f"candidate is stale: {'; '.join(freshness.reasons)}"
            )
        _, common_dir, current_revision = await self.lab._repo(repo)
        if current_revision != replay.source_revision:
            raise PromotionRejected("repository changed after verified replay")
        await self._require_clean_repo(candidate.repo_root)
        self._shadow_dir_from_candidate(candidate, common_dir)
        admitted_state_path = self._admitted_state_path(candidate)
        source_dir = self.replay._harness_dir(candidate, harness_state_dir)
        state_path = (source_dir / "harness_state.json").absolute()
        if state_path != admitted_state_path:
            raise PromotionRejected(
                "promotion harness differs from the admitted shadow harness"
            )
        if state_path.is_symlink():
            raise PromotionRejected("promotion harness state is a symlink")
        lock_path = state_path.with_name(f".{state_path.name}.evolution.lock")
        async with self._lock(lock_path):
            latest, latest_store = await self.lab._candidate_and_store(
                candidate_id, repo
            )
            if latest.digest != candidate.digest:
                raise PromotionRejected("candidate changed during promotion")
            replay = await self._attested_report(replay.id, repo)
            shadow = await self._attested_report(shadow.id, repo)
            shadow_metadata = self._require_promotion_lineage(latest, replay, shadow)
            freshness = await self.lab.freshness(latest, for_activation=True)
            if not freshness.fresh:
                raise PromotionRejected(
                    f"candidate became stale during promotion: "
                    f"{'; '.join(freshness.reasons)}"
                )
            await self._require_clean_repo(latest.repo_root)
            repo_root, locked_common_dir, current_revision = await self.lab._repo(repo)
            if (
                repo_root != latest.repo_root
                or locked_common_dir != common_dir
                or current_revision != replay.source_revision
                or current_revision != shadow.source_revision
            ):
                raise PromotionRejected(
                    "repository changed while waiting for the promotion lock"
                )
            self._shadow_dir_from_candidate(latest, common_dir)
            if state_path != self._admitted_state_path(latest):
                raise PromotionRejected(
                    "promotion harness differs from the admitted shadow harness"
                )
            candidate, store = latest, latest_store
            timestamp = self.lab._now_text()
            current = await asyncio.to_thread(self.replay._load_harness, state_path)
            records = current["entries"]["memory"]
            before = records.get(candidate.target_id)
            if self._entry_digest(before) != shadow_metadata.get(
                "baseline_entry_digest"
            ):
                raise PromotionRejected(
                    "target harness entry changed after shadow admission"
                )
            after = self.replay._harness_entry(
                candidate,
                before,
                status="active",
                timestamp=timestamp,
            )
            result = self._promotion_result(
                candidate,
                replay,
                shadow,
                state_path,
                before,
                after,
                timestamp,
            )
            result_path = self._promotion_path(common_dir, result.id).absolute()
            self._require_managed_path(result_path, "promotion record")
            changed = json.loads(json.dumps(current))
            changed["entries"]["memory"][candidate.target_id] = after
            updated = replace(
                candidate,
                revision=candidate.revision + 1,
                status="active",
                metadata={
                    **candidate.metadata,
                    "promotion": {
                        "id": result.id,
                        "digest": result.digest,
                        "path": str(result_path),
                        "harness_state_path": str(state_path),
                        "replay_report_id": replay.id,
                        "shadow_report_id": shadow.id,
                        "promoted_at": timestamp,
                    },
                },
                updated_at=timestamp,
            )
            journal_path = self._journal_path(common_dir, candidate.id)
            self._require_managed_path(journal_path, "promotion journal")
            journal = self._journal_payload(
                operation="promote",
                candidate=candidate,
                state_path=state_path,
                before_entry=before,
                after_entry=after,
                record_path=result_path,
                record_payload=self._promotion_payload(result),
            )
            await asyncio.to_thread(self._write_json_atomic, journal_path, journal)
            try:
                await asyncio.to_thread(
                    self._write_json_atomic,
                    result_path,
                    self._promotion_payload(result),
                )
                await asyncio.to_thread(self._write_json_atomic, state_path, changed)
                await self._settled_store_update(
                    store,
                    updated,
                    action="promote",
                    reason=f"replay {replay.id} and shadow {shadow.id} passed",
                    evidence=(
                        self._report_evidence(replay),
                        self._report_evidence(shadow),
                    ),
                )
            except BaseException:
                latest = await asyncio.to_thread(store.get, candidate.id)
                committed = latest.status == "active" and latest.metadata.get(
                    "promotion"
                ) == updated.metadata.get("promotion")
                if committed:
                    await asyncio.to_thread(
                        self._write_json_atomic, state_path, changed
                    )
                else:
                    await asyncio.to_thread(
                        self._write_json_atomic, state_path, current
                    )
                    await asyncio.to_thread(self._unlink_regular, result_path)
                    await asyncio.to_thread(self._unlink_regular, journal_path)
                raise
        await self.cleanup_shadow(updated, repo=repo)
        await asyncio.to_thread(self._unlink_regular, journal_path)
        return result

    async def rollback(
        self,
        candidate_id: str,
        *,
        reason: str,
        repo: str | os.PathLike[str] = ".",
    ) -> RollbackResult:
        return await self._rollback(
            candidate_id,
            reason=reason,
            repo=repo,
        )

    async def _rollback(
        self,
        candidate_id: str,
        *,
        reason: str,
        repo: str | os.PathLike[str],
        expected_digest: str | None = None,
        require_stale: bool = False,
    ) -> RollbackResult:
        await self._recover_candidate(candidate_id, repo)
        candidate, store = await self.lab._candidate_and_store(candidate_id, repo)
        if expected_digest is not None and candidate.digest != expected_digest:
            raise PromotionRejected("candidate changed since maintenance evaluated it")
        if candidate.status != "active":
            raise PromotionRejected(
                f"candidate must be active, got {candidate.status!r}"
            )
        promotion_metadata = candidate.metadata.get("promotion")
        if not isinstance(promotion_metadata, dict):
            raise PromotionRejected("candidate has no promotion record")
        promotion_id = promotion_metadata.get("id")
        promotion_path = promotion_metadata.get("path")
        if not isinstance(promotion_id, str) or not isinstance(promotion_path, str):
            raise PromotionRejected("candidate promotion metadata is invalid")
        _, common_dir, _ = await self.lab._repo(repo)
        expected_path = self._promotion_path(common_dir, promotion_id).absolute()
        supplied_path = Path(os.path.abspath(Path(promotion_path).expanduser()))
        if supplied_path != expected_path or supplied_path.is_symlink():
            raise PromotionRejected("candidate promotion record path is invalid")
        self._require_managed_path(supplied_path, "promotion record")
        promotion = await asyncio.to_thread(self._load_promotion, supplied_path)
        if (
            promotion.candidate_id != candidate.id
            or promotion.id != promotion_id
            or promotion.digest != promotion_metadata.get("digest")
        ):
            raise PromotionRejected("promotion record does not match candidate")
        state_path = Path(os.path.abspath(promotion.harness_state_path.expanduser()))
        if state_path != self._admitted_state_path(candidate):
            raise PromotionRejected("promotion record harness path is invalid")
        timestamp = self.lab._now_text()
        rollback = self._rollback_result(
            candidate, promotion, timestamp, self._text(reason, "reason")
        )
        rollback_path = self._rollback_path(common_dir, rollback.id).absolute()
        self._require_managed_path(rollback_path, "rollback record")
        lock_path = state_path.with_name(f".{state_path.name}.evolution.lock")
        async with self._lock(lock_path):
            latest, latest_store = await self.lab._candidate_and_store(
                candidate_id, repo
            )
            if latest.digest != candidate.digest:
                raise PromotionRejected("candidate changed during rollback")
            if require_stale:
                reasons = await self._candidate_staleness(latest, repo)
                if not reasons:
                    raise PromotionRejected("candidate is no longer stale at rollback")
            candidate, store = latest, latest_store
            current = await asyncio.to_thread(self.replay._load_harness, state_path)
            active_entry = current["entries"]["memory"].get(candidate.target_id)
            if self._entry_digest(active_entry) != self._entry_digest(
                promotion.after_entry
            ):
                raise PromotionRejected("active harness entry changed after promotion")
            restored = json.loads(json.dumps(current))
            records = restored["entries"]["memory"]
            if promotion.before_entry is None:
                records.pop(candidate.target_id, None)
            else:
                records[candidate.target_id] = promotion.before_entry
            updated = replace(
                candidate,
                revision=candidate.revision + 1,
                status="rolled_back",
                metadata={
                    **candidate.metadata,
                    "rollback": {
                        "id": rollback.id,
                        "digest": rollback.digest,
                        "path": str(rollback_path),
                        "reason": rollback.reason,
                        "rolled_back_at": timestamp,
                    },
                },
                updated_at=timestamp,
            )
            journal_path = self._journal_path(common_dir, candidate.id)
            self._require_managed_path(journal_path, "promotion journal")
            journal = self._journal_payload(
                operation="rollback",
                candidate=candidate,
                state_path=state_path,
                before_entry=promotion.after_entry,
                after_entry=promotion.before_entry,
                record_path=rollback_path,
                record_payload=self._rollback_payload(rollback),
            )
            await asyncio.to_thread(self._write_json_atomic, journal_path, journal)
            try:
                await asyncio.to_thread(
                    self._write_json_atomic,
                    rollback_path,
                    self._rollback_payload(rollback),
                )
                await asyncio.to_thread(self._write_json_atomic, state_path, restored)
                await self._settled_store_update(
                    store,
                    updated,
                    action="rollback",
                    reason=rollback.reason,
                )
            except BaseException:
                latest = await asyncio.to_thread(store.get, candidate.id)
                if latest.status == "rolled_back" and latest.metadata.get(
                    "rollback"
                ) == updated.metadata.get("rollback"):
                    await asyncio.to_thread(
                        self._write_json_atomic, state_path, restored
                    )
                else:
                    await asyncio.to_thread(
                        self._write_json_atomic, state_path, current
                    )
                    await asyncio.to_thread(self._unlink_regular, rollback_path)
                await asyncio.to_thread(self._unlink_regular, journal_path)
                raise
            await asyncio.to_thread(self._unlink_regular, journal_path)
        return rollback

    async def maintain(self, *, repo: str | os.PathLike[str] = ".") -> tuple[str, ...]:
        """Rollback active memories whose complete promotion proof became stale."""
        await self.recover(repo=repo)
        active = await self.lab.list(status="active", limit=2_000, repo=repo)
        rolled_back: list[str] = []
        conflicts: list[str] = []
        for candidate in active:
            reasons = await self._candidate_staleness(candidate, repo)
            if not reasons:
                continue
            try:
                await self._rollback(
                    candidate.id,
                    reason=("automatic stale-memory rollback: " + "; ".join(reasons)),
                    repo=repo,
                    expected_digest=candidate.digest,
                    require_stale=True,
                )
            except EvolutionError as error:
                conflicts.append(f"{candidate.id}: {error}")
                continue
            rolled_back.append(candidate.id)
        if conflicts:
            completed = ",".join(rolled_back) or "none"
            raise PromotionRejected(
                "maintenance rollback conflicts: "
                + " | ".join(conflicts)
                + f"; completed={completed}"
            )
        return tuple(rolled_back)

    async def _candidate_staleness(
        self,
        candidate: MemoryCandidate,
        repo: str | os.PathLike[str],
    ) -> tuple[str, ...]:
        freshness = await self.lab.freshness(candidate)
        reasons = list(freshness.reasons)
        proof_count = 0
        for evidence in candidate.evidence:
            if evidence.kind != "verification_report":
                continue
            proof_count += 1
            if not evidence.verifier.startswith("prove:"):
                reasons.append("verification evidence has invalid provenance")
                continue
            report_id = evidence.verifier.rsplit(":", 1)[-1]
            try:
                report = await self.lab._proof_runtime.load_report(
                    report_id,
                    repo=candidate.repo_root,
                )
                if (
                    report.report_path.resolve().as_uri() != evidence.uri
                    or await asyncio.to_thread(
                        ReplayRuntime._sha256, report.report_path
                    )
                    != evidence.sha256
                ):
                    reasons.append("verification ledger attestation changed")
            except (ProofError, OSError) as error:
                reasons.append(f"verification ledger is invalid: {error}")
        if proof_count == 0:
            reasons.append("active memory has no verifier ledger")
        shadow_metadata = candidate.metadata.get("shadow")
        promotion_metadata = candidate.metadata.get("promotion")
        if not isinstance(shadow_metadata, dict) or not isinstance(
            promotion_metadata, dict
        ):
            reasons.append("active memory promotion metadata is invalid")
            return tuple(dict.fromkeys(reasons))
        replay_id = shadow_metadata.get("replay_report_id")
        shadow_id = shadow_metadata.get("shadow_report_id")
        if not isinstance(replay_id, str) or not isinstance(shadow_id, str):
            reasons.append("active memory replay lineage is missing")
            return tuple(dict.fromkeys(reasons))
        try:
            replay = await self._attested_report(replay_id, repo)
            shadow = await self._attested_report(shadow_id, repo)
            if (
                replay.digest != shadow_metadata.get("replay_report_digest")
                or shadow.digest != shadow_metadata.get("shadow_report_digest")
                or replay.status != "passed"
                or shadow.status != "passed"
                or replay.regressions
                or shadow.regressions
                or replay.phase != "replay"
                or shadow.phase != "shadow"
                or replay.suite_digest != shadow.suite_digest
                or replay.source_revision != shadow.source_revision
                or promotion_metadata.get("replay_report_id") != replay.id
                or promotion_metadata.get("shadow_report_id") != shadow.id
            ):
                reasons.append("active memory replay lineage is invalid")
        except EvolutionError as error:
            reasons.append(f"active memory replay evidence is invalid: {error}")
        promotion_id = promotion_metadata.get("id")
        promotion_path = promotion_metadata.get("path")
        if not isinstance(promotion_id, str) or not isinstance(promotion_path, str):
            reasons.append("active memory promotion record is missing")
            return tuple(dict.fromkeys(reasons))
        try:
            _, common_dir, _ = await self.lab._repo(repo)
            expected_path = self._promotion_path(common_dir, promotion_id).absolute()
            supplied_path = Path(os.path.abspath(Path(promotion_path).expanduser()))
            if supplied_path != expected_path or supplied_path.is_symlink():
                raise PromotionRejected("promotion record path is invalid")
            self._require_managed_path(supplied_path, "promotion record")
            promotion = await asyncio.to_thread(self._load_promotion, supplied_path)
            if (
                promotion.candidate_id != candidate.id
                or promotion.id != promotion_id
                or promotion.digest != promotion_metadata.get("digest")
                or promotion.replay_report_id != replay_id
                or promotion.shadow_report_id != shadow_id
                or promotion.target_id != candidate.target_id
                or Path(os.path.abspath(promotion.harness_state_path.expanduser()))
                != self._admitted_state_path(candidate)
            ):
                reasons.append("active memory promotion record is invalid")
        except (EvolutionError, OSError) as error:
            reasons.append(f"active memory promotion record is invalid: {error}")
        return tuple(dict.fromkeys(reasons))

    async def recover(self, *, repo: str | os.PathLike[str] = ".") -> tuple[str, ...]:
        """Reconcile durable promotion journals against the candidate ledger."""
        _, common_dir, _ = await self.lab._repo(repo)
        root = self._journal_root(common_dir)
        self._require_managed_path(root, "promotion journal root")
        if root.is_symlink():
            raise PromotionRejected(f"promotion journal root is a symlink: {root}")
        if not root.exists():
            return ()
        if not root.is_dir():
            raise PromotionRejected(f"promotion journal root is invalid: {root}")
        paths = sorted(path for path in root.iterdir() if path.name.endswith(".json"))
        recovered: list[str] = []
        for path in paths:
            journal = await asyncio.to_thread(self._load_journal, path)
            candidate_id = str(journal["candidate_id"])
            if path != self._journal_path(common_dir, candidate_id):
                raise PromotionRejected(
                    f"promotion journal path does not match candidate: {path}"
                )
            await self._recover_journal(journal, path, common_dir, repo)
            recovered.append(candidate_id)
        return tuple(recovered)

    async def _recover_candidate(
        self,
        candidate_id: str,
        repo: str | os.PathLike[str],
    ) -> None:
        _, common_dir, _ = await self.lab._repo(repo)
        path = self._journal_path(common_dir, candidate_id)
        self._require_managed_path(path, "promotion journal")
        if not path.exists() and not path.is_symlink():
            return
        journal = await asyncio.to_thread(self._load_journal, path)
        if journal["candidate_id"] != candidate_id:
            raise PromotionRejected(
                f"promotion journal targets another candidate: {path}"
            )
        await self._recover_journal(journal, path, common_dir, repo)

    async def _recover_journal(
        self,
        journal: dict[str, object],
        journal_path: Path,
        common_dir: Path,
        repo: str | os.PathLike[str],
    ) -> None:
        self._require_managed_path(journal_path, "promotion journal")
        candidate_id = str(journal["candidate_id"])
        candidate, _ = await self.lab._candidate_and_store(candidate_id, repo)
        state_path = Path(str(journal["state_path"]))
        if not state_path.is_absolute():
            raise PromotionRejected("promotion journal harness path is not absolute")
        state_path = state_path.absolute()
        if state_path != self._admitted_state_path(candidate):
            raise PromotionRejected(
                "promotion journal harness differs from admitted shadow harness"
            )
        operation = str(journal["operation"])
        record_payload = journal["record_payload"]
        if not isinstance(record_payload, dict):
            raise PromotionRejected("promotion journal record is invalid")
        record_id = record_payload.get("id")
        record_digest = record_payload.get("digest")
        if not isinstance(record_id, str) or not isinstance(record_digest, str):
            raise PromotionRejected("promotion journal record identity is invalid")
        expected_record_path = (
            self._promotion_path(common_dir, record_id)
            if operation == "promote"
            else self._rollback_path(common_dir, record_id)
        ).absolute()
        record_path = Path(str(journal["record_path"]))
        if (
            not record_path.is_absolute()
            or record_path.absolute() != expected_record_path
        ):
            raise PromotionRejected("promotion journal record path is invalid")
        self._require_managed_path(record_path, "promotion record")
        if (
            record_payload.get("candidate_id") != candidate.id
            or record_payload.get("target_id") != candidate.target_id
            or Path(
                os.path.abspath(
                    Path(str(record_payload.get("harness_state_path"))).expanduser()
                )
            )
            != state_path
        ):
            raise PromotionRejected("promotion journal record lineage is invalid")
        before = journal["before_entry"]
        after = journal["after_entry"]
        lock_path = state_path.with_name(f".{state_path.name}.evolution.lock")
        committed = False
        async with self._lock(lock_path):
            latest, _ = await self.lab._candidate_and_store(candidate_id, repo)
            if state_path != self._admitted_state_path(latest):
                raise PromotionRejected(
                    "candidate admitted harness changed during recovery"
                )
            if latest.target_id != journal["target_id"]:
                raise PromotionRejected("candidate target changed during recovery")
            if operation == "promote":
                metadata = latest.metadata.get("promotion")
                committed = (
                    latest.status == "active"
                    and isinstance(metadata, dict)
                    and metadata.get("id") == record_id
                    and metadata.get("digest") == record_digest
                    and Path(
                        os.path.abspath(Path(str(metadata.get("path"))).expanduser())
                    )
                    == record_path
                )
                if latest.status == "active" and not committed:
                    raise PromotionRejected(
                        "active candidate conflicts with promotion journal"
                    )
            else:
                metadata = latest.metadata.get("rollback")
                committed = (
                    latest.status == "rolled_back"
                    and isinstance(metadata, dict)
                    and metadata.get("id") == record_id
                    and metadata.get("digest") == record_digest
                    and Path(
                        os.path.abspath(Path(str(metadata.get("path"))).expanduser())
                    )
                    == record_path
                )
                if latest.status == "rolled_back" and not committed:
                    raise PromotionRejected(
                        "rolled-back candidate conflicts with rollback journal"
                    )
            current = await asyncio.to_thread(self.replay._load_harness, state_path)
            current_entry = current["entries"]["memory"].get(latest.target_id)
            current_digest = self._entry_digest(current_entry)
            before_digest = self._entry_digest(before)
            after_digest = self._entry_digest(after)
            desired = after if committed else before
            desired_digest = after_digest if committed else before_digest
            if current_digest not in {before_digest, after_digest}:
                raise PromotionRejected(
                    "harness entry conflicts with promotion recovery journal"
                )
            if current_digest != desired_digest:
                await asyncio.to_thread(
                    self._write_harness_entry,
                    state_path,
                    current,
                    latest.target_id,
                    desired,
                )
            if committed:
                await asyncio.to_thread(
                    self._ensure_record,
                    record_path,
                    record_payload,
                )
            else:
                await asyncio.to_thread(self._unlink_regular, record_path)
            if not (committed and operation == "promote"):
                await asyncio.to_thread(self._unlink_regular, journal_path)
        if committed and operation == "promote":
            await self.cleanup_shadow(latest, repo=repo)
            await asyncio.to_thread(self._unlink_regular, journal_path)

    async def _settled_store_update(
        self,
        store: EvolutionStore,
        candidate: MemoryCandidate,
        *,
        action: str,
        reason: str,
        evidence: tuple[EvidenceRef, ...] = (),
    ) -> MemoryCandidate:
        task = asyncio.create_task(
            asyncio.to_thread(
                store.update,
                candidate,
                action=action,
                reason=reason,
                evidence=evidence,
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    def _journal_root(self, common_dir: Path) -> Path:
        key = hashlib.sha256(
            os.path.normcase(str(common_dir.resolve())).encode("utf-8")
        ).hexdigest()[:20]
        return self.lab.state_root / "transactions" / key

    def _journal_path(self, common_dir: Path, candidate_id: str) -> Path:
        key = hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()[:32]
        return self._journal_root(common_dir) / f"{key}.json"

    def _journal_payload(
        self,
        *,
        operation: str,
        candidate: MemoryCandidate,
        state_path: Path,
        before_entry: object,
        after_entry: object,
        record_path: Path,
        record_payload: dict[str, object],
    ) -> dict[str, object]:
        if operation not in {"promote", "rollback"}:
            raise ValueError(f"unknown journal operation: {operation}")
        payload: dict[str, object] = {
            "schema_version": _JOURNAL_VERSION,
            "operation": operation,
            "candidate_id": candidate.id,
            "expected_candidate_digest": candidate.digest,
            "expected_candidate_revision": candidate.revision,
            "state_path": str(state_path.absolute()),
            "target_id": candidate.target_id,
            "before_entry": json.loads(json.dumps(before_entry)),
            "after_entry": json.loads(json.dumps(after_entry)),
            "record_path": str(record_path.absolute()),
            "record_payload": json.loads(json.dumps(record_payload)),
            "digest": None,
        }
        payload["digest"] = hashlib.sha256(self._canonical(payload)).hexdigest()
        return payload

    @staticmethod
    def _load_journal(path: Path) -> dict[str, object]:
        if path.is_symlink() or not path.is_file():
            raise PromotionRejected(f"promotion journal path is unsafe: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PromotionRejected(f"promotion journal is invalid: {path}") from error
        if not isinstance(payload, dict):
            raise PromotionRejected(f"promotion journal is invalid: {path}")
        digest = payload.get("digest")
        canonical = dict(payload)
        canonical["digest"] = None
        if (
            payload.get("schema_version") != _JOURNAL_VERSION
            or payload.get("operation") not in {"promote", "rollback"}
            or not isinstance(payload.get("candidate_id"), str)
            or not isinstance(payload.get("expected_candidate_digest"), str)
            or not isinstance(payload.get("expected_candidate_revision"), int)
            or not isinstance(payload.get("state_path"), str)
            or not isinstance(payload.get("target_id"), str)
            or not isinstance(payload.get("record_path"), str)
            or not isinstance(payload.get("record_payload"), dict)
            or payload.get("before_entry") is not None
            and not isinstance(payload.get("before_entry"), dict)
            or payload.get("after_entry") is not None
            and not isinstance(payload.get("after_entry"), dict)
            or not isinstance(digest, str)
            or hashlib.sha256(PromotionRuntime._canonical(canonical)).hexdigest()
            != digest
        ):
            raise PromotionRejected(f"promotion journal integrity failed: {path}")
        record = payload["record_payload"]
        record_digest = record.get("digest")
        canonical_record = dict(record)
        canonical_record["digest"] = None
        if (
            not isinstance(record_digest, str)
            or hashlib.sha256(PromotionRuntime._canonical(canonical_record)).hexdigest()
            != record_digest
        ):
            raise PromotionRejected(
                f"promotion journal record integrity failed: {path}"
            )
        return payload

    @staticmethod
    def _write_harness_entry(
        state_path: Path,
        current: dict[str, object],
        target_id: str,
        entry: object,
    ) -> None:
        changed = json.loads(json.dumps(current))
        records = changed["entries"]["memory"]
        if entry is None:
            records.pop(target_id, None)
        else:
            records[target_id] = entry
        PromotionRuntime._write_json_atomic(state_path, changed)

    @staticmethod
    def _ensure_record(path: Path, payload: dict[str, object]) -> None:
        if path.is_symlink():
            raise PromotionRejected(f"promotion record path is unsafe: {path}")
        if path.exists():
            if not path.is_file():
                raise PromotionRejected(f"promotion record path is invalid: {path}")
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise PromotionRejected(
                    f"promotion record is invalid: {path}"
                ) from error
            if current != payload:
                raise PromotionRejected(
                    f"promotion record conflicts with recovery journal: {path}"
                )
            return
        PromotionRuntime._write_json_atomic(path, payload)

    @staticmethod
    def _unlink_regular(path: Path) -> None:
        if path.is_symlink():
            raise PromotionRejected(f"refusing to unlink symlink: {path}")
        if not path.exists():
            return
        if not path.is_file():
            raise PromotionRejected(f"expected a regular file: {path}")
        path.unlink()
        PromotionRuntime._fsync_directory(path.parent)

    async def _attested_report(
        self,
        report: ReplayReport | str,
        repo: str | os.PathLike[str],
    ) -> ReplayReport:
        report_id = report if isinstance(report, str) else report.id
        persisted = await self.replay.load_report(report_id, repo=repo)
        if not isinstance(report, str) and persisted != report:
            raise PromotionRejected(
                f"replay report object differs from persisted evidence: {report_id}"
            )
        return persisted

    @staticmethod
    def _require_report(
        report: ReplayReport, candidate: MemoryCandidate, *, phase: str
    ) -> None:
        if report.phase != phase or report.status != "passed" or report.regressions:
            raise PromotionRejected(f"{phase} report did not pass without regressions")
        if report.candidate_id != candidate.id:
            raise PromotionRejected("report targets another candidate")
        if (
            report.candidate_digest != candidate.digest
            or report.candidate_revision != candidate.revision
        ):
            raise PromotionRejected("report targets a stale candidate revision")

    def _require_promotion_lineage(
        self,
        candidate: MemoryCandidate,
        replay: ReplayReport,
        shadow: ReplayReport,
    ) -> dict[str, object]:
        if candidate.status != "shadow":
            raise PromotionRejected(
                f"candidate must be in shadow, got {candidate.status!r}"
            )
        metadata = self._shadow_metadata(candidate)
        if replay.id != metadata.get(
            "replay_report_id"
        ) or replay.digest != metadata.get("replay_report_digest"):
            raise PromotionRejected(
                "replay report is not the report that admitted shadow mode"
            )
        if (
            replay.status != "passed"
            or replay.regressions
            or replay.phase != "replay"
            or replay.candidate_id != candidate.id
        ):
            raise PromotionRejected("replay report did not pass")
        if shadow.id != metadata.get(
            "shadow_report_id"
        ) or shadow.digest != metadata.get("shadow_report_digest"):
            raise PromotionRejected("shadow report has not passed evaluation")
        if (
            shadow.status != "passed"
            or shadow.regressions
            or shadow.phase != "shadow"
            or shadow.candidate_id != candidate.id
        ):
            raise PromotionRejected("shadow report did not pass")
        if shadow.candidate_digest != metadata.get(
            "evaluated_candidate_digest"
        ) or shadow.candidate_revision != metadata.get("evaluated_candidate_revision"):
            raise PromotionRejected("shadow candidate lineage is invalid")
        source_digest = metadata.get("source_candidate_digest")
        source_revision = metadata.get("source_candidate_revision")
        if (
            replay.candidate_digest != source_digest
            or replay.candidate_revision != source_revision
            or not isinstance(source_revision, int)
            or shadow.candidate_revision != source_revision + 1
        ):
            raise PromotionRejected("candidate shadow admission lineage is invalid")
        evaluated_revision = metadata.get("evaluated_candidate_revision")
        if (
            not isinstance(evaluated_revision, int)
            or candidate.revision != evaluated_revision + 1
        ):
            raise PromotionRejected("candidate changed after shadow evaluation")
        if (
            replay.suite_id != shadow.suite_id
            or replay.suite_digest != shadow.suite_digest
            or replay.source_revision != shadow.source_revision
            or replay.source_revision != candidate.code_version
        ):
            raise PromotionRejected(
                "replay and shadow reports must use the same suite and source revision"
            )
        return metadata

    @staticmethod
    def _shadow_metadata(candidate: MemoryCandidate) -> dict[str, object]:
        value = candidate.metadata.get("shadow")
        if not isinstance(value, dict):
            raise PromotionRejected("candidate has no shadow metadata")
        return value

    @staticmethod
    def _report_evidence(report: ReplayReport) -> EvidenceRef:
        return EvidenceRef(
            uri=report.report_path.resolve().as_uri(),
            sha256=ReplayRuntime._sha256(report.report_path),
            kind="replay_report",
            verified=True,
            verifier=f"evolve:{report.id}",
            captured_at=report.finished_at,
        )

    def _promotion_result(
        self,
        candidate: MemoryCandidate,
        replay: ReplayReport,
        shadow: ReplayReport,
        state_path: Path,
        before: object,
        after: dict[str, object],
        timestamp: str,
    ) -> PromotionResult:
        identity = {
            "candidate": candidate.id,
            "revision": candidate.revision,
            "replay": replay.id,
            "shadow": shadow.id,
            "promoted_at": timestamp,
        }
        result_id = (
            f"promotion-{hashlib.sha256(self._canonical(identity)).hexdigest()[:24]}"
        )
        result = PromotionResult(
            id=result_id,
            candidate_id=candidate.id,
            candidate_revision=candidate.revision,
            replay_report_id=replay.id,
            shadow_report_id=shadow.id,
            harness_state_path=state_path.resolve(),
            target_id=candidate.target_id,
            before_entry=json.loads(json.dumps(before))
            if isinstance(before, dict)
            else None,
            after_entry=json.loads(json.dumps(after)),
            promoted_at=timestamp,
            digest="",
        )
        return replace(result, digest=self._promotion_digest(result))

    def _rollback_result(
        self,
        candidate: MemoryCandidate,
        promotion: PromotionResult,
        timestamp: str,
        reason: str,
    ) -> RollbackResult:
        identity = {
            "candidate": candidate.id,
            "promotion": promotion.id,
            "rolled_back_at": timestamp,
            "reason": reason,
        }
        result_id = (
            f"rollback-{hashlib.sha256(self._canonical(identity)).hexdigest()[:24]}"
        )
        result = RollbackResult(
            id=result_id,
            candidate_id=candidate.id,
            promotion_id=promotion.id,
            harness_state_path=promotion.harness_state_path,
            target_id=candidate.target_id,
            restored_entry=promotion.before_entry,
            rolled_back_at=timestamp,
            reason=reason,
            digest="",
        )
        return replace(result, digest=self._rollback_digest(result))

    @staticmethod
    def _promotion_digest(result: PromotionResult) -> str:
        payload = PromotionRuntime._promotion_payload(result)
        payload["digest"] = None
        return hashlib.sha256(PromotionRuntime._canonical(payload)).hexdigest()

    @staticmethod
    def _rollback_digest(result: RollbackResult) -> str:
        payload = PromotionRuntime._rollback_payload(result)
        payload["digest"] = None
        return hashlib.sha256(PromotionRuntime._canonical(payload)).hexdigest()

    @staticmethod
    def _promotion_payload(result: PromotionResult) -> dict[str, object]:
        payload = asdict(result)
        payload["harness_state_path"] = str(result.harness_state_path)
        return payload

    @staticmethod
    def _rollback_payload(result: RollbackResult) -> dict[str, object]:
        payload = asdict(result)
        payload["harness_state_path"] = str(result.harness_state_path)
        return payload

    @staticmethod
    def _load_promotion(path: Path) -> PromotionResult:
        if path.is_symlink() or not path.is_file():
            raise PromotionRejected(f"promotion record path is unsafe: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            result = PromotionResult(
                id=payload["id"],
                candidate_id=payload["candidate_id"],
                candidate_revision=payload["candidate_revision"],
                replay_report_id=payload["replay_report_id"],
                shadow_report_id=payload["shadow_report_id"],
                harness_state_path=Path(payload["harness_state_path"]),
                target_id=payload["target_id"],
                before_entry=dict(payload["before_entry"])
                if payload["before_entry"] is not None
                else None,
                after_entry=dict(payload["after_entry"]),
                promoted_at=payload["promoted_at"],
                digest=payload["digest"],
            )
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise PromotionRejected(f"promotion record is invalid: {path}") from error
        if PromotionRuntime._promotion_digest(result) != result.digest:
            raise PromotionRejected(f"promotion record digest mismatch: {result.id}")
        return result

    async def cleanup_shadow(
        self,
        candidate: MemoryCandidate,
        *,
        repo: str | os.PathLike[str] = ".",
    ) -> None:
        """Remove an admitted shadow snapshot without following symlinks."""
        if not isinstance(candidate, MemoryCandidate):
            raise TypeError("candidate must be a MemoryCandidate")
        repo_root, common_dir, _ = await self.lab._repo(repo)
        if candidate.repo_root.resolve() != repo_root:
            raise PromotionRejected("candidate belongs to a different repository")
        metadata = self._shadow_metadata(candidate)
        expected_dir = self._shadow_dir(common_dir, candidate.id).absolute()
        expected_state = expected_dir / "harness_state.json"
        raw = metadata.get("shadow_state_path")
        if (
            not isinstance(raw, str)
            or Path(os.path.abspath(Path(raw).expanduser())) != expected_state
        ):
            raise PromotionRejected(
                "candidate shadow path does not match its repository"
            )
        shadow_root = (self.lab.state_root / "shadows").absolute()
        cursor = expected_dir.parent
        while True:
            if cursor.is_symlink():
                raise PromotionRejected(
                    f"candidate shadow parent is a symlink: {cursor}"
                )
            if cursor == shadow_root:
                break
            if not cursor.is_relative_to(shadow_root):
                raise PromotionRejected(
                    "candidate shadow path escapes Evolution Lab state"
                )
            cursor = cursor.parent
        if expected_dir.is_symlink():
            await asyncio.to_thread(expected_dir.unlink)
            return
        await asyncio.to_thread(self._remove_tree, expected_dir)

    def _require_managed_path(self, path: Path, label: str) -> None:
        root = self.lab.state_root.absolute()
        absolute = path.absolute()
        if not absolute.is_relative_to(root):
            raise PromotionRejected(f"{label} escapes Evolution Lab state: {path}")
        cursor = absolute
        while True:
            if cursor.is_symlink():
                raise PromotionRejected(f"{label} uses a symlink: {cursor}")
            if cursor == root:
                break
            cursor = cursor.parent

    async def _require_clean_repo(self, repo_root: Path) -> None:
        status = await self.lab._git(
            repo_root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        if status:
            raise PromotionRejected(
                "repository worktree must be clean for shadow evaluation and promotion"
            )

    def _admitted_state_path(self, candidate: MemoryCandidate) -> Path:
        raw = self._shadow_metadata(candidate).get("harness_state_path")
        if not isinstance(raw, str):
            raise PromotionRejected("candidate admitted harness path is missing")
        path = Path(os.path.abspath(Path(raw).expanduser()))
        cursor = path
        while True:
            if cursor.is_symlink():
                raise PromotionRejected(
                    f"candidate admitted harness path is a symlink: {cursor}"
                )
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        return path

    def _shadow_dir(self, common_dir: Path, candidate_id: str) -> Path:
        key = hashlib.sha256(
            os.path.normcase(str(common_dir.resolve())).encode("utf-8")
        ).hexdigest()[:20]
        return self.lab.state_root / "shadows" / key / candidate_id

    def _shadow_dir_from_candidate(
        self,
        candidate: MemoryCandidate,
        common_dir: Path,
    ) -> Path:
        metadata = self._shadow_metadata(candidate)
        value = metadata.get("shadow_state_path")
        if not isinstance(value, str):
            raise PromotionRejected("candidate shadow path is missing")
        expected_dir = self._shadow_dir(common_dir, candidate.id).absolute()
        expected_state = expected_dir / "harness_state.json"
        supplied_state = Path(os.path.abspath(Path(value).expanduser()))
        if supplied_state != expected_state:
            raise PromotionRejected(
                "candidate shadow path does not match its repository"
            )
        shadow_root = (self.lab.state_root / "shadows").absolute()
        cursor = expected_dir
        while True:
            if cursor.is_symlink():
                raise PromotionRejected(f"candidate shadow path is a symlink: {cursor}")
            if cursor == shadow_root:
                break
            if not cursor.is_relative_to(shadow_root):
                raise PromotionRejected(
                    "candidate shadow path escapes Evolution Lab state"
                )
            cursor = cursor.parent
        if expected_state.is_symlink():
            raise PromotionRejected("candidate shadow state is a symlink")
        return expected_dir

    def _promotion_path(self, common_dir: Path, promotion_id: str) -> Path:
        key = hashlib.sha256(
            os.path.normcase(str(common_dir.resolve())).encode("utf-8")
        ).hexdigest()[:20]
        return self.lab.state_root / "promotions" / key / f"{promotion_id}.json"

    def _rollback_path(self, common_dir: Path, rollback_id: str) -> Path:
        key = hashlib.sha256(
            os.path.normcase(str(common_dir.resolve())).encode("utf-8")
        ).hexdigest()[:20]
        return self.lab.state_root / "promotions" / key / f"{rollback_id}.json"

    @staticmethod
    def _entry_digest(value: object) -> str | None:
        if value is None:
            return None
        return hashlib.sha256(PromotionRuntime._canonical(value)).hexdigest()

    @staticmethod
    def _write_json_atomic(path: Path, payload: object) -> None:
        PromotionRuntime._mkdir_durable(path.parent)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
            PromotionRuntime._fsync_directory(path.parent)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _mkdir_durable(path: Path) -> None:
        missing: list[Path] = []
        cursor = path
        while not cursor.exists():
            missing.append(cursor)
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        path.mkdir(parents=True, exist_ok=True)
        for directory in reversed(missing):
            PromotionRuntime._fsync_directory(directory.parent)
            PromotionRuntime._fsync_directory(directory)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    @asynccontextmanager
    async def _lock(path: Path) -> AsyncIterator[None]:
        acquisition = asyncio.create_task(
            asyncio.to_thread(PromotionRuntime._acquire_lock, path)
        )
        try:
            lock = await asyncio.shield(acquisition)
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(
                PromotionRuntime._release_when_acquired(acquisition)
            )
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                pass
            raise
        heartbeat = asyncio.create_task(PromotionRuntime._heartbeat_lock(lock))
        try:
            yield
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await asyncio.to_thread(PromotionRuntime._release_lock, lock)

    @staticmethod
    async def _release_when_acquired(
        acquisition: asyncio.Task[tuple[Path, int]],
    ) -> None:
        try:
            lock = await acquisition
        except (OSError, PromotionRejected):
            return
        try:
            await asyncio.to_thread(PromotionRuntime._release_lock, lock)
        except (OSError, PromotionRejected):
            return

    @staticmethod
    async def _heartbeat_lock(lock: tuple[Path, int]) -> None:
        while True:
            await asyncio.sleep(100)
            await asyncio.to_thread(PromotionRuntime._refresh_lock, lock)

    @staticmethod
    def _refresh_lock(lock: tuple[Path, int]) -> None:
        path, inode = lock
        current = path.stat()
        if current.st_ino != inode or not path.is_dir() or path.is_symlink():
            raise PromotionRejected(f"harness lock ownership changed: {path}")
        os.utime(path, None)

    @staticmethod
    def _acquire_lock(path: Path) -> tuple[Path, int]:
        path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 30.0
        while True:
            try:
                path.mkdir(mode=0o700)
                return path, path.stat().st_ino
            except FileExistsError:
                try:
                    stale = time.time() - path.stat().st_mtime > 300.0
                    if stale:
                        path.rmdir()
                        continue
                except FileNotFoundError:
                    continue
                except OSError as error:
                    raise PromotionRejected(
                        f"harness lock is invalid: {path}"
                    ) from error
                if time.monotonic() >= deadline:
                    raise PromotionRejected(f"harness lock is busy: {path}")
                time.sleep(0.025)

    @staticmethod
    def _release_lock(lock: tuple[Path, int]) -> None:
        path, inode = lock
        try:
            current = path.stat()
        except FileNotFoundError:
            return
        if current.st_ino != inode or not path.is_dir() or path.is_symlink():
            raise PromotionRejected(f"harness lock ownership changed: {path}")
        path.rmdir()

    @staticmethod
    def _remove_tree(path: Path) -> None:
        import shutil

        if path.is_symlink():
            raise PromotionRejected(f"refusing to remove symlinked tree: {path}")
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass

    @staticmethod
    def _canonical(value: object) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _text(value: object, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise EvolutionError(f"{name} must be a non-empty string")
        return value.strip()
