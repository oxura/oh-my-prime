from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from ._fallback_extractor import parse_typescript_fallback
from ._models import (
    AtlasError,
    AtlasNotBuilt,
    BuildReport,
    Edge,
    FileNode,
    GraphStats,
    Language,
    Symbol,
)
from ._python_extractor import parse_python


_SCHEMA_VERSION = 1
_MAX_PARSE_BYTES = 2 * 1024 * 1024
_MAX_EXTRACTOR_OUTPUT_BYTES = 256 * 1024 * 1024
_TYPESCRIPT_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs"}


_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    language TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    size INTEGER NOT NULL,
    parse_status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    exported INTEGER NOT NULL,
    signature_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY,
    source_symbol_key TEXT,
    source_file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    target_symbol_key TEXT,
    target_name TEXT NOT NULL,
    target_file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    line INTEGER NOT NULL,
    confidence REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS symbols_name_idx ON symbols(name);
CREATE INDEX IF NOT EXISTS symbols_qualified_name_idx ON symbols(qualified_name);
CREATE INDEX IF NOT EXISTS symbols_file_idx ON symbols(file_id);
CREATE INDEX IF NOT EXISTS edges_source_symbol_idx ON edges(source_symbol_key);
CREATE INDEX IF NOT EXISTS edges_target_symbol_idx ON edges(target_symbol_key);
CREATE INDEX IF NOT EXISTS edges_kind_idx ON edges(kind);
CREATE INDEX IF NOT EXISTS edges_source_file_idx ON edges(source_file_id);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state_dir() -> Path:
    override = os.environ.get("OH_MY_PRIME_ATLAS_STATE")
    if override:
        return Path(override).expanduser()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    root = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    return root / "oh-my-prime" / "atlas"


