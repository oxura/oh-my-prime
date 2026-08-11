from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from ._models import FlowConflict, FlowError, FlowNotFound, FlowRecord

_SCHEMA_VERSION = 1
_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
_FLOW_STATUSES = frozenset({"pending", "running", "succeeded", "failed", "cancelled"})
_SCHEMA = """
CREATE TABLE IF NOT EXISTS flows (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    repo_root TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS flows_status_idx ON flows(status, updated_at DESC, id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('create', 'update')),
    before_sha256 TEXT,
    after_sha256 TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (flow_id) REFERENCES flows(id) ON DELETE RESTRICT,
    UNIQUE (flow_id, revision)
);
CREATE INDEX IF NOT EXISTS events_flow_idx ON events(flow_id, revision);
"""


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise FlowError("flow record is not JSON serializable") from error
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise FlowError(
            f"flow record exceeds the {_MAX_PAYLOAD_BYTES}-byte payload limit"
        )
    return encoded


def _bound_record(record: FlowRecord) -> FlowRecord:
    if not isinstance(record, FlowRecord):
        raise FlowError("record must be a FlowRecord")
    try:
        repo_root = Path(record.repo_root).expanduser().resolve()
        bound = replace(record, repo_root=repo_root)
        # Round-tripping invokes the constructors for the record and all nested models.
        persisted = FlowRecord.from_dict(bound.to_dict())
        artifact_ids: set[str] = set()
        for task in persisted.tasks:
            if task.status != "succeeded" and task.artifact_ids:
                raise ValueError(
                    "only succeeded tasks may carry a committed artifact manifest"
                )
            overlap = artifact_ids.intersection(task.artifact_ids)
            if overlap:
                raise ValueError(
                    "committed artifact ids must belong to exactly one task"
                )
            artifact_ids.update(task.artifact_ids)
        return persisted
    except FlowError:
        raise
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise FlowError("invalid flow record") from error


def _encode_record(record: FlowRecord) -> tuple[str, str]:
    encoded = _canonical_json(record.to_dict())
    return encoded.decode("utf-8"), hashlib.sha256(encoded).hexdigest()


def _decode_record(raw: object, claimed_digest: object) -> FlowRecord:
    if not isinstance(raw, str) or not isinstance(claimed_digest, str):
        raise FlowError("flow store contains an invalid payload")
    try:
        encoded = raw.encode("utf-8")
    except UnicodeError as error:
        raise FlowError("flow store contains an invalid payload") from error
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise FlowError("flow store payload exceeds the size limit")
    actual_digest = hashlib.sha256(encoded).hexdigest()
    if not hmac.compare_digest(actual_digest, claimed_digest):
        raise FlowError("flow store payload digest mismatch")
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise TypeError("record payload is not an object")
        # Reject non-canonical encodings as well as malformed records. This makes one
        # deterministic representation authoritative for both rows and events.
        if _canonical_json(payload) != encoded:
            raise ValueError("record payload is not canonical JSON")
        record = FlowRecord.from_dict(payload)
        record = _bound_record(record)
    except FlowError:
        raise
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
        raise FlowError("flow store contains an invalid payload") from error
    if record.schema_version != _SCHEMA_VERSION:
        raise FlowError(f"unsupported flow record schema: {record.schema_version}")
    return record


