"""Project discovery and a lightweight, AST-based symbol index.

The index answers the questions the codemod needs answered across files:
*which classes are Pydantic models* (directly or through project-local base classes),
*which of their fields are typed as other models*, and *which are settings classes*.
It never imports project code.
"""

from __future__ import annotations

import ast
import fnmatch
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    ".env",
    ".tox",
    ".nox",
    "node_modules",
    "build",
    "dist",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "site-packages",
    ".migrationguard",
    ".eggs",
}

PYDANTIC_BASES = {
    "pydantic.BaseModel": "model",
    "pydantic.main.BaseModel": "model",
    "pydantic.BaseSettings": "settings",
    "pydantic.env_settings.BaseSettings": "settings",
    "pydantic_settings.BaseSettings": "settings",
    "pydantic.generics.GenericModel": "generic",
    "pydantic.v1.BaseModel": "model",
    "pydantic.v1.BaseSettings": "settings",
    "pydantic.v1.generics.GenericModel": "generic",
    "pydantic.RootModel": "root",
}


def iter_python_files(root: Path) -> Iterator[Path]:
    """Yield repository Python files in a stable order, skipping virtualenvs and caches."""
    stack = [root]
    found: list[Path] = []
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name in SKIP_DIRS or entry.name.endswith(".egg-info"):
                    continue
                if (entry / "pyvenv.cfg").exists():
                    continue
                stack.append(entry)
            elif entry.suffix == ".py":
                found.append(entry)
    yield from sorted(found)


def rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def module_name_for(relpath: str) -> str:
    parts = relpath[:-3].split("/") if relpath.endswith(".py") else relpath.split("/")
    if parts and parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def matches_any(path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if fnmatch.fnmatchcase(path, pat):
            return True
        # "**/x" should also match "x" at the root
        if pat.startswith("**/") and fnmatch.fnmatchcase(path, pat[3:]):
            return True
    return False


def is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    parts = path.split("/")
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
        or "tests" in parts[:-1]
        or "test" in parts[:-1]
    )


# --------------------------------------------------------------------------- index
@dataclass
class FieldInfo:
    name: str
    annotation: str
    lineno: int
    has_default: bool
    model_ref: str | None = None  # model type referenced directly (incl. Optional[M])
    item_model_ref: str | None = None  # model type of list/sequence items


@dataclass
class ClassInfo:
    name: str
    module: str
    path: str
    lineno: int
    bases: list[str]
    kind: str | None = None  # model | settings | generic | root | None
    fields: dict[str, FieldInfo] = field(default_factory=dict)
    has_config_class: bool = False
    has_root_field: bool = False

    @property
    def is_model(self) -> bool:
        return self.kind is not None

    @property
    def qualname(self) -> str:
        return f"{self.module}:{self.name}"


@dataclass
class FunctionInfo:
    name: str
    module: str
    return_annotation: str | None
    returned_calls: list[str]  # dotted call targets of `return X(...)` / `return X.parse_obj(...)`
    returns_model: str | None = None
    returns_items_of: str | None = None


@dataclass
class ModuleInfo:
    path: str
    module: str
    imports_pydantic: bool
    pydantic_aliases: dict[str, str]  # local name -> canonical dotted name
    pydantic_modules: set[str]  # local names bound to the pydantic module (or submodules)
    classes: list[ClassInfo]
    functions: list[FunctionInfo] = field(default_factory=list)