def _repo_key(common_dir: Path) -> str:
    canonical = os.path.normcase(str(common_dir.resolve()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def _language(path: str) -> Language:
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in {".ts", ".mts", ".cts"}:
        return "typescript"
    if suffix == ".tsx":
        return "tsx"
    if suffix in {".js", ".mjs", ".cjs"}:
        return "javascript"
    if suffix == ".jsx":
        return "jsx"
    if suffix == ".json":
        return "json"
    return "other"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _connect(path: Path, *, require_existing: bool = False) -> sqlite3.Connection:
    if require_existing and not path.is_file():
        raise AtlasNotBuilt(f"Code Atlas has not been built: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(_SCHEMA)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in {0, _SCHEMA_VERSION}:
        connection.close()
        raise AtlasError(f"unsupported Code Atlas schema version: {version}")
    if version == 0:
        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        connection.commit()
    return connection


class CodeAtlas:
    """Repository semantic graph backed by an atomic SQLite index."""

    def __init__(self, state_dir: str | os.PathLike[str] | None = None) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve() if state_dir else _default_state_dir().resolve()

    async def build(self, repo: str | os.PathLike[str] = ".") -> BuildReport:
        repo_root, common_dir = await self._repo_paths(repo)
        database_path = self._database_path(common_dir)
        head_commit, tracked = await asyncio.gather(
            self._git_text(repo_root, "rev-parse", "HEAD"),
            self._tracked_files(repo_root),
        )
        existing_hashes = await asyncio.to_thread(self._existing_hashes, database_path)
        file_records: list[dict[str, object]] = []
        sources: dict[str, str] = {}
        for relative in tracked:
            absolute = repo_root / relative
            if absolute.is_symlink() or not absolute.is_file():
                continue
            try:
                size = absolute.stat().st_size
                content_hash = await asyncio.to_thread(_hash_file, absolute)
            except OSError:
                continue
            language = _language(relative)
            file_records.append(
                {
                    "path": relative,
                    "language": language,
                    "content_hash": content_hash,
                    "size": size,
                    "parse_status": "not_parsed",
                }
            )
            if language in {"python", "typescript", "tsx", "javascript", "jsx"} and size <= _MAX_PARSE_BYTES:
                try:
                    sources[relative] = await asyncio.to_thread(absolute.read_text, encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    file_records[-1]["parse_status"] = "encoding_error"

        current_hashes = {str(record["path"]): str(record["content_hash"]) for record in file_records}
        changed_files = sum(existing_hashes.get(path) != digest for path, digest in current_hashes.items())
        removed_files = len(set(existing_hashes) - set(current_hashes))
        symbols: list[dict[str, object]] = []
        edges: list[dict[str, object]] = []
        diagnostics = 0
        status_by_file: dict[str, str] = {}

        for relative, source in sources.items():
            if _language(relative) != "python":
                continue
            parsed = await asyncio.to_thread(parse_python, relative, source)
            symbols.extend(parsed.symbols)
            edges.extend(parsed.edges)
            status_by_file[relative] = parsed.parse_status
            diagnostics += parsed.diagnostics

        typescript_sources = {
            relative: source
            for relative, source in sources.items()
            if Path(relative).suffix.lower() in _TYPESCRIPT_SUFFIXES
        }
        if typescript_sources:
            extracted = await self._extract_typescript(repo_root, tuple(typescript_sources))
            if extracted is None:
                for relative, source in typescript_sources.items():
                    fallback_symbols, fallback_edges = parse_typescript_fallback(relative, source)
                    symbols.extend(fallback_symbols)
                    edges.extend(fallback_edges)
                    status_by_file[relative] = "fallback"
                    diagnostics += 1
            else:
                ts_symbols, ts_edges, ts_statuses, ts_diagnostics = extracted
                symbols.extend(ts_symbols)
                edges.extend(ts_edges)
                status_by_file.update(ts_statuses)
                diagnostics += ts_diagnostics

        self._resolve_python_imports(edges, tuple(current_hashes))
        for record in file_records:
            path = str(record["path"])
            if path in status_by_file:
                record["parse_status"] = status_by_file[path]
        built_at = _utc_now()
        await asyncio.to_thread(
            self._replace_graph,
            database_path,
            repo_root,
            common_dir,
            head_commit.strip(),
            built_at,
            file_records,
            symbols,
            edges,
        )
        return BuildReport(
            repo_root=repo_root,
            database_path=database_path,
            head_commit=head_commit.strip(),
            indexed_files=len(file_records),
            changed_files=changed_files,
            removed_files=removed_files,
            symbols=len(symbols),
            edges=len(edges),
            parser_diagnostics=diagnostics,
            built_at=built_at,
        )

    async def stats(self, repo: str | os.PathLike[str] = ".") -> GraphStats:
        _, common_dir = await self._repo_paths(repo)
        return await asyncio.to_thread(self._stats_sync, self._database_path(common_dir))

    async def files(
        self,
        query: str = "",
        *,
        language: str | None = None,
        limit: int = 50,
        repo: str | os.PathLike[str] = ".",
    ) -> list[FileNode]:
        self._validate_limit(limit)
        _, common_dir = await self._repo_paths(repo)
        return await asyncio.to_thread(
            self._files_sync,
            self._database_path(common_dir),
            query,
            language,
            limit,
        )

    async def symbols(
        self,
        query: str = "",
        *,
        kind: str | None = None,
        limit: int = 50,
        repo: str | os.PathLike[str] = ".",
    ) -> list[Symbol]:
        self._validate_limit(limit)
        _, common_dir = await self._repo_paths(repo)
        return await asyncio.to_thread(
            self._symbols_sync,
            self._database_path(common_dir),
            query,
            kind,
            limit,
        )

    async def symbol(
        self,
        query: str,
        *,
        repo: str | os.PathLike[str] = ".",
    ) -> Symbol:
        if not isinstance(query, str) or not query.strip():
            raise AtlasError("symbol query must be a non-empty string")
        matches = await self.symbols(query.strip(), limit=20, repo=repo)
        exact = [
            symbol
            for symbol in matches
            if symbol.qualified_name == query.strip() or symbol.name == query.strip() or symbol.key == query.strip()
        ]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            choices = ", ".join(f"{symbol.qualified_name} ({symbol.file}:{symbol.start_line})" for symbol in exact)
            raise AtlasError(f"ambiguous symbol {query!r}: {choices}")
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise AtlasError(f"symbol not found: {query}")
        choices = ", ".join(f"{symbol.qualified_name} ({symbol.file}:{symbol.start_line})" for symbol in matches[:8])
        raise AtlasError(f"ambiguous symbol {query!r}: {choices}")

    async def references(
        self,
        target: str | Symbol,
        *,
        kinds: Sequence[str] = ("references", "calls", "extends", "implements"),
        limit: int = 200,
        repo: str | os.PathLike[str] = ".",
    ) -> list[Edge]:
        self._validate_limit(limit, maximum=2_000)
        symbol = target if isinstance(target, Symbol) else await self.symbol(target, repo=repo)
        _, common_dir = await self._repo_paths(repo)
        return await asyncio.to_thread(
            self._references_sync,
            self._database_path(common_dir),
            symbol.key,
            tuple(kinds),
            limit,
        )

    async def outgoing(
        self,
        source: str | Symbol,
        *,
        kinds: Sequence[str] = (),
        limit: int = 200,
        repo: str | os.PathLike[str] = ".",
    ) -> list[Edge]:
        self._validate_limit(limit, maximum=2_000)
        symbol = source if isinstance(source, Symbol) else await self.symbol(source, repo=repo)
        _, common_dir = await self._repo_paths(repo)
        return await asyncio.to_thread(
            self._outgoing_sync,
            self._database_path(common_dir),
            symbol.key,
            tuple(kinds),
            limit,
        )

    def _database_path(self, common_dir: Path) -> Path:
        return self.state_dir / f"{_repo_key(common_dir)}.sqlite"

    @staticmethod
    async def _repo_paths(repo: str | os.PathLike[str]) -> tuple[Path, Path]:
        candidate = Path(repo).expanduser().resolve()
        root, common = await asyncio.gather(
            CodeAtlas._git_text(candidate, "rev-parse", "--path-format=absolute", "--show-toplevel"),
            CodeAtlas._git_text(candidate, "rev-parse", "--path-format=absolute", "--git-common-dir"),
        )
        return Path(root.strip()).resolve(), Path(common.strip()).resolve()

    @staticmethod
    async def _git_text(repo: Path, *args: str) -> str:
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
            raise AtlasError(f"git {' '.join(args)} failed: {detail or 'no output'}")
        return stdout.decode("utf-8", errors="surrogateescape")

    @staticmethod
    async def _tracked_files(repo_root: Path) -> list[str]:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo_root),
            "ls-files",
            "-z",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise AtlasError(f"git ls-files failed: {detail or 'no output'}")
        return sorted(
            value.decode("utf-8", errors="surrogateescape")
            for value in stdout.split(b"\0")
            if value
        )

    async def _extract_typescript(
        self,
        repo_root: Path,
        files: tuple[str, ...],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, str], int] | None:
        script = Path(__file__).with_name("_typescript_extractor.mjs")
        with tempfile.TemporaryDirectory(prefix="atlas-ts-") as directory:
            output_path = Path(directory) / "records.jsonl"
            with output_path.open("wb") as output:
                try:
                    process = await asyncio.create_subprocess_exec(
                        "node",
                        str(script),
                        str(repo_root),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=output,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=repo_root,
                    )
                except FileNotFoundError:
                    return None
                _, stderr = await process.communicate("\0".join(files).encode("utf-8", errors="surrogateescape"))
            if process.returncode != 0:
                if process.returncode == 42:
                    return None
                detail = stderr.decode("utf-8", errors="replace").strip()
                raise AtlasError(f"TypeScript semantic extractor failed: {detail or 'no output'}")
            if output_path.stat().st_size > _MAX_EXTRACTOR_OUTPUT_BYTES:
                raise AtlasError("TypeScript semantic extractor output exceeded 256 MiB")
            symbols: list[dict[str, object]] = []
            edges: list[dict[str, object]] = []
            statuses: dict[str, str] = {}
            diagnostics = 0
            with output_path.open("r", encoding="utf-8") as records:
                for line in records:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise AtlasError("TypeScript semantic extractor returned invalid JSON") from error
                    if not isinstance(record, dict):
                        raise AtlasError("TypeScript semantic extractor returned an invalid record")
                    record_type = record.get("type")
                    if record_type == "symbol":
                        symbols.append(record)
                    elif record_type == "edge":
                        edges.append(record)
                    elif record_type == "diagnostic":
                        file = record.get("file")
                        status = record.get("parse_status")
                        errors = record.get("errors", [])
                        if isinstance(file, str) and isinstance(status, str) and isinstance(errors, list):
                            statuses[file] = status
                            diagnostics += len(errors)
            return symbols, edges, statuses, diagnostics

    @staticmethod
    def _resolve_python_imports(edges: list[dict[str, object]], files: tuple[str, ...]) -> None:
        python_files = [path for path in files if path.endswith(".py")]
        for edge in edges:
            if edge.get("kind") != "imports" or edge.get("target_file"):
                continue
            target = edge.get("target_name")
            if not isinstance(target, str):
                continue
            suffix = target.lstrip(".").replace(".", "/")
            matches = [
                path
                for path in python_files
                if path.endswith(f"/{suffix}.py")
                or path == f"{suffix}.py"
                or path.endswith(f"/{suffix}/__init__.py")
                or path == f"{suffix}/__init__.py"
            ]
            if len(matches) == 1:
                edge["target_file"] = matches[0]
                edge["confidence"] = 1.0

    @staticmethod
    def _existing_hashes(database_path: Path) -> dict[str, str]:
        if not database_path.is_file():
            return {}
        connection = _connect(database_path, require_existing=True)
        try:
            return {row["path"]: row["content_hash"] for row in connection.execute("SELECT path, content_hash FROM files")}
        finally:
            connection.close()

    @staticmethod
    def _replace_graph(
        database_path: Path,
        repo_root: Path,
        common_dir: Path,
        head_commit: str,
        built_at: str,
        files: list[dict[str, object]],
        symbols: list[dict[str, object]],
        edges: list[dict[str, object]],
    ) -> None:
        connection = _connect(database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM edges")
            connection.execute("DELETE FROM symbols")
            connection.execute("DELETE FROM files")
            connection.executemany(
                "INSERT INTO files(path, language, content_hash, size, parse_status) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        record["path"],
                        record["language"],
                        record["content_hash"],
                        record["size"],
                        record["parse_status"],
                    )
                    for record in files
                ],
            )
            file_ids = {row["path"]: row["id"] for row in connection.execute("SELECT id, path FROM files")}
            unique_symbols = {str(record["key"]): record for record in symbols if record.get("file") in file_ids}
            connection.executemany(
                """
                INSERT INTO symbols(
                    key, file_id, kind, name, qualified_name, start_line, end_line, exported, signature_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        key,
                        file_ids[str(record["file"])],
                        record["kind"],
                        record["name"],
                        record["qualified_name"],
                        record["start_line"],
                        record["end_line"],
                        int(bool(record["exported"])),
                        record.get("signature_hash")
                        or hashlib.sha256(str(record.get("signature", "")).encode("utf-8")).hexdigest(),
                    )
                    for key, record in unique_symbols.items()
                ],
            )
            edge_rows: list[tuple[object, ...]] = []
            seen_edges: set[tuple[object, ...]] = set()
            for record in edges:
                source_file = record.get("source_file")
                if source_file not in file_ids:
                    continue
                target_file = record.get("target_file")
                row = (
                    record.get("source_key"),
                    file_ids[str(source_file)],
                    record.get("target_key"),
                    record.get("target_name"),
                    file_ids.get(str(target_file)) if target_file else None,
                    record.get("kind"),
                    record.get("line", 1),
                    record.get("confidence", 0.5),
                )
                if row in seen_edges:
                    continue
                seen_edges.add(row)
                edge_rows.append(row)
            connection.executemany(
                """
                INSERT INTO edges(
                    source_symbol_key, source_file_id, target_symbol_key, target_name,
                    target_file_id, kind, line, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                edge_rows,
            )
            metadata = {
                "repo_root": str(repo_root),
                "common_dir": str(common_dir),
                "head_commit": head_commit,
                "indexed_at": built_at,
                "schema_version": str(_SCHEMA_VERSION),
            }
            connection.executemany(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                metadata.items(),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _stats_sync(database_path: Path) -> GraphStats:
        connection = _connect(database_path, require_existing=True)
        try:
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            languages = {
                row["language"]: row["count"]
                for row in connection.execute("SELECT language, COUNT(*) AS count FROM files GROUP BY language")
            }
            return GraphStats(
                files=connection.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                symbols=connection.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
                edges=connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
                languages=languages,
                head_commit=metadata.get("head_commit", ""),
                indexed_at=metadata.get("indexed_at", ""),
            )
        finally:
            connection.close()

    @staticmethod
    def _files_sync(
        database_path: Path,
        query: str,
        language: str | None,
        limit: int,
    ) -> list[FileNode]:
        connection = _connect(database_path, require_existing=True)
        try:
            clauses = ["path LIKE ?"]
            parameters: list[object] = [f"%{query}%"]
            if language:
                clauses.append("language = ?")
                parameters.append(language)
            parameters.append(limit)
            rows = connection.execute(
                f"SELECT * FROM files WHERE {' AND '.join(clauses)} ORDER BY path LIMIT ?",
                parameters,
            )
            return [
                FileNode(
                    id=row["id"],
                    path=row["path"],
                    language=row["language"],
                    content_hash=row["content_hash"],
                    size=row["size"],
                    parse_status=row["parse_status"],
                )
                for row in rows
            ]
        finally:
            connection.close()

    @staticmethod
    def _symbols_sync(
        database_path: Path,
        query: str,
        kind: str | None,
        limit: int,
    ) -> list[Symbol]:
        connection = _connect(database_path, require_existing=True)
        try:
            clauses = ["(s.name LIKE ? OR s.qualified_name LIKE ? OR s.key = ?)"]
            parameters: list[object] = [f"%{query}%", f"%{query}%", query]
            if kind:
                clauses.append("s.kind = ?")
                parameters.append(kind)
            parameters.extend([query, query, query, limit])
            rows = connection.execute(
                f"""
                SELECT s.*, f.path AS file_path
                FROM symbols s JOIN files f ON f.id = s.file_id
                WHERE {' AND '.join(clauses)}
                ORDER BY
                    CASE WHEN s.key = ? THEN 0 WHEN s.qualified_name = ? THEN 1 WHEN s.name = ? THEN 2 ELSE 3 END,
                    s.qualified_name, f.path, s.start_line
                LIMIT ?
                """,
                parameters,
            )
            return [CodeAtlas._symbol_row(row) for row in rows]
        finally:
            connection.close()

    @staticmethod
    def _references_sync(
        database_path: Path,
        target_key: str,
        kinds: tuple[str, ...],
        limit: int,
    ) -> list[Edge]:
        connection = _connect(database_path, require_existing=True)
        try:
            kind_clause = ""
            parameters: list[object] = [target_key]
            if kinds:
                kind_clause = f" AND e.kind IN ({','.join('?' for _ in kinds)})"
                parameters.extend(kinds)
            parameters.append(limit)
            rows = connection.execute(
                f"""
                SELECT e.*, source.path AS source_path, target.path AS target_path
                FROM edges e
                JOIN files source ON source.id = e.source_file_id
                LEFT JOIN files target ON target.id = e.target_file_id
                WHERE e.target_symbol_key = ? {kind_clause}
                ORDER BY source.path, e.line LIMIT ?
                """,
                parameters,
            )
            return [CodeAtlas._edge_row(row) for row in rows]
        finally:
            connection.close()

    @staticmethod
    def _outgoing_sync(
        database_path: Path,
        source_key: str,
        kinds: tuple[str, ...],
        limit: int,
    ) -> list[Edge]:
        connection = _connect(database_path, require_existing=True)
        try:
            kind_clause = ""
            parameters: list[object] = [source_key]
            if kinds:
                kind_clause = f" AND e.kind IN ({','.join('?' for _ in kinds)})"
                parameters.extend(kinds)
            parameters.append(limit)
            rows = connection.execute(
                f"""
                SELECT e.*, source.path AS source_path, target.path AS target_path
                FROM edges e
                JOIN files source ON source.id = e.source_file_id
                LEFT JOIN files target ON target.id = e.target_file_id
                WHERE e.source_symbol_key = ? {kind_clause}
                ORDER BY source.path, e.line LIMIT ?
                """,
                parameters,
            )
            return [CodeAtlas._edge_row(row) for row in rows]
        finally:
            connection.close()

    @staticmethod
    def _symbol_row(row: sqlite3.Row) -> Symbol:
        return Symbol(
            id=row["id"],
            key=row["key"],
            file=row["file_path"],
            kind=row["kind"],
            name=row["name"],
            qualified_name=row["qualified_name"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            exported=bool(row["exported"]),
            signature_hash=row["signature_hash"],
        )

    @staticmethod
    def _edge_row(row: sqlite3.Row) -> Edge:
        return Edge(
            id=row["id"],
            source_symbol_key=row["source_symbol_key"],
            source_file=row["source_path"],
            target_symbol_key=row["target_symbol_key"],
            target_name=row["target_name"],
            target_file=row["target_path"],
            kind=row["kind"],
            line=row["line"],
            confidence=row["confidence"],
        )

    @staticmethod
    def _validate_limit(limit: int, *, maximum: int = 500) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > maximum:
            raise AtlasError(f"limit must be an integer from 1 to {maximum}")
