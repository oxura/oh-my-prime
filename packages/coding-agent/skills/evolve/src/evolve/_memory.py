from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from prove import ProofError, ProofRuntime, VerificationReport

from ._models import (
    CandidateNotFound,
    DecayReport,
    EvidenceError,
    EvidenceRef,
    EvolutionError,
    EvolutionEvent,
    MemoryCandidate,
    MemoryCategory,
    MemoryFreshness,
    StoreStats,
)
from ._store import EvolutionStore

_SCHEMA_VERSION = 1
_CATEGORIES = {
    "verified_knowledge",
    "hypothesis",
    "known_error",
    "temporary_observation",
}
_STATUSES = {
    "candidate",
    "shadow",
    "active",
    "rejected",
    "rolled_back",
    "expired",
    "invalidated",
}
_HALF_LIFE_DAYS = {
    "verified_knowledge": 365.0,
    "known_error": 180.0,
    "hypothesis": 30.0,
    "temporary_observation": 7.0,
}
_MIN_ACTIVE_CONFIDENCE = 0.5


def _default_state_root() -> Path:
    override = os.environ.get("OH_MY_PRIME_EVOLUTION_STATE")
    if override:
        return Path(override).expanduser()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    root = (
        Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    )
    return root / "oh-my-prime" / "evolution"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _slug(value: str, fallback: str = "memory") -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return (normalized or fallback)[:80]