@dataclass
class ProjectIndex:
    root: Path
    modules: dict[str, ModuleInfo] = field(default_factory=dict)  # relpath -> info
    classes: dict[str, list[ClassInfo]] = field(default_factory=dict)  # simple name -> infos
    functions: dict[str, list[FunctionInfo]] = field(default_factory=dict)  # simple name -> infos
    parse_errors: dict[str, str] = field(default_factory=dict)

    def function_returns(self, name: str) -> tuple[str | None, str | None]:
        """(model instance, model items) returned by a project function, if unambiguous."""
        infos = self.functions.get(name, [])
        results = {(f.returns_model, f.returns_items_of) for f in infos if f.returns_model or f.returns_items_of}
        if len(results) == 1:
            return next(iter(results))
        return None, None

    # ---------------------------------------------------------------- queries
    @property
    def model_names(self) -> set[str]:
        return {name for name, infos in self.classes.items() if any(c.is_model for c in infos)}

    def model(self, name: str) -> ClassInfo | None:
        for info in self.classes.get(name, []):
            if info.is_model:
                return info
        return None

    def models(self) -> list[ClassInfo]:
        return [c for infos in self.classes.values() for c in infos if c.is_model]

    def settings_names(self) -> set[str]:
        return {c.name for c in self.models() if c.kind == "settings"}

    def uses_pydantic(self) -> bool:
        return any(m.imports_pydantic for m in self.modules.values())

    # ---------------------------------------------------------------- building
    @classmethod
    def build(cls, root: Path) -> ProjectIndex:
        index = cls(root=root)
        for path in iter_python_files(root):
            relpath = rel(root, path)
            try:
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=relpath)
            except (SyntaxError, UnicodeDecodeError) as exc:
                index.parse_errors[relpath] = str(exc)
                continue
            info = _scan_module(relpath, tree)
            index.modules[relpath] = info
            for c in info.classes:
                index.classes.setdefault(c.name, []).append(c)
            for f in info.functions:
                index.functions.setdefault(f.name, []).append(f)
        index._resolve_kinds()
        index._resolve_field_refs()
        index._resolve_function_returns()
        return index

    def _resolve_function_returns(self) -> None:
        names = self.model_names
        constructors = {"parse_obj", "parse_raw", "from_orm", "construct", "validate", "model_validate",
                        "model_validate_json", "model_construct", "parse_file"}
        for infos in self.functions.values():
            for f in infos:
                if f.return_annotation:
                    f.returns_model, f.returns_items_of = annotation_model_refs(f.return_annotation, names)
                    if f.returns_model or f.returns_items_of:
                        continue
                candidates = set()
                for target in f.returned_calls:
                    parts = target.split(".")
                    if parts[-1] in names:
                        candidates.add(parts[-1])
                    elif len(parts) >= 2 and parts[-1] in constructors and parts[-2] in names:
                        candidates.add(parts[-2])
                    else:
                        candidates.add("?")
                if len(candidates) == 1 and "?" not in candidates:
                    f.returns_model = candidates.pop()

    def _resolve_kinds(self) -> None:
        all_classes = [c for infos in self.classes.values() for c in infos]
        by_module = {m.path: m for m in self.modules.values()}
        # direct pydantic bases
        for c in all_classes:
            mod = by_module[c.path]
            for base in c.bases:
                canonical = _canonical(base, mod)
                if canonical in PYDANTIC_BASES:
                    c.kind = PYDANTIC_BASES[canonical]
                    break
        # transitive through project classes (fixpoint)
        changed = True
        while changed:
            changed = False
            for c in all_classes:
                if c.kind:
                    continue
                for base in c.bases:
                    simple = base.split("[")[0].rsplit(".", 1)[-1]
                    parent = next((p for p in self.classes.get(simple, []) if p.kind), None)
                    if parent is not None:
                        c.kind = "settings" if parent.kind == "settings" else "model"
                        changed = True
                        break

    def _resolve_field_refs(self) -> None:
        names = self.model_names
        for c in self.models():
            for f in c.fields.values():
                f.model_ref, f.item_model_ref = annotation_model_refs(f.annotation, names)


def _canonical(expr: str, mod: ModuleInfo) -> str:
    base = expr.split("[")[0]
    head, _, tail = base.partition(".")
    if head in mod.pydantic_aliases and not tail:
        return mod.pydantic_aliases[head]
    if head in mod.pydantic_modules:
        target = mod.pydantic_aliases.get(head, "pydantic")
        return f"{target}.{tail}" if tail else target
    return base


