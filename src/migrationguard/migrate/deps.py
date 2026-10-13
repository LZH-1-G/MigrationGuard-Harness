"""Dependency discovery and format-preserving dependency rewriting.

Supported manifests: ``requirements*.txt`` (incl. ``-r`` includes), PEP 621
``pyproject.toml`` (``[project]`` dependencies and optional-dependencies), Poetry
``[tool.poetry.dependencies]``, ``setup.cfg`` ``install_requires`` and string literals in
``setup.py``. Edits are textual (regex based) so comments and layout survive.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

REQ_GLOBS = ("requirements*.txt", "requirements/*.txt", "requirements*.in")
TEST_EXTRAS = ("test", "tests", "testing", "dev")
_NAME_CHARS = r"A-Za-z0-9._-"


@dataclass
class DependencyEdit:
    path: str
    before: str
    after: str
    description: str


@dataclass
class DependencyReport:
    files: list[str] = field(default_factory=list)
    edits: list[DependencyEdit] = field(default_factory=list)
    pydantic_specs: list[tuple[str, str]] = field(default_factory=list)  # (file, requirement)


def find_dependency_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for pattern in REQ_GLOBS:
        found.extend(sorted(root.glob(pattern)))
    for name in ("pyproject.toml", "setup.cfg", "setup.py"):
        if (root / name).is_file():
            found.append(root / name)
    seen: set[Path] = set()
    out = []
    for p in found:
        if p not in seen and p.is_file():
            seen.add(p)
            out.append(p)
    return out


# --------------------------------------------------------------------------- reading
def _parse_req(text: str) -> Requirement | None:
    try:
        return Requirement(text.strip())
    except InvalidRequirement:
        return None


def read_requirements_file(path: Path, _seen: set[Path] | None = None) -> list[str]:
    seen = _seen or set()
    if path in seen or not path.is_file():
        return []
    seen.add(path)
    reqs: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r ", "--requirement ")):
            reqs.extend(read_requirements_file(path.parent / line.split(None, 1)[1], seen))
            continue
        if line.startswith(("-c ", "--constraint ")):
            continue
        if line.startswith("-") or line.startswith((".", "/")) or "://" in line:
            continue  # editable installs / local paths / urls: project itself is put on PYTHONPATH
        if _parse_req(line):
            reqs.append(line)
    return reqs


def _poetry_to_pep508(name: str, spec: object) -> str | None:
    version = spec.get("version") if isinstance(spec, dict) else spec
    extras = spec.get("extras", []) if isinstance(spec, dict) else []
    if not isinstance(version, str):
        return None
    version = version.strip()
    if version in ("*", ""):
        sset = ""
    elif version.startswith("^"):
        parts = version[1:].split(".")
        major = int(parts[0]) if parts[0].isdigit() else 0
        if major > 0:
            upper = f"{major + 1}"
        elif len(parts) > 1 and parts[1].isdigit():
            upper = f"0.{int(parts[1]) + 1}"
        else:
            upper = "1"
        sset = f">={version[1:]},<{upper}"
    elif version.startswith("~"):
        sset = f"~={version.lstrip('~=')}" if version.count(".") >= 1 else f">={version[1:]}"
    elif version[0].isdigit():
        sset = f"=={version}"
    else:
        sset = version
    extra = f"[{','.join(extras)}]" if extras else ""
    return f"{name}{extra}{sset}"


def collect_requirements(root: Path, explicit: list[str] | None = None, include_test_extras: bool = True) -> list[str]:
    """Requirement strings needed to run the project's tests (project itself excluded)."""
    reqs: list[str] = []
    if explicit:
        for rel in explicit:
            reqs.extend(read_requirements_file(root / rel))
        return _dedupe(reqs)
    for pattern in REQ_GLOBS:
        for path in sorted(root.glob(pattern)):
            reqs.extend(read_requirements_file(path))
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        reqs.extend(_pyproject_requires(pyproject, include_test_extras))
    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        reqs.extend(_setup_cfg_requires(setup_cfg.read_text(encoding="utf-8")))
    setup_py = root / "setup.py"
    if setup_py.is_file() and not reqs:
        reqs.extend(_setup_py_requires(setup_py.read_text(encoding="utf-8")))
    return _dedupe(reqs)