class EvolutionLab:
    """Proof-backed memory candidate lifecycle; proposals never mutate the harness."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        proof_runtime: ProofRuntime | None = None,
    ) -> None:
        self.state_root = (
            Path(state_dir).expanduser().resolve()
            if state_dir is not None
            else _default_state_root().resolve()
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._proof_runtime = proof_runtime or ProofRuntime()

    async def attest(
        self,
        artifact: str | os.PathLike[str],
        *,
        kind: str = "artifact",
        verifier: str = "",
    ) -> EvidenceRef:
        """Hash an observation artifact; this does not make it verified evidence."""
        if kind not in {"artifact", "observation", "replay_report"}:
            raise EvidenceError(
                "attest kind must be artifact, observation, or replay_report"
            )
        path = self._regular_file(artifact)
        digest = await asyncio.to_thread(_sha256_file, path)
        return EvidenceRef(
            uri=path.as_uri(),
            sha256=digest,
            kind=kind,
            verified=False,
            verifier=self._text(verifier, "verifier", allow_empty=True),
            captured_at=self._now_text(),
        )

    async def attest_verification(
        self,
        report: VerificationReport,
        *,
        repo: str | os.PathLike[str] = ".",
    ) -> EvidenceRef:
        """Attest an integrity-checked persisted `prove` report."""
        if not isinstance(report, VerificationReport):
            raise EvidenceError(
                "verification evidence must be a VerificationReport returned by prove.run"
            )
        try:
            persisted = await self._proof_runtime.load_report(report.id, repo=repo)
            persisted.require_verified()
        except ProofError as error:
            raise EvidenceError(
                f"verification report is not trusted: {error}"
            ) from error
        if persisted != report:
            raise EvidenceError(
                "verification report differs from the persisted verifier ledger"
            )
        if persisted.required_gates_passed < 1:
            raise EvidenceError(
                "verification report has no complete required-gate proof"
            )
        path = self._regular_file(persisted.report_path)
        digest = await asyncio.to_thread(_sha256_file, path)
        return EvidenceRef(
            uri=path.as_uri(),
            sha256=digest,
            kind="verification_report",
            verified=True,
            verifier=f"prove:{persisted.contract_id}:{persisted.id}",
            captured_at=self._now_text(),
        )

    async def propose_memory(
        self,
        claim: str,
        *,
        title: str,
        category: MemoryCategory,
        evidence: Sequence[EvidenceRef] = (),
        confidence: float,
        scope: str = "local",
        path: str = "knowledge",
        target_id: str | None = None,
        applies_to: Sequence[str] = (),
        dependencies: Sequence[str] = (),
        expires_at: str | datetime | None = None,
        metadata: Mapping[str, object] | None = None,
        repo: str | os.PathLike[str] = ".",
    ) -> MemoryCandidate:
        """Persist a candidate with provenance; no active harness entry is written."""
        claim = self._text(claim, "claim")
        title = self._text(title, "title")
        category = self._category(category)
        scope = self._scope(scope)
        path = self._text(path, "path")
        confidence = self._confidence(confidence)
        evidence = self._evidence_sequence(evidence)
        await self._validate_evidence_set(evidence, repo=repo)
        verified_count = sum(item.verified for item in evidence)
        if category in {"verified_knowledge", "known_error"} and verified_count == 0:
            raise EvidenceError(f"{category} requires a verified deterministic report")
        if scope == "global" and (
            category not in {"verified_knowledge", "known_error"} or verified_count == 0
        ):
            raise EvidenceError(
                "global memory requires verified knowledge or a verified known error"
            )
        expiration = self._expiration(expires_at)
        if category == "temporary_observation" and expiration is None:
            raise EvolutionError("temporary_observation requires expires_at")
        applies = self._strings(applies_to, "applies_to")
        repo_root, common_dir, code_version = await self._repo(repo)
        if not applies:
            applies = (repo_root.name,)
        dependency_hashes = await self._dependency_hashes(
            repo_root,
            self._strings(dependencies, "dependencies"),
        )
        normalized_metadata = self._json_mapping(metadata or {})
        created_at = self._now_text()
        identity = {
            "claim": claim,
            "category": category,
            "scope": scope,
            "target_id": target_id,
            "created_at": created_at,
            "evidence": [(item.uri, item.sha256) for item in evidence],
        }
        candidate_id = (
            f"memory-{hashlib.sha256(self._canonical(identity)).hexdigest()[:24]}"
        )
        candidate = MemoryCandidate(
            schema_version=_SCHEMA_VERSION,
            id=candidate_id,
            revision=1,
            category=category,
            title=title,
            claim=claim,
            scope=scope,
            path=path,
            target_id=self._text(target_id or _slug(title), "target_id"),
            applies_to=applies,
            evidence=evidence,
            confidence=confidence,
            confirmations=1 if verified_count else 0,
            contradictions=0,
            status="candidate",
            repo_root=repo_root,
            code_version=code_version,
            dependency_hashes=dependency_hashes,
            created_at=created_at,
            updated_at=created_at,
            expires_at=expiration,
            metadata=normalized_metadata,
            digest="",
        )
        store = self._store(common_dir)
        return await asyncio.to_thread(
            store.create,
            candidate,
            reason="proof-backed memory candidate proposed",
        )

    async def get(
        self,
        candidate_id: str,
        *,
        repo: str | os.PathLike[str] = ".",
    ) -> MemoryCandidate:
        _, common_dir, _ = await self._repo(repo)
        return await asyncio.to_thread(
            self._store(common_dir).get, self._candidate_id(candidate_id)
        )

    async def list(
        self,
        *,
        status: str | None = None,
        category: str | None = None,
        limit: int = 200,
        repo: str | os.PathLike[str] = ".",
    ) -> list[MemoryCandidate]:
        if status is not None and status not in _STATUSES:
            raise EvolutionError(f"unknown candidate status: {status}")
        if category is not None:
            self._category(category)
        _, common_dir, _ = await self._repo(repo)
        return await asyncio.to_thread(
            self._store(common_dir).list,
            status=status,
            category=category,
            limit=limit,
        )

    async def events(
        self,
        candidate_id: str,
        *,
        limit: int = 200,
        repo: str | os.PathLike[str] = ".",
    ) -> list[EvolutionEvent]:
        _, common_dir, _ = await self._repo(repo)
        return await asyncio.to_thread(
            self._store(common_dir).events,
            self._candidate_id(candidate_id),
            limit=limit,
        )

    async def stats(self, *, repo: str | os.PathLike[str] = ".") -> StoreStats:
        _, common_dir, _ = await self._repo(repo)
        return await asyncio.to_thread(self._store(common_dir).stats)

    async def freshness(
        self,
        candidate: MemoryCandidate,
        *,
        for_activation: bool = False,
    ) -> MemoryFreshness:
        if not isinstance(candidate, MemoryCandidate):
            raise TypeError("candidate must be a MemoryCandidate")
        if not isinstance(for_activation, bool):
            raise TypeError("for_activation must be a boolean")
        now = self._now()
        expired = (
            candidate.expires_at is not None
            and self._parse_time(candidate.expires_at) <= now
        )
        changed_dependencies: list[str] = []
        for relative, expected in sorted(candidate.dependency_hashes.items()):
            try:
                current = await asyncio.to_thread(
                    _sha256_file, self._dependency_path(candidate.repo_root, relative)
                )
            except (OSError, EvolutionError):
                current = None
            if current != expected:
                changed_dependencies.append(relative)
        missing_evidence: list[str] = []
        invalid_evidence: list[str] = []
        for item in candidate.evidence:
            try:
                await self._validate_evidence(item, repo=candidate.repo_root)
            except FileNotFoundError:
                missing_evidence.append(item.uri)
            except EvidenceError:
                invalid_evidence.append(item.uri)
        effective = self._effective_confidence(candidate, now)
        reasons: list[str] = []
        if expired:
            reasons.append("memory expired")
        if changed_dependencies:
            reasons.append("dependency content changed")
        if missing_evidence:
            reasons.append("evidence artifact is missing")
        if invalid_evidence:
            reasons.append("evidence artifact or verification status is invalid")
        if candidate.status in {"rejected", "rolled_back", "expired", "invalidated"}:
            reasons.append(f"candidate status is {candidate.status}")
        if candidate.status == "active" or for_activation:
            if candidate.metadata.get("rollback_required") is True:
                reasons.append("memory is marked for rollback")
            if effective < _MIN_ACTIVE_CONFIDENCE:
                reasons.append("effective confidence is below active threshold")
        return MemoryFreshness(
            fresh=not reasons,
            effective_confidence=effective,
            expired=expired,
            changed_dependencies=tuple(changed_dependencies),
            missing_evidence=tuple(missing_evidence),
            invalid_evidence=tuple(invalid_evidence),
            reasons=tuple(reasons),
        )

    async def confirm(
        self,
        candidate_id: str,
        evidence: Sequence[EvidenceRef],
        *,
        reason: str,
        repo: str | os.PathLike[str] = ".",
    ) -> MemoryCandidate:
        evidence = self._evidence_sequence(evidence)
        if not evidence:
            raise EvidenceError("confirmation requires evidence")
        await self._validate_evidence_set(evidence, repo=repo)
        if not any(item.verified for item in evidence):
            raise EvidenceError("confirmation requires verified evidence")
        candidate, store = await self._candidate_and_store(candidate_id, repo)
        merged = {
            (item.uri, item.sha256): item for item in (*candidate.evidence, *evidence)
        }
        updated = replace(
            candidate,
            revision=candidate.revision + 1,
            evidence=tuple(merged.values()),
            confidence=min(
                0.999,
                candidate.confidence + 0.05 * sum(item.verified for item in evidence),
            ),
            confirmations=candidate.confirmations + 1,
            updated_at=self._now_text(),
        )
        return await asyncio.to_thread(
            store.update,
            updated,
            action="confirm",
            reason=self._text(reason, "reason"),
            evidence=evidence,
        )

    async def contradict(
        self,
        candidate_id: str,
        evidence: Sequence[EvidenceRef],
        *,
        reason: str,
        repo: str | os.PathLike[str] = ".",
    ) -> MemoryCandidate:
        evidence = self._evidence_sequence(evidence)
        if not evidence or not any(item.verified for item in evidence):
            raise EvidenceError("contradiction requires verified evidence")
        await self._validate_evidence_set(evidence, repo=repo)
        candidate, store = await self._candidate_and_store(candidate_id, repo)
        reason = self._text(reason, "reason")
        confidence = max(0.0, candidate.confidence - 0.2)
        updated_at = self._now_text()
        updated = replace(
            candidate,
            revision=candidate.revision + 1,
            evidence=(*candidate.evidence, *evidence),
            confidence=confidence,
            contradictions=candidate.contradictions + 1,
            updated_at=updated_at,
        )
        effective_confidence = self._effective_confidence(updated, self._now())
        rollback_required = updated.status == "active" and effective_confidence < 0.5
        status = (
            "invalidated"
            if confidence < 0.5 and updated.status != "active"
            else updated.status
        )
        updated = replace(
            updated,
            status=status,
            metadata={
                **updated.metadata,
                **(
                    {
                        "rollback_required": True,
                        "rollback_reason": reason,
                    }
                    if rollback_required
                    else {}
                ),
            },
        )
        return await asyncio.to_thread(
            store.update,
            updated,
            action="contradict",
            reason=reason,
            evidence=evidence,
        )

    async def invalidate(
        self,
        candidate_id: str,
        *,
        reason: str,
        repo: str | os.PathLike[str] = ".",
    ) -> MemoryCandidate:
        candidate, store = await self._candidate_and_store(candidate_id, repo)
        if candidate.status in {"rolled_back", "rejected", "expired", "invalidated"}:
            return candidate
        reason = self._text(reason, "reason")
        active = candidate.status == "active"
        updated = replace(
            candidate,
            revision=candidate.revision + 1,
            status=candidate.status if active else "invalidated",
            metadata={
                **candidate.metadata,
                **(
                    {"rollback_required": True, "rollback_reason": reason}
                    if active
                    else {}
                ),
            },
            updated_at=self._now_text(),
        )
        return await asyncio.to_thread(
            store.update,
            updated,
            action="invalidate",
            reason=reason,
        )

    async def decay(self, *, repo: str | os.PathLike[str] = ".") -> DecayReport:
        candidates = await self.list(limit=2_000, repo=repo)
        expired_ids: list[str] = []
        invalidated_ids: list[str] = []
        for candidate in candidates:
            if candidate.status not in {"candidate", "shadow"}:
                continue
            freshness = await self.freshness(candidate)
            if freshness.fresh:
                continue
            if freshness.expired:
                await self._set_status(
                    candidate, "expired", "memory expiration reached", repo
                )
                expired_ids.append(candidate.id)
            else:
                await self._set_status(
                    candidate,
                    "invalidated",
                    "; ".join(freshness.reasons),
                    repo,
                )
                invalidated_ids.append(candidate.id)
        return DecayReport(
            checked=len(candidates),
            expired_ids=tuple(expired_ids),
            invalidated_ids=tuple(invalidated_ids),
            evaluated_at=self._now_text(),
        )

    async def _set_status(
        self,
        candidate: MemoryCandidate,
        status: str,
        reason: str,
        repo: str | os.PathLike[str],
    ) -> MemoryCandidate:
        _, common_dir, _ = await self._repo(repo)
        updated = replace(
            candidate,
            revision=candidate.revision + 1,
            status=status,
            updated_at=self._now_text(),
        )
        return await asyncio.to_thread(
            self._store(common_dir).update,
            updated,
            action=status,
            reason=reason,
        )

    async def _candidate_and_store(
        self,
        candidate_id: str,
        repo: str | os.PathLike[str],
    ) -> tuple[MemoryCandidate, EvolutionStore]:
        _, common_dir, _ = await self._repo(repo)
        store = self._store(common_dir)
        candidate = await asyncio.to_thread(store.get, self._candidate_id(candidate_id))
        return candidate, store

    async def _validate_evidence_set(
        self,
        evidence: Sequence[EvidenceRef],
        *,
        repo: str | os.PathLike[str],
    ) -> None:
        for item in evidence:
            await self._validate_evidence(item, repo=repo)

    async def _validate_evidence(
        self,
        evidence: EvidenceRef,
        *,
        repo: str | os.PathLike[str],
    ) -> None:
        await asyncio.to_thread(self._validate_evidence_sync, evidence)
        if not evidence.verified:
            return
        report_id = evidence.verifier.rsplit(":", 1)[-1]
        try:
            report = await self._proof_runtime.load_report(report_id, repo=repo)
            report.require_verified()
        except (ProofError, OSError) as error:
            raise EvidenceError(
                f"verification ledger is invalid: {report_id}"
            ) from error
        expected_verifier = f"prove:{report.contract_id}:{report.id}"
        if (
            evidence.verifier != expected_verifier
            or report.report_path.resolve().as_uri() != evidence.uri
        ):
            raise EvidenceError("verification evidence does not match its ledger")

    @staticmethod
    def _validate_evidence_sync(evidence: EvidenceRef) -> None:
        if not isinstance(evidence, EvidenceRef):
            raise EvidenceError("evidence entries must be EvidenceRef values")
        if not re.fullmatch(r"[0-9a-f]{64}", evidence.sha256):
            raise EvidenceError("evidence SHA-256 is invalid")
        path = EvolutionLab._evidence_path(evidence.uri)
        if not path.exists():
            raise FileNotFoundError(path)
        if not path.is_file() or path.is_symlink():
            raise EvidenceError(f"evidence must be a regular file: {path}")
        if _sha256_file(path) != evidence.sha256:
            raise EvidenceError(f"evidence content hash changed: {path}")
        if evidence.verified:
            if (
                evidence.kind != "verification_report"
                or not evidence.verifier.startswith("prove:")
            ):
                raise EvidenceError(
                    "only persisted prove reports may be marked verified"
                )
            return

    @staticmethod
    def _evidence_path(uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise EvidenceError("only local file:// evidence URIs are supported")
        return Path(unquote(parsed.path)).resolve()

    async def _dependency_hashes(
        self,
        repo_root: Path,
        dependencies: tuple[str, ...],
    ) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for dependency in dependencies:
            path = self._dependency_path(repo_root, dependency)
            hashes[dependency] = await asyncio.to_thread(_sha256_file, path)
        return hashes

    @staticmethod
    def _dependency_path(repo_root: Path, dependency: str) -> Path:
        if Path(dependency).is_absolute() or ".." in Path(dependency).parts:
            raise EvolutionError(
                f"dependency must be repository-relative: {dependency}"
            )
        unresolved = repo_root / dependency
        if unresolved.is_symlink():
            raise EvolutionError(f"dependency must not be a symlink: {dependency}")
        path = unresolved.resolve()
        if not path.is_relative_to(repo_root.resolve()) or not path.is_file():
            raise EvolutionError(
                f"dependency is not a regular repository file: {dependency}"
            )
        return path

    async def _repo(self, repo: str | os.PathLike[str]) -> tuple[Path, Path, str]:
        candidate = Path(repo).expanduser().resolve()
        root, common, version = await asyncio.gather(
            self._git(
                candidate, "rev-parse", "--path-format=absolute", "--show-toplevel"
            ),
            self._git(
                candidate, "rev-parse", "--path-format=absolute", "--git-common-dir"
            ),
            self._git(candidate, "rev-parse", "HEAD"),
        )
        return (
            Path(root.strip()).resolve(),
            Path(common.strip()).resolve(),
            version.strip(),
        )

    @staticmethod
    async def _git(repo: Path, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo),
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise EvolutionError(
                f"git {' '.join(args)} failed: {detail or 'no output'}"
            )
        return stdout.decode("utf-8", errors="surrogateescape")

    def _store(self, common_dir: Path) -> EvolutionStore:
        key = hashlib.sha256(
            os.path.normcase(str(common_dir.resolve())).encode("utf-8")
        ).hexdigest()[:20]
        return EvolutionStore(self.state_root / f"{key}.sqlite")

    def _effective_confidence(self, candidate: MemoryCandidate, now: datetime) -> float:
        age = max(
            0.0, (now - self._parse_time(candidate.updated_at)).total_seconds() / 86_400
        )
        half_life = _HALF_LIFE_DAYS[candidate.category]
        decayed = candidate.confidence * math.pow(0.5, age / half_life)
        decayed += min(0.1, candidate.confirmations * 0.01)
        decayed -= min(0.6, candidate.contradictions * 0.15)
        return max(0.0, min(1.0, decayed))

    def _expiration(self, value: str | datetime | None) -> str | None:
        if value is None:
            return None
        parsed = (
            value
            if isinstance(value, datetime)
            else self._parse_time(self._text(value, "expires_at"))
        )
        if parsed.tzinfo is None:
            raise EvolutionError("expires_at must include a timezone")
        parsed = parsed.astimezone(timezone.utc)
        if parsed <= self._now():
            raise EvolutionError("expires_at must be in the future")
        return parsed.isoformat()

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise EvolutionError(f"invalid timestamp: {value}") from error
        if parsed.tzinfo is None:
            raise EvolutionError(f"timestamp must include a timezone: {value}")
        return parsed.astimezone(timezone.utc)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise EvolutionError(
                "EvolutionLab clock must return a timezone-aware datetime"
            )
        return value.astimezone(timezone.utc)

    def _now_text(self) -> str:
        return self._now().isoformat()

    @staticmethod
    def _regular_file(value: object) -> Path:
        try:
            path = Path(value).expanduser()
        except TypeError as error:
            raise EvidenceError("artifact must be a filesystem path") from error
        if path.is_symlink() or not path.is_file():
            raise EvidenceError(f"artifact must be a regular file: {path}")
        return path.resolve()

    @staticmethod
    def _text(value: object, name: str, *, allow_empty: bool = False) -> str:
        if not isinstance(value, str):
            raise EvolutionError(f"{name} must be a string")
        normalized = value.strip()
        if not normalized and not allow_empty:
            raise EvolutionError(f"{name} must not be empty")
        return normalized

    @staticmethod
    def _strings(values: Sequence[str], name: str) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise EvolutionError(f"{name} must be a sequence of strings")
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise EvolutionError(f"{name} entries must be non-empty strings")
        return tuple(dict.fromkeys(value.strip() for value in values))

    @staticmethod
    def _evidence_sequence(values: Sequence[EvidenceRef]) -> tuple[EvidenceRef, ...]:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise EvidenceError("evidence must be a sequence")
        if any(not isinstance(value, EvidenceRef) for value in values):
            raise EvidenceError("evidence entries must be EvidenceRef values")
        return tuple(values)

    @staticmethod
    def _confidence(value: float) -> float:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise EvolutionError("confidence must be a finite number")
        normalized = float(value)
        if not 0 <= normalized <= 1:
            raise EvolutionError("confidence must be between 0 and 1")
        return normalized

    @staticmethod
    def _category(value: object) -> MemoryCategory:
        if value not in _CATEGORIES:
            raise EvolutionError(f"unknown memory category: {value}")
        return value  # type: ignore[return-value]

    @staticmethod
    def _scope(value: object) -> str:
        if value not in {"local", "global"}:
            raise EvolutionError("scope must be local or global")
        return str(value)

    @staticmethod
    def _candidate_id(value: object) -> str:
        if not isinstance(value, str) or not re.fullmatch(
            r"memory-[0-9a-f]{24}", value
        ):
            raise CandidateNotFound("invalid memory candidate id")
        return value

    @classmethod
    def _json_mapping(cls, value: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise EvolutionError("metadata must be a mapping")
        normalized = cls._jsonable(value)
        if not isinstance(normalized, dict):
            raise EvolutionError("metadata must be a JSON object")
        return normalized

    @classmethod
    def _jsonable(cls, value: object) -> object:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, Mapping):
            return {str(key): cls._jsonable(item) for key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return [cls._jsonable(item) for item in value]
        if is_dataclass(value) and not isinstance(value, type):
            from dataclasses import asdict

            return cls._jsonable(asdict(value))
        raise EvolutionError(f"value is not JSON serializable: {type(value).__name__}")

    @staticmethod
    def _canonical(value: object) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
