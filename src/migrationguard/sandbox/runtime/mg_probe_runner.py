"""MigrationGuard behavioural probe runner (runs inside the sandbox).

Differential verification needs *the same inputs* fed to the original and the migrated
code. This runner works under Pydantic v1 and v2 and has two commands:

``discover``  import every project module, find Pydantic models, synthesise a
              deterministic set of probe inputs per model from its field definitions
              (valid payloads, coercion edge cases, missing / extra / None values,
              alias population) and write them to a JSON file. Run in the *baseline*.
``run``       replay an inputs file: validate each payload, record success + the
              normalised python dump + the JSON dump, or the error locations. Also runs
              user probes (``probe_*`` functions in ``migrationguard_probes.py``) and
              records import-time warnings (class-definition deprecations).

Usage: python mg_probe_runner.py discover|run --root R --paths P1:P2 --inputs F --out F
Kept compatible with Python 3.8+.
"""

import argparse
import datetime as dt
import decimal
import enum
import importlib
import inspect
import json
import os
import sys
import traceback
import uuid
import warnings

SKIP_DIRS = {"tests", "test", ".git", "__pycache__", ".venv", "venv", "build", "dist", "docs", "examples", "benchmarks", "node_modules"}
SKIP_FILES = {"setup.py", "conftest.py", "noxfile.py", "migrationguard_probes.py", "manage.py"}

import pydantic  # noqa: E402

PYD_VERSION = getattr(pydantic, "VERSION", getattr(pydantic, "__version__", "0"))
PYD_VERSION = str(PYD_VERSION)
V2 = PYD_VERSION.startswith("2")
BaseModel = pydantic.BaseModel
try:
    if V2:
        from pydantic_settings import BaseSettings  # type: ignore
    else:
        from pydantic import BaseSettings  # type: ignore
except Exception:  # pragma: no cover
    BaseSettings = None  # type: ignore

try:
    from typing import get_args, get_origin
except ImportError:  # pragma: no cover
    def get_origin(tp):
        return getattr(tp, "__origin__", None)

    def get_args(tp):
        return getattr(tp, "__args__", ())

import types  # noqa: E402
import typing  # noqa: E402

# typing.Optional[X] / Union[...] and PEP 604 unions (X | None, Python >= 3.10)
_UNION_TYPES = (typing.Union,) + ((types.UnionType,) if hasattr(types, "UnionType") else ())

try:
    from typing import Literal
except ImportError:  # pragma: no cover
    Literal = None


# ----------------------------------------------------------------------------- normalise
def jsonable(value, depth=0):
    if depth > 40:
        return "<depth>"
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return {"__float__": repr(value)}
        return value
    if isinstance(value, enum.Enum):
        return {"__enum__": type(value).__name__, "value": jsonable(value.value, depth + 1)}
    if isinstance(value, decimal.Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return {"__%s__" % type(value).__name__: value.isoformat()}
    if isinstance(value, dt.timedelta):
        return {"__timedelta__": value.total_seconds()}
    if isinstance(value, uuid.UUID):
        return {"__uuid__": str(value)}
    if isinstance(value, bytes):
        return {"__bytes__": value.decode("latin-1")}
    if isinstance(value, dict):
        if list(value.keys()) == ["__root__"]:  # v1 custom root types dump as {"__root__": ...}
            return jsonable(value["__root__"], depth + 1)
        return {str(k): jsonable(v, depth + 1) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (set, frozenset)):
        items = [jsonable(v, depth + 1) for v in value]
        return {"__set__": sorted(items, key=lambda x: json.dumps(x, sort_keys=True))}
    if isinstance(value, tuple):
        return {"__tuple__": [jsonable(v, depth + 1) for v in value]}
    if isinstance(value, list):
        return [jsonable(v, depth + 1) for v in value]
    if isinstance(value, BaseModel):
        return {"__model__": type(value).__name__, "data": jsonable(dump(value), depth + 1)}
    return {"__repr__": type(value).__name__}


def validate(model, data):
    return model.model_validate(data) if V2 else model.parse_obj(data)


def dump(instance):
    return instance.model_dump() if V2 else instance.dict()


def dump_json(instance):
    return json.loads(instance.model_dump_json() if V2 else instance.json())


def error_locs(exc):
    try:
        errors = exc.errors()
    except Exception:
        return []
    out = []
    for err in errors:
        loc = err.get("loc", ())
        out.append(".".join(str(p) for p in loc) or "__root__")
    return sorted(set(out))


# ----------------------------------------------------------------------------- discovery
def iter_modules(root, paths):
    seen = set()
    for base in paths:
        base_abs = os.path.join(root, base) if not os.path.isabs(base) else base
        if not os.path.isdir(base_abs):
            continue
        for dirpath, dirnames, filenames in os.walk(base_abs):
            dirnames[:] = sorted(
                d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".") and not os.path.exists(os.path.join(dirpath, d, "pyvenv.cfg"))
            )
            rel_dir = os.path.relpath(dirpath, base_abs)
            if rel_dir != "." and not os.path.exists(os.path.join(dirpath, "__init__.py")):
                # namespace dirs below the root are only scanned if they are packages
                dirnames[:] = []
                continue
            for fn in sorted(filenames):
                if not fn.endswith(".py") or fn in SKIP_FILES or fn.startswith("test_") or fn.endswith("_test.py"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), base_abs)
                parts = rel[:-3].split(os.sep)
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                if not parts:
                    continue
                name = ".".join(parts)
                if name not in seen:
                    seen.add(name)
                    yield name


