from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Mapping

from ._models import (
    FileChange,
    PromotionResult,
    Workspace,
    WorkspaceCommandError,
    WorkspaceDiff,
    WorkspaceDirtyError,
    WorkspaceError,
    WorkspaceStaleError,
    WorkspaceStateError,
)


_MANIFEST_VERSION = 1
_ACTIVE_STATUSES = {"active", "promoting", "promoted", "discarded"}
_LOCK_TIMEOUT_SECONDS = 15.0
_LOCK_RETRY_SECONDS = 0.05


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state_dir() -> Path:
    override = os.environ.get("OH_MY_PRIME_STATE_HOME")
    if override:
        return Path(override).expanduser()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    root = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    return root / "oh-my-prime" / "workspaces"


def _repo_key(common_dir: Path) -> str:
    canonical = os.path.normcase(str(common_dir.resolve()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", errors="ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-") or "candidate"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _lock_nonblocking(descriptor: int) -> bool:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(descriptor: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


class WorkspaceManager:
    """Create, inspect, promote, and discard isolated Git worktrees.

    Manifests and promotion patches live outside the repository. Every mutating
    operation is serialized per Git common directory. A promotion requires the
    source worktree to remain clean and at the exact commit from which the
    candidate was forked.
    """

    def __init__(self, state_dir: str | os.PathLike[str] | None = None) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve() if state_dir else _default_state_dir().resolve()

    async def fork(
        self,
        repo: str | os.PathLike[str] = ".",
        *,
        name: str | None = None,
        base: str = "HEAD",
    ) -> Workspace:
        """Fork a clean Git worktree into durable isolated storage."""
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise TypeError("name must be a non-empty str or None")
        if not isinstance(base, str) or not base.strip():
            raise TypeError("base must be a non-empty str")

        repo_root, common_dir = await self._repo_paths(repo)
        async with self._repository_lock(common_dir):
            self._require_clean(await self._status(repo_root), "source worktree must be clean before fork")
            base_commit = (await self._git(repo_root, "rev-parse", "--verify", f"{base.strip()}^{{commit}}"))
            base_commit_text = base_commit.decode("ascii").strip()
            identifier = uuid.uuid4().hex
            display_name = (name or f"candidate-{identifier[:8]}").strip()
            branch = f"omp/workspace/{_slug(display_name)[:36]}-{identifier[:10]}"
            repository_dir = self._repository_dir(common_dir)
            path = repository_dir / "trees" / identifier
            manifest_path = self._manifest_path(common_dir, identifier)
            if path.exists() or manifest_path.exists():
                raise WorkspaceStateError(f"workspace id collision: {identifier}")

            path.parent.mkdir(parents=True, exist_ok=True)
            created = False
            try:
                await self._git(
                    repo_root,
                    "worktree",
                    "add",
                    "--quiet",
                    "-b",
                    branch,
                    str(path),
                    base_commit_text,
                )
                created = True
                timestamp = _utc_now()
                workspace = Workspace(
                    id=identifier,
                    name=display_name,
                    path=path.resolve(),
                    repo_root=repo_root,
                    common_dir=common_dir,
                    branch=branch,
                    base_commit=base_commit_text,
                    status="active",
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                self._write_manifest(workspace)
                return workspace
            except Exception:
                if created:
                    await self._best_effort_git(repo_root, "worktree", "remove", "--force", str(path))
                    await self._best_effort_git(repo_root, "update-ref", "-d", f"refs/heads/{branch}")
                shutil.rmtree(path, ignore_errors=True)
                raise

    async def get(self, target: str | Workspace, *, repo: str | os.PathLike[str] = ".") -> Workspace:
        """Reload a workspace handle from its durable manifest."""
        return await self._load_workspace(target, repo=repo)

    async def list(self, repo: str | os.PathLike[str] = ".") -> list[Workspace]:
        """List durable workspace records for the repository, newest first."""
        _, common_dir = await self._repo_paths(repo)
        manifests_dir = self._repository_dir(common_dir) / "manifests"
        if not manifests_dir.exists():
            return []
        workspaces: list[Workspace] = []
        for manifest_path in manifests_dir.glob("*.json"):
            try:
                workspaces.append(self._read_manifest(manifest_path, expected_common_dir=common_dir))
            except WorkspaceError:
                continue
        return sorted(workspaces, key=lambda workspace: workspace.created_at, reverse=True)

    async def diff(self, target: str | Workspace, *, repo: str | os.PathLike[str] = ".") -> WorkspaceDiff:
        """Snapshot all non-ignored candidate changes, including untracked files."""
        workspace = await self._load_workspace(target, repo=repo)
        if workspace.status == "discarded":
            raise WorkspaceStateError(f"workspace {workspace.id} has been discarded")
        if workspace.status == "promoting":
            raise WorkspaceStateError(f"workspace {workspace.id} has an interrupted promotion")
        self._validate_workspace_path(workspace, require_exists=True)
        return await self._snapshot(workspace.path, workspace.base_commit, workspace.id)

    async def promote(
        self,
        target: str | Workspace,
        *,
        repo: str | os.PathLike[str] | None = None,
        expected_patch_sha256: str | None = None,
    ) -> PromotionResult:
        """Apply a candidate snapshot to its unchanged, clean source worktree.

        Pass the SHA-256 returned by :meth:`diff` after verification. Promotion
        recomputes the snapshot under the repository lock and rejects any
        candidate mutation between verification and apply.
        """
        if expected_patch_sha256 is not None and (
            not isinstance(expected_patch_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_patch_sha256) is None
        ):
            raise TypeError("expected_patch_sha256 must be a lowercase SHA-256 hex string or None")

        initial = await self._load_workspace(target, repo=repo or ".")
        async with self._repository_lock(initial.common_dir):
            workspace = await self._load_workspace(initial, repo=repo or initial.repo_root)
            if workspace.status != "active":
                raise WorkspaceStateError(
                    f"workspace {workspace.id} cannot be promoted from status {workspace.status!r}"
                )
            self._validate_workspace_path(workspace, require_exists=True)

            target_root, common_dir = await self._repo_paths(repo or workspace.repo_root)
            if common_dir != workspace.common_dir:
                raise WorkspaceStateError("promotion target belongs to a different Git repository")
            if target_root == workspace.path:
                raise WorkspaceStateError("a workspace cannot be promoted into itself")
            self._require_clean(await self._status(target_root), "promotion target must be clean")
            target_head = (await self._git(target_root, "rev-parse", "HEAD")).decode("ascii").strip()
            if target_head != workspace.base_commit:
                raise WorkspaceStaleError(
                    "promotion target moved after fork: "
                    f"expected {workspace.base_commit}, found {target_head}"
                )

            snapshot = await self._snapshot(workspace.path, workspace.base_commit, workspace.id)
            if snapshot.is_empty:
                raise WorkspaceStateError(f"workspace {workspace.id} has no changes to promote")
            if expected_patch_sha256 is not None and snapshot.patch_sha256 != expected_patch_sha256:
                raise WorkspaceStaleError(
                    "candidate changed after verification: "
                    f"expected {expected_patch_sha256}, found {snapshot.patch_sha256}"
                )

            await self._git(
                target_root,
                "apply",
                "--check",
                "--binary",
                "--whitespace=nowarn",
                "-",
                input_data=snapshot.patch,
            )
            artifact_path = self._promotion_artifact_path(
                workspace.common_dir, workspace.id, snapshot.patch_sha256
            )
            _atomic_write(artifact_path, snapshot.patch)
            promoting = replace(
                workspace,
                status="promoting",
                updated_at=_utc_now(),
                promotion_patch_sha256=snapshot.patch_sha256,
            )
            self._write_manifest(promoting)

            patch_applied = False
            try:
                await self._git(
                    target_root,
                    "apply",
                    "--binary",
                    "--whitespace=nowarn",
                    "-",
                    input_data=snapshot.patch,
                )
                patch_applied = True
                applied = await self._snapshot(target_root, workspace.base_commit, workspace.id)
                if applied.patch_sha256 != snapshot.patch_sha256:
                    raise WorkspaceStaleError(
                        "promotion result did not match the verified snapshot"
                    )
            except Exception:
                if patch_applied:
                    try:
                        await self._rollback_patch(target_root, snapshot.patch)
                    except Exception as rollback_error:
                        raise WorkspaceStateError(
                            "promotion failed after applying the patch and automatic rollback also failed; "
                            f"workspace {workspace.id} remains in 'promoting' state for manual recovery"
                        ) from rollback_error
                self._write_manifest(
                    replace(
                        workspace,
                        status="active",
                        updated_at=_utc_now(),
                        promotion_patch_sha256=None,
                    )
                )
                raise

            promoted = replace(
                workspace,
                status="promoted",
                updated_at=_utc_now(),
                promoted_at=_utc_now(),
                promotion_patch_sha256=snapshot.patch_sha256,
            )
            self._write_manifest(promoted)
            return PromotionResult(
                workspace=promoted,
                target_root=target_root,
                patch_sha256=snapshot.patch_sha256,
                files=snapshot.files,
                artifact_path=artifact_path,
            )

    async def discard(
        self,
        target: str | Workspace,
        *,
        repo: str | os.PathLike[str] = ".",
    ) -> Workspace:
        """Permanently remove a candidate worktree while retaining its audit manifest."""
        initial = await self._load_workspace(target, repo=repo)
        async with self._repository_lock(initial.common_dir):
            workspace = await self._load_workspace(initial, repo=repo)
            if workspace.status == "discarded":
                return workspace
            if workspace.status == "promoting":
                raise WorkspaceStateError(
                    f"workspace {workspace.id} has an interrupted promotion and cannot be discarded"
                )
            self._validate_workspace_path(workspace, require_exists=False)
            if workspace.path.exists():
                await self._git(
                    workspace.repo_root,
                    "worktree",
                    "remove",
                    "--force",
                    str(workspace.path),
                )
            await self._best_effort_git(
                workspace.repo_root,
                "update-ref",
                "-d",
                f"refs/heads/{workspace.branch}",
            )
            discarded = replace(workspace, status="discarded", updated_at=_utc_now())
            self._write_manifest(discarded)
            return discarded

    async def _repo_paths(self, repo: str | os.PathLike[str]) -> tuple[Path, Path]:
        candidate = Path(repo).expanduser().resolve()
        root = (await self._git(candidate, "rev-parse", "--path-format=absolute", "--show-toplevel"))
        common = (await self._git(candidate, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        repo_root = Path(root.decode("utf-8", errors="surrogateescape").strip()).resolve()
        common_dir = Path(common.decode("utf-8", errors="surrogateescape").strip()).resolve()
        return repo_root, common_dir

    async def _load_workspace(
        self,
        target: str | Workspace,
        *,
        repo: str | os.PathLike[str],
    ) -> Workspace:
        if isinstance(target, Workspace):
            manifest_path = self._manifest_path(target.common_dir, target.id)
            return self._read_manifest(manifest_path, expected_common_dir=target.common_dir)
        if not isinstance(target, str) or not target.strip():
            raise TypeError("workspace must be a Workspace handle or non-empty id")
        _, common_dir = await self._repo_paths(repo)
        return self._read_manifest(
            self._manifest_path(common_dir, target.strip()), expected_common_dir=common_dir
        )

    def _repository_dir(self, common_dir: Path) -> Path:
        return self.state_dir / _repo_key(common_dir)

    def _manifest_path(self, common_dir: Path, identifier: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{32}", identifier) is None:
            raise WorkspaceStateError(f"invalid workspace id: {identifier!r}")
        return self._repository_dir(common_dir) / "manifests" / f"{identifier}.json"

    def _promotion_artifact_path(self, common_dir: Path, identifier: str, digest: str) -> Path:
        return self._repository_dir(common_dir) / "promotions" / f"{identifier}-{digest}.patch"

    def _write_manifest(self, workspace: Workspace) -> None:
        payload = {
            "version": _MANIFEST_VERSION,
            "id": workspace.id,
            "name": workspace.name,
            "path": str(workspace.path),
            "repo_root": str(workspace.repo_root),
            "common_dir": str(workspace.common_dir),
            "branch": workspace.branch,
            "base_commit": workspace.base_commit,
            "status": workspace.status,
            "created_at": workspace.created_at,
            "updated_at": workspace.updated_at,
            "promoted_at": workspace.promoted_at,
            "promotion_patch_sha256": workspace.promotion_patch_sha256,
        }
        data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_write(self._manifest_path(workspace.common_dir, workspace.id), data)

    def _read_manifest(self, path: Path, *, expected_common_dir: Path) -> Workspace:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise WorkspaceStateError(f"workspace manifest not found: {path.stem}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise WorkspaceStateError(f"cannot read workspace manifest {path}: {error}") from error
        if not isinstance(payload, dict) or payload.get("version") != _MANIFEST_VERSION:
            raise WorkspaceStateError(f"unsupported workspace manifest: {path}")

        required_strings = (
            "id",
            "name",
            "path",
            "repo_root",
            "common_dir",
            "branch",
            "base_commit",
            "status",
            "created_at",
            "updated_at",
        )
        if any(not isinstance(payload.get(key), str) or not payload[key] for key in required_strings):
            raise WorkspaceStateError(f"invalid workspace manifest fields: {path}")
        status = payload["status"]
        if status not in _ACTIVE_STATUSES:
            raise WorkspaceStateError(f"invalid workspace status {status!r}: {path}")
        common_dir = Path(payload["common_dir"]).resolve()
        if common_dir != expected_common_dir.resolve():
            raise WorkspaceStateError("workspace manifest belongs to a different Git repository")
        promoted_at = payload.get("promoted_at")
        patch_sha = payload.get("promotion_patch_sha256")
        if promoted_at is not None and not isinstance(promoted_at, str):
            raise WorkspaceStateError(f"invalid promoted_at in workspace manifest: {path}")
        if patch_sha is not None and (
            not isinstance(patch_sha, str) or re.fullmatch(r"[0-9a-f]{64}", patch_sha) is None
        ):
            raise WorkspaceStateError(f"invalid promotion patch hash in workspace manifest: {path}")

        return Workspace(
            id=payload["id"],
            name=payload["name"],
            path=Path(payload["path"]).resolve(),
            repo_root=Path(payload["repo_root"]).resolve(),
            common_dir=common_dir,
            branch=payload["branch"],
            base_commit=payload["base_commit"],
            status=status,
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
            promoted_at=promoted_at,
            promotion_patch_sha256=patch_sha,
        )

    def _validate_workspace_path(self, workspace: Workspace, *, require_exists: bool) -> None:
        expected_parent = (self._repository_dir(workspace.common_dir) / "trees").resolve()
        if workspace.path.parent.resolve() != expected_parent:
            raise WorkspaceStateError(f"workspace path escaped managed storage: {workspace.path}")
        if require_exists and not workspace.path.is_dir():
            raise WorkspaceStateError(f"workspace path is missing: {workspace.path}")

    @asynccontextmanager
    async def _repository_lock(self, common_dir: Path) -> AsyncIterator[None]:
        lock_path = self._repository_dir(common_dir) / ".lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _LOCK_TIMEOUT_SECONDS
        try:
            while not _lock_nonblocking(descriptor):
                if loop.time() >= deadline:
                    raise WorkspaceStateError(
                        f"timed out waiting for workspace repository lock: {common_dir}"
                    )
                await asyncio.sleep(_LOCK_RETRY_SECONDS)
            os.ftruncate(descriptor, 0)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(descriptor)
            yield
        finally:
            try:
                _unlock(descriptor)
            finally:
                os.close(descriptor)

    async def _status(self, repo: Path) -> bytes:
        return await self._git(
            repo,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )

    @staticmethod
    def _require_clean(status: bytes, message: str) -> None:
        if not status:
            return
        entries = [entry for entry in status.decode("utf-8", errors="replace").split("\0") if entry]
        preview = ", ".join(entries[:5])
        if len(entries) > 5:
            preview += f", … (+{len(entries) - 5})"
        raise WorkspaceDirtyError(f"{message}: {preview}")

    async def _snapshot(self, root: Path, base_commit: str, workspace_id: str) -> WorkspaceDiff:
        temporary_root = self._repository_dir((await self._repo_paths(root))[1]) / "tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="index-", dir=temporary_root) as directory:
            index_path = Path(directory) / "index"
            environment = {"GIT_INDEX_FILE": str(index_path)}
            await self._git(root, "read-tree", base_commit, extra_env=environment)
            untracked = await self._git(root, "ls-files", "--others", "--exclude-standard", "-z")
            if untracked:
                await self._git(
                    root,
                    "add",
                    "--intent-to-add",
                    "--pathspec-from-file=-",
                    "--pathspec-file-nul",
                    input_data=untracked,
                    extra_env=environment,
                )
            common_diff_args = (
                "--binary",
                "--full-index",
                "--no-ext-diff",
                "--no-color",
                "--find-renames",
                base_commit,
                "--",
                ".",
            )
            patch = await self._git(root, "diff", *common_diff_args, extra_env=environment)
            names = await self._git(
                root,
                "diff",
                "--name-status",
                "-z",
                "--find-renames",
                base_commit,
                "--",
                ".",
                extra_env=environment,
            )
        digest = hashlib.sha256(patch).hexdigest()
        return WorkspaceDiff(
            workspace_id=workspace_id,
            base_commit=base_commit,
            patch=patch,
            patch_sha256=digest,
            files=self._parse_name_status(names),
        )

    @staticmethod
    def _parse_name_status(data: bytes) -> tuple[FileChange, ...]:
        fields = data.split(b"\0")
        if fields and not fields[-1]:
            fields.pop()
        changes: list[FileChange] = []
        index = 0
        while index < len(fields):
            status = fields[index].decode("ascii", errors="replace")
            index += 1
            if not status:
                raise WorkspaceStateError("Git returned an invalid empty name-status entry")
            if status[0] in {"R", "C"}:
                if index + 1 >= len(fields):
                    raise WorkspaceStateError("Git returned an incomplete rename/copy entry")
                previous = fields[index].decode("utf-8", errors="surrogateescape")
                path = fields[index + 1].decode("utf-8", errors="surrogateescape")
                index += 2
                changes.append(FileChange(status=status, path=path, previous_path=previous))
                continue
            if index >= len(fields):
                raise WorkspaceStateError("Git returned an incomplete name-status entry")
            path = fields[index].decode("utf-8", errors="surrogateescape")
            index += 1
            changes.append(FileChange(status=status, path=path))
        return tuple(changes)

    async def _rollback_patch(self, target_root: Path, patch: bytes) -> None:
        await self._git(
            target_root,
            "apply",
            "--reverse",
            "--check",
            "--binary",
            "--whitespace=nowarn",
            "-",
            input_data=patch,
        )
        await self._git(
            target_root,
            "apply",
            "--reverse",
            "--binary",
            "--whitespace=nowarn",
            "-",
            input_data=patch,
        )

    async def _best_effort_git(self, repo: Path, *args: str) -> None:
        try:
            await self._git(repo, *args)
        except WorkspaceCommandError:
            pass

    async def _git(
        self,
        repo: Path,
        *args: str,
        input_data: bytes | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> bytes:
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["LC_ALL"] = "C"
        if extra_env:
            environment.update(extra_env)
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(repo),
                *args,
                stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
        except FileNotFoundError as error:
            raise WorkspaceError("Git is required for isolated workspaces but was not found") from error
        stdout, stderr = await process.communicate(input_data)
        if process.returncode != 0:
            raise WorkspaceCommandError(tuple(args), process.returncode or 1, stdout, stderr)
        return stdout