def _pyproject_requires(pyproject: Path, include_test_extras: bool = True) -> list[str]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    reqs: list[str] = []
    project = data.get("project", {})
    reqs.extend(project.get("dependencies", []) or [])
    if include_test_extras:
        for extra, deps in (project.get("optional-dependencies", {}) or {}).items():
            if extra in TEST_EXTRAS:
                reqs.extend(deps)
        for group, deps in (data.get("dependency-groups", {}) or {}).items():
            if group in TEST_EXTRAS:
                reqs.extend(d for d in deps if isinstance(d, str))
    poetry = data.get("tool", {}).get("poetry", {})
    sections = [poetry.get("dependencies", {})]
    if include_test_extras:
        sections.append(poetry.get("dev-dependencies", {}))
        for gname, group in (poetry.get("group", {}) or {}).items():
            if gname in TEST_EXTRAS:
                sections.append(group.get("dependencies", {}))
    for section in sections:
        for name, spec in (section or {}).items():
            if name.lower() == "python":
                continue
            converted = _poetry_to_pep508(name, spec)
            if converted:
                reqs.append(converted)
    return reqs


def _setup_cfg_requires(text: str) -> list[str]:
    out: list[str] = []
    capture = False
    for line in text.splitlines():
        if re.match(r"^\s*install_requires\s*=", line):
            capture = True
            rest = line.split("=", 1)[1].strip()
            if rest and _parse_req(rest):
                out.append(rest)
            continue
        if capture:
            if line.startswith((" ", "\t")) and line.strip():
                if _parse_req(line.strip()):
                    out.append(line.strip())
            elif line.strip():
                capture = False
    return out


def _setup_py_requires(text: str) -> list[str]:
    match = re.search(r"install_requires\s*=\s*\[(.*?)\]", text, re.S)
    if not match:
        return []
    return [s for s in re.findall(r"[\"']([^\"']+)[\"']", match.group(1)) if _parse_req(s)]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def pydantic_requirements(reqs: list[str]) -> list[Requirement]:
    out = []
    for r in reqs:
        parsed = _parse_req(r)
        if parsed and parsed.name.lower().replace("_", "-") == "pydantic":
            out.append(parsed)
    return out


def spec_allows_v1(spec: SpecifierSet) -> bool:
    return spec.contains("1.10.26", prereleases=True) or spec.contains("1.9.0", prereleases=True)


def spec_allows_v2(spec: SpecifierSet) -> bool:
    return any(spec.contains(v, prereleases=True) for v in ("2.7.0", "2.9.2", "2.11.0", "2.14.1"))


# --------------------------------------------------------------------------- rewriting
_REQ_LINE = re.compile(
    rf"^(?P<indent>\s*)(?P<name>pydantic)(?![{_NAME_CHARS}])(?P<extras>\s*\[[^\]]*\])?"
    r"(?P<spec>\s*[<>=!~][^;#\s]*(?:\s*,\s*[<>=!~][^;#\s]*)*)?(?P<rest>.*)$",
    re.I,
)
_QUOTED = re.compile(
    rf"(?P<q>[\"'])(?P<name>pydantic)(?![{_NAME_CHARS}])(?P<extras>\s*\[[^\]]*\])?"
    r"(?P<spec>\s*[<>=!~][^;\"']*?)?(?P<marker>\s*;[^\"']*)?(?P=q)",
    re.I,
)
_POETRY = re.compile(r"^(?P<lead>\s*pydantic\s*=\s*)(?P<value>.+)$", re.I | re.M)


def _target_spec(target_requirement: str) -> tuple[str, str]:
    """Split ``name<spec>`` keeping the user's spelling of the specifier."""
    req = Requirement(target_requirement)
    text = target_requirement.strip()
    spec = text[len(req.name) :].strip() if text.lower().startswith(req.name.lower()) else str(req.specifier)
    if spec.startswith("["):
        spec = spec[spec.index("]") + 1 :].strip()
    return req.name, spec.split(";")[0].strip()