def import_all(root, paths):
    modules = {}
    errors = []
    recorded = []
    for name in iter_modules(root, paths):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                modules[name] = importlib.import_module(name)
            except BaseException as exc:  # noqa: BLE001 - project code may raise anything
                errors.append("%s: %s: %s" % (name, type(exc).__name__, str(exc)[:300]))
        for w in caught:
            recorded.append(
                {
                    "category": "%s.%s" % (w.category.__module__, w.category.__name__),
                    "message": str(w.message)[:400],
                    "filename": str(w.filename),
                    "lineno": int(w.lineno or 0),
                    "nodeid": "",
                    "when": "import:%s" % name,
                }
            )
    return modules, errors, recorded


def project_models(modules):
    found = []
    for modname, module in sorted(modules.items()):
        for attr, obj in sorted(vars(module).items()):
            if not inspect.isclass(obj) or getattr(obj, "__module__", None) != modname:
                continue
            if not issubclass(obj, BaseModel) or obj is BaseModel:
                continue
            if BaseSettings is not None and issubclass(obj, BaseSettings):
                continue
            if getattr(obj, "__parameters__", None):
                continue  # unparametrised generics
            if "[" in obj.__name__:
                continue
            found.append(("%s:%s" % (modname, obj.__qualname__), obj))
    return found


class FieldSpec:
    def __init__(self, name, alias, required, annotation, allow_none, constraints):
        self.name = name
        self.alias = alias
        self.required = required
        self.annotation = annotation
        self.allow_none = allow_none
        self.constraints = constraints


def field_specs(model):
    specs = []
    if V2:
        for name, info in model.model_fields.items():
            alias = info.alias or name
            constraints = {}
            for meta in getattr(info, "metadata", []):
                for key in ("min_length", "max_length", "ge", "gt", "le", "lt", "pattern"):
                    if getattr(meta, key, None) is not None:
                        constraints[key] = getattr(meta, key)
            specs.append(FieldSpec(name, alias, info.is_required(), info.annotation, False, constraints))
    else:
        for name, f in model.__fields__.items():
            fi = f.field_info
            constraints = {}
            for key in ("min_length", "max_length", "ge", "gt", "le", "lt", "regex", "min_items", "max_items"):
                if getattr(fi, key, None) is not None:
                    constraints[key] = getattr(fi, key)
            specs.append(FieldSpec(name, f.alias or name, bool(f.required), f.outer_type_, bool(f.allow_none), constraints))
    return specs


def _bounds_number(tp, constraints, base):
    lo = None
    for key in ("ge", "gt"):
        v = constraints.get(key, getattr(tp, key, None) if inspect.isclass(tp) else None)
        if v is not None:
            lo = v + (1 if key == "gt" else 0)
    hi = None
    for key in ("le", "lt"):
        v = constraints.get(key, getattr(tp, key, None) if inspect.isclass(tp) else None)
        if v is not None:
            hi = v - (1 if key == "lt" else 0)
    value = base
    if lo is not None and value < lo:
        value = lo
    if hi is not None and value > hi:
        value = hi
    return value


