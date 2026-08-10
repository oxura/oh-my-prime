from __future__ import annotations

import hashlib
import re


_DECLARATION = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:(class|interface|enum|type|function)\s+([A-Za-z_$][\w$]*)|"
    r"(?:const|let|var)\s+([A-Za-z_$][\w$]*))",
    re.MULTILINE,
)
_IMPORT = re.compile(r"(?:import|export)\s+(?:[^;]*?\s+from\s+)?[\"']([^\"']+)[\"']")
_CALL = re.compile(r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(")
_CALL_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return"}


def parse_typescript_fallback(path: str, source: str) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    symbols: list[dict[str, object]] = []
    edges: list[dict[str, object]] = []
    definitions: dict[str, str] = {}
    for match in _DECLARATION.finditer(source):
        kind = match.group(1) or "variable"
        name = match.group(2) or match.group(3)
        if not name:
            continue
        line = source.count("\n", 0, match.start()) + 1
        key = f"{path}:{match.start()}:{kind}:{name}"
        definitions[name] = key
        symbols.append(
            {
                "key": key,
                "file": path,
                "kind": kind,
                "name": name,
                "qualified_name": name,
                "start_line": line,
                "end_line": line,
                "exported": "export" in match.group(0),
                "signature_hash": hashlib.sha256(match.group(0).strip().encode("utf-8")).hexdigest(),
            }
        )
    for match in _IMPORT.finditer(source):
        edges.append(
            {
                "source_key": None,
                "source_file": path,
                "target_key": None,
                "target_name": match.group(1),
                "target_file": None,
                "kind": "imports",
                "line": source.count("\n", 0, match.start()) + 1,
                "confidence": 0.45,
            }
        )
    for match in _CALL.finditer(source):
        name = match.group(1)
        if name in _CALL_KEYWORDS:
            continue
        short_name = name.rsplit(".", 1)[-1]
        edges.append(
            {
                "source_key": None,
                "source_file": path,
                "target_key": definitions.get(name) or definitions.get(short_name),
                "target_name": name,
                "target_file": path if short_name in definitions else None,
                "kind": "calls",
                "line": source.count("\n", 0, match.start()) + 1,
                "confidence": 0.35,
            }
        )
    return symbols, edges
