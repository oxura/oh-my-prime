from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import stat
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from ._models import (
    MAX_JSON_BYTES,
    ArtifactRecord,
    ArtifactSpec,
    ArtifactValidationError,
    FlowNotFound,
    JsonValue,
    canonical_json_bytes,
    decode_canonical_json,
)

_SCHEMA_VERSION = 1
_DIGEST_CHARS = frozenset("0123456789abcdef")
_ENVELOPE_OVERHEAD_LIMIT = 64 * 1024


class ArtifactBlackboard:
    """A durable, content-addressed blackboard of strictly typed JSON artifacts."""

    def __init__(
        self, root: str | os.PathLike[str], max_bytes: int = MAX_JSON_BYTES
    ) -> None:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        try:
            root_path = Path(root).expanduser().absolute()
        except (TypeError, ValueError, OSError) as exc:
            raise ValueError(f"invalid artifact root: {exc}") from exc
        self._root = root_path
        self._max_bytes = max_bytes
        self._ensure_root()
        self._commit_root = self._root / "_commits"

    @property
    def root(self) -> Path:
        return self._root

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    async def publish(
        self, flow_id: str, task_id: str, spec: ArtifactSpec, value: JsonValue
    ) -> ArtifactRecord:
        """Validate and atomically publish one immediately committed artifact."""
        return await asyncio.to_thread(
            self._publish_committed, flow_id, task_id, spec, value
        )

    async def stage(
        self,
        flow_id: str,
        task_id: str,
        attempt: int,
        spec: ArtifactSpec,
        value: JsonValue,
    ) -> ArtifactRecord:
        """Write one validated attempt artifact without making it queryable."""
        self._validate_attempt(attempt)
        return await asyncio.to_thread(self._publish, flow_id, task_id, spec, value)

    async def commit_attempt(
        self,
        flow_id: str,
        task_id: str,
        attempt: int,
        artifact_ids: Sequence[str],
    ) -> None:
        """Atomically expose the complete artifact manifest for one successful attempt."""
        await asyncio.to_thread(
            self._commit_attempt,
            flow_id,
            task_id,
            attempt,
            tuple(artifact_ids),
        )

    async def get(self, artifact_id: str) -> tuple[ArtifactRecord, JsonValue]:
        """Load a committed artifact and verify its payload and attestation."""
        return await asyncio.to_thread(self._get_committed, artifact_id)

    async def query(
        self,
        flow_id: str,
        name: str | None = None,
        producer_task_id: str | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        """Return committed, integrity-checked records in deterministic order."""
        return await asyncio.to_thread(self._query, flow_id, name, producer_task_id)

    def _publish_committed(
        self, flow_id: str, task_id: str, spec: ArtifactSpec, value: JsonValue
    ) -> ArtifactRecord:
        record = self._publish(flow_id, task_id, spec, value)
        existing = self._manifest_for(flow_id, task_id, 0)
        artifact_ids = tuple(existing["artifact_ids"]) if existing is not None else ()
        if record.id not in artifact_ids:
            artifact_ids += (record.id,)
        self._commit_attempt(flow_id, task_id, 0, artifact_ids)
        return record

    def _publish(
        self, flow_id: str, task_id: str, spec: ArtifactSpec, value: JsonValue
    ) -> ArtifactRecord:
        self._validate_label(flow_id, "flow_id")
        self._validate_label(task_id, "task_id")
        if not isinstance(spec, ArtifactSpec):
            raise ArtifactValidationError("spec must be an ArtifactSpec")
        validated = spec.validate(value)
        payload_bytes = canonical_json_bytes(validated, max_bytes=self._max_bytes)
        payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        created_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        unsigned_record: dict[str, JsonValue] = {
            "schema_version": _SCHEMA_VERSION,
            "flow_id": flow_id,
            "producer_task_id": task_id,
            "name": spec.name,
            "value_type": spec.value_type,
            "sha256": payload_sha256,
            "created_at": created_at,
        }
        attestation: dict[str, JsonValue] = {
            "record": unsigned_record,
            "value": validated,
        }
        artifact_id = hashlib.sha256(
            canonical_json_bytes(
                attestation, max_bytes=self._max_bytes + _ENVELOPE_OVERHEAD_LIMIT
            )
        ).hexdigest()
        relative_path = self._relative_path(artifact_id)
        record = ArtifactRecord(
            schema_version=_SCHEMA_VERSION,
            id=artifact_id,
            flow_id=flow_id,
            producer_task_id=task_id,
            name=spec.name,
            value_type=spec.value_type,
            path=relative_path,
            sha256=payload_sha256,
            created_at=created_at,
        )
        envelope: dict[str, JsonValue] = {
            "record": record.to_dict(),
            "value": validated,
        }
        encoded = canonical_json_bytes(
            envelope, max_bytes=self._max_bytes + _ENVELOPE_OVERHEAD_LIMIT
        )
        target = self._safe_target(artifact_id, create_parent=True)
        self._atomic_write(target, encoded)
        loaded_record, _ = self._get(artifact_id)
        if loaded_record != record:
            raise ArtifactValidationError(
                "published artifact metadata failed verification"
            )
        return record

    def _get_committed(self, artifact_id: str) -> tuple[ArtifactRecord, JsonValue]:
        self._validate_digest(artifact_id)
        if artifact_id not in self._committed_artifact_ids():
            raise FlowNotFound(f"artifact {artifact_id!r} is not committed")
        return self._get(artifact_id)

    def _get(self, artifact_id: str) -> tuple[ArtifactRecord, JsonValue]:
        self._validate_digest(artifact_id)
        target = self._safe_target(artifact_id, create_parent=False)
        try:
            raw = self._read_no_follow(
                target, self._max_bytes + _ENVELOPE_OVERHEAD_LIMIT
            )
        except FileNotFoundError as exc:
            raise FlowNotFound(f"artifact {artifact_id!r} does not exist") from exc
        value = decode_canonical_json(
            raw, max_bytes=self._max_bytes + _ENVELOPE_OVERHEAD_LIMIT
        )
        if type(value) is not dict or set(value) != {"record", "value"}:
            raise ArtifactValidationError(
                "artifact envelope must contain exactly record and value"
            )
        record_data = value["record"]
        payload = value["value"]
        if type(record_data) is not dict:
            raise ArtifactValidationError("artifact record metadata must be an object")
        try:
            record = ArtifactRecord.from_dict(record_data)
        except (TypeError, ValueError) as exc:
            raise ArtifactValidationError(
                f"invalid artifact record metadata: {exc}"
            ) from exc
        expected_path = self._relative_path(artifact_id)
        if record.id != artifact_id or record.path != expected_path:
            raise ArtifactValidationError(
                "artifact id or path does not match its storage location"
            )
        payload_bytes = canonical_json_bytes(payload, max_bytes=self._max_bytes)
        if not secrets.compare_digest(
            hashlib.sha256(payload_bytes).hexdigest(), record.sha256
        ):
            raise ArtifactValidationError("artifact payload checksum mismatch")
        ArtifactSpec(record.name, record.value_type).validate(payload)
        unsigned_record: dict[str, JsonValue] = {
            "schema_version": record.schema_version,
            "flow_id": record.flow_id,
            "producer_task_id": record.producer_task_id,
            "name": record.name,
            "value_type": record.value_type,
            "sha256": record.sha256,
            "created_at": record.created_at,
        }
        expected_id = hashlib.sha256(
            canonical_json_bytes(
                {"record": unsigned_record, "value": payload},
                max_bytes=self._max_bytes + _ENVELOPE_OVERHEAD_LIMIT,
            )
        ).hexdigest()
        if not secrets.compare_digest(expected_id, artifact_id):
            raise ArtifactValidationError("artifact metadata attestation mismatch")
        return record, cast(JsonValue, payload)

    def _query(
        self, flow_id: str, name: str | None, producer_task_id: str | None
    ) -> tuple[ArtifactRecord, ...]:
        self._validate_label(flow_id, "flow_id")
        if name is not None:
            self._validate_label(name, "name")
        if producer_task_id is not None:
            self._validate_label(producer_task_id, "producer_task_id")
        records: list[ArtifactRecord] = []
        for artifact_id in self._committed_artifact_ids(flow_id=flow_id):
            record, _ = self._get(artifact_id)
            if record.flow_id != flow_id:
                raise ArtifactValidationError(
                    "committed artifact does not belong to its manifest flow"
                )
            if name is not None and record.name != name:
                continue
            if (
                producer_task_id is not None
                and record.producer_task_id != producer_task_id
            ):
                continue
            records.append(record)
        records.sort(key=lambda item: (item.created_at, item.id))
        return tuple(records)

    def _commit_attempt(
        self,
        flow_id: str,
        task_id: str,
        attempt: int,
        artifact_ids: tuple[str, ...],
    ) -> None:
        self._validate_label(flow_id, "flow_id")
        self._validate_label(task_id, "task_id")
        self._validate_attempt(attempt)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ArtifactValidationError("artifact manifest ids must be unique")
        for artifact_id in artifact_ids:
            self._validate_digest(artifact_id)
            record, _ = self._get(artifact_id)
            if record.flow_id != flow_id or record.producer_task_id != task_id:
                raise ArtifactValidationError(
                    "artifact does not belong to its attempt manifest"
                )
        payload: dict[str, JsonValue] = {
            "schema_version": _SCHEMA_VERSION,
            "flow_id": flow_id,
            "producer_task_id": task_id,
            "attempt": attempt,
            "artifact_ids": list(artifact_ids),
        }
        encoded = canonical_json_bytes(payload, max_bytes=_ENVELOPE_OVERHEAD_LIMIT)
        target = self._manifest_target(flow_id, task_id, attempt, create_parent=True)
        self._atomic_write(target, encoded)
        loaded = self._read_manifest(target)
        if loaded != payload:
            raise ArtifactValidationError(
                "committed artifact manifest failed verification"
            )

    def _manifest_for(
        self, flow_id: str, task_id: str, attempt: int
    ) -> dict[str, JsonValue] | None:
        target = self._manifest_target(flow_id, task_id, attempt, create_parent=False)
        try:
            return self._read_manifest(target)
        except FileNotFoundError:
            return None

    def _committed_artifact_ids(self, *, flow_id: str | None = None) -> tuple[str, ...]:
        committed: set[str] = set()
        for target in self._manifest_paths():
            manifest = self._read_manifest(target)
            if flow_id is not None and manifest["flow_id"] != flow_id:
                continue
            committed.update(cast(list[str], manifest["artifact_ids"]))
        return tuple(sorted(committed))

    def _read_manifest(self, target: Path) -> dict[str, JsonValue]:
        raw = self._read_no_follow(target, _ENVELOPE_OVERHEAD_LIMIT)
        value = decode_canonical_json(raw, max_bytes=_ENVELOPE_OVERHEAD_LIMIT)
        expected = {
            "schema_version",
            "flow_id",
            "producer_task_id",
            "attempt",
            "artifact_ids",
        }
        if type(value) is not dict or set(value) != expected:
            raise ArtifactValidationError("artifact manifest has invalid fields")
        if value["schema_version"] != _SCHEMA_VERSION:
            raise ArtifactValidationError("artifact manifest has invalid schema")
        self._validate_label(value["flow_id"], "manifest flow_id")
        self._validate_label(value["producer_task_id"], "manifest producer_task_id")
        self._validate_attempt(value["attempt"])
        artifact_ids = value["artifact_ids"]
        if (
            type(artifact_ids) is not list
            or any(type(artifact_id) is not str for artifact_id in artifact_ids)
            or len(artifact_ids) != len(set(cast(list[str], artifact_ids)))
        ):
            raise ArtifactValidationError("artifact manifest ids must be a unique list")
        for artifact_id in artifact_ids:
            self._validate_digest(artifact_id)
        expected_target = self._manifest_target(
            cast(str, value["flow_id"]),
            cast(str, value["producer_task_id"]),
            cast(int, value["attempt"]),
            create_parent=False,
        )
        if target != expected_target:
            raise ArtifactValidationError(
                "artifact manifest is stored at an invalid path"
            )
        return cast(dict[str, JsonValue], value)

    def _manifest_target(
        self,
        flow_id: str,
        task_id: str,
        attempt: int,
        *,
        create_parent: bool,
    ) -> Path:
        self._validate_label(flow_id, "flow_id")
        self._validate_label(task_id, "task_id")
        self._validate_attempt(attempt)
        key = canonical_json_bytes([flow_id, task_id, attempt])
        digest = hashlib.sha256(key).hexdigest()
        self._ensure_commit_root()
        shard = self._commit_root / digest[:2]
        if create_parent:
            try:
                shard.mkdir(mode=0o700, exist_ok=True)
            except OSError as exc:
                raise ArtifactValidationError(
                    f"cannot create artifact manifest shard: {exc}"
                ) from exc
        self._reject_symlink_components(shard)
        if shard.exists() and not shard.is_dir():
            raise ArtifactValidationError("artifact manifest shard is not a directory")
        return shard / f"{digest}.json"

    def _manifest_paths(self) -> tuple[Path, ...]:
        self._ensure_commit_root()
        paths: list[Path] = []
        try:
            shards = list(os.scandir(self._commit_root))
        except OSError as exc:
            raise ArtifactValidationError(
                f"cannot scan artifact manifests: {exc}"
            ) from exc
        for shard in shards:
            if shard.is_symlink() or not shard.is_dir(follow_symlinks=False):
                raise ArtifactValidationError("invalid artifact manifest shard")
            if len(shard.name) != 2 or any(
                character not in _DIGEST_CHARS for character in shard.name
            ):
                raise ArtifactValidationError("invalid artifact manifest shard")
            for entry in os.scandir(shard.path):
                if entry.name.startswith(".") and entry.name.endswith(".tmp"):
                    if entry.is_symlink():
                        raise ArtifactValidationError(
                            "temporary artifact manifest is a symlink"
                        )
                    continue
                if (
                    entry.is_symlink()
                    or not entry.is_file(follow_symlinks=False)
                    or not entry.name.endswith(".json")
                ):
                    raise ArtifactValidationError("invalid artifact manifest file")
                digest = entry.name[:-5]
                self._validate_digest(digest)
                if digest[:2] != shard.name:
                    raise ArtifactValidationError(
                        "artifact manifest is stored in the wrong shard"
                    )
                paths.append(Path(entry.path))
        paths.sort()
        return tuple(paths)

    def _ensure_commit_root(self) -> None:
        self._ensure_root()
        try:
            self._commit_root.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise ArtifactValidationError(
                f"cannot create artifact manifest root: {exc}"
            ) from exc
        self._reject_symlink_components(self._commit_root)
        if not self._commit_root.is_dir():
            raise ArtifactValidationError("artifact manifest root is not a directory")

    def _ensure_root(self) -> None:
        self._reject_symlink_components(self._root)
        try:
            self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise ArtifactValidationError(
                f"cannot create artifact root: {exc}"
            ) from exc
        self._reject_symlink_components(self._root)
        if not self._root.is_dir():
            raise ArtifactValidationError("artifact root is not a directory")

    @staticmethod
    def _validate_attempt(value: object) -> None:
        if type(value) is not int or value < 0:
            raise ArtifactValidationError(
                "artifact manifest attempt must be a non-negative integer"
            )

    def _safe_target(self, artifact_id: str, *, create_parent: bool) -> Path:
        self._ensure_root()
        shard = self._root / artifact_id[:2]
        if create_parent:
            try:
                shard.mkdir(mode=0o700, exist_ok=True)
            except OSError as exc:
                raise ArtifactValidationError(
                    f"cannot create artifact shard: {exc}"
                ) from exc
        self._reject_symlink_components(shard)
        if shard.exists() and not shard.is_dir():
            raise ArtifactValidationError("artifact shard is not a directory")
        target = shard / f"{artifact_id}.json"
        try:
            mode = target.lstat().st_mode
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ArtifactValidationError(
                f"cannot inspect artifact path: {exc}"
            ) from exc
        else:
            if stat.S_ISLNK(mode):
                raise ArtifactValidationError("artifact path is a symlink")
            if not stat.S_ISREG(mode):
                raise ArtifactValidationError("artifact path is not a regular file")
        return target

    def _atomic_write(self, target: Path, data: bytes) -> None:
        temp = target.parent / f".{target.stem}.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd: int | None = None
        try:
            fd = os.open(temp, flags, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as handle:
                fd = None
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self._reject_symlink_components(target.parent)
            os.replace(temp, target)
            directory_fd = os.open(
                target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise ArtifactValidationError(
                f"cannot atomically publish artifact: {exc}"
            ) from exc
        finally:
            if fd is not None:
                os.close(fd)
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _read_no_follow(path: Path, max_bytes: int) -> bytes:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            if exc.errno in (getattr(os, "ELOOP", 40),):
                raise ArtifactValidationError("artifact path is a symlink") from exc
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ArtifactValidationError("artifact path is not a regular file")
            if info.st_size > max_bytes:
                raise ArtifactValidationError("artifact file exceeds its size limit")
            with os.fdopen(fd, "rb", closefd=True) as handle:
                fd = -1
                data = handle.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ArtifactValidationError("artifact file exceeds its size limit")
            return data
        finally:
            if fd >= 0:
                os.close(fd)

    @staticmethod
    def _validate_label(value: object, field_name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ArtifactValidationError(f"{field_name} must be a non-empty string")

    @staticmethod
    def _validate_digest(value: object) -> None:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(ch not in _DIGEST_CHARS for ch in value)
        ):
            raise ArtifactValidationError(
                "artifact id must be a lowercase SHA-256 digest"
            )

    @staticmethod
    def _relative_path(artifact_id: str) -> Path:
        return Path(artifact_id[:2]) / f"{artifact_id}.json"

    @staticmethod
    def _reject_symlink_components(path: Path) -> None:
        absolute = path.absolute()
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current /= part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ArtifactValidationError(
                    f"cannot inspect path component: {exc}"
                ) from exc
            if stat.S_ISLNK(mode):
                raise ArtifactValidationError(
                    f"symlink path component is not allowed: {current}"
                )
