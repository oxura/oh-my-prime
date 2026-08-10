from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, replace
from pathlib import Path

from ._models import (
    CandidateConflict,
    CandidateNotFound,
    EvidenceRef,
    EvolutionError,
    EvolutionEvent,
    MemoryCandidate,
    StoreStats,
)

_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    before_digest TEXT,
    after_digest TEXT,
    evidence TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS candidates_status_idx ON candidates(status);
CREATE INDEX IF NOT EXISTS candidates_category_idx ON candidates(category);
CREATE INDEX IF NOT EXISTS events_candidate_idx ON events(candidate_id, id);
"""


def candidate_digest(candidate: MemoryCandidate) -> str:
    payload = asdict(candidate)
    payload["repo_root"] = str(candidate.repo_root)
    payload["digest"] = None
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def attest_candidate(candidate: MemoryCandidate) -> MemoryCandidate:
    return replace(candidate, digest=candidate_digest(candidate))


def _candidate_json(candidate: MemoryCandidate) -> str:
    payload = asdict(candidate)
    payload["repo_root"] = str(candidate.repo_root)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _decode_candidate(raw: str) -> MemoryCandidate:
    try:
        payload = json.loads(raw)
        candidate = MemoryCandidate(
            schema_version=payload["schema_version"],
            id=payload["id"],
            revision=payload["revision"],
            category=payload["category"],
            title=payload["title"],
            claim=payload["claim"],
            scope=payload["scope"],
            path=payload["path"],
            target_id=payload["target_id"],
            applies_to=tuple(payload["applies_to"]),
            evidence=tuple(EvidenceRef(**item) for item in payload["evidence"]),
            confidence=payload["confidence"],
            confirmations=payload["confirmations"],
            contradictions=payload["contradictions"],
            status=payload["status"],
            repo_root=Path(payload["repo_root"]).resolve(),
            code_version=payload["code_version"],
            dependency_hashes=dict(payload["dependency_hashes"]),
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
            expires_at=payload["expires_at"],
            metadata=dict(payload["metadata"]),
            digest=payload["digest"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise EvolutionError("candidate store contains an invalid payload") from error
    if candidate.schema_version != _SCHEMA_VERSION:
        raise EvolutionError(
            f"unsupported candidate schema: {candidate.schema_version}"
        )
    if candidate_digest(candidate) != candidate.digest:
        raise EvolutionError(f"candidate digest mismatch: {candidate.id}")
    return candidate


def _evidence_json(evidence: Sequence[EvidenceRef]) -> str:
    return json.dumps(
        [asdict(item) for item in evidence], sort_keys=True, separators=(",", ":")
    )


class EvolutionStore:
    """SQLite candidate and event ledger with optimistic revision checks."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def create(self, candidate: MemoryCandidate, *, reason: str) -> MemoryCandidate:
        candidate = attest_candidate(candidate)
        if candidate.revision != 1:
            raise EvolutionError("new candidates must start at revision 1")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO candidates(id, revision, category, status, payload, digest, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.id,
                    candidate.revision,
                    candidate.category,
                    candidate.status,
                    _candidate_json(candidate),
                    candidate.digest,
                    candidate.created_at,
                    candidate.updated_at,
                ),
            )
            self._insert_event(
                connection,
                candidate.id,
                "propose",
                reason,
                None,
                candidate.digest,
                candidate.evidence,
                candidate.created_at,
            )
            connection.commit()
            return candidate
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise CandidateConflict(
                f"candidate already exists: {candidate.id}"
            ) from error
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def update(
        self,
        candidate: MemoryCandidate,
        *,
        action: str,
        reason: str,
        evidence: Sequence[EvidenceRef] = (),
    ) -> MemoryCandidate:
        candidate = attest_candidate(candidate)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision, digest FROM candidates WHERE id = ?",
                (candidate.id,),
            ).fetchone()
            if row is None:
                raise CandidateNotFound(f"candidate not found: {candidate.id}")
            expected = candidate.revision - 1
            if row["revision"] != expected:
                raise CandidateConflict(
                    f"candidate {candidate.id} changed at revision {row['revision']}; expected {expected}"
                )
            updated = connection.execute(
                """
                UPDATE candidates
                SET revision = ?, category = ?, status = ?, payload = ?, digest = ?, updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (
                    candidate.revision,
                    candidate.category,
                    candidate.status,
                    _candidate_json(candidate),
                    candidate.digest,
                    candidate.updated_at,
                    candidate.id,
                    expected,
                ),
            )
            if updated.rowcount != 1:
                raise CandidateConflict(
                    f"candidate changed during update: {candidate.id}"
                )
            self._insert_event(
                connection,
                candidate.id,
                action,
                reason,
                row["digest"],
                candidate.digest,
                evidence,
                candidate.updated_at,
            )
            connection.commit()
            return candidate
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, candidate_id: str) -> MemoryCandidate:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT payload FROM candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise CandidateNotFound(f"candidate not found: {candidate_id}")
            return _decode_candidate(row["payload"])
        finally:
            connection.close()

    def list(
        self,
        *,
        status: str | None = None,
        category: str | None = None,
        limit: int = 200,
    ) -> list[MemoryCandidate]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 2_000
        ):
            raise EvolutionError("limit must be an integer from 1 to 2000")
        clauses: list[str] = []
        parameters: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if category is not None:
            clauses.append("category = ?")
            parameters.append(category)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT payload FROM candidates {where} ORDER BY updated_at DESC, id LIMIT ?",
                parameters,
            )
            return [_decode_candidate(row["payload"]) for row in rows]
        finally:
            connection.close()

    def events(self, candidate_id: str, *, limit: int = 200) -> list[EvolutionEvent]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 2_000
        ):
            raise EvolutionError("limit must be an integer from 1 to 2000")
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM events WHERE candidate_id = ? ORDER BY id DESC LIMIT ?
                """,
                (candidate_id, limit),
            )
            return [
                EvolutionEvent(
                    id=row["id"],
                    candidate_id=row["candidate_id"],
                    action=row["action"],
                    reason=row["reason"],
                    before_digest=row["before_digest"],
                    after_digest=row["after_digest"],
                    evidence=tuple(
                        EvidenceRef(**item) for item in json.loads(row["evidence"])
                    ),
                    created_at=row["created_at"],
                )
                for row in rows
            ]
        finally:
            connection.close()

    def stats(self) -> StoreStats:
        connection = self._connect()
        try:
            by_status = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM candidates GROUP BY status"
                )
            }
            by_category = {
                row["category"]: row["count"]
                for row in connection.execute(
                    "SELECT category, COUNT(*) AS count FROM candidates GROUP BY category"
                )
            }
            return StoreStats(
                candidates=connection.execute(
                    "SELECT COUNT(*) FROM candidates"
                ).fetchone()[0],
                events=connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                by_status=by_status,
                by_category=by_category,
            )
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(_SCHEMA)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, _SCHEMA_VERSION}:
            connection.close()
            raise EvolutionError(f"unsupported Evolution store schema: {version}")
        if version == 0:
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            connection.commit()
        return connection

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        candidate_id: str,
        action: str,
        reason: str,
        before_digest: str | None,
        after_digest: str | None,
        evidence: Sequence[EvidenceRef],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO events(candidate_id, action, reason, before_digest, after_digest, evidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                action,
                reason,
                before_digest,
                after_digest,
                _evidence_json(evidence),
                created_at,
            ),
        )