class FlowStore:
    """Durable SQLite flow ledger with optimistic revision checks."""

    def __init__(self, root: str | Path) -> None:
        try:
            self.root = Path(root).expanduser().resolve()
            self.path = self.root / "flows.sqlite3"
            self.root.mkdir(parents=True, exist_ok=True)
        except (TypeError, OSError) as error:
            raise FlowError("invalid flow store root") from error
        connection = self._connect()
        connection.close()

    def create(self, record: FlowRecord) -> FlowRecord:
        record = _bound_record(record)
        if record.schema_version != _SCHEMA_VERSION:
            raise FlowError(f"unsupported flow record schema: {record.schema_version}")
        if record.revision != 1:
            raise FlowError("new flows must start at revision 1")
        payload, digest = _encode_record(record)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO flows(
                    id, revision, status, repo_root, payload, payload_sha256,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.revision,
                    record.status,
                    str(record.repo_root),
                    payload,
                    digest,
                    record.created_at,
                    record.updated_at,
                ),
            )
            self._append_event(
                connection,
                record=record,
                action="create",
                before_digest=None,
                payload=payload,
                digest=digest,
            )
            connection.commit()
            return record
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise FlowConflict(f"flow already exists: {record.id}") from error
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, flow_id: str) -> FlowRecord:
        if not isinstance(flow_id, str) or not flow_id:
            raise FlowError("flow_id must be a nonempty string")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM flows WHERE id = ?", (flow_id,)
            ).fetchone()
            if row is None:
                raise FlowNotFound(f"flow not found: {flow_id}")
            return self._decode_row(row)
        except sqlite3.DatabaseError as error:
            raise FlowError("failed to read flow store") from error
        finally:
            connection.close()

    def update(
        self, record: FlowRecord, *, expected_revision: int | None = None
    ) -> FlowRecord:
        record = _bound_record(record)
        if record.schema_version != _SCHEMA_VERSION:
            raise FlowError(f"unsupported flow record schema: {record.schema_version}")
        if expected_revision is None:
            expected_revision = record.revision - 1
            stored = record
        else:
            if (
                not isinstance(expected_revision, int)
                or isinstance(expected_revision, bool)
                or expected_revision < 1
            ):
                raise FlowError("expected_revision must be a positive integer")
            if record.revision != expected_revision:
                raise FlowError(
                    "record revision must equal expected_revision before update"
                )
            stored = replace(record, revision=expected_revision + 1)
            stored = _bound_record(stored)
        if expected_revision < 1 or stored.revision != expected_revision + 1:
            raise FlowError("updated flow revision must advance by exactly one")
        payload, digest = _encode_record(stored)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                "SELECT * FROM flows WHERE id = ?", (stored.id,)
            ).fetchone()
            if current_row is None:
                raise FlowNotFound(f"flow not found: {stored.id}")
            current = self._decode_row(current_row)
            if current.revision != expected_revision:
                raise FlowConflict(
                    f"flow {stored.id} changed at revision {current.revision}; "
                    f"expected {expected_revision}"
                )
            updated = connection.execute(
                """
                UPDATE flows
                SET revision = ?, status = ?, repo_root = ?, payload = ?,
                    payload_sha256 = ?, created_at = ?, updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (
                    stored.revision,
                    stored.status,
                    str(stored.repo_root),
                    payload,
                    digest,
                    stored.created_at,
                    stored.updated_at,
                    stored.id,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                raise FlowConflict(f"flow changed during update: {stored.id}")
            self._append_event(
                connection,
                record=stored,
                action="update",
                before_digest=current_row["payload_sha256"],
                payload=payload,
                digest=digest,
            )
            connection.commit()
            return stored
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list(
        self, *, status: str | None = None, limit: int = 2_000
    ) -> tuple[FlowRecord, ...]:
        if status is not None and status not in _FLOW_STATUSES:
            raise FlowError(f"invalid flow status: {status!r}")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 2_000
        ):
            raise FlowError("limit must be an integer from 1 to 2000")
        where = "WHERE status = ?" if status is not None else ""
        parameters: tuple[object, ...]
        if status is None:
            parameters = (limit,)
        else:
            parameters = (status, limit)
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT * FROM flows {where} ORDER BY updated_at DESC, id ASC LIMIT ?",
                parameters,
            ).fetchall()
            return tuple(self._decode_row(row) for row in rows)
        except sqlite3.DatabaseError as error:
            raise FlowError("failed to read flow store") from error
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA journal_mode = WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, _SCHEMA_VERSION}:
                raise FlowError(f"unsupported Flow store schema: {version}")
            connection.executescript(_SCHEMA)
            if version == 0:
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            return connection
        except FlowError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.DatabaseError) as error:
            if connection is not None:
                connection.close()
            raise FlowError("failed to open flow store") from error

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> FlowRecord:
        record = _decode_record(row["payload"], row["payload_sha256"])
        expected = {
            "id": record.id,
            "revision": record.revision,
            "status": record.status,
            "repo_root": str(record.repo_root),
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }
        for column, value in expected.items():
            if row[column] != value:
                raise FlowError(
                    f"flow store indexed field does not match payload: {column}"
                )
        return record

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        record: FlowRecord,
        action: str,
        before_digest: str | None,
        payload: str,
        digest: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO events(
                flow_id, revision, action, before_sha256, after_sha256,
                payload, payload_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.revision,
                action,
                before_digest,
                digest,
                payload,
                digest,
                record.updated_at,
            ),
        )
