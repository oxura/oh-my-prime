from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


WorkspaceStatus = Literal["active", "promoting", "promoted", "discarded"]


class WorkspaceError(RuntimeError):
    """Base error for isolated workspace operations."""


class WorkspaceStateError(WorkspaceError):
    """Raised when a workspace cannot transition from its current state."""


class WorkspaceDirtyError(WorkspaceError):
    """Raised when an operation requires a clean source or promotion target."""


class WorkspaceStaleError(WorkspaceError):
    """Raised when a candidate or its promotion target changed after verification."""


class WorkspaceCommandError(WorkspaceError):
    """Raised when an underlying Git operation fails."""

    def __init__(self, args: tuple[str, ...], returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.args_list = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        detail = stderr.decode("utf-8", errors="replace").strip() or stdout.decode(
            "utf-8", errors="replace"
        ).strip()
        command = "git " + " ".join(args)
        super().__init__(f"{command} failed with exit code {returncode}: {detail or 'no output'}")


@dataclass(frozen=True, slots=True)
class Workspace:
    """A durable handle to one isolated Git worktree."""

    id: str
    name: str
    path: Path
    repo_root: Path
    common_dir: Path
    branch: str
    base_commit: str
    status: WorkspaceStatus
    created_at: str
    updated_at: str
    promoted_at: str | None = None
    promotion_patch_sha256: str | None = None

    def __fspath__(self) -> str:
        return str(self.path)


@dataclass(frozen=True, slots=True)
class FileChange:
    """One path-level change in a candidate snapshot."""

    status: str
    path: str
    previous_path: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceDiff:
    """Immutable candidate snapshot used for review and stale-write protection."""

    workspace_id: str
    base_commit: str
    patch: bytes
    patch_sha256: str
    files: tuple[FileChange, ...]

    @property
    def is_empty(self) -> bool:
        return not self.patch

    @property
    def text(self) -> str:
        return self.patch.decode("utf-8", errors="replace")

    def __str__(self) -> str:
        paths = ", ".join(change.path for change in self.files[:8])
        if len(self.files) > 8:
            paths += f", … (+{len(self.files) - 8})"
        return f"WorkspaceDiff(files={len(self.files)}, sha256={self.patch_sha256}, paths=[{paths}])"


@dataclass(frozen=True, slots=True)
class PromotionResult:
    """Result of applying one verified snapshot to its clean source worktree."""

    workspace: Workspace
    target_root: Path
    patch_sha256: str
    files: tuple[FileChange, ...]
    artifact_path: Path
