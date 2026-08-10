from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from ._graph import CodeAtlas, _connect, _hash_file, _repo_key
from ._models import (
    AtlasError,
    AtlasNotBuilt,
    AtlasStale,
    CapsuleError,
    CapsuleFreshness,
    ContextCapsule,
    ContextItem,
    ExcludedContext,
)

_CAPSULE_SCHEMA_VERSION = 1
_MIN_TOKEN_BUDGET = 256
_MAX_TOKEN_BUDGET = 1_000_000
_MAX_ROOTS = 32
_MAX_DEPTH = 3
_MAX_SELECTED_SYMBOLS = 160
_MAX_SELECTED_FILES = 100
_MAX_SNIPPET_LINES = 160
_MAX_EXCLUSIONS = 24
_PATH_PATTERN = re.compile(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9]+")
_TERM_PATTERN = re.compile(r"[A-Za-z_$][A-Za-z0-9_$.:-]{2,}")
_STOP_WORDS = {
    "add",
    "after",
    "agent",
    "before",
    "break",
    "change",
    "code",
    "create",
    "delete",
    "ensure",
    "file",
    "files",
    "from",
    "implement",
    "into",
    "make",
    "must",
    "project",
    "refactor",
    "remove",
    "repository",
    "return",
    "should",
    "test",
    "tests",
    "that",
    "then",
    "this",
    "update",
    "when",
    "with",
    "without",
}


@dataclass(slots=True)
class _Span:
    path: str
    start_line: int
    end_line: int
    score: float
    reasons: set[str]
    relations: set[str]
    symbol_keys: set[str]
    content_hash: str