def sample(tp, constraints=None, depth=0):
    """A canonical, valid-looking JSON value for annotation ``tp`` (best effort)."""
    constraints = constraints or {}
    if depth > 4:
        return None
    origin = get_origin(tp)
    args = [a for a in get_args(tp) if a is not type(None)] if origin is not None else []
    if tp is typing.Any or tp is object:
        return "mg-any"
    if origin in _UNION_TYPES or (hasattr(typing, "Annotated") and origin is getattr(typing, "Annotated", None)):
        return sample(args[0], constraints, depth + 1) if args else None
    if str(origin) == "typing.Annotated" or (origin is not None and getattr(origin, "__name__", "") == "Annotated"):
        return sample(args[0], constraints, depth + 1) if args else None
    if Literal is not None and origin is Literal:
        return get_args(tp)[0]
    if origin in (list, set, frozenset, tuple) or origin in (typing.List, typing.Set, typing.Tuple):
        if origin is tuple and args and len(args) > 1 and args[-1] is not Ellipsis:
            return [sample(a, None, depth + 1) for a in args]
        item = sample(args[0], None, depth + 1) if args else "x"
        n = max(1, int(constraints.get("min_length", constraints.get("min_items", 1)) or 1))
        return [item for _ in range(n)]
    if origin in (dict, typing.Dict) or tp is dict:
        val = sample(args[1], None, depth + 1) if len(args) == 2 else "v"
        return {"k": val}
    if inspect.isclass(tp):
        if issubclass(tp, BaseModel):
            return minimal_payload(tp, depth + 1)
        if issubclass(tp, enum.Enum):
            first = list(tp)[0]
            return first.value
        if issubclass(tp, bool):
            return True
        if issubclass(tp, int):
            return int(_bounds_number(tp, constraints, 7))
        if issubclass(tp, float):
            return float(_bounds_number(tp, constraints, 1.5))
        if issubclass(tp, decimal.Decimal):
            return "1.10"
        if issubclass(tp, dt.datetime):
            return "2024-01-02T03:04:05"
        if issubclass(tp, dt.date):
            return "2024-01-02"
        if issubclass(tp, dt.time):
            return "03:04:05"
        if issubclass(tp, dt.timedelta):
            return 90
        if issubclass(tp, uuid.UUID):
            return "12345678-1234-5678-1234-567812345678"
        if issubclass(tp, bytes):
            return "bytes"
        name = tp.__name__.lower()
        if "email" in name:
            return "user@example.com"
        if "url" in name or "dsn" in name:
            return "https://example.com/path"
        if issubclass(tp, str):
            n = int(constraints.get("min_length", getattr(tp, "min_length", None) or 0) or 0)
            pattern = constraints.get("regex") or constraints.get("pattern") or getattr(tp, "regex", None) or getattr(tp, "pattern", None)
            if pattern is not None and not isinstance(pattern, (int, float)):
                generated = sample_regex(pattern)
                if generated is not None:
                    return generated
            value = "mg-sample"
            if n > len(value):
                value = value + "x" * (n - len(value))
            mx = constraints.get("max_length", getattr(tp, "max_length", None))
            if mx:
                value = value[: int(mx)]
            return value
        if issubclass(tp, (list, set, tuple)):
            item_type = getattr(tp, "item_type", str)
            n = int(getattr(tp, "min_items", None) or 1)
            return [sample(item_type, None, depth + 1) for _ in range(n)]
    return "mg-sample"


try:  # Python >= 3.11
    import re._parser as _sre_parse  # type: ignore
except ImportError:  # pragma: no cover
    import sre_parse as _sre_parse  # type: ignore

_CATEGORY_CHARS = {"CATEGORY_DIGIT": "7", "CATEGORY_WORD": "a", "CATEGORY_SPACE": " ", "CATEGORY_NOT_DIGIT": "a",
                   "CATEGORY_NOT_WORD": "-", "CATEGORY_NOT_SPACE": "a"}