def rewrite_dependencies(
    root: Path, target_requirement: str, settings_requirement: str | None
) -> DependencyReport:
    """Rewrite pydantic pins to ``target_requirement``; optionally add pydantic-settings."""
    _, target_spec = _target_spec(target_requirement)
    report = DependencyReport()
    for path in find_dependency_files(root):
        rel = path.relative_to(root).as_posix()
        report.files.append(rel)
        text = path.read_text(encoding="utf-8")
        if path.suffix in (".txt", ".in"):
            new = _rewrite_requirements_txt(text, target_spec, settings_requirement)
        elif path.name == "pyproject.toml":
            new = _rewrite_quoted(text, target_spec, settings_requirement)
            new = _rewrite_poetry(new, target_spec, settings_requirement)
        else:
            new = _rewrite_quoted(text, target_spec, settings_requirement) if path.name == "setup.py" else _rewrite_setup_cfg(
                text, target_spec, settings_requirement
            )
        if new != text:
            path.write_text(new, encoding="utf-8")
            report.edits.append(DependencyEdit(rel, text, new, f"pydantic -> {target_spec}"))
    return report


def _has_settings(text: str) -> bool:
    return re.search(r"pydantic[-_]settings", text, re.I) is not None


def _rewrite_requirements_txt(text: str, target_spec: str, settings_req: str | None) -> str:
    lines = text.splitlines(keepends=True)
    out = []
    add_settings = settings_req and not _has_settings(text)
    for line in lines:
        body = line.rstrip("\n")
        newline = line[len(body) :]
        match = _REQ_LINE.match(body)
        if match and not body.lstrip().startswith("#"):
            rest = match.group("rest") or ""
            body = f"{match.group('indent')}{match.group('name')}{match.group('extras') or ''}{target_spec}{rest}"
            out.append(body + (newline or "\n"))
            if add_settings:
                out.append(f"{match.group('indent')}{settings_req}\n")
                add_settings = False
            continue
        out.append(line)
    return "".join(out)


def _rewrite_quoted(text: str, target_spec: str, settings_req: str | None) -> str:
    added = {"done": not settings_req or _has_settings(text)}

    def repl(match: re.Match[str]) -> str:
        q = match.group("q")
        new = f"{q}{match.group('name')}{match.group('extras') or ''}{target_spec}{match.group('marker') or ''}{q}"
        if not added["done"]:
            added["done"] = True
            new += f", {q}{settings_req}{q}"
        return new

    return _QUOTED.sub(repl, text)


def _rewrite_poetry(text: str, target_spec: str, settings_req: str | None) -> str:
    if "[tool.poetry" not in text:
        return text
    added = {"done": not settings_req or _has_settings(text)}

    def repl(match: re.Match[str]) -> str:
        value = match.group("value")
        if value.lstrip().startswith("{"):
            value = re.sub(r"version\s*=\s*([\"'])[^\"']*\1", f'version = "{target_spec}"', value)
        else:
            value = f'"{target_spec}"'
        line = f"{match.group('lead')}{value}"
        if not added["done"]:
            added["done"] = True
            name, spec = _target_spec(settings_req or "")
            line += f'\n{name} = "{spec}"'
        return line

    return _POETRY.sub(repl, text)


def _rewrite_setup_cfg(text: str, target_spec: str, settings_req: str | None) -> str:
    out = []
    add_settings = settings_req and not _has_settings(text)
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\n")
        match = _REQ_LINE.match(body)
        if match and body.startswith((" ", "\t")):
            out.append(f"{match.group('indent')}{match.group('name')}{match.group('extras') or ''}{target_spec}{match.group('rest') or ''}\n")
            if add_settings:
                out.append(f"{match.group('indent')}{settings_req}\n")
                add_settings = False
            continue
        out.append(line)
    return "".join(out)


def declared_pydantic_specs(root: Path) -> list[tuple[str, str]]:
    """(file, requirement) for every pydantic requirement declared in manifests."""
    out: list[tuple[str, str]] = []
    for path in find_dependency_files(root):
        rel = path.relative_to(root).as_posix()
        if path.suffix in (".txt", ".in"):
            reqs = read_requirements_file(path)
        elif path.name == "pyproject.toml":
            reqs = _pyproject_requires(path)
        elif path.name == "setup.cfg":
            reqs = _setup_cfg_requires(path.read_text(encoding="utf-8"))
        else:
            reqs = _setup_py_requires(path.read_text(encoding="utf-8"))
        for r in pydantic_requirements(reqs):
            out.append((rel, str(r)))
    return sorted(set(out))