class ContextCompiler:
    """Compile bounded, provenance-rich source context from a current Code Atlas."""

    def __init__(self, atlas: CodeAtlas | None = None) -> None:
        self.atlas = atlas or CodeAtlas()

    async def compile(
        self,
        task: str,
        *,
        contract: object | None = None,
        roots: Sequence[str] = (),
        paths: Sequence[str] = (),
        token_budget: int = 18_000,
        depth: int = 2,
        auto_refresh: bool = True,
        repo: str | os.PathLike[str] = ".",
    ) -> ContextCapsule:
        """Build and persist a current capsule; stale indexes are refreshed by default."""
        task = self._validate_task(task)
        normalized_contract = self._normalize_contract(contract)
        roots = self._validate_strings("roots", roots, maximum=_MAX_ROOTS)
        paths = self._validate_strings("paths", paths, maximum=_MAX_SELECTED_FILES)
        if not isinstance(token_budget, int) or isinstance(token_budget, bool):
            raise CapsuleError("token_budget must be an integer")
        if token_budget < _MIN_TOKEN_BUDGET or token_budget > _MAX_TOKEN_BUDGET:
            raise CapsuleError(
                f"token_budget must be between {_MIN_TOKEN_BUDGET} and {_MAX_TOKEN_BUDGET}"
            )
        if (
            not isinstance(depth, int)
            or isinstance(depth, bool)
            or depth < 0
            or depth > _MAX_DEPTH
        ):
            raise CapsuleError(f"depth must be an integer from 0 to {_MAX_DEPTH}")
        if not isinstance(auto_refresh, bool):
            raise CapsuleError("auto_refresh must be a boolean")

        for attempt in range(2):
            await self._ensure_current(repo, auto_refresh=auto_refresh)
            repo_root, common_dir = await self.atlas._repo_paths(repo)
            dirty_paths = await self._dirty_paths(repo_root)
            stats = await self.atlas.stats(repo_root)
            database_path = self.atlas._database_path(common_dir)
            spans, unrelated_files = await asyncio.to_thread(
                self._select,
                database_path,
                task,
                normalized_contract,
                roots,
                paths,
                dirty_paths,
                depth,
            )
            capsule = await self._materialize(
                repo_root=repo_root,
                task=task,
                contract=normalized_contract,
                roots=roots,
                spans=spans,
                unrelated_files=unrelated_files,
                token_budget=token_budget,
                head_commit=stats.head_commit,
                graph_indexed_at=stats.indexed_at,
            )
            freshness = await self.freshness(capsule)
            if freshness.fresh:
                await asyncio.to_thread(self._persist, common_dir, capsule)
                return capsule
            if not auto_refresh or attempt == 1:
                changed = ", ".join(
                    (*freshness.changed_files, *freshness.missing_files)
                )
                raise AtlasStale(
                    f"context sources changed during compilation: {changed}"
                )
            await self.atlas.build(repo_root)
        raise AtlasStale("context sources remained unstable during compilation")

    async def load(
        self,
        capsule_id: str,
        *,
        repo: str | os.PathLike[str] = ".",
    ) -> ContextCapsule:
        """Load a validated persisted capsule for this repository."""
        if not isinstance(capsule_id, str) or not re.fullmatch(
            r"capsule-[0-9a-f]{24}", capsule_id
        ):
            raise CapsuleError("invalid capsule id")
        repo_root, common_dir = await self.atlas._repo_paths(repo)
        return await asyncio.to_thread(
            self._load_sync, common_dir, repo_root, capsule_id
        )

    async def freshness(self, capsule: ContextCapsule) -> CapsuleFreshness:
        """Check source hashes and the semantic snapshot represented by a capsule."""
        if not isinstance(capsule, ContextCapsule):
            raise TypeError("capsule must be a ContextCapsule")
        expected: dict[str, str] = {}
        for item in capsule.items:
            previous = expected.setdefault(item.source, item.content_hash)
            if previous != item.content_hash:
                raise CapsuleError(
                    f"capsule contains conflicting hashes for {item.source}"
                )
        changed: list[str] = []
        missing: list[str] = []
        for source, expected_hash in sorted(expected.items()):
            try:
                path = self._safe_source_path(capsule.repo_root, source)
            except CapsuleError:
                missing.append(source)
                continue
            if not path.is_file() or path.is_symlink():
                missing.append(source)
                continue
            try:
                current_hash = await asyncio.to_thread(_hash_file, path)
            except OSError:
                missing.append(source)
                continue
            if current_hash != expected_hash:
                changed.append(source)
        graph_changed = False
        try:
            graph_freshness, stats = await asyncio.gather(
                self.atlas.freshness(capsule.repo_root),
                self.atlas.stats(capsule.repo_root),
            )
            graph_changed = (
                not graph_freshness.fresh
                or stats.indexed_at != capsule.graph_indexed_at
            )
            changed.extend(graph_freshness.changed_files)
        except (AtlasError, OSError):
            graph_changed = True
        changed_files = tuple(sorted(set(changed)))
        missing_files = tuple(sorted(set(missing)))
        return CapsuleFreshness(
            fresh=not changed_files and not missing_files and not graph_changed,
            changed_files=changed_files,
            missing_files=missing_files,
            graph_changed=graph_changed,
        )

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
    async def _dirty_paths(repo_root: Path) -> tuple[str, ...]:
        output = await CodeAtlas._git_text(
            repo_root, "diff", "--name-only", "-z", "HEAD", "--"
        )
        return tuple(sorted({path for path in output.split("\0") if path}))

    @staticmethod
    def _validate_task(task: str) -> str:
        if not isinstance(task, str) or not task.strip():
            raise CapsuleError("task must be a non-empty string")
        return task.strip()

    @staticmethod
    def _validate_strings(
        name: str, values: Sequence[str], *, maximum: int
    ) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise CapsuleError(f"{name} must be a sequence of strings")
        if any(not isinstance(value, str) for value in values):
            raise CapsuleError(f"{name} entries must be strings")
        normalized = tuple(value.strip() for value in values)
        if len(normalized) > maximum:
            raise CapsuleError(f"{name} may contain at most {maximum} entries")
        if any(not value for value in normalized):
            raise CapsuleError(f"{name} entries must be non-empty strings")
        return normalized

    @classmethod
    def _normalize_contract(
        cls, contract: object | None
    ) -> str | dict[str, object] | list[object] | None:
        if contract is None or isinstance(contract, str):
            return contract
        value = (
            asdict(contract)
            if is_dataclass(contract) and not isinstance(contract, type)
            else contract
        )
        normalized = cls._jsonable(value)
        if not isinstance(normalized, (dict, list)):
            raise CapsuleError(
                "contract must be text, a mapping, a sequence, or a dataclass"
            )
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
            return cls._jsonable(asdict(value))
        raise CapsuleError(
            f"contract contains unsupported value: {type(value).__name__}"
        )

    @classmethod
    def _select(
        cls,
        database_path: Path,
        task: str,
        contract: str | dict[str, object] | list[object] | None,
        requested_roots: tuple[str, ...],
        requested_paths: tuple[str, ...],
        dirty_paths: tuple[str, ...],
        depth: int,
    ) -> tuple[list[_Span], int]:
        connection = _connect(database_path, require_existing=True)
        try:
            file_count = connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            symbol_scores: dict[str, float] = {}
            symbol_reasons: dict[str, set[str]] = {}
            symbol_relations: dict[str, set[str]] = {}
            file_scores: dict[str, float] = {}
            file_reasons: dict[str, set[str]] = {}
            file_relations: dict[str, set[str]] = {}

            def add_symbol(key: str, score: float, reason: str, relation: str) -> None:
                if (
                    len(symbol_scores) >= _MAX_SELECTED_SYMBOLS
                    and key not in symbol_scores
                ):
                    return
                symbol_scores[key] = max(score, symbol_scores.get(key, 0))
                symbol_reasons.setdefault(key, set()).add(reason)
                symbol_relations.setdefault(key, set()).add(relation)

            def add_file(path: str, score: float, reason: str, relation: str) -> None:
                if len(file_scores) >= _MAX_SELECTED_FILES and path not in file_scores:
                    return
                file_scores[path] = max(score, file_scores.get(path, 0))
                file_reasons.setdefault(path, set()).add(reason)
                file_relations.setdefault(path, set()).add(relation)

            for root in requested_roots:
                row = cls._resolve_symbol_row(connection, root)
                add_symbol(
                    row["key"], 150, f"explicit symbol root {root}", "direct task root"
                )
                add_file(
                    row["file_path"],
                    145,
                    f"defines explicit root {root}",
                    "direct task root",
                )

            for requested in requested_paths:
                row = cls._resolve_file_row(connection, requested)
                add_file(
                    row["path"],
                    150,
                    f"explicit file root {requested}",
                    "direct task root",
                )

            for path in dirty_paths:
                row = connection.execute(
                    "SELECT path FROM files WHERE path = ?", (path,)
                ).fetchone()
                if row is not None:
                    add_file(
                        path, 125, "tracked worktree change", "current candidate diff"
                    )

            contract_text = (
                contract
                if isinstance(contract, str)
                else json.dumps(contract, sort_keys=True)
                if contract is not None
                else ""
            )
            search_text = f"{task}\n{contract_text}"
            terms = cls._task_terms(search_text)
            for term in terms:
                lowered = term.lower()
                escaped = cls._like(term)
                rows = connection.execute(
                    """
                    SELECT s.key, s.name, s.qualified_name, f.path AS file_path
                    FROM symbols s JOIN files f ON f.id = s.file_id
                    WHERE lower(s.name) = ? OR lower(s.qualified_name) = ?
                       OR s.name LIKE ? ESCAPE '\\' OR s.qualified_name LIKE ? ESCAPE '\\'
                    ORDER BY
                        CASE WHEN lower(s.qualified_name) = ? THEN 0 WHEN lower(s.name) = ? THEN 1 ELSE 2 END,
                        s.exported DESC, length(s.qualified_name), f.path
                    LIMIT 4
                    """,
                    (
                        lowered,
                        lowered,
                        f"%{escaped}%",
                        f"%{escaped}%",
                        lowered,
                        lowered,
                    ),
                ).fetchall()
                for position, row in enumerate(rows):
                    exact = (
                        row["name"].lower() == lowered
                        or row["qualified_name"].lower() == lowered
                    )
                    score = (112 if exact else 84) - position * 4
                    add_symbol(
                        row["key"],
                        score,
                        f"symbol name matches task term {term}",
                        "lexical task match",
                    )
                    add_file(
                        row["file_path"],
                        score - 8,
                        f"contains symbol matching {term}",
                        "lexical task match",
                    )
                path_rows = connection.execute(
                    "SELECT path FROM files WHERE path LIKE ? ESCAPE '\\' ORDER BY length(path), path LIMIT 2",
                    (f"%{escaped}%",),
                ).fetchall()
                for row in path_rows:
                    add_file(
                        row["path"],
                        70,
                        f"path matches task term {term}",
                        "lexical task match",
                    )

            for mentioned in _PATH_PATTERN.findall(search_text):
                row = connection.execute(
                    "SELECT path FROM files WHERE path = ?", (mentioned,)
                ).fetchone()
                if row is not None:
                    add_file(
                        mentioned,
                        130,
                        "path explicitly mentioned in task",
                        "direct task mention",
                    )

            frontier = set(symbol_scores)
            seen = set(frontier)
            for level in range(depth):
                if not frontier:
                    break
                placeholders = ",".join("?" for _ in frontier)
                rows = connection.execute(
                    f"""
                    SELECT e.*, source.path AS source_path, target.path AS target_path
                    FROM edges e
                    JOIN files source ON source.id = e.source_file_id
                    LEFT JOIN files target ON target.id = e.target_file_id
                    WHERE e.source_symbol_key IN ({placeholders})
                       OR e.target_symbol_key IN ({placeholders})
                    ORDER BY e.confidence DESC, e.kind, source.path, e.line
                    LIMIT 3000
                    """,
                    (*frontier, *frontier),
                ).fetchall()
                candidate_keys: set[str] = set()
                edge_by_key: dict[str, list[object]] = {}
                for row in rows:
                    if (
                        row["source_symbol_key"] in frontier
                        and row["target_symbol_key"]
                    ):
                        key = row["target_symbol_key"]
                        candidate_keys.add(key)
                        edge_by_key.setdefault(key, []).append(row)
                    if (
                        row["target_symbol_key"] in frontier
                        and row["source_symbol_key"]
                    ):
                        key = row["source_symbol_key"]
                        candidate_keys.add(key)
                        edge_by_key.setdefault(key, []).append(row)
                    add_file(
                        row["source_path"],
                        72 - level * 14,
                        f"{row['kind']} edge from selected symbol",
                        f"graph distance {level + 1}",
                    )
                    if row["target_path"]:
                        add_file(
                            row["target_path"],
                            68 - level * 14,
                            f"target of {row['kind']} edge",
                            f"graph distance {level + 1}",
                        )
                available = cls._existing_symbol_keys(connection, candidate_keys - seen)
                next_frontier: set[str] = set()
                for key in sorted(available):
                    evidence = edge_by_key.get(key, [])
                    confidence = max(
                        (float(row["confidence"]) for row in evidence), default=0.5
                    )
                    kinds = ", ".join(sorted({str(row["kind"]) for row in evidence}))
                    add_symbol(
                        key,
                        (78 - level * 18) * confidence,
                        f"connected by {kinds}",
                        f"semantic graph distance {level + 1}",
                    )
                    next_frontier.add(key)
                seen.update(next_frontier)
                frontier = next_frontier

            cls._add_import_neighbors(
                connection,
                file_scores,
                add_file,
            )
            spans = cls._spans_from_selection(
                connection,
                symbol_scores,
                symbol_reasons,
                symbol_relations,
                file_scores,
                file_reasons,
                file_relations,
            )
            selected_files = {span.path for span in spans}
            return spans, max(0, file_count - len(selected_files))
        finally:
            connection.close()

    @staticmethod
    def _resolve_symbol_row(connection: sqlite3.Connection, query: str) -> sqlite3.Row:
        rows = connection.execute(
            """
            SELECT s.*, f.path AS file_path
            FROM symbols s JOIN files f ON f.id = s.file_id
            WHERE s.key = ? OR s.qualified_name = ? OR s.name = ?
               OR s.qualified_name LIKE ? OR s.name LIKE ?
            ORDER BY
                CASE WHEN s.key = ? THEN 0 WHEN s.qualified_name = ? THEN 1 WHEN s.name = ? THEN 2 ELSE 3 END,
                s.qualified_name, f.path, s.start_line
            LIMIT 20
            """,
            (query, query, query, f"%{query}%", f"%{query}%", query, query, query),
        ).fetchall()
        exact = [
            row
            for row in rows
            if query in {row["key"], row["qualified_name"], row["name"]}
        ]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            detail = ", ".join(
                f"{row['qualified_name']} ({row['file_path']}:{row['start_line']})"
                for row in exact
            )
            raise AtlasError(f"ambiguous symbol {query!r}: {detail}")
        if len(rows) == 1:
            return rows[0]
        if not rows:
            raise AtlasError(f"symbol not found: {query}")
        detail = ", ".join(
            f"{row['qualified_name']} ({row['file_path']}:{row['start_line']})"
            for row in rows[:8]
        )
        raise AtlasError(f"ambiguous symbol {query!r}: {detail}")

    @staticmethod
    def _resolve_file_row(connection: sqlite3.Connection, query: str) -> sqlite3.Row:
        normalized = query.removeprefix("./")
        if Path(normalized).is_absolute() or ".." in Path(normalized).parts:
            raise CapsuleError(f"file root must be repository-relative: {query}")
        exact = connection.execute(
            "SELECT * FROM files WHERE path = ?", (normalized,)
        ).fetchone()
        if exact is not None:
            return exact
        rows = connection.execute(
            "SELECT * FROM files WHERE path LIKE ? ORDER BY length(path), path LIMIT 20",
            (f"%{normalized}%",),
        ).fetchall()
        if len(rows) == 1:
            return rows[0]
        if not rows:
            raise AtlasError(f"file not found: {query}")
        raise AtlasError(
            f"ambiguous file {query!r}: {', '.join(row['path'] for row in rows[:8])}"
        )

    @staticmethod
    def _existing_symbol_keys(
        connection: sqlite3.Connection, keys: set[str]
    ) -> set[str]:
        existing: set[str] = set()
        values = tuple(keys)
        for offset in range(0, len(values), 500):
            batch = values[offset : offset + 500]
            if not batch:
                continue
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"SELECT key FROM symbols WHERE key IN ({placeholders})", batch
            )
            existing.update(row["key"] for row in rows)
        return existing

    @staticmethod
    def _add_import_neighbors(
        connection: sqlite3.Connection,
        file_scores: dict[str, float],
        add_file: Callable[[str, float, str, str], None],
    ) -> None:
        selected = sorted(file_scores, key=lambda path: (-file_scores[path], path))[
            :_MAX_SELECTED_FILES
        ]
        if not selected:
            return
        placeholders = ",".join("?" for _ in selected)
        rows = connection.execute(
            f"""
            SELECT source.path AS source_path, target.path AS target_path
            FROM edges e
            JOIN files source ON source.id = e.source_file_id
            LEFT JOIN files target ON target.id = e.target_file_id
            WHERE e.kind = 'imports'
              AND (source.path IN ({placeholders}) OR target.path IN ({placeholders}))
            ORDER BY e.confidence DESC, source.path LIMIT 1000
            """,
            (*selected, *selected),
        ).fetchall()
        for row in rows:
            add_file(
                row["source_path"], 58, "imports a selected module", "module dependency"
            )
            if row["target_path"]:
                add_file(
                    row["target_path"],
                    58,
                    "imported by a selected module",
                    "module dependency",
                )

    @classmethod
    def _spans_from_selection(
        cls,
        connection: sqlite3.Connection,
        symbol_scores: dict[str, float],
        symbol_reasons: dict[str, set[str]],
        symbol_relations: dict[str, set[str]],
        file_scores: dict[str, float],
        file_reasons: dict[str, set[str]],
        file_relations: dict[str, set[str]],
    ) -> list[_Span]:
        spans: list[_Span] = []
        keys = tuple(symbol_scores)
        for offset in range(0, len(keys), 500):
            batch = keys[offset : offset + 500]
            if not batch:
                continue
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"""
                SELECT s.*, f.path AS file_path, f.content_hash
                FROM symbols s JOIN files f ON f.id = s.file_id
                WHERE s.key IN ({placeholders})
                """,
                batch,
            )
            for row in rows:
                spans.append(
                    _Span(
                        path=row["file_path"],
                        start_line=max(1, int(row["start_line"]) - 3),
                        end_line=int(row["end_line"]) + 3,
                        score=symbol_scores[row["key"]],
                        reasons=set(symbol_reasons[row["key"]]),
                        relations=set(symbol_relations[row["key"]]),
                        symbol_keys={row["key"]},
                        content_hash=row["content_hash"],
                    )
                )

        for path in sorted(file_scores, key=lambda item: (-file_scores[item], item))[
            :_MAX_SELECTED_FILES
        ]:
            row = connection.execute(
                "SELECT content_hash FROM files WHERE path = ?",
                (path,),
            ).fetchone()
            if row is None:
                continue
            has_symbol_span = any(span.path == path for span in spans)
            if not has_symbol_span or file_scores[path] >= 120:
                suffix = Path(path).suffix.lower()
                end_line = 160 if suffix in {".json", ".toml", ".yaml", ".yml"} else 60
                spans.append(
                    _Span(
                        path=path,
                        start_line=1,
                        end_line=end_line,
                        score=file_scores[path],
                        reasons=set(file_reasons[path]),
                        relations=set(file_relations[path]),
                        symbol_keys=set(),
                        content_hash=row["content_hash"],
                    )
                )
        return cls._merge_and_split_spans(spans)

    @staticmethod
    def _merge_and_split_spans(spans: list[_Span]) -> list[_Span]:
        merged: list[_Span] = []
        for span in sorted(
            spans, key=lambda item: (item.path, item.start_line, item.end_line)
        ):
            if (
                merged
                and merged[-1].path == span.path
                and span.start_line <= merged[-1].end_line + 6
            ):
                previous = merged[-1]
                previous.end_line = max(previous.end_line, span.end_line)
                previous.score = max(previous.score, span.score)
                previous.reasons.update(span.reasons)
                previous.relations.update(span.relations)
                previous.symbol_keys.update(span.symbol_keys)
                continue
            merged.append(span)
        bounded: list[_Span] = []
        for span in merged:
            line_count = span.end_line - span.start_line + 1
            if line_count <= _MAX_SNIPPET_LINES:
                bounded.append(span)
                continue
            bounded.append(
                _Span(
                    path=span.path,
                    start_line=span.start_line,
                    end_line=span.start_line + 119,
                    score=span.score,
                    reasons=set(span.reasons) | {"opening portion of a long symbol"},
                    relations=set(span.relations),
                    symbol_keys=set(span.symbol_keys),
                    content_hash=span.content_hash,
                )
            )
            bounded.append(
                _Span(
                    path=span.path,
                    start_line=max(span.start_line + 120, span.end_line - 39),
                    end_line=span.end_line,
                    score=span.score - 5,
                    reasons=set(span.reasons) | {"closing portion of a long symbol"},
                    relations=set(span.relations),
                    symbol_keys=set(span.symbol_keys),
                    content_hash=span.content_hash,
                )
            )
        return sorted(
            bounded, key=lambda item: (-item.score, item.path, item.start_line)
        )

    async def _materialize(
        self,
        *,
        repo_root: Path,
        task: str,
        contract: str | dict[str, object] | list[object] | None,
        roots: tuple[str, ...],
        spans: list[_Span],
        unrelated_files: int,
        token_budget: int,
        head_commit: str,
        graph_indexed_at: str,
    ) -> ContextCapsule:
        base_tokens = (
            self._estimate_tokens(task)
            + self._estimate_tokens(json.dumps(contract, sort_keys=True))
            + 180
        )
        if base_tokens >= token_budget:
            raise CapsuleError(
                "token budget is too small for the task and acceptance contract"
            )
        remaining = token_budget - base_tokens
        loaded: dict[str, tuple[list[str], str, str]] = {}
        included: list[ContextItem] = []
        excluded: list[ExcludedContext] = []
        for span in spans:
            if span.path not in loaded:
                path = self._safe_source_path(repo_root, span.path)
                try:
                    source, actual_hash, updated_at = await asyncio.to_thread(
                        self._read_source, path
                    )
                except (OSError, UnicodeDecodeError):
                    excluded.append(
                        ExcludedContext(span.path, "source is unreadable UTF-8")
                    )
                    continue
                if actual_hash != span.content_hash:
                    raise AtlasStale(
                        f"indexed source changed before capsule compilation: {span.path}"
                    )
                loaded[span.path] = (
                    source.splitlines(keepends=True),
                    actual_hash,
                    updated_at,
                )
            lines, actual_hash, updated_at = loaded[span.path]
            if not lines:
                continue
            start_line = min(max(1, span.start_line), len(lines))
            end_line = min(max(start_line, span.end_line), len(lines))
            content = "".join(lines[start_line - 1 : end_line])
            reason = "; ".join(sorted(span.reasons))
            relation = "; ".join(sorted(span.relations))
            estimated = (
                self._estimate_tokens(content)
                + self._estimate_tokens(reason + relation)
                + 72
            )
            source_label = f"{span.path}:{start_line}-{end_line}"
            if estimated > remaining:
                excluded.append(ExcludedContext(source_label, "capsule token budget"))
                continue
            included.append(
                ContextItem(
                    source=span.path,
                    start_line=start_line,
                    end_line=end_line,
                    symbol_keys=tuple(sorted(span.symbol_keys)),
                    content_hash=actual_hash,
                    excerpt_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    updated_at=updated_at,
                    reason=reason,
                    relation=relation,
                    content=content,
                    estimated_tokens=estimated,
                )
            )
            remaining -= estimated

        if len(excluded) > _MAX_EXCLUSIONS:
            omitted = len(excluded) - (_MAX_EXCLUSIONS - 1)
            excluded = excluded[: _MAX_EXCLUSIONS - 1] + [
                ExcludedContext(
                    f"{omitted} additional excerpts", "capsule token budget"
                )
            ]
        built_at = datetime.now(timezone.utc).isoformat()
        capsule = ContextCapsule(
            schema_version=_CAPSULE_SCHEMA_VERSION,
            id="capsule-000000000000000000000000",
            repo_root=repo_root,
            task=task,
            task_contract=contract,
            roots=roots,
            items=tuple(included),
            excluded=tuple(excluded),
            unrelated_files=unrelated_files,
            token_budget=token_budget,
            estimated_tokens=0,
            built_at=built_at,
            head_commit=head_commit,
            graph_indexed_at=graph_indexed_at,
        )
        for _ in range(3):
            estimate = self._estimate_tokens(capsule.render())
            capsule = replace(capsule, estimated_tokens=estimate)
        while capsule.estimated_tokens > token_budget and capsule.items:
            removed = capsule.items[-1]
            new_exclusions = (
                *capsule.excluded,
                ExcludedContext(
                    f"{removed.source}:{removed.start_line}-{removed.end_line}",
                    "capsule token budget",
                ),
            )
            capsule = replace(
                capsule,
                items=capsule.items[:-1],
                excluded=new_exclusions[:_MAX_EXCLUSIONS],
            )
            for _ in range(3):
                capsule = replace(
                    capsule, estimated_tokens=self._estimate_tokens(capsule.render())
                )
        if capsule.estimated_tokens > token_budget:
            raise CapsuleError("token budget is too small for capsule metadata")
        capsule_id = self._capsule_id(capsule)
        capsule = replace(capsule, id=capsule_id)
        for _ in range(3):
            capsule = replace(
                capsule, estimated_tokens=self._estimate_tokens(capsule.render())
            )
        return capsule

    @staticmethod
    def _read_source(path: Path) -> tuple[str, str, str]:
        raw = path.read_bytes()
        content = raw.decode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        updated_at = datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc
        ).isoformat()
        return content, digest, updated_at

    @staticmethod
    def _safe_source_path(repo_root: Path, source: str) -> Path:
        candidate = repo_root / source
        if candidate.is_symlink():
            raise CapsuleError(f"capsule source is a symlink: {source}")
        path = candidate.resolve()
        if not path.is_relative_to(repo_root.resolve()):
            raise CapsuleError(f"capsule source escapes repository: {source}")
        return path

    @staticmethod
    def _task_terms(value: str) -> tuple[str, ...]:
        terms: list[str] = []
        seen: set[str] = set()
        for match in _TERM_PATTERN.finditer(value):
            term = match.group(0).strip(".:-")
            lowered = term.lower()
            if len(term) < 4 or lowered in _STOP_WORDS or lowered in seen:
                continue
            if term.isdigit():
                continue
            seen.add(lowered)
            terms.append(term)
            if len(terms) == 24:
                break
        return tuple(terms)

    @staticmethod
    def _like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _estimate_tokens(value: str) -> int:
        return max(1, (len(value.encode("utf-8")) + 3) // 4)

    @staticmethod
    def _capsule_id(capsule: ContextCapsule) -> str:
        payload = asdict(capsule)
        payload["id"] = None
        payload["repo_root"] = str(capsule.repo_root)
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"capsule-{digest[:24]}"

    def _capsule_path(self, common_dir: Path, capsule_id: str) -> Path:
        return (
            self.atlas.state_dir
            / "capsules"
            / _repo_key(common_dir)
            / f"{capsule_id}.json"
        )

    def _persist(self, common_dir: Path, capsule: ContextCapsule) -> None:
        path = self._capsule_path(common_dir, capsule.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(capsule)
        payload["repo_root"] = str(capsule.repo_root)
        serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{capsule.id}-", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    def _load_sync(
        self, common_dir: Path, repo_root: Path, capsule_id: str
    ) -> ContextCapsule:
        path = self._capsule_path(common_dir, capsule_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise CapsuleError(f"context capsule not found: {capsule_id}") from error
        except json.JSONDecodeError as error:
            raise CapsuleError(
                f"context capsule is invalid JSON: {capsule_id}"
            ) from error
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != _CAPSULE_SCHEMA_VERSION
        ):
            raise CapsuleError(f"unsupported context capsule schema: {capsule_id}")
        if payload.get("id") != capsule_id:
            raise CapsuleError(f"context capsule id mismatch: {capsule_id}")
        try:
            stored_root = Path(payload["repo_root"]).resolve()
            if stored_root != repo_root.resolve():
                raise CapsuleError(
                    f"context capsule belongs to another repository: {capsule_id}"
                )
            items: list[ContextItem] = []
            for raw_item in payload["items"]:
                item_payload = dict(raw_item)
                item_payload["symbol_keys"] = tuple(item_payload["symbol_keys"])
                item = ContextItem(**item_payload)
                if (
                    hashlib.sha256(item.content.encode("utf-8")).hexdigest()
                    != item.excerpt_hash
                ):
                    raise CapsuleError(
                        f"context capsule excerpt hash mismatch: {capsule_id}"
                    )
                if not re.fullmatch(r"[0-9a-f]{64}", item.content_hash):
                    raise CapsuleError(
                        f"context capsule source hash is invalid: {capsule_id}"
                    )
                items.append(item)
            exclusions = tuple(ExcludedContext(**item) for item in payload["excluded"])
            capsule = ContextCapsule(
                schema_version=payload["schema_version"],
                id=payload["id"],
                repo_root=stored_root,
                task=payload["task"],
                task_contract=payload["task_contract"],
                roots=tuple(payload["roots"]),
                items=tuple(items),
                excluded=exclusions,
                unrelated_files=payload["unrelated_files"],
                token_budget=payload["token_budget"],
                estimated_tokens=payload["estimated_tokens"],
                built_at=payload["built_at"],
                head_commit=payload["head_commit"],
                graph_indexed_at=payload["graph_indexed_at"],
            )
        except CapsuleError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise CapsuleError(
                f"context capsule has an invalid shape: {capsule_id}"
            ) from error
        if self._capsule_id(capsule) != capsule_id:
            raise CapsuleError(f"context capsule digest mismatch: {capsule_id}")
        return capsule