def sample_regex(pattern):
    """Deterministically produce a string matching ``pattern`` (common subset of regex)."""
    if hasattr(pattern, "pattern"):
        pattern = pattern.pattern
    try:
        parsed = _sre_parse.parse(pattern)
    except Exception:
        return None

    def gen(items):
        out = []
        for op, arg in items:
            name = str(op)
            if name == "LITERAL":
                out.append(chr(arg))
            elif name == "NOT_LITERAL":
                out.append("a" if chr(arg) != "a" else "b")
            elif name == "ANY":
                out.append("a")
            elif name == "IN":
                out.append(_in_char(arg))
            elif name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
                lo, hi, sub = arg
                count = lo if lo > 0 else (1 if hi and hi > 0 else 0)
                out.append(gen(sub) * count)
            elif name == "SUBPATTERN":
                out.append(gen(arg[-1]))
            elif name == "BRANCH":
                out.append(gen(arg[1][0]))
            elif name == "CATEGORY":
                out.append(_CATEGORY_CHARS.get(str(arg), "a"))
            elif name in ("AT", "ASSERT", "ASSERT_NOT", "GROUPREF"):
                continue
            else:
                out.append("a")
        return "".join(out)

    def _in_char(items):
        negate = any(str(op) == "NEGATE" for op, _ in items)
        if negate:
            import re as _re

            klass = "[" + "".join(_class_part(op, arg) for op, arg in items) + "]"
            for candidate in "xqzXQZ079_-a":
                if _re.fullmatch(klass, candidate):
                    return candidate
            return "x"
        for op, arg in items:
            name = str(op)
            if name == "LITERAL":
                return chr(arg)
            if name == "RANGE":
                return chr(arg[0])
            if name == "CATEGORY":
                return _CATEGORY_CHARS.get(str(arg), "a")
        return "a"

    def _class_part(op, arg):
        name = str(op)
        if name == "NEGATE":
            return "^"
        if name == "LITERAL":
            import re as _re

            return _re.escape(chr(arg))
        if name == "RANGE":
            return "%s-%s" % (chr(arg[0]), chr(arg[1]))
        if name == "CATEGORY":
            return {"CATEGORY_DIGIT": "\\d", "CATEGORY_WORD": "\\w", "CATEGORY_SPACE": "\\s",
                    "CATEGORY_NOT_DIGIT": "\\D", "CATEGORY_NOT_WORD": "\\W", "CATEGORY_NOT_SPACE": "\\S"}.get(str(arg), "")
        return ""

    try:
        value = gen(parsed)
    except Exception:
        return None
    import re as _re

    return value if _re.search(pattern, value) else None


def _is_custom_root(model):
    return bool(getattr(model, "__custom_root_type__", False))


def minimal_payload(model, depth=0):
    if _is_custom_root(model):
        spec = field_specs(model)[0]
        return sample(spec.annotation, spec.constraints, depth)
    out = {}
    for spec in field_specs(model):
        if spec.required:
            out[spec.alias] = sample(spec.annotation, spec.constraints, depth)
    return out


def full_payload(model):
    out = {}
    for spec in field_specs(model):
        out[spec.alias] = sample(spec.annotation, spec.constraints, 0)
    return out


def _is_type(tp, cls):
    if inspect.isclass(tp):
        try:
            return issubclass(tp, cls) and not (cls is int and issubclass(tp, bool))
        except TypeError:
            return False
    origin = get_origin(tp)
    if origin in _UNION_TYPES:
        args = [a for a in get_args(tp) if a is not type(None)]
        return len(args) == 1 and _is_type(args[0], cls)
    return False


def _is_optional(spec):
    if spec.allow_none:
        return True
    tp = spec.annotation
    return get_origin(tp) in _UNION_TYPES and type(None) in get_args(tp)


def generate_inputs(model, limit):
    if _is_custom_root(model):
        base = minimal_payload(model)
        return [{"kind": "root", "payload": base}, {"kind": "root-empty", "payload": [] if isinstance(base, list) else {}}]
    specs = field_specs(model)
    base = minimal_payload(model)
    full = full_payload(model)
    cases = [("minimal", base), ("full", full)]
    # variants are derived from a payload the *baseline* accepts, otherwise every variant
    # would just re-trigger the same (cross-field) validation error
    seed = full
    for candidate in (full, base):
        try:
            validate(model, candidate)
            seed = candidate
            break
        except Exception:  # noqa: BLE001
            continue
    full = seed
    for spec in specs:
        if _is_type(spec.annotation, str) and not _is_type(spec.annotation, enum.Enum):
            cases.append(("coerce-str:%s" % spec.name, dict(full, **{spec.alias: 123})))
        elif _is_type(spec.annotation, bool):
            cases.append(("coerce-bool:%s" % spec.name, dict(full, **{spec.alias: "yes"})))
        elif _is_type(spec.annotation, int):
            cases.append(("coerce-int:%s" % spec.name, dict(full, **{spec.alias: "42"})))
            cases.append(("truncate-int:%s" % spec.name, dict(full, **{spec.alias: 42.7})))
        elif _is_type(spec.annotation, float):
            cases.append(("coerce-float:%s" % spec.name, dict(full, **{spec.alias: "2.5"})))
        elif _is_type(spec.annotation, dt.datetime):
            cases.append(("tz-aware:%s" % spec.name, dict(full, **{spec.alias: "2024-01-02T03:04:05+00:00"})))
    for spec in specs:
        if _is_optional(spec):
            cases.append(("none:%s" % spec.name, dict(full, **{spec.alias: None})))
        if spec.alias != spec.name:
            payload = dict(full)
            payload.pop(spec.alias, None)
            payload[spec.name] = full.get(spec.alias)
            cases.append(("by-name:%s" % spec.name, payload))
    for spec in specs:
        if spec.required:
            payload = dict(full)
            payload.pop(spec.alias, None)
            cases.append(("missing:%s" % spec.name, payload))
            break
    cases.append(("extra-key", dict(full, mg_extra_key="x")))
    unique = []
    seen = set()
    for kind, payload in cases:
        if kind in seen:
            continue
        seen.add(kind)
        unique.append({"kind": kind, "payload": payload})
    return unique[:limit]