def _scan_module(relpath: str, tree: ast.Module) -> ModuleInfo:
    aliases: dict[str, str] = {}
    modules: set[str] = set()
    imports_pydantic = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in (
            "pydantic",
            "pydantic_settings",
        ):
            imports_pydantic = True
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in ("pydantic", "pydantic_settings"):
                    imports_pydantic = True
                    local = alias.asname or alias.name.split(".")[0]
                    modules.add(local)
                    aliases[local] = alias.name if alias.asname else alias.name.split(".")[0]
    module = module_name_for(relpath)
    classes = [
        _scan_class(relpath, module, node) for node in tree.body if isinstance(node, ast.ClassDef)
    ]
    functions = [
        _scan_function(module, node) for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return ModuleInfo(relpath, module, imports_pydantic, aliases, modules, classes, functions)


def _scan_function(module: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> FunctionInfo:
    returned: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Return) and sub.value is not None:
            if isinstance(sub.value, ast.Call):
                returned.append(ast.unparse(sub.value.func))
            else:
                returned.append("?")
    annotation = ast.unparse(node.returns) if node.returns is not None else None
    return FunctionInfo(node.name, module, annotation, returned)


def _scan_class(relpath: str, module: str, node: ast.ClassDef) -> ClassInfo:
    info = ClassInfo(
        name=node.name,
        module=module,
        path=relpath,
        lineno=node.lineno,
        bases=[ast.unparse(b) for b in node.bases],
    )
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name = stmt.target.id
            annotation = ast.unparse(stmt.annotation)
            if annotation.startswith(("ClassVar", "typing.ClassVar")):
                continue
            info.fields[name] = FieldInfo(name, annotation, stmt.lineno, stmt.value is not None)
            if name == "__root__":
                info.has_root_field = True
        elif isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
            info.has_config_class = True
    return info


_WRAPPERS = {"Optional", "typing.Optional", "Annotated", "typing.Annotated"}
_SEQUENCES = {
    "List",
    "list",
    "Sequence",
    "Iterable",
    "Tuple",
    "tuple",
    "Set",
    "set",
    "FrozenSet",
    "frozenset",
    "typing.List",
    "typing.Sequence",
    "typing.Iterable",
    "typing.Tuple",
    "conlist",
}


def annotation_model_refs(annotation: str, model_names: set[str]) -> tuple[str | None, str | None]:
    """Return (direct model ref, sequence item model ref) for an annotation string."""
    try:
        expr = ast.parse(annotation.strip("'\""), mode="eval").body
    except SyntaxError:
        return None, None
    return _refs(expr, model_names)


def _refs(expr: ast.expr, names: set[str]) -> tuple[str | None, str | None]:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        try:
            return _refs(ast.parse(expr.value, mode="eval").body, names)
        except SyntaxError:
            return None, None
    if isinstance(expr, ast.Name):
        return (expr.id if expr.id in names else None), None
    if isinstance(expr, ast.Attribute):
        return (expr.attr if expr.attr in names else None), None
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
        for side in (expr.left, expr.right):
            if not (isinstance(side, ast.Constant) and side.value is None):
                direct, item = _refs(side, names)
                if direct or item:
                    return direct, item
        return None, None
    if isinstance(expr, ast.Subscript):
        head = ast.unparse(expr.value)
        args = expr.slice.elts if isinstance(expr.slice, ast.Tuple) else [expr.slice]
        if head in _WRAPPERS or head in ("Union", "typing.Union"):
            non_none = [a for a in args if not (isinstance(a, ast.Constant) and a.value is None)]
            if head in ("Annotated", "typing.Annotated"):
                non_none = non_none[:1]
            if len(non_none) == 1:
                return _refs(non_none[0], names)
            return None, None
        if head in _SEQUENCES and args:
            direct, _ = _refs(args[0], names)
            return None, direct
    if isinstance(expr, ast.Call) and ast.unparse(expr.func) in ("conlist", "pydantic.conlist") and expr.args:
        direct, _ = _refs(expr.args[0], names)
        return None, direct
    return None, None
