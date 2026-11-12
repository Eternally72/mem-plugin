"""Enforce import boundaries rather than relying on directory names alone."""

import ast
from importlib.util import resolve_name
from pathlib import Path


def test_dependency_directions():
    source = Path(__file__).resolve().parents[2] / "src"
    root = source / "mem_plugin"
    allowed = {
        "engine": {"engine", "observability"},
        "interfaces": {"interfaces", "engine"},
        "infrastructure": {"infrastructure", "engine", "observability"},
        "observability": {"observability"},
        "config": {"config"},
        "workers": {"workers", "engine", "observability"},
        "scheduler": {"scheduler", "engine", "observability"},
    }
    external_core_dependencies = {
        "fastmcp",
        "pydantic",
        "starlette",
        "uvicorn",
        "dotenv",
        "sqlite3",
        "sqlalchemy",
        "psycopg",
        "psycopg2",
        "redis",
    }
    violations = []
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        layer = relative.parts[0]
        package = ".".join(path.parent.relative_to(source).parts)
        for node in ast.walk(ast.parse(path.read_text())):
            imports = []
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    module = resolve_name("." * node.level + module, package)
                imports = [f"{module}.{alias.name}" for alias in node.names]
            for name in imports:
                parts = name.split(".")
                if layer in {"engine", "observability"} and parts[0] in external_core_dependencies:
                    violations.append(f"{relative}:{node.lineno}: engine -> {name}")
                if len(parts) < 2 or parts[0] != "mem_plugin":
                    continue
                target = parts[1]
                if layer in allowed and target not in allowed[layer]:
                    violations.append(f"{relative}:{node.lineno}: {layer} -> {name}")
                if (
                    layer == "interfaces"
                    and target == "engine"
                    and len(parts) >= 3
                    and parts[2] not in {"api", "models", "errors"}
                ):
                    violations.append(
                        f"{relative}:{node.lineno}: interface -> engine internals: {name}"
                    )
                if (
                    layer == "infrastructure"
                    and target == "engine"
                    and len(parts) >= 3
                    and parts[2] not in {"ports", "models", "errors"}
                ):
                    violations.append(
                        f"{relative}:{node.lineno}: storage -> engine internals: {name}"
                    )
    assert not violations, "\n".join(violations)


def test_no_module_import_cycles():
    root = Path(__file__).resolve().parents[2] / "src"
    modules = {}
    for path in (root / "mem_plugin").rglob("*.py"):
        parts = path.relative_to(root).with_suffix("").parts
        name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        modules[name] = path
    graph = {}
    for name, path in modules.items():
        imports = set()
        package = ".".join(path.parent.relative_to(root).parts)
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = (
                    resolve_name("." * node.level + (node.module or ""), package)
                    if node.level
                    else node.module or ""
                )
                targets = [base, *(f"{base}.{alias.name}" for alias in node.names)]
            else:
                continue
            imports.update(target for target in targets if target in modules and target != name)
        graph[name] = imports

    finished = set()

    def visit(name, chain):
        assert name not in chain, " -> ".join((*chain, name))
        if name in finished:
            return
        for dependency in graph[name]:
            visit(dependency, (*chain, name))
        finished.add(name)

    for name in modules:
        visit(name, ())


def test_no_embedded_database_backend_or_import():
    root = Path(__file__).resolve().parents[2] / "src"
    for path in root.rglob("*.py"):
        assert "sqlite" not in path.name.lower()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                assert all(alias.name != "sqlite3" for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert node.module != "sqlite3"
