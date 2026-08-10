from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass


@dataclass(slots=True)
class ParsedPython:
    symbols: list[dict[str, object]]
    edges: list[dict[str, object]]
    parse_status: str
    diagnostics: int


def _signature_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_python(path: str, source: str) -> ParsedPython:
    try:
        tree = ast.parse(source, filename=path, type_comments=True)
    except SyntaxError:
        return ParsedPython(symbols=[], edges=[], parse_status="error", diagnostics=1)

    symbols: list[dict[str, object]] = []
    edges: list[dict[str, object]] = []
    definitions: dict[str, str] = {}
    node_keys: dict[int, str] = {}

    def collect(
        node: ast.AST,
        parents: tuple[str, ...] = (),
        parent_kind: str | None = None,
        parent_exported: bool = False,
    ) -> None:
        name: str | None = None
        kind: str | None = None
        signature = ""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            kind = "method" if parent_kind == "class" else "function"
            prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
            signature = f"{prefix}def {name}{ast.unparse(node.args)}"
        elif isinstance(node, ast.ClassDef):
            name = node.name
            kind = "class"
            bases = ", ".join(ast.unparse(base) for base in node.bases)
            signature = f"class {name}({bases})" if bases else f"class {name}"
        if name and kind:
            exported = not name.startswith("_") and (
                not parents or (parent_kind == "class" and parent_exported)
            )
            qualified_name = ".".join((*parents, name))
            key = f"{path}:{node.lineno}:{kind}:{qualified_name}"
            node_keys[id(node)] = key
            definitions.setdefault(name, key)
            definitions[qualified_name] = key
            symbols.append(
                {
                    "key": key,
                    "file": path,
                    "kind": kind,
                    "name": name,
                    "qualified_name": qualified_name,
                    "start_line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno),
                    "exported": exported,
                    "signature_hash": _signature_hash(signature),
                }
            )
            next_parents = (*parents, name)
            next_parent_kind = kind
            next_parent_exported = exported
        else:
            next_parents = parents
            next_parent_kind = parent_kind
            next_parent_exported = parent_exported
        for child in ast.iter_child_nodes(node):
            collect(child, next_parents, next_parent_kind, next_parent_exported)

    collect(tree)

    class EdgeVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []

        def source_key(self) -> str | None:
            return self.scope[-1] if self.scope else None

        def edge(
            self,
            node: ast.AST,
            kind: str,
            target_name: str,
            *,
            target_key: str | None = None,
            target_file: str | None = None,
            confidence: float = 1,
        ) -> None:
            edges.append(
                {
                    "source_key": self.source_key(),
                    "source_file": path,
                    "target_key": target_key,
                    "target_name": target_name,
                    "target_file": target_file,
                    "kind": kind,
                    "line": getattr(node, "lineno", 1),
                    "confidence": confidence,
                }
            )

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            key = node_keys[id(node)]
            self.scope.append(key)
            self.generic_visit(node)
            self.scope.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            key = node_keys[id(node)]
            self.scope.append(key)
            self.generic_visit(node)
            self.scope.pop()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            for base in node.bases:
                name = ast.unparse(base)
                self.edge(
                    base,
                    "extends",
                    name,
                    target_key=definitions.get(name)
                    or definitions.get(name.rsplit(".", 1)[-1]),
                    confidence=1 if name in definitions else 0.6,
                )
            key = node_keys[id(node)]
            self.scope.append(key)
            self.generic_visit(node)
            self.scope.pop()

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                self.edge(node, "imports", alias.name, confidence=0.8)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            prefix = "." * node.level
            if node.module:
                self.edge(node, "imports", f"{prefix}{node.module}", confidence=0.8)
                return
            for alias in node.names:
                self.edge(node, "imports", f"{prefix}{alias.name}", confidence=0.8)

        def visit_Call(self, node: ast.Call) -> None:
            name = ast.unparse(node.func)
            short_name = name.rsplit(".", 1)[-1]
            target_key = definitions.get(name) or definitions.get(short_name)
            self.edge(
                node,
                "calls",
                name,
                target_key=target_key,
                confidence=1 if target_key else 0.55,
            )
            self.generic_visit(node)

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Load) and (
                target_key := definitions.get(node.id)
            ):
                self.edge(node, "references", node.id, target_key=target_key)

    EdgeVisitor().visit(tree)
    deduplicated = list(
        {
            (
                edge["source_key"],
                edge["source_file"],
                edge["target_key"],
                edge["target_name"],
                edge["target_file"],
                edge["kind"],
                edge["line"],
            ): edge
            for edge in edges
        }.values()
    )
    return ParsedPython(
        symbols=symbols,
        edges=deduplicated,
        parse_status="ok",
        diagnostics=0,
    )