# ----------------------------------------------------------------------------- execution
def run_model_probe(model, payload):
    try:
        instance = validate(model, payload)
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "ValidationError":
            return {"status": "error", "error_type": "ValidationError", "error_locs": error_locs(exc)}
        return {"status": "crash", "error_type": type(exc).__name__, "error_locs": []}
    out = {"status": "ok"}
    try:
        out["value"] = jsonable(dump(instance))
    except Exception as exc:  # noqa: BLE001
        out["value"] = {"__dump_error__": type(exc).__name__}
    try:
        out["json_value"] = dump_json(instance)
    except Exception as exc:  # noqa: BLE001
        out["json_value"] = {"__json_error__": type(exc).__name__}
    return out


def resolve(qualname):
    modname, _, clsname = qualname.partition(":")
    obj = importlib.import_module(modname)
    for part in clsname.split("."):
        obj = getattr(obj, part)
    return obj


def run_user_probes(root):
    path = os.path.join(root, "migrationguard_probes.py")
    results = {}
    if not os.path.isfile(path):
        return results
    import importlib.util

    spec = importlib.util.spec_from_file_location("migrationguard_probes", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        results["user:<import>"] = {"status": "crash", "error_type": type(exc).__name__, "error_locs": [str(exc)[:200]]}
        return results
    for name, fn in sorted(vars(module).items()):
        if name.startswith("probe_") and callable(fn):
            pid = "user:%s" % name
            try:
                value = fn()
                results[pid] = {"status": "ok", "value": jsonable(value), "json_value": None}
            except Exception as exc:  # noqa: BLE001
                status = "error" if type(exc).__name__ == "ValidationError" else "crash"
                results[pid] = {"status": status, "error_type": type(exc).__name__, "error_locs": error_locs(exc) if status == "error" else []}
    return results


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["discover", "run"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--paths", default=".")
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=14)
    args = parser.parse_args(argv)
    paths = [p for p in args.paths.split(":") if p]
    for p in reversed(paths):
        full = os.path.join(args.root, p)
        if full not in sys.path:
            sys.path.insert(0, full)
    modules, import_errors, import_warnings = import_all(args.root, paths)
    if args.command == "discover":
        inputs = {}
        for qualname, model in project_models(modules):
            try:
                inputs[qualname] = generate_inputs(model, args.limit)
            except Exception:  # noqa: BLE001
                inputs[qualname] = [{"kind": "generation-failed", "payload": {}, "trace": traceback.format_exc()[-500:]}]
        with open(args.inputs, "w", encoding="utf-8") as fh:
            json.dump(inputs, fh, indent=1, sort_keys=True)
    with open(args.inputs, encoding="utf-8") as fh:
        inputs = json.load(fh)
    outcomes = {}
    for qualname, cases in sorted(inputs.items()):
        try:
            model = resolve(qualname)
        except Exception as exc:  # noqa: BLE001
            for case in cases:
                outcomes["%s/%s" % (qualname, case["kind"])] = {"status": "missing", "error_type": type(exc).__name__, "error_locs": []}
            continue
        for case in cases:
            outcomes["%s/%s" % (qualname, case["kind"])] = run_model_probe(model, case["payload"])
    outcomes.update(run_user_probes(args.root))
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "pydantic_version": PYD_VERSION,
                "outcomes": outcomes,
                "import_errors": import_errors,
                "import_warnings": import_warnings,
            },
            fh,
            sort_keys=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
