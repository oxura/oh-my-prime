from __future__ import annotations

import ast
import asyncio
import fcntl
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from ._graph import CodeAtlas, _connect, _hash_file, _repo_key
from ._models import (
    AtlasError,
    AtlasNotBuilt,
    AtlasStale,
    ChangedFile,
    ChangedRange,
    ImpactApplyResult,
    ImpactError,
    ImpactFile,
    ImpactFreshness,
    ImpactReport,
    ImpactStale,
    ImpactSymbol,
)

_IMPACT_SCHEMA_VERSION = 1
_MAX_PATCH_BYTES = 16 * 1024 * 1024
_MAX_DEPTH = 5
_MAX_GRAPH_NODES = 2_000
_HUNK = re.compile(
    r"^@@\s+-(?P<old_start>\d+)(?:,(?P<old_count>\d+))?\s+"
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))?\s+@@"
)
_UNSAFE_MODE = re.compile(r"^(?:new|old|deleted) (?:file )?mode (?:120000|160000)$")
_CONFIG_NAMES = {
    ".editorconfig",
    ".env",
    ".gitattributes",
    ".gitignore",
    "biome.json",
    "deno.json",
    "package.json",
    "pyproject.toml",
    "tsconfig.json",
}
_CONFIG_SUFFIXES = {".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf"}


@dataclass(slots=True)
class _PatchFile:
    header_old: str | None = None
    header_new: str | None = None
    status: str | None = None
    old_path: str | None = None
    new_path: str | None = None
    rename_from: str | None = None
    rename_to: str | None = None
    ranges: list[ChangedRange] | None = None
    additions: int = 0
    deletions: int = 0
    in_hunk: bool = False

    def __post_init__(self) -> None:
        if self.ranges is None:
            self.ranges = []


@dataclass(frozen=True, slots=True)
class _Analysis:
    changes: tuple[ChangedFile, ...]
    changed_symbols: tuple[ImpactSymbol, ...]
    impacted_symbols: tuple[ImpactSymbol, ...]
    impacted_files: tuple[ImpactFile, ...]
    tests: tuple[str, ...]
    docs: tuple[str, ...]
    configs: tuple[str, ...]
    migrations: tuple[str, ...]
    public_api_symbols: tuple[str, ...]
    unresolved_targets: tuple[str, ...]
    risk_score: int
    risk_level: str
    limitations: tuple[str, ...]


class ImpactAnalyzer:
    """Analyze and hash-gate a text patch against a semantic graph snapshot."""

    def __init__(self, atlas: CodeAtlas | None = None) -> None:
        self.atlas = atlas or CodeAtlas()

    async def analyze(
        self,
        proposed_diff: str,
        *,
        max_depth: int = 3,
        auto_refresh: bool = True,
        repo: str | os.PathLike[str] = ".",
    ) -> ImpactReport:
        """Validate a patch and persist its transitive static impact report."""
        patch = self._validate_patch_input(proposed_diff)
        if (
            not isinstance(max_depth, int)
            or isinstance(max_depth, bool)
            or not 0 <= max_depth <= _MAX_DEPTH
        ):
            raise ImpactError(f"max_depth must be an integer from 0 to {_MAX_DEPTH}")
        if not isinstance(auto_refresh, bool):
            raise ImpactError("auto_refresh must be a boolean")
        parsed = await asyncio.to_thread(self._parse_patch, patch.decode("utf-8"))
        for attempt in range(2):
            await self._ensure_current(repo, auto_refresh=auto_refresh)
            repo_root, common_dir = await self.atlas._repo_paths(repo)
            await asyncio.to_thread(self._check_patch, repo_root, patch)
            stats = await self.atlas.stats(repo_root)
            database_path = self.atlas._database_path(common_dir)
            analysis = await asyncio.to_thread(
                self._analyze_graph,
                database_path,
                parsed,
                max_depth,
            )
            created_at = datetime.now(timezone.utc).isoformat()
            placeholder = "impact-000000000000000000000000"
            report = ImpactReport(
                schema_version=_IMPACT_SCHEMA_VERSION,
                id=placeholder,
                repo_root=repo_root,
                common_dir=common_dir,
                indexed_head=stats.head_commit,
                graph_indexed_at=stats.indexed_at,
                patch_sha256=hashlib.sha256(patch).hexdigest(),
                patch_path=self._impact_path(common_dir, placeholder, ".patch"),
                changes=analysis.changes,
                changed_symbols=analysis.changed_symbols,
                impacted_symbols=analysis.impacted_symbols,
                impacted_files=analysis.impacted_files,
                tests=analysis.tests,
                docs=analysis.docs,
                configs=analysis.configs,
                migrations=analysis.migrations,
                public_api_symbols=analysis.public_api_symbols,
                unresolved_targets=analysis.unresolved_targets,
                risk_score=analysis.risk_score,
                risk_level=analysis.risk_level,
                limitations=analysis.limitations,
                created_at=created_at,
            )
            report_id = self._report_id(report)
            report = replace(
                report,
                id=report_id,
                patch_path=self._impact_path(common_dir, report_id, ".patch"),
            )
            freshness = await asyncio.to_thread(self._freshness_sync, report, patch)
            if freshness.fresh:
                await asyncio.to_thread(self._persist, report, patch)
                return report
            if not auto_refresh or attempt == 1:
                detail = freshness.reason or ", ".join(freshness.changed_files)
                raise ImpactStale(f"impact snapshot changed during analysis: {detail}")
            await self.atlas.build(repo_root)
        raise ImpactStale("impact snapshot remained unstable during analysis")

    async def load(
        self,
        report_id: str,
        *,
        repo: str | os.PathLike[str] = ".",
    ) -> ImpactReport:
        """Load and attest a persisted impact report and its exact patch."""
        self._validate_report_id(report_id)
        repo_root, common_dir = await self.atlas._repo_paths(repo)
        return await asyncio.to_thread(
            self._load_sync, common_dir, repo_root, report_id
        )

    async def freshness(self, report: ImpactReport) -> ImpactFreshness:
        """Check HEAD, graph, file hashes, and patch applicability without writing."""
        patch = await asyncio.to_thread(self._validate_report, report)
        return await asyncio.to_thread(self._freshness_sync, report, patch)

    async def require_fresh(self, report: ImpactReport) -> ImpactReport:
        """Fail closed unless every hash precondition still holds."""
        freshness = await self.freshness(report)
        if not freshness.fresh:
            detail = (
                freshness.reason
                or ", ".join(freshness.changed_files)
                or "unknown mismatch"
            )
            raise ImpactStale(f"impact report {report.id} is stale: {detail}")
        return report

    async def apply(self, report: ImpactReport) -> ImpactApplyResult:
        """Apply the attested patch under a repository lock after a final hash check."""
        patch = await asyncio.to_thread(self._validate_report, report)
        return await asyncio.to_thread(self._apply_sync, report, patch)

    async def _ensure_current(
        self, repo: str | os.PathLike[str], *, auto_refresh: bool
    ) -> None:
        try:
            freshness = await self.atlas.freshness(repo)
        except AtlasNotBuilt:
            if not auto_refresh:
                raise
            await self.atlas.build(repo)
            return
        if freshness.fresh:
            return
        if not auto_refresh:
            detail = ", ".join(freshness.changed_files)
            raise AtlasStale(f"Code Atlas is stale: {detail}")
        await self.atlas.build(repo)

    @staticmethod
    def _validate_patch_input(proposed_diff: str) -> bytes:
        if not isinstance(proposed_diff, str) or not proposed_diff.strip():
            raise ImpactError("proposed_diff must be a non-empty unified diff")
        normalized = (
            proposed_diff if proposed_diff.endswith("\n") else f"{proposed_diff}\n"
        )
        patch = normalized.encode("utf-8")
        if len(patch) > _MAX_PATCH_BYTES:
            raise ImpactError("proposed_diff exceeds 16 MiB")
        if "\0" in proposed_diff:
            raise ImpactError("proposed_diff must not contain NUL bytes")
        return patch

    @classmethod
    def _parse_patch(cls, patch: str) -> tuple[_PatchFile, ...]:
        files: list[_PatchFile] = []
        current: _PatchFile | None = None

        def finish() -> None:
            nonlocal current
            if current is None:
                return
            old_path = current.rename_from or current.old_path or current.header_old
            new_path = current.rename_to or current.new_path or current.header_new
            if old_path is None and new_path is None:
                raise ImpactError("patch file block has no source or destination path")
            if old_path == "/dev/null":
                old_path = None
            if new_path == "/dev/null":
                new_path = None
            if old_path is None:
                status = "added"
            elif new_path is None:
                status = "deleted"
            elif old_path != new_path:
                status = "renamed"
            else:
                status = "modified"
            current.old_path = old_path
            current.new_path = new_path
            current.status = status
            files.append(current)
            current = None

        for line in patch.splitlines():
            if line.startswith("diff --git "):
                finish()
                current = _PatchFile()
                try:
                    parts = shlex.split(line)
                except ValueError as error:
                    raise ImpactError("patch has an invalid diff header") from error
                if len(parts) >= 4:
                    current.header_old = cls._decode_path(parts[2], prefix="a/")
                    current.header_new = cls._decode_path(parts[3], prefix="b/")
                continue
            if current is None and line.startswith("--- "):
                current = _PatchFile()
            if current is None:
                continue
            if (
                _UNSAFE_MODE.match(line)
                or line in {"GIT binary patch"}
                or line.startswith("Binary files ")
            ):
                raise ImpactError(
                    "binary, symlink, and submodule patches are not supported"
                )
            if not current.in_hunk and line.startswith("rename from "):
                current.rename_from = cls._decode_path(
                    line.removeprefix("rename from ")
                )
            elif not current.in_hunk and line.startswith("rename to "):
                current.rename_to = cls._decode_path(line.removeprefix("rename to "))
            elif not current.in_hunk and line.startswith("--- "):
                current.old_path = cls._decode_path(line[4:], prefix="a/")
            elif not current.in_hunk and line.startswith("+++ "):
                current.new_path = cls._decode_path(line[4:], prefix="b/")
            elif line.startswith("@@"):
                match = _HUNK.match(line)
                if match is None:
                    raise ImpactError(f"invalid unified diff hunk: {line}")
                current.in_hunk = True
                current.ranges.append(
                    ChangedRange(
                        old_start=int(match.group("old_start")),
                        old_count=int(match.group("old_count") or 1),
                        new_start=int(match.group("new_start")),
                        new_count=int(match.group("new_count") or 1),
                    )
                )
            elif current.in_hunk and line.startswith("+"):
                current.additions += 1
            elif current.in_hunk and line.startswith("-"):
                current.deletions += 1
        finish()
        if not files:
            raise ImpactError("proposed_diff contains no file changes")
        normalized_paths: set[str] = set()
        for item in files:
            path = item.new_path or item.old_path
            if path is None:
                raise ImpactError("patch file has no usable path")
            if path in normalized_paths:
                raise ImpactError(f"patch contains duplicate file blocks: {path}")
            normalized_paths.add(path)
        return tuple(files)

    @staticmethod
    def _decode_path(value: str, *, prefix: str = "") -> str:
        raw = value.split("\t", 1)[0].strip()
        if raw == "/dev/null":
            return raw
        if raw.startswith('"'):
            try:
                decoded = ast.literal_eval(raw)
            except (SyntaxError, ValueError) as error:
                raise ImpactError(f"invalid quoted patch path: {raw}") from error
            if not isinstance(decoded, str):
                raise ImpactError(f"invalid quoted patch path: {raw}")
            raw = decoded
        if prefix and raw.startswith(("a/", "b/")):
            raw = raw[2:]
        path = PurePosixPath(raw)
        if not raw or path.is_absolute() or ".." in path.parts or "\0" in raw:
            raise ImpactError(f"unsafe patch path: {raw!r}")
        return path.as_posix()

    @staticmethod
    def _check_patch(repo_root: Path, patch: bytes) -> None:
        result = subprocess.run(
            (
                "git",
                "-C",
                str(repo_root),
                "apply",
                "--check",
                "--recount",
                "--whitespace=nowarn",
            ),
            check=False,
            input=patch,
            capture_output=True,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise ImpactError(
                f"proposed diff does not apply cleanly: {detail or 'no output'}"
            )

    @classmethod
    def _analyze_graph(
        cls,
        database_path: Path,
        parsed: tuple[_PatchFile, ...],
        max_depth: int,
    ) -> _Analysis:
        connection = _connect(database_path, require_existing=True)
        try:
            changes: list[ChangedFile] = []
            change_by_graph_path: dict[str, ChangedFile] = {}
            for item in parsed:
                status = item.status
                if status is None:
                    raise ImpactError("patch file status was not resolved")
                destination = item.new_path or item.old_path
                if destination is None:
                    raise ImpactError("patch file has no destination")
                graph_path = (
                    item.old_path if status in {"deleted", "renamed"} else destination
                )
                base_hash: str | None = None
                if status != "added":
                    row = connection.execute(
                        "SELECT content_hash FROM files WHERE path = ?",
                        (graph_path,),
                    ).fetchone()
                    if row is None:
                        raise ImpactError(
                            f"patch target is absent from Code Atlas: {graph_path}"
                        )
                    base_hash = row["content_hash"]
                change = ChangedFile(
                    path=destination,
                    old_path=item.old_path if item.old_path != destination else None,
                    status=status,
                    additions=item.additions,
                    deletions=item.deletions,
                    ranges=tuple(item.ranges),
                    base_hash=base_hash,
                )
                changes.append(change)
                if graph_path is not None:
                    change_by_graph_path[graph_path] = change

            changed_rows: dict[str, sqlite3.Row] = {}
            for graph_path, change in change_by_graph_path.items():
                rows = connection.execute(
                    """
                    SELECT s.*, f.path AS file_path
                    FROM symbols s JOIN files f ON f.id = s.file_id
                    WHERE f.path = ?
                    ORDER BY s.start_line, s.end_line
                    """,
                    (graph_path,),
                ).fetchall()
                for row in rows:
                    if cls._symbol_touched(row, change):
                        changed_rows[row["key"]] = row

            changed_symbols = tuple(
                cls._impact_symbol(
                    row, depth=0, relation="changed declaration", confidence=1
                )
                for row in sorted(
                    changed_rows.values(),
                    key=lambda value: (
                        value["file_path"],
                        value["start_line"],
                        value["qualified_name"],
                    ),
                )
            )
            seen_keys = set(changed_rows)
            frontier = set(changed_rows)
            impacted_rows: dict[str, tuple[sqlite3.Row, int, set[str], float]] = {}
            file_depth: dict[str, int] = {path: 0 for path in change_by_graph_path}
            file_reasons: dict[str, set[str]] = defaultdict(set)
            for path in change_by_graph_path:
                file_reasons[path].add("proposed change")

            for depth in range(1, max_depth + 1):
                if not frontier or len(seen_keys) >= _MAX_GRAPH_NODES:
                    break
                placeholders = ",".join("?" for _ in frontier)
                rows = connection.execute(
                    f"""
                    SELECT e.kind AS edge_kind, e.confidence AS edge_confidence,
                           s.*, f.path AS file_path
                    FROM edges e
                    JOIN symbols s ON s.key = e.source_symbol_key
                    JOIN files f ON f.id = s.file_id
                    WHERE e.target_symbol_key IN ({placeholders})
                    ORDER BY e.confidence DESC, f.path, s.start_line
                    LIMIT {_MAX_GRAPH_NODES}
                    """,
                    tuple(frontier),
                ).fetchall()
                next_frontier: set[str] = set()
                for row in rows:
                    key = row["key"]
                    relation = f"incoming {row['edge_kind']} edge"
                    confidence = float(row["edge_confidence"])
                    if key not in seen_keys:
                        impacted_rows[key] = (row, depth, {relation}, confidence)
                        seen_keys.add(key)
                        next_frontier.add(key)
                    elif key in impacted_rows and impacted_rows[key][1] == depth:
                        existing_row, existing_depth, reasons, previous_confidence = (
                            impacted_rows[key]
                        )
                        reasons.add(relation)
                        impacted_rows[key] = (
                            existing_row,
                            existing_depth,
                            reasons,
                            max(previous_confidence, confidence),
                        )
                    path = row["file_path"]
                    file_depth[path] = min(depth, file_depth.get(path, depth))
                    file_reasons[path].add(relation)
                frontier = next_frontier

            file_frontier = set(change_by_graph_path)
            seen_files = set(file_frontier)
            for depth in range(1, max_depth + 1):
                if not file_frontier or len(seen_files) >= _MAX_GRAPH_NODES:
                    break
                placeholders = ",".join("?" for _ in file_frontier)
                rows = connection.execute(
                    f"""
                    SELECT DISTINCT source.path AS source_path, e.kind
                    FROM edges e
                    JOIN files source ON source.id = e.source_file_id
                    JOIN files target ON target.id = e.target_file_id
                    WHERE target.path IN ({placeholders}) AND source.path != target.path
                    ORDER BY source.path LIMIT {_MAX_GRAPH_NODES}
                    """,
                    tuple(file_frontier),
                ).fetchall()
                next_frontier: set[str] = set()
                for row in rows:
                    path = row["source_path"]
                    file_depth[path] = min(depth, file_depth.get(path, depth))
                    file_reasons[path].add(f"incoming {row['kind']} file edge")
                    if path not in seen_files:
                        seen_files.add(path)
                        next_frontier.add(path)
                file_frontier = next_frontier

            impacted_symbols = tuple(
                cls._impact_symbol(
                    value[0],
                    depth=value[1],
                    relation="; ".join(sorted(value[2])),
                    confidence=value[3],
                )
                for _, value in sorted(
                    impacted_rows.items(),
                    key=lambda item: (
                        item[1][1],
                        item[1][0]["file_path"],
                        item[1][0]["start_line"],
                    ),
                )
            )
            for symbol in impacted_symbols:
                file_depth[symbol.file] = min(
                    symbol.depth, file_depth.get(symbol.file, symbol.depth)
                )
                file_reasons[symbol.file].add(symbol.relation)

            changed_destinations = {change.path for change in changes}
            for path in changed_destinations:
                file_depth.setdefault(path, 0)
                file_reasons[path].add("proposed change destination")
            impacted_files = tuple(
                ImpactFile(
                    path=path,
                    depth=file_depth[path],
                    categories=cls._categories(path),
                    reasons=tuple(sorted(file_reasons[path])),
                )
                for path in sorted(
                    file_depth, key=lambda item: (file_depth[item], item)
                )
            )
            tests = tuple(
                item.path for item in impacted_files if "test" in item.categories
            )
            docs = tuple(
                item.path
                for item in impacted_files
                if "documentation" in item.categories
            )
            configs = tuple(
                item.path
                for item in impacted_files
                if "configuration" in item.categories
            )
            migrations = tuple(
                item.path for item in impacted_files if "migration" in item.categories
            )
            public_api = tuple(
                sorted(
                    {item.qualified_name for item in changed_symbols if item.exported}
                )
            )

            unresolved: tuple[str, ...] = ()
            if changed_rows:
                placeholders = ",".join("?" for _ in changed_rows)
                rows = connection.execute(
                    f"""
                    SELECT DISTINCT target_name
                    FROM edges
                    WHERE source_symbol_key IN ({placeholders})
                      AND target_symbol_key IS NULL
                      AND kind IN ('calls', 'references', 'extends', 'implements')
                    ORDER BY target_name LIMIT 50
                    """,
                    tuple(changed_rows),
                )
                unresolved = tuple(row["target_name"] for row in rows)

            relevant_paths = tuple(file_depth)
            parse_issues: list[str] = []
            for offset in range(0, len(relevant_paths), 500):
                batch = relevant_paths[offset : offset + 500]
                if not batch:
                    continue
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"SELECT path, parse_status FROM files WHERE path IN ({placeholders})",
                    batch,
                )
                parse_issues.extend(
                    f"{row['path']} uses parser status {row['parse_status']}"
                    for row in rows
                    if row["parse_status"] in {"fallback", "error", "encoding_error"}
                )

            limitations = [
                "Static impact cannot prove reflective, generated, data-driven, or runtime-only dependencies."
            ]
            limitations.extend(sorted(parse_issues))
            if unresolved:
                limitations.append(
                    f"{len(unresolved)} outgoing targets from changed symbols are unresolved."
                )
            if not tests:
                limitations.append("No statically connected test file was discovered.")

            risk = min(50, len(public_api) * 20)
            risk += min(15, len(changes) * 3)
            risk += min(20, len(impacted_symbols) * 2)
            risk += 15 if public_api and not tests else 0
            risk += 12 if any(change.status == "deleted" for change in changes) else 0
            risk += 10 if unresolved else 0
            risk += 10 if migrations else 0
            risk += 5 if configs else 0
            risk = min(100, risk)
            if risk >= 75:
                risk_level = "critical"
            elif risk >= 50:
                risk_level = "high"
            elif risk >= 25:
                risk_level = "medium"
            else:
                risk_level = "low"
            return _Analysis(
                changes=tuple(changes),
                changed_symbols=changed_symbols,
                impacted_symbols=impacted_symbols,
                impacted_files=impacted_files,
                tests=tests,
                docs=docs,
                configs=configs,
                migrations=migrations,
                public_api_symbols=public_api,
                unresolved_targets=unresolved,
                risk_score=risk,
                risk_level=risk_level,
                limitations=tuple(limitations),
            )
        finally:
            connection.close()

    @staticmethod
    def _symbol_touched(row: sqlite3.Row, change: ChangedFile) -> bool:
        if change.status == "deleted" or not change.ranges:
            return True
        start = int(row["start_line"])
        end = int(row["end_line"])
        for changed in change.ranges:
            if changed.old_count == 0:
                if start <= changed.old_start <= end:
                    return True
                continue
            changed_end = changed.old_start + changed.old_count - 1
            if start <= changed_end and end >= changed.old_start:
                return True
        return False

    @staticmethod
    def _impact_symbol(
        row: sqlite3.Row,
        *,
        depth: int,
        relation: str,
        confidence: float,
    ) -> ImpactSymbol:
        return ImpactSymbol(
            key=row["key"],
            file=row["file_path"],
            kind=row["kind"],
            name=row["name"],
            qualified_name=row["qualified_name"],
            line=row["start_line"],
            exported=bool(row["exported"]),
            depth=depth,
            relation=relation,
            confidence=confidence,
        )

    @staticmethod
    def _categories(path: str) -> tuple[str, ...]:
        lowered = path.lower()
        name = PurePosixPath(lowered).name
        suffix = PurePosixPath(lowered).suffix
        categories: list[str] = []
        if (
            "/tests/" in f"/{lowered}"
            or "/test/" in f"/{lowered}"
            or name.startswith("test_")
            or ".test." in name
            or ".spec." in name
        ):
            categories.append("test")
        if (
            lowered.startswith("docs/")
            or "/docs/" in f"/{lowered}"
            or suffix in {".md", ".rst"}
        ):
            categories.append("documentation")
        if name in _CONFIG_NAMES or suffix in _CONFIG_SUFFIXES:
            categories.append("configuration")
        if "migration" in lowered or "/migrations/" in f"/{lowered}":
            categories.append("migration")
        if not categories:
            categories.append("source")
        return tuple(categories)

    def _validate_report(self, report: ImpactReport) -> bytes:
        if not isinstance(report, ImpactReport):
            raise TypeError("report must be an ImpactReport")
        self._validate_report_id(report.id)
        if (
            report.schema_version != _IMPACT_SCHEMA_VERSION
            or self._report_id(report) != report.id
        ):
            raise ImpactError(f"impact report attestation failed: {report.id}")
        expected_path = self._impact_path(report.common_dir, report.id, ".patch")
        if report.patch_path.resolve() != expected_path.resolve():
            raise ImpactError(f"impact report patch path mismatch: {report.id}")
        try:
            patch = report.patch_path.read_bytes()
        except OSError as error:
            raise ImpactError(
                f"impact report patch is unreadable: {report.id}"
            ) from error
        if hashlib.sha256(patch).hexdigest() != report.patch_sha256:
            raise ImpactError(f"impact report patch hash mismatch: {report.id}")
        return patch

    def _freshness_sync(self, report: ImpactReport, patch: bytes) -> ImpactFreshness:
        current_head = (
            self._git_output(report.repo_root, "rev-parse", "HEAD").decode().strip()
        )
        head_changed = current_head != report.indexed_head
        database_path = self.atlas._database_path(report.common_dir)
        graph_changed = False
        changed_files: set[str] = set()
        reason: str | None = None
        try:
            connection = _connect(database_path, require_existing=True)
            try:
                metadata = dict(connection.execute("SELECT key, value FROM metadata"))
                graph_changed = (
                    metadata.get("head_commit") != report.indexed_head
                    or metadata.get("indexed_at") != report.graph_indexed_at
                )
                dirty_output = self._git_output(
                    report.repo_root,
                    "diff",
                    "--name-only",
                    "-z",
                    "HEAD",
                    "--",
                )
                dirty_paths = tuple(
                    path
                    for path in dirty_output.decode(
                        "utf-8", errors="surrogateescape"
                    ).split("\0")
                    if path
                )
                indexed_hashes = self._indexed_hashes(connection, dirty_paths)
                for relative in dirty_paths:
                    current_hash = self._current_hash(report.repo_root / relative)
                    if indexed_hashes.get(relative) != current_hash:
                        graph_changed = True
                        changed_files.add(relative)
            finally:
                connection.close()
        except (AtlasError, ImpactError, OSError, sqlite3.Error, UnicodeError) as error:
            graph_changed = True
            reason = f"graph freshness check failed: {error}"

        for change in report.changes:
            base_path = change.old_path or change.path
            if change.status == "added":
                if (report.repo_root / change.path).exists():
                    changed_files.add(change.path)
                continue
            current_hash = self._current_hash(report.repo_root / base_path)
            if current_hash != change.base_hash:
                changed_files.add(base_path)
            if (
                change.status == "renamed"
                and change.path != base_path
                and (report.repo_root / change.path).exists()
            ):
                changed_files.add(change.path)

        patch_applies = False
        if not head_changed and not changed_files:
            try:
                self._check_patch(report.repo_root, patch)
                patch_applies = True
            except ImpactError as error:
                reason = str(error)
        if head_changed and reason is None:
            reason = "repository HEAD changed"
        elif changed_files and reason is None:
            reason = "file content hash changed"
        elif graph_changed and reason is None:
            reason = "Code Atlas snapshot changed"
        elif not patch_applies and reason is None:
            reason = "patch no longer applies"
        return ImpactFreshness(
            fresh=not head_changed
            and not graph_changed
            and not changed_files
            and patch_applies,
            head_changed=head_changed,
            graph_changed=graph_changed,
            changed_files=tuple(sorted(changed_files)),
            patch_applies=patch_applies,
            reason=reason,
        )

    def _apply_sync(self, report: ImpactReport, patch: bytes) -> ImpactApplyResult:
        lock_path = (
            self.atlas.state_dir
            / "locks"
            / f"{_repo_key(report.common_dir)}.impact.lock"
        )
        with self._lock(lock_path):
            freshness = self._freshness_sync(report, patch)
            if not freshness.fresh:
                detail = freshness.reason or ", ".join(freshness.changed_files)
                raise ImpactStale(f"impact report {report.id} is stale: {detail}")
            result = subprocess.run(
                (
                    "git",
                    "-C",
                    str(report.repo_root),
                    "apply",
                    "--recount",
                    "--whitespace=nowarn",
                ),
                check=False,
                input=patch,
                capture_output=True,
            )
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", errors="replace").strip()
                raise ImpactStale(
                    f"attested patch failed during apply: {detail or 'no output'}"
                )
            hashes = {
                change.path: self._current_hash(report.repo_root / change.path)
                for change in report.changes
            }
            applied = ImpactApplyResult(
                report_id=report.id,
                applied_files=tuple(change.path for change in report.changes),
                content_hashes=hashes,
                applied_at=datetime.now(timezone.utc).isoformat(),
            )
            self._write_json_atomic(
                self._impact_path(report.common_dir, report.id, ".applied.json"),
                asdict(applied),
            )
            return applied

    @staticmethod
    @contextmanager
    def _lock(path: Path) -> Iterator[None]:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _current_hash(path: Path) -> str | None:
        if not path.is_file() or path.is_symlink():
            return None
        try:
            return _hash_file(path)
        except OSError:
            return None

    @staticmethod
    def _indexed_hashes(
        connection: sqlite3.Connection, paths: tuple[str, ...]
    ) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for offset in range(0, len(paths), 500):
            batch = paths[offset : offset + 500]
            if not batch:
                continue
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"SELECT path, content_hash FROM files WHERE path IN ({placeholders})",
                batch,
            )
            hashes.update({row["path"]: row["content_hash"] for row in rows})
        return hashes

    @staticmethod
    def _git_output(repo_root: Path, *args: str) -> bytes:
        result = subprocess.run(
            ("git", "-C", str(repo_root), *args),
            check=False,
            capture_output=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise ImpactError(f"git {' '.join(args)} failed: {detail or 'no output'}")
        return result.stdout

    def _impact_path(self, common_dir: Path, report_id: str, suffix: str) -> Path:
        return (
            self.atlas.state_dir
            / "impacts"
            / _repo_key(common_dir)
            / f"{report_id}{suffix}"
        )

    @staticmethod
    def _report_id(report: ImpactReport) -> str:
        payload = asdict(report)
        payload["id"] = None
        payload["repo_root"] = str(report.repo_root)
        payload["common_dir"] = str(report.common_dir)
        payload["patch_path"] = None
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"impact-{digest[:24]}"

    @staticmethod
    def _validate_report_id(report_id: str) -> None:
        if not isinstance(report_id, str) or not re.fullmatch(
            r"impact-[0-9a-f]{24}", report_id
        ):
            raise ImpactError("invalid impact report id")

    def _persist(self, report: ImpactReport, patch: bytes) -> None:
        report.patch_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_bytes_atomic(report.patch_path, patch)
        payload = asdict(report)
        payload["repo_root"] = str(report.repo_root)
        payload["common_dir"] = str(report.common_dir)
        payload["patch_path"] = str(report.patch_path)
        self._write_json_atomic(
            self._impact_path(report.common_dir, report.id, ".json"), payload
        )

    def _load_sync(
        self, common_dir: Path, repo_root: Path, report_id: str
    ) -> ImpactReport:
        path = self._impact_path(common_dir, report_id, ".json")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ImpactError(f"impact report not found: {report_id}") from error
        except json.JSONDecodeError as error:
            raise ImpactError(f"impact report is invalid JSON: {report_id}") from error
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != _IMPACT_SCHEMA_VERSION
        ):
            raise ImpactError(f"unsupported impact report schema: {report_id}")
        try:
            stored_root = Path(payload["repo_root"]).resolve()
            stored_common = Path(payload["common_dir"]).resolve()
            if (
                stored_root != repo_root.resolve()
                or stored_common != common_dir.resolve()
            ):
                raise ImpactError(
                    f"impact report belongs to another repository: {report_id}"
                )
            changes = tuple(
                ChangedFile(
                    path=item["path"],
                    old_path=item["old_path"],
                    status=item["status"],
                    additions=item["additions"],
                    deletions=item["deletions"],
                    ranges=tuple(
                        ChangedRange(**changed_range)
                        for changed_range in item["ranges"]
                    ),
                    base_hash=item["base_hash"],
                )
                for item in payload["changes"]
            )
            report = ImpactReport(
                schema_version=payload["schema_version"],
                id=payload["id"],
                repo_root=stored_root,
                common_dir=stored_common,
                indexed_head=payload["indexed_head"],
                graph_indexed_at=payload["graph_indexed_at"],
                patch_sha256=payload["patch_sha256"],
                patch_path=Path(payload["patch_path"]),
                changes=changes,
                changed_symbols=tuple(
                    ImpactSymbol(**item) for item in payload["changed_symbols"]
                ),
                impacted_symbols=tuple(
                    ImpactSymbol(**item) for item in payload["impacted_symbols"]
                ),
                impacted_files=tuple(
                    ImpactFile(
                        path=item["path"],
                        depth=item["depth"],
                        categories=tuple(item["categories"]),
                        reasons=tuple(item["reasons"]),
                    )
                    for item in payload["impacted_files"]
                ),
                tests=tuple(payload["tests"]),
                docs=tuple(payload["docs"]),
                configs=tuple(payload["configs"]),
                migrations=tuple(payload["migrations"]),
                public_api_symbols=tuple(payload["public_api_symbols"]),
                unresolved_targets=tuple(payload["unresolved_targets"]),
                risk_score=payload["risk_score"],
                risk_level=payload["risk_level"],
                limitations=tuple(payload["limitations"]),
                created_at=payload["created_at"],
            )
        except ImpactError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise ImpactError(
                f"impact report has an invalid shape: {report_id}"
            ) from error
        if report.id != report_id:
            raise ImpactError(f"impact report id mismatch: {report_id}")
        self._validate_report(report)
        return report

    @staticmethod
    def _write_bytes_atomic(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    @classmethod
    def _write_json_atomic(cls, path: Path, payload: object) -> None:
        cls._write_bytes_atomic(
            path,
            (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
