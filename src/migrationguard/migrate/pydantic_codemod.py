"""Deterministic Pydantic v1 → v2 codemod (libcst based).

Design rules
------------
* **Only semantics-preserving rewrites are applied automatically.** Anything that needs
  judgement (custom root types, ``each_item`` validators, post root-validators that mutate
  ``values`` ...) is reported as an *unresolved* finding and left untouched for the repair
  agent. A partially-converted construct is never emitted.
* **Receiver-aware method renames.** ``.dict()`` / ``.json()`` / ``.copy()`` are only
  rewritten when the receiver is known (via a small flow-insensitive type inference over
  annotations, constructors and model field types) to be a Pydantic model. Unknown
  receivers produce low-severity findings instead of risky edits.
* **The same codemod is the residual detector and the test-change auditor.** Running it in
  dry-run mode over migrated code must produce zero changes and zero unresolved findings,
  and a modification to a protected test file is only "audited" if it equals the codemod's
  output for the original file.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import libcst as cst
import libcst.matchers as m
from libcst.codemod import CodemodContext, VisitorBasedCodemodCommand
from libcst.codemod.visitors import AddImportsVisitor, RemoveImportsVisitor
from libcst.metadata import PositionProvider

from migrationguard.analysis.project import ProjectIndex, annotation_model_refs
from migrationguard.models import AppliedChange, Finding, Severity

# ----------------------------------------------------------------------------- tables
CONFIG_RENAMES = {
    "orm_mode": "from_attributes",
    "allow_population_by_field_name": "populate_by_name",
    "anystr_strip_whitespace": "str_strip_whitespace",
    "anystr_lower": "str_to_lower",
    "anystr_upper": "str_to_upper",
    "min_anystr_length": "str_min_length",
    "max_anystr_length": "str_max_length",
    "validate_all": "validate_default",
    "schema_extra": "json_schema_extra",
    "keep_untouched": "ignored_types",
}
CONFIG_SAME = {
    "extra",
    "frozen",
    "title",
    "validate_assignment",
    "arbitrary_types_allowed",
    "use_enum_values",
    "json_encoders",
    "alias_generator",
    "allow_inf_nan",
    "str_strip_whitespace",
    "str_min_length",
    "str_max_length",
    "populate_by_name",
    "from_attributes",
    "validate_default",
    "protected_namespaces",
}
CONFIG_DROP = {"underscore_attrs_are_private", "smart_union"}
SETTINGS_CONFIG_KEYS = {
    "env_prefix",
    "env_file",
    "env_file_encoding",
    "env_nested_delimiter",
    "case_sensitive",
    "secrets_dir",
}

V2_FIELD_KWARGS = {
    "default",
    "default_factory",
    "alias",
    "alias_priority",
    "validation_alias",
    "serialization_alias",
    "title",
    "description",
    "examples",
    "exclude",
    "discriminator",
    "json_schema_extra",
    "frozen",
    "validate_default",
    "repr",
    "init_var",
    "kw_only",
    "pattern",
    "strict",
    "gt",
    "ge",
    "lt",
    "le",
    "multiple_of",
    "allow_inf_nan",
    "max_digits",
    "decimal_places",
    "min_length",
    "max_length",
    "union_mode",
    "coerce_numbers_to_str",
}
FIELD_RENAMES = {"regex": "pattern", "min_items": "min_length", "max_items": "max_length"}
FIELD_UNSUPPORTED = {"const", "unique_items", "include", "allow_mutation_", "final"}

CON_RENAMES = {
    "constr": {"regex": "pattern"},
    "conbytes": {},
    "conlist": {"min_items": "min_length", "max_items": "max_length"},
    "conset": {"min_items": "min_length", "max_items": "max_length"},
    "confrozenset": {"min_items": "min_length", "max_items": "max_length"},
}
CON_UNSUPPORTED = {"unique_items", "curtail_length"}

INSTANCE_METHODS = {
    "dict": (
        "model_dump",
        {"include", "exclude", "by_alias", "exclude_unset", "exclude_defaults", "exclude_none"},
    ),
    "json": (
        "model_dump_json",
        {"include", "exclude", "by_alias", "exclude_unset", "exclude_defaults", "exclude_none", "indent"},
    ),
    "copy": ("model_copy", {"update", "deep"}),
}
CLASS_METHODS = {
    "parse_obj": ("model_validate", set()),
    "validate": ("model_validate", set()),
    "parse_raw": ("model_validate_json", set()),
    "construct": ("model_construct", None),  # any kwargs allowed
    "schema": ("model_json_schema", {"by_alias", "ref_template"}),
    "update_forward_refs": ("model_rebuild", set()),
}
PYDANTIC_SPECIFIC_METHODS = {"parse_obj", "parse_raw", "parse_file", "from_orm", "update_forward_refs", "schema_json"}
CONSTRUCTOR_METHODS = {
    "parse_obj",
    "parse_raw",
    "parse_file",
    "from_orm",
    "construct",
    "validate",
    "model_validate",
    "model_validate_json",
    "model_construct",
}
COPY_METHODS = {"copy", "model_copy"}

UNSUPPORTED_IMPORTS = {
    "pydantic.json": "pydantic.json helpers were removed; use pydantic_core.to_jsonable_python",
    "pydantic.typing": "pydantic.typing was removed in v2",
    "pydantic.utils": "pydantic.utils was removed in v2",
    "pydantic.error_wrappers": "pydantic.error_wrappers was removed in v2 (import ValidationError from pydantic)",
    "pydantic.class_validators": "pydantic.class_validators was removed in v2",
}

RULE_SEVERITY = {
    "PYD101": Severity.ERROR,
    "PYD102": Severity.ERROR,
    "PYD110": Severity.WARNING,
    "PYD111": Severity.WARNING,
    "PYD112": Severity.WARNING,
    "PYD120": Severity.WARNING,
    "PYD121": Severity.WARNING,
    "PYD130": Severity.WARNING,
    "PYD140": Severity.WARNING,
    "PYD141": Severity.WARNING,
    "PYD142": Severity.WARNING,
    "PYD143": Severity.WARNING,
    "PYD150": Severity.WARNING,
    "PYD151": Severity.WARNING,
    "PYD160": Severity.ERROR,
    "PYD170": Severity.WARNING,
    "PYD171": Severity.WARNING,
}

RULE_DOCS = {
    "PYD101": "BaseSettings moved to pydantic-settings",
    "PYD102": "GenericModel removed; use BaseModel + typing.Generic",
    "PYD110": "@validator -> @field_validator",
    "PYD111": "validator 'values'/'field' parameters -> ValidationInfo",
    "PYD112": "validator always=True -> Field(validate_default=True)",
    "PYD120": "@root_validator(pre=True) -> @model_validator(mode='before')",
    "PYD121": "@root_validator -> @model_validator(mode='after')",
    "PYD130": "class Config -> model_config = ConfigDict(...)",
    "PYD140": "instance method renamed (dict/json/copy)",
    "PYD141": "class method renamed (parse_obj/parse_raw/from_orm/schema/...)",
    "PYD142": "__fields__/__fields_set__ -> model_fields/model_fields_set",
    "PYD143": "overridden dict()/json() -> model_dump()/model_dump_json()",
    "PYD150": "Field() keyword arguments renamed / moved",
    "PYD151": "constrained type keyword arguments renamed",
    "PYD160": "Optional field without default is required in v2 (add = None)",
    "PYD170": "parse_obj_as -> TypeAdapter",
    "PYD171": "validate_arguments -> validate_call",
    "PYD201": "validator needs manual migration",
    "PYD202": "root_validator needs manual migration",
    "PYD203": "Config option needs manual migration",
    "PYD204": "custom root type (__root__) -> RootModel",
    "PYD205": "method call uses v1-only arguments",
    "PYD206": "pydantic v1 method on an object of unknown type",
    "PYD207": "Field()/con*() argument without v2 equivalent",
    "PYD208": "import of a module removed in v2",
    "PYD209": "pydantic.v1 compatibility shim in use",
    "PYD210": "__fields__ / ModelField API needs manual migration",
    "PYD299": "possible v1 .dict()/.json() call on an object of unknown type",
}


def _code(node: cst.CSTNode) -> str:
    return cst.Module([]).code_for_node(node)


def _string_value(node: cst.BaseExpression) -> str | None:
    if isinstance(node, cst.SimpleString):
        try:
            value = node.evaluated_value
        except Exception:  # pragma: no cover - defensive
            return None
        return value if isinstance(value, str) else None
    return None


@dataclass
class _ClassCtx:
    name: str
    is_model: bool
    is_settings: bool
    func_depth: int
    always_fields: dict[str, int] = field(default_factory=dict)


@dataclass
class _FuncCtx:
    instances: dict[str, str] = field(default_factory=dict)  # var -> model name (instance)
    classes: dict[str, str] = field(default_factory=dict)  # var -> model name (class object)
    iterables: dict[str, str] = field(default_factory=dict)  # var -> model name (items)


@dataclass
class CodemodReport:
    path: str
    changes: list[AppliedChange] = field(default_factory=list)
    unresolved: list[Finding] = field(default_factory=list)
    infos: list[Finding] = field(default_factory=list)
    needs_settings_dependency: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    def findings(self, include_info: bool = True) -> list[Finding]:
        out = [
            Finding(
                rule=c.rule,
                path=c.path,
                line=c.line,
                message=c.description,
                severity=RULE_SEVERITY.get(c.rule, Severity.WARNING),
                auto_fixable=True,
            )
            for c in self.changes
        ]
        out.extend(self.unresolved)
        if include_info:
            out.extend(self.infos)
        return sorted(out, key=lambda f: (f.path, f.line, f.rule))


class PydanticV2Codemod(VisitorBasedCodemodCommand):
    DESCRIPTION = "Migrate Pydantic v1 code to Pydantic v2"
    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, context: CodemodContext, index: ProjectIndex, relpath: str, source_lines: list[str]):
        super().__init__(context)
        self.index = index
        self.relpath = relpath
        self.lines = source_lines
        self.report = CodemodReport(relpath)
        info = index.modules.get(relpath)
        self.aliases: dict[str, str] = {}
        if info:
            self.aliases = dict(info.pydantic_aliases)
            self.pyd_modules = set(info.pydantic_modules)
        else:
            self.pyd_modules = set()
        self.model_names = index.model_names
        self.settings_names = index.settings_names()
        self.local_models = {c.name: c for c in (info.classes if info else []) if c.is_model}
        self.class_stack: list[_ClassCtx] = []
        self.func_stack: list[_FuncCtx] = [_FuncCtx()]  # module scope at index 0
        self._anchors: list[cst.CSTNode] = []

    # ----------------------------------------------------------------- reporting
    def _line(self, node: cst.CSTNode) -> int:
        try:
            return self.get_metadata(PositionProvider, node).start.line
        except Exception:
            # nodes from the updated tree carry no metadata: fall back to the
            # enclosing original statement being processed
            for anchor in reversed(self._anchors):
                try:
                    return self.get_metadata(PositionProvider, anchor).start.line
                except Exception:
                    continue
            return 0

    def _snippet(self, line: int) -> str:
        if 0 < line <= len(self.lines):
            return self.lines[line - 1].strip()[:200]
        return ""

    def _change(self, rule: str, node: cst.CSTNode, description: str) -> None:
        self.report.changes.append(AppliedChange(rule=rule, path=self.relpath, line=self._line(node), description=description))

    def _unresolved(self, rule: str, node: cst.CSTNode, message: str, severity: Severity = Severity.ERROR) -> None:
        line = self._line(node)
        target = self.report.infos if severity is Severity.INFO else self.report.unresolved
        target.append(
            Finding(
                rule=rule,
                path=self.relpath,
                line=line,
                message=message,
                severity=severity,
                auto_fixable=False,
                snippet=self._snippet(line),
            )
        )

    # ----------------------------------------------------------------- resolution helpers
    def _dotted(self, expr: cst.BaseExpression) -> str | None:
        if isinstance(expr, cst.Name):
            return expr.value
        if isinstance(expr, cst.Attribute):
            head = self._dotted(expr.value)
            return f"{head}.{expr.attr.value}" if head else None
        return None

    def _canon(self, expr: cst.BaseExpression) -> str | None:
        dotted = self._dotted(expr)
        if not dotted:
            return None
        head, _, tail = dotted.partition(".")
        if head in self.pyd_modules:
            base = self.aliases.get(head, "pydantic")
            return f"{base}.{tail}" if tail else base
        if head in self.aliases and not tail:
            return self.aliases[head]
        return None

    def _canon_name(self, expr: cst.BaseExpression) -> str | None:
        """Canonical pydantic object name with submodule paths collapsed (pydantic.X)."""
        canonical = self._canon(expr)
        if not canonical:
            return None
        parts = canonical.split(".")
        if parts[0] == "pydantic" and len(parts) > 2 and parts[1] not in ("v1",):
            return f"pydantic.{parts[-1]}"
        return canonical

    def _pyd_ref(self, original: cst.BaseExpression, new_name: str, module: str = "pydantic") -> cst.BaseExpression:
        """Reference ``module.new_name`` in the same style as ``original``."""
        if isinstance(original, cst.Attribute) and module == "pydantic":
            return original.with_changes(attr=cst.Name(new_name))
        AddImportsVisitor.add_needed_import(self.context, module, new_name)
        return cst.Name(new_name)

    def _model_class_ref(self, expr: cst.BaseExpression) -> str | None:
        """Model name if ``expr`` evaluates to a model *class*."""
        if isinstance(expr, cst.Name):
            for scope in reversed(self.func_stack):
                if expr.value in scope.classes:
                    return scope.classes[expr.value]
                if expr.value in scope.instances:
                    return None
            return expr.value if expr.value in self.model_names else None
        if isinstance(expr, cst.Attribute) and expr.attr.value in self.model_names:
            # module.Model (but not instance.attr where attr happens to be a model name)
            if self._infer_instance(expr.value) is None:
                return expr.attr.value
        return None

    def _infer_instance(self, expr: cst.BaseExpression) -> str | None:
        """Model name if ``expr`` evaluates to a model *instance*."""
        if isinstance(expr, cst.Name):
            for scope in reversed(self.func_stack):
                if expr.value in scope.instances:
                    return scope.instances[expr.value]
                if expr.value in scope.classes or expr.value in scope.iterables:
                    return None
            return None
        if isinstance(expr, cst.Attribute):
            owner = self._infer_instance(expr.value)
            if owner:
                model = self.index.model(owner)
                if model and expr.attr.value in model.fields:
                    return model.fields[expr.attr.value].model_ref
            return None
        if isinstance(expr, cst.Call):
            cls_name = self._model_class_ref(expr.func)
            if cls_name:
                return cls_name
            if isinstance(expr.func, cst.Name) and not self._shadowed(expr.func.value):
                returned, _ = self.index.function_returns(expr.func.value)
                if returned:
                    return returned
            if isinstance(expr.func, cst.Attribute):
                method = expr.func.attr.value
                if method in CONSTRUCTOR_METHODS:
                    return self._model_class_ref(expr.func.value)
                if method in COPY_METHODS:
                    return self._infer_instance(expr.func.value)
            return None
        if isinstance(expr, cst.Subscript):
            return self._infer_iterable(expr.value)
        return None

    def _shadowed(self, name: str) -> bool:
        return any(name in s.instances or name in s.classes or name in s.iterables for s in self.func_stack[1:])

    def _infer_iterable(self, expr: cst.BaseExpression) -> str | None:
        if isinstance(expr, cst.Call) and isinstance(expr.func, cst.Name) and not self._shadowed(expr.func.value):
            _, items = self.index.function_returns(expr.func.value)
            return items
        if isinstance(expr, cst.Name):
            for scope in reversed(self.func_stack):
                if expr.value in scope.iterables:
                    return scope.iterables[expr.value]
            return None
        if isinstance(expr, cst.Attribute):
            owner = self._infer_instance(expr.value)
            if owner:
                model = self.index.model(owner)
                if model and expr.attr.value in model.fields:
                    return model.fields[expr.attr.value].item_model_ref
        return None

    def _annotation_refs(self, annotation: cst.Annotation | None) -> tuple[str | None, str | None]:
        if annotation is None:
            return None, None
        return annotation_model_refs(_code(annotation.annotation), self.model_names)

    @property
    def _cls(self) -> _ClassCtx | None:
        return self.class_stack[-1] if self.class_stack else None

    def _in_model_method(self) -> bool:
        ctx = self._cls
        return bool(ctx and ctx.is_model and len(self.func_stack) >= ctx.func_depth + 1)

    # ----------------------------------------------------------------- imports
    def leave_ImportFrom(self, original: cst.ImportFrom, updated: cst.ImportFrom):
        if updated.module is None or isinstance(updated.names, cst.ImportStar):
            return updated
        module = _code(updated.module)
        if module in UNSUPPORTED_IMPORTS:
            self._unresolved("PYD208", original, UNSUPPORTED_IMPORTS[module])
            return updated
        if module == "pydantic.v1" or module.startswith("pydantic.v1."):
            self._unresolved("PYD209", original, "pydantic.v1 compatibility layer is not a migration", Severity.WARNING)
            return updated
        if module == "pydantic.generics":
            keep = [a for a in updated.names if _code(a.name) != "GenericModel"]
            if len(keep) != len(updated.names):
                AddImportsVisitor.add_needed_import(self.context, "pydantic", "BaseModel")
                self._change("PYD102", original, "removed pydantic.generics.GenericModel import")
                if not keep:
                    return cst.RemoveFromParent()
                return updated.with_changes(names=self._fix_trailing_comma(keep))
            return updated
        if module in ("pydantic", "pydantic.env_settings"):
            keep = []
            relocated = False
            for alias in updated.names:
                name = _code(alias.name)
                if name == "BaseSettings":
                    relocated = True
                    if alias.asname:
                        AddImportsVisitor.add_needed_import(
                            self.context, "pydantic_settings", "BaseSettings", _code(alias.asname.name)
                        )
                    else:
                        AddImportsVisitor.add_needed_import(self.context, "pydantic_settings", "BaseSettings")
                elif name == "GenericModel":
                    relocated = True
                    AddImportsVisitor.add_needed_import(self.context, "pydantic", "BaseModel")
                else:
                    keep.append(alias)
            if relocated:
                self.report.needs_settings_dependency = self.report.needs_settings_dependency or any(
                    _code(a.name) == "BaseSettings" for a in updated.names
                )
                self._change("PYD101", original, "moved BaseSettings import to pydantic_settings")
                if not keep:
                    return cst.RemoveFromParent()
                return updated.with_changes(names=self._fix_trailing_comma(keep))
        return updated

    @staticmethod
    def _fix_trailing_comma(aliases: list[cst.ImportAlias]) -> list[cst.ImportAlias]:
        if aliases:
            aliases[-1] = aliases[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)
        return aliases

    def leave_Import(self, original: cst.Import, updated: cst.Import):
        for alias in updated.names:
            name = _code(alias.name)
            if name == "pydantic.v1" or name.startswith("pydantic.v1."):
                self._unresolved("PYD209", original, "pydantic.v1 compatibility layer is not a migration", Severity.WARNING)
            elif name in UNSUPPORTED_IMPORTS:
                self._unresolved("PYD208", original, UNSUPPORTED_IMPORTS[name])
        return updated

    # ----------------------------------------------------------------- classes
    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        local = self.local_models.get(node.name.value) if not self.class_stack else None
        is_model = local is not None
        self.class_stack.append(
            _ClassCtx(
                name=node.name.value,
                is_model=is_model,
                is_settings=bool(local and local.kind == "settings"),
                func_depth=len(self.func_stack),
            )
        )

    def leave_ClassDef(self, original: cst.ClassDef, updated: cst.ClassDef):
        self._anchors.append(original)
        try:
            return self._leave_class(original, updated)
        finally:
            self._anchors.pop()

    def _leave_class(self, original: cst.ClassDef, updated: cst.ClassDef):
        ctx = self.class_stack.pop()
        if not ctx.is_model or not isinstance(updated.body, cst.IndentedBlock):
            return updated
        new_body: list[cst.BaseStatement] = []
        seen_fields: set[str] = set()
        for stmt in updated.body.body:
            if isinstance(stmt, cst.ClassDef) and stmt.name.value == "Config":
                new_body.append(self._convert_config(stmt, ctx))
                continue
            if (
                isinstance(stmt, cst.SimpleStatementLine)
                and len(stmt.body) == 1
                and isinstance(stmt.body[0], cst.AnnAssign)
                and isinstance(stmt.body[0].target, cst.Name)
            ):
                fname = stmt.body[0].target.value
                seen_fields.add(fname)
                if fname == "__root__":
                    self._unresolved(
                        "PYD204",
                        stmt,
                        f"{ctx.name}: custom root type '__root__' must become a RootModel subclass",
                    )
                    new_body.append(stmt)
                    continue
                new_body.append(self._fix_field(stmt, ctx))
                continue
            new_body.append(stmt)
        for fname in ctx.always_fields:
            if fname not in seen_fields and fname != "*":
                self._unresolved(
                    "PYD201",
                    original,
                    f"{ctx.name}: validator(always=True) targets inherited field '{fname}'; "
                    "set validate_default=True on that field",
                )
        bases = []
        for arg in updated.bases:
            canonical = self._canon_name(arg.value)
            if canonical in ("pydantic.GenericModel", "pydantic.generics.GenericModel"):
                bases.append(arg.with_changes(value=self._pyd_ref(arg.value, "BaseModel")))
                self._change("PYD102", arg, f"{ctx.name}: GenericModel base -> BaseModel")
            elif canonical in ("pydantic.BaseSettings",) and isinstance(arg.value, cst.Attribute):
                AddImportsVisitor.add_needed_import(self.context, "pydantic_settings")
                bases.append(arg.with_changes(value=cst.parse_expression("pydantic_settings.BaseSettings")))
                self.report.needs_settings_dependency = True
                self._change("PYD101", arg, f"{ctx.name}: pydantic.BaseSettings -> pydantic_settings.BaseSettings")
            else:
                bases.append(arg)
        return updated.with_changes(body=updated.body.with_changes(body=new_body), bases=bases)

    def _fix_field(self, line: cst.SimpleStatementLine, ctx: _ClassCtx) -> cst.SimpleStatementLine:
        ann: cst.AnnAssign = line.body[0]  # type: ignore[assignment]
        name = ann.target.value  # type: ignore[union-attr]
        if name.startswith("_") or self._is_classvar(ann.annotation.annotation):
            return line
        value = ann.value
        optional = self._is_optional(ann.annotation.annotation)
        if optional and value is None:
            value = cst.Name("None")
            self._change("PYD160", line, f"{ctx.name}.{name}: Optional field gets explicit default None")
        elif optional and isinstance(value, cst.Call) and self._canon_name(value.func) == "pydantic.Field":
            has_default = any(a.keyword is None and a.star == "" for a in value.args) or any(
                a.keyword and a.keyword.value in ("default", "default_factory") for a in value.args
            )
            if not has_default:
                value = value.with_changes(args=[cst.Arg(cst.Name("None")), *value.args])
                self._change("PYD160", line, f"{ctx.name}.{name}: Optional Field() gets explicit default None")
        if name in ctx.always_fields or ("*" in ctx.always_fields):
            if value is None:
                pass  # required field: validator always runs on provided values anyway
            elif isinstance(value, cst.Call) and self._canon_name(value.func) == "pydantic.Field":
                if not any(a.keyword and a.keyword.value == "validate_default" for a in value.args):
                    args = list(value.args)
                    if args:
                        args[-1] = args[-1].with_changes(comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" ")))
                    args.append(cst.Arg(keyword=cst.Name("validate_default"), value=cst.Name("True"), equal=_EQ))
                    value = value.with_changes(args=args)
                    self._change("PYD112", line, f"{ctx.name}.{name}: validate_default=True (validator always=True)")
            else:
                field_ref = self._field_ref()
                value = cst.Call(
                    func=field_ref,
                    args=[
                        cst.Arg(value, comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))),
                        cst.Arg(keyword=cst.Name("validate_default"), value=cst.Name("True"), equal=_EQ),
                    ],
                )
                self._change("PYD112", line, f"{ctx.name}.{name}: wrapped default in Field(validate_default=True)")
        if value is ann.value:
            return line
        new_ann = ann.with_changes(
            value=value,
            equal=ann.equal if ann.value is not None else cst.AssignEqual(),
        )
        return line.with_changes(body=[new_ann])

    def _field_ref(self) -> cst.BaseExpression:
        for local, canonical in self.aliases.items():
            if canonical == "pydantic.Field":
                return cst.Name(local)
        if self.pyd_modules:
            mod = sorted(self.pyd_modules)[0]
            return cst.Attribute(cst.Name(mod), cst.Name("Field"))
        AddImportsVisitor.add_needed_import(self.context, "pydantic", "Field")
        return cst.Name("Field")

    def _is_classvar(self, expr: cst.BaseExpression) -> bool:
        target = expr.value if isinstance(expr, cst.Subscript) else expr
        dotted = self._dotted(target) or ""
        return dotted.rsplit(".", 1)[-1] == "ClassVar"

    def _is_optional(self, expr: cst.BaseExpression) -> bool:
        if isinstance(expr, (cst.SimpleString, cst.ConcatenatedString)):
            try:
                inner = cst.parse_expression(expr.evaluated_value if isinstance(expr, cst.SimpleString) else "")
            except Exception:
                return False
            return self._is_optional(inner)
        dotted = self._dotted(expr)
        if dotted and dotted.rsplit(".", 1)[-1] in ("Any", "Optional"):
            return True
        if isinstance(expr, cst.BinaryOperation) and isinstance(expr.operator, cst.BitOr):
            return self._is_none(expr.left) or self._is_none(expr.right) or self._is_optional(expr.left) or self._is_optional(expr.right)
        if isinstance(expr, cst.Subscript):
            head = (self._dotted(expr.value) or "").rsplit(".", 1)[-1]
            elems = [e.slice.value for e in expr.slice if isinstance(e.slice, cst.Index)]
            if head == "Optional":
                return True
            if head == "Union":
                return any(self._is_none(e) for e in elems)
            if head == "Annotated" and elems:
                return self._is_optional(elems[0])
        return False

    @staticmethod
    def _is_none(expr: cst.BaseExpression) -> bool:
        return isinstance(expr, cst.Name) and expr.value == "None"

    # ----------------------------------------------------------------- Config
    def _convert_config(self, config: cst.ClassDef, ctx: _ClassCtx) -> cst.BaseStatement:
        entries: list[tuple[str, str]] = []
        problems: list[str] = []
        body = config.body.body if isinstance(config.body, cst.IndentedBlock) else []
        for stmt in body:
            if isinstance(stmt, cst.SimpleStatementLine):
                for small in stmt.body:
                    if isinstance(small, cst.Pass) or (
                        isinstance(small, cst.Expr) and isinstance(small.value, (cst.SimpleString, cst.ConcatenatedString))
                    ):
                        continue
                    if (
                        isinstance(small, cst.Assign)
                        and len(small.targets) == 1
                        and isinstance(small.targets[0].target, cst.Name)
                    ):
                        key = small.targets[0].target.value
                        value_code = _code(small.value)
                        result = self._config_entry(key, small.value, value_code, ctx)
                        if result is None:
                            problems.append(key)
                        elif result:
                            entries.append(result)
                        continue
                    problems.append(_code(small).split("=")[0].strip()[:40])
            else:
                problems.append("non-assignment statement")
        if problems:
            self._unresolved(
                "PYD203",
                config,
                f"{ctx.name}.Config uses options without a mechanical v2 equivalent: {', '.join(problems)}",
            )
            return config
        cfg_cls = "SettingsConfigDict" if ctx.is_settings else "ConfigDict"
        module = "pydantic_settings" if ctx.is_settings else "pydantic"
        AddImportsVisitor.add_needed_import(self.context, module, cfg_cls)
        if ctx.is_settings:
            self.report.needs_settings_dependency = True
        args = ", ".join(f"{k}={v}" for k, v in entries)
        stmt = cst.parse_statement(f"model_config = {cfg_cls}({args})\n")
        self._change("PYD130", config, f"{ctx.name}: class Config -> model_config = {cfg_cls}(...)")
        return stmt.with_changes(leading_lines=config.leading_lines)

    def _config_entry(self, key: str, value: cst.BaseExpression, code: str, ctx: _ClassCtx) -> tuple[str, str] | tuple[()] | None:
        if key in CONFIG_DROP:
            return ()
        if key == "allow_mutation":
            if code == "False":
                return ("frozen", "True")
            if code == "True":
                return ()
            return None
        if key == "extra":
            canonical = self._canon_name(value.value) if isinstance(value, cst.Attribute) else None
            if isinstance(value, cst.Attribute) and (
                canonical == "pydantic.Extra" or (self._dotted(value.value) or "").endswith("Extra")
            ):
                RemoveImportsVisitor.remove_unused_import(self.context, "pydantic", "Extra")
                return ("extra", repr(value.attr.value))
            if _string_value(value) is not None:
                return ("extra", code)
            return None
        if key in CONFIG_RENAMES:
            return (CONFIG_RENAMES[key], code)
        if key in CONFIG_SAME:
            return (key, code)
        if ctx.is_settings and key in SETTINGS_CONFIG_KEYS:
            return (key, code)
        return None

    # ----------------------------------------------------------------- functions / decorators
    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        scope = _FuncCtx()
        ctx = self._cls
        direct_method = bool(ctx and len(self.func_stack) == ctx.func_depth)
        params = list(node.params.posonly_params) + list(node.params.params)
        decorator_names = {_decorator_name(d) for d in node.decorators}
        if direct_method and ctx and ctx.is_model and params:
            first = params[0].name.value
            if decorator_names & {"classmethod", "validator", "root_validator", "field_validator", "model_validator"}:
                scope.classes[first] = ctx.name
            elif "staticmethod" not in decorator_names:
                scope.instances[first] = ctx.name
            params = params[1:]
        for p in params + list(node.params.kwonly_params):
            direct, item = self._annotation_refs(p.annotation)
            if direct:
                ann_code = _code(p.annotation.annotation) if p.annotation else ""
                if ann_code.startswith(("Type[", "type[", "typing.Type[")):
                    scope.classes[p.name.value] = direct
                else:
                    scope.instances[p.name.value] = direct
            elif item:
                scope.iterables[p.name.value] = item
        self.func_stack.append(scope)

    def visit_Assign(self, node: cst.Assign) -> None:
        if self._cls and len(self.func_stack) == self._cls.func_depth:
            return  # class attribute, not a variable
        if len(node.targets) != 1 or not isinstance(node.targets[0].target, cst.Name):
            return
        name = node.targets[0].target.value
        scope = self.func_stack[-1]
        inst = self._infer_instance(node.value)
        if inst:
            scope.instances[name] = inst
            return
        cls_ref = self._model_class_ref(node.value) if isinstance(node.value, (cst.Name, cst.Attribute)) else None
        if cls_ref:
            scope.classes[name] = cls_ref
            return
        if isinstance(node.value, (cst.List, cst.ListComp)):
            elem = None
            if isinstance(node.value, cst.ListComp):
                elem = self._infer_instance(node.value.elt)
            elif node.value.elements:
                elem = self._infer_instance(node.value.elements[0].value)
            if elem:
                scope.iterables[name] = elem
                return
        for mapping in (scope.instances, scope.classes, scope.iterables):
            mapping.pop(name, None)

    def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
        if self._cls and self._cls.is_model and len(self.func_stack) == self._cls.func_depth:
            return  # model field declaration, not a variable
        if not isinstance(node.target, cst.Name):
            return
        direct, item = self._annotation_refs(node.annotation)
        scope = self.func_stack[-1]
        if direct:
            scope.instances[node.target.value] = direct
        elif item:
            scope.iterables[node.target.value] = item

    def _register_comprehension(self, comp: cst.CompFor | None) -> None:
        # the element expression is visited before the `for` clause, so targets are
        # registered up-front when entering the comprehension
        while comp is not None:
            if isinstance(comp.target, cst.Name):
                item = self._infer_iterable(comp.iter)
                if item:
                    self.func_stack[-1].instances[comp.target.value] = item
            comp = comp.inner_for_in

    def visit_ListComp(self, node: cst.ListComp) -> None:
        self._register_comprehension(node.for_in)

    def visit_SetComp(self, node: cst.SetComp) -> None:
        self._register_comprehension(node.for_in)

    def visit_GeneratorExp(self, node: cst.GeneratorExp) -> None:
        self._register_comprehension(node.for_in)

    def visit_DictComp(self, node: cst.DictComp) -> None:
        self._register_comprehension(node.for_in)

    def visit_For(self, node: cst.For) -> None:
        if isinstance(node.target, cst.Name):
            item = self._infer_iterable(node.iter)
            if item:
                self.func_stack[-1].instances[node.target.value] = item

    def leave_FunctionDef(self, original: cst.FunctionDef, updated: cst.FunctionDef):
        self.func_stack.pop()
        self._anchors.append(original)
        try:
            return self._leave_function(original, updated)
        finally:
            self._anchors.pop()

    def _leave_function(self, original: cst.FunctionDef, updated: cst.FunctionDef):
        ctx = self._cls
        direct_method = bool(ctx and len(self.func_stack) == ctx.func_depth)
        new_decorators: list[cst.Decorator] = []
        add_classmethod = False
        func = updated
        for dec in updated.decorators:
            expr = dec.decorator
            call = expr if isinstance(expr, cst.Call) else None
            canonical = self._canon_name(call.func if call else expr)
            if canonical == "pydantic.validator":
                converted = self._convert_validator(dec, call, func, ctx)
                if converted is None:
                    new_decorators.append(dec)
                    continue
                new_dec, func = converted
                new_decorators.append(new_dec)
                add_classmethod = True
            elif canonical == "pydantic.root_validator":
                converted = self._convert_root_validator(dec, call, func, ctx)
                if converted is None:
                    new_decorators.append(dec)
                    continue
                new_dec, func, needs_cm = converted
                new_decorators.append(new_dec)
                add_classmethod = add_classmethod or needs_cm
            elif canonical == "pydantic.validate_arguments":
                target = call.func if call else expr
                new_ref = self._pyd_ref(target, "validate_call")
                new_decorators.append(dec.with_changes(decorator=call.with_changes(func=new_ref) if call else new_ref))
                self._change("PYD171", dec, "validate_arguments -> validate_call")
            else:
                new_decorators.append(dec)
        if add_classmethod and not any(self._dotted(d.decorator) == "classmethod" for d in new_decorators):
            new_decorators.append(cst.Decorator(decorator=cst.Name("classmethod")))
        func = func.with_changes(decorators=new_decorators)
        if direct_method and ctx and ctx.is_model and func.name.value in ("dict", "json"):
            new_name = "model_dump" if func.name.value == "dict" else "model_dump_json"
            self._change("PYD143", original, f"{ctx.name}.{func.name.value}() override -> {new_name}()")
            func = func.with_changes(name=cst.Name(new_name))
        return func

    def _convert_validator(self, dec, call, func: cst.FunctionDef, ctx: _ClassCtx | None):
        if call is None:
            self._unresolved("PYD201", dec, "bare @validator without field names")
            return None
        fields: list[cst.Arg] = []
        mode = None
        always = False
        check_fields = None
        for arg in call.args:
            if arg.keyword is None:
                if arg.star:
                    self._unresolved("PYD201", dec, "validator with *args field list needs manual migration")
                    return None
                fields.append(arg)
                continue
            kw = arg.keyword.value
            val = _code(arg.value)
            if kw == "pre":
                mode = "before" if val == "True" else None
            elif kw == "always":
                always = val == "True"
            elif kw == "allow_reuse":
                continue
            elif kw == "check_fields":
                check_fields = arg
            elif kw == "each_item" and val == "False":
                continue
            elif kw == "each_item":
                self._unresolved(
                    "PYD201",
                    dec,
                    f"{func.name.value}: validator(each_item=True) has no direct v2 equivalent "
                    "(validate the container or use Annotated[..., AfterValidator(...)] on the item type)",
                )
                return None
            else:
                self._unresolved("PYD201", dec, f"{func.name.value}: unsupported validator argument {kw}={val}")
                return None
        field_names = [_string_value(a.value) for a in fields]
        if always:
            if ctx is None or not ctx.is_model or any(n is None for n in field_names):
                self._unresolved("PYD201", dec, f"{func.name.value}: validator(always=True) needs validate_default on the field")
                return None
        new_func = self._convert_validator_signature(func, dec)
        if new_func is None:
            return None
        if always and ctx:
            for name in field_names:
                ctx.always_fields[name or "*"] = self._line(dec)
        args = [a.with_changes(comma=cst.MaybeSentinel.DEFAULT) for a in fields]
        if mode:
            args.append(cst.Arg(keyword=cst.Name("mode"), value=cst.SimpleString(f'"{mode}"'), equal=_EQ))
        if check_fields is not None:
            args.append(check_fields.with_changes(comma=cst.MaybeSentinel.DEFAULT))
        args = _with_commas(args)
        new_call = call.with_changes(func=self._pyd_ref(call.func, "field_validator"), args=args)
        RemoveImportsVisitor.remove_unused_import(self.context, "pydantic", "validator")
        self._change("PYD110", dec, f"{func.name.value}: @validator -> @field_validator" + (f"(mode='{mode}')" if mode else ""))
        return dec.with_changes(decorator=new_call), new_func

    def _convert_validator_signature(self, func: cst.FunctionDef, dec) -> cst.FunctionDef | None:
        params = list(func.params.params)
        if func.params.star_kwarg is not None or isinstance(func.params.star_arg, cst.Param):
            self._unresolved("PYD201", dec, f"{func.name.value}: validator with *args/**kwargs signature needs manual migration")
            return None
        extra = params[2:] + list(func.params.kwonly_params)
        names = [p.name.value for p in extra]
        if not names:
            return func
        if not set(names) <= {"values", "config", "field"}:
            self._unresolved("PYD201", dec, f"{func.name.value}: unexpected validator parameters {names}")
            return None
        body = func.body
        uses = {n: bool(count_name_uses(body, n)) for n in names}
        if uses.get("config"):
            self._unresolved("PYD201", dec, f"{func.name.value}: validator uses the 'config' parameter")
            return None
        if "values" in names and m.findall(body, m.AssignTarget(target=m.Name("values")) | m.AugAssign(target=m.Name("values"))):
            self._unresolved("PYD201", dec, f"{func.name.value}: validator rebinds 'values'")
            return None
        if uses.get("field"):
            other_field_uses = count_name_uses(body, "field") - len(
                m.findall(body, m.Attribute(value=m.Name("field"), attr=m.Name("name")))
            )
            if other_field_uses:
                self._unresolved("PYD201", dec, f"{func.name.value}: validator uses ModelField attributes other than .name")
                return None
        needs_info = uses.get("values") or uses.get("field")
        body = body.visit(_ValidatorBodyRewriter(_protected_ids(body)))
        new_params = params[:2]
        if needs_info:
            AddImportsVisitor.add_needed_import(self.context, "pydantic", "ValidationInfo")
            new_params.append(cst.Param(name=cst.Name("info"), annotation=cst.Annotation(cst.Name("ValidationInfo"))))
            self._change("PYD111", dec, f"{func.name.value}: 'values'/'field' parameters -> info: ValidationInfo")
        new_params = [p.with_changes(comma=cst.MaybeSentinel.DEFAULT) for p in new_params]
        return func.with_changes(
            params=func.params.with_changes(params=new_params, kwonly_params=[], star_arg=cst.MaybeSentinel.DEFAULT),
            body=body,
        )

    def _convert_root_validator(self, dec, call, func: cst.FunctionDef, ctx: _ClassCtx | None):
        pre = False
        if call is not None:
            for arg in call.args:
                kw = arg.keyword.value if arg.keyword else None
                val = _code(arg.value)
                if kw == "pre":
                    pre = val == "True"
                elif kw in ("allow_reuse", "skip_on_failure"):
                    continue
                else:
                    self._unresolved("PYD202", dec, f"{func.name.value}: unsupported root_validator argument {_code(arg)}")
                    return None
        func_ref = call.func if call else dec.decorator
        if pre:
            new_call = cst.Call(
                func=self._pyd_ref(func_ref, "model_validator"),
                args=[cst.Arg(keyword=cst.Name("mode"), value=cst.SimpleString('"before"'), equal=_EQ)],
            )
            RemoveImportsVisitor.remove_unused_import(self.context, "pydantic", "root_validator")
            self._change("PYD120", dec, f"{func.name.value}: @root_validator(pre=True) -> @model_validator(mode='before')")
            return dec.with_changes(decorator=new_call), func, True
        # post root validator: only the "read-only values" shape is converted mechanically
        params = list(func.params.params)
        if len(params) != 2 or func.params.kwonly_params or func.params.star_kwarg is not None:
            self._unresolved("PYD202", dec, f"{func.name.value}: root_validator signature needs manual migration")
            return None
        values_name = params[1].name.value
        cls_name = params[0].name.value
        rewriter = _PostRootRewriter(values_name, cls_name, _protected_ids(func.body))
        new_body = func.body.visit(rewriter)
        if rewriter.unsafe or count_name_uses(new_body, values_name):
            self._unresolved(
                "PYD202",
                dec,
                f"{func.name.value}: post root_validator mutates or iterates '{values_name}'; rewrite as "
                "@model_validator(mode='after') operating on self (or mode='before' on the raw dict)",
            )
            return None
        new_call = cst.Call(
            func=self._pyd_ref(func_ref, "model_validator"),
            args=[cst.Arg(keyword=cst.Name("mode"), value=cst.SimpleString('"after"'), equal=_EQ)],
        )
        RemoveImportsVisitor.remove_unused_import(self.context, "pydantic", "root_validator")
        new_params = func.params.with_changes(params=[params[0].with_changes(name=cst.Name("self"), comma=cst.MaybeSentinel.DEFAULT)])
        self._change("PYD121", dec, f"{func.name.value}: @root_validator -> @model_validator(mode='after') on self")
        return dec.with_changes(decorator=new_call), func.with_changes(params=new_params, body=new_body), False

    # ----------------------------------------------------------------- calls & attributes
    def leave_Call(self, original: cst.Call, updated: cst.Call):
        canonical = self._canon_name(updated.func)
        if canonical == "pydantic.Field":
            return self._convert_field_call(original, updated)
        if canonical and canonical.split(".")[-1] in CON_RENAMES and canonical.startswith("pydantic."):
            return self._convert_con_call(original, updated, canonical.split(".")[-1])
        if canonical in ("pydantic.parse_obj_as", "pydantic.parse_raw_as"):
            return self._convert_parse_obj_as(original, updated, canonical)
        if isinstance(updated.func, cst.Attribute):
            return self._convert_method_call(original, updated)
        return updated

    def _convert_field_call(self, original: cst.Call, updated: cst.Call):
        structural = False
        args = []
        extra: list[tuple[str, str]] = []
        changed = []
        for arg in updated.args:
            if arg.keyword is None:
                args.append(arg)
                continue
            kw = arg.keyword.value
            val = _code(arg.value)
            if kw in FIELD_RENAMES:
                args.append(arg.with_changes(keyword=cst.Name(FIELD_RENAMES[kw])))
                changed.append(f"{kw}->{FIELD_RENAMES[kw]}")
            elif kw == "allow_mutation":
                if val == "False":
                    args.append(arg.with_changes(keyword=cst.Name("frozen"), value=cst.Name("True")))
                    changed.append("allow_mutation=False->frozen=True")
                else:
                    changed.append("dropped allow_mutation=True")
                    structural = True
            elif kw == "env":
                ctx = self._cls
                if not (ctx and ctx.is_settings):
                    self._unresolved("PYD207", original, "Field(env=...) outside a settings class")
                    return updated
                if isinstance(arg.value, (cst.List, cst.Tuple)):
                    AddImportsVisitor.add_needed_import(self.context, "pydantic", "AliasChoices")
                    inner = ", ".join(_code(e.value) for e in arg.value.elements)
                    args.append(arg.with_changes(keyword=cst.Name("validation_alias"), value=cst.parse_expression(f"AliasChoices({inner})")))
                else:
                    args.append(arg.with_changes(keyword=cst.Name("validation_alias")))
                changed.append("env->validation_alias")
            elif kw == "exclude" and val not in ("True", "False"):
                self._unresolved("PYD207", original, "Field(exclude=<set/dict>) has no v2 equivalent")
                return updated
            elif kw in FIELD_UNSUPPORTED:
                self._unresolved("PYD207", original, f"Field({kw}=...) has no direct v2 equivalent")
                return updated
            elif kw in V2_FIELD_KWARGS:
                args.append(arg)
            else:
                extra.append((kw, val))
        if extra:
            if any(a.keyword and a.keyword.value == "json_schema_extra" for a in args):
                self._unresolved("PYD207", original, "Field() mixes extra kwargs with json_schema_extra")
                return updated
            mapping = ", ".join(f"{k!r}: {v}" for k, v in extra)
            args.append(cst.Arg(keyword=cst.Name("json_schema_extra"), value=cst.parse_expression("{" + mapping + "}"), equal=_EQ))
            changed.append("extra kwargs -> json_schema_extra")
        if not changed:
            return updated
        self._change("PYD150", original, "Field(): " + ", ".join(changed))
        if not structural and not extra:
            return updated.with_changes(args=args)  # keep original formatting
        return updated.with_changes(args=_with_commas([a.with_changes(comma=cst.MaybeSentinel.DEFAULT) for a in args]))

    def _convert_con_call(self, original: cst.Call, updated: cst.Call, name: str):
        renames = CON_RENAMES[name]
        args = []
        changed = []
        for arg in updated.args:
            kw = arg.keyword.value if arg.keyword else None
            if kw in CON_UNSUPPORTED:
                self._unresolved("PYD207", original, f"{name}({kw}=...) has no v2 equivalent")
                return updated
            if kw in renames:
                args.append(arg.with_changes(keyword=cst.Name(renames[kw])))
                changed.append(f"{kw}->{renames[kw]}")
            else:
                args.append(arg)
        if not changed:
            return updated
        self._change("PYD151", original, f"{name}(): " + ", ".join(changed))
        return updated.with_changes(args=args)

    def _convert_parse_obj_as(self, original: cst.Call, updated: cst.Call, canonical: str):
        positional = [a for a in updated.args if a.keyword is None and not a.star]
        if len(positional) != 2 or len(updated.args) != 2:
            self._unresolved("PYD205", original, f"{canonical.split('.')[-1]}() with extra arguments")
            return updated
        adapter = self._pyd_ref(updated.func, "TypeAdapter")
        method = "validate_python" if canonical.endswith("parse_obj_as") else "validate_json"
        RemoveImportsVisitor.remove_unused_import(self.context, "pydantic", canonical.split(".")[-1])
        self._change("PYD170", original, f"{canonical.split('.')[-1]}(T, x) -> TypeAdapter(T).{method}(x)")
        return cst.Call(
            func=cst.Attribute(
                value=cst.Call(func=adapter, args=[positional[0].with_changes(comma=cst.MaybeSentinel.DEFAULT)]),
                attr=cst.Name(method),
            ),
            args=[positional[1].with_changes(comma=cst.MaybeSentinel.DEFAULT)],
        )

    def _convert_method_call(self, original: cst.Call, updated: cst.Call):
        func: cst.Attribute = updated.func  # type: ignore[assignment]
        method = func.attr.value
        receiver = func.value
        kwargs = {a.keyword.value for a in updated.args if a.keyword}
        has_positional = any(a.keyword is None for a in updated.args)
        has_star = any(a.star for a in updated.args)

        # --- class-level API on a model class
        model_cls = self._model_class_ref(receiver)
        if model_cls and method in CLASS_METHODS:
            new_name, allowed = CLASS_METHODS[method]
            if allowed is not None and (not kwargs <= allowed or has_star):
                self._unresolved("PYD205", original, f"{model_cls}.{method}() uses v1-only arguments {sorted(kwargs - allowed)}")
                return updated
            if method == "update_forward_refs" and (updated.args):
                self._unresolved("PYD205", original, f"{model_cls}.update_forward_refs(**localns) needs manual migration")
                return updated
            self._change("PYD141", original, f"{model_cls}.{method}() -> {model_cls}.{new_name}()")
            return updated.with_changes(func=func.with_changes(attr=cst.Name(new_name)))
        if model_cls and method == "from_orm":
            if has_star or kwargs or len(updated.args) != 1:
                self._unresolved("PYD205", original, f"{model_cls}.from_orm() with extra arguments")
                return updated
            self._change("PYD141", original, f"{model_cls}.from_orm(x) -> model_validate(x, from_attributes=True)")
            args = [updated.args[0].with_changes(comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))),
                    cst.Arg(keyword=cst.Name("from_attributes"), value=cst.Name("True"), equal=_EQ)]
            return updated.with_changes(func=func.with_changes(attr=cst.Name("model_validate")), args=args)
        if model_cls and method == "schema_json":
            schema_kw = [a for a in updated.args if a.keyword and a.keyword.value in ("by_alias", "ref_template")]
            dumps_kw = [a for a in updated.args if not (a.keyword and a.keyword.value in ("by_alias", "ref_template"))]
            if has_positional:
                self._unresolved("PYD205", original, f"{model_cls}.schema_json() with positional arguments")
                return updated
            AddImportsVisitor.add_needed_import(self.context, "json")
            inner = cst.Call(func=func.with_changes(attr=cst.Name("model_json_schema")), args=_with_commas([a.with_changes(comma=cst.MaybeSentinel.DEFAULT) for a in schema_kw]))
            self._change("PYD141", original, f"{model_cls}.schema_json() -> json.dumps({model_cls}.model_json_schema())")
            return cst.Call(
                func=cst.parse_expression("json.dumps"),
                args=_with_commas([cst.Arg(inner)] + [a.with_changes(comma=cst.MaybeSentinel.DEFAULT) for a in dumps_kw]),
            )
        if model_cls and method == "parse_file":
            self._unresolved("PYD205", original, f"{model_cls}.parse_file() was removed; read the file and use model_validate_json")
            return updated

        # --- instance-level API
        if method in INSTANCE_METHODS:
            instance = self._infer_instance(receiver)
            if instance is None and self._is_super_in_model(receiver):
                instance = self._cls.name if self._cls else None
            if instance:
                new_name, allowed = INSTANCE_METHODS[method]
                if has_positional or has_star or not kwargs <= allowed:
                    self._unresolved(
                        "PYD205",
                        original,
                        f"{instance}.{method}() uses arguments without a v2 equivalent: {sorted(kwargs - allowed) or 'positional'}",
                    )
                    return updated
                self._change("PYD140", original, f"{instance}.{method}() -> .{new_name}()")
                return updated.with_changes(func=func.with_changes(attr=cst.Name(new_name)))
            if method in ("dict", "json") and not has_positional:
                self._unresolved(
                    "PYD299",
                    original,
                    f".{method}() on an object whose type could not be inferred; if it is a model use .model_dump"
                    + ("_json" if method == "json" else "")
                    + "()",
                    Severity.INFO,
                )
            return updated
        if method in PYDANTIC_SPECIFIC_METHODS and model_cls is None:
            self._unresolved(
                "PYD206",
                original,
                f".{method}() is a pydantic v1 API but the receiver type is unknown",
                Severity.WARNING,
            )
        return updated

    def _is_super_in_model(self, receiver: cst.BaseExpression) -> bool:
        return bool(
            isinstance(receiver, cst.Call)
            and isinstance(receiver.func, cst.Name)
            and receiver.func.value == "super"
            and self._in_model_method()
        )

    def leave_Attribute(self, original: cst.Attribute, updated: cst.Attribute):
        attr = updated.attr.value
        if attr == "__fields_set__":
            self._change("PYD142", original, "__fields_set__ -> model_fields_set")
            return updated.with_changes(attr=cst.Name("model_fields_set"))
        if attr == "__fields__":
            cls_ref = self._model_class_ref(updated.value)
            if cls_ref:
                self._change("PYD142", original, f"{cls_ref}.__fields__ -> model_fields")
                return updated.with_changes(attr=cst.Name("model_fields"))
            inst = self._infer_instance(updated.value)
            if inst:
                self._change("PYD142", original, f"<{inst}>.__fields__ -> type(...).model_fields")
                return cst.Attribute(
                    value=cst.Call(func=cst.Name("type"), args=[cst.Arg(updated.value)]),
                    attr=cst.Name("model_fields"),
                )
            self._unresolved("PYD210", original, "__fields__ on an object of unknown type", Severity.WARNING)
            return updated
        if (
            attr in ("required", "outer_type_", "type_", "field_info", "name")
            and isinstance(updated.value, cst.Subscript)
            and isinstance(updated.value.value, cst.Attribute)
            and updated.value.value.attr.value == "model_fields"
        ):
            if attr == "required":
                self._change("PYD142", original, "ModelField.required -> FieldInfo.is_required()")
                return cst.Call(func=updated.with_changes(attr=cst.Name("is_required")))
            if attr in ("outer_type_", "type_"):
                self._change("PYD142", original, f"ModelField.{attr} -> FieldInfo.annotation")
                return updated.with_changes(attr=cst.Name("annotation"))
            if attr == "field_info":
                self._change("PYD142", original, "ModelField.field_info -> FieldInfo")
                return updated.value
            self._unresolved("PYD210", original, "FieldInfo has no .name in v2 (use the model_fields key)")
        return updated


_EQ = cst.AssignEqual(whitespace_before=cst.SimpleWhitespace(""), whitespace_after=cst.SimpleWhitespace(""))


def _with_commas(args: list[cst.Arg]) -> list[cst.Arg]:
    out = []
    for i, a in enumerate(args):
        if i < len(args) - 1:
            out.append(a.with_changes(comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))))
        else:
            out.append(a.with_changes(comma=cst.MaybeSentinel.DEFAULT))
    return out


def _decorator_name(dec: cst.Decorator) -> str:
    expr = dec.decorator.func if isinstance(dec.decorator, cst.Call) else dec.decorator
    if isinstance(expr, cst.Attribute):
        return expr.attr.value
    return expr.value if isinstance(expr, cst.Name) else ""


class _NamePositions(cst.CSTVisitor):
    """Collects identities of Name nodes that are *not* variable references
    (attribute names, keyword-argument names, parameter names)."""

    def __init__(self) -> None:
        self.protected: set[int] = set()

    def visit_Attribute(self, node: cst.Attribute) -> None:
        self.protected.add(id(node.attr))

    def visit_Arg(self, node: cst.Arg) -> None:
        if node.keyword is not None:
            self.protected.add(id(node.keyword))

    def visit_Param(self, node: cst.Param) -> None:
        self.protected.add(id(node.name))

    def visit_ImportAlias(self, node: cst.ImportAlias) -> bool:
        return False


def _protected_ids(node: cst.CSTNode) -> set[int]:
    collector = _NamePositions()
    node.visit(collector)
    return collector.protected


class _NameCounter(cst.CSTVisitor):
    def __init__(self, name: str, protected: set[int]) -> None:
        self.name = name
        self.protected = protected
        self.count = 0

    def visit_Name(self, node: cst.Name) -> None:
        if node.value == self.name and id(node) not in self.protected:
            self.count += 1


def count_name_uses(node: cst.CSTNode, name: str) -> int:
    """Number of variable references to ``name`` (ignores attribute/keyword/param names)."""
    counter = _NameCounter(name, _protected_ids(node))
    node.visit(counter)
    return counter.count


class _ValidatorBodyRewriter(cst.CSTTransformer):
    """values -> info.data and field.name -> info.field_name inside a validator body."""

    def __init__(self, protected: set[int]) -> None:
        self.protected = protected

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        return False  # nested functions keep their own scope

    def leave_Attribute(self, original: cst.Attribute, updated: cst.Attribute):
        if m.matches(original, m.Attribute(value=m.Name("field"), attr=m.Name("name"))):
            return cst.parse_expression("info.field_name")
        return updated

    def leave_Name(self, original: cst.Name, updated: cst.Name):
        if original.value == "values" and id(original) not in self.protected:
            return cst.parse_expression("info.data")
        return updated


class _PostRootRewriter(cst.CSTTransformer):
    """values['k'] / values.get('k'[, d]) -> self.k ; return values -> return self."""

    def __init__(self, values_name: str, cls_name: str, protected: set[int]) -> None:
        self.values = values_name
        self.cls = cls_name
        self.protected = protected
        self.unsafe = False

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        return False

    def leave_Subscript(self, original: cst.Subscript, updated: cst.Subscript):
        if m.matches(updated.value, m.Name(self.values)) and len(updated.slice) == 1:
            index = updated.slice[0].slice
            key = _string_value(index.value) if isinstance(index, cst.Index) else None
            if key and key.isidentifier():
                return cst.Attribute(value=cst.Name("self"), attr=cst.Name(key))
        return updated

    def leave_Call(self, original: cst.Call, updated: cst.Call):
        if m.matches(updated.func, m.Attribute(value=m.Name(self.values), attr=m.Name("get"))) and 1 <= len(updated.args) <= 2:
            key = _string_value(updated.args[0].value)
            if key and key.isidentifier():
                return cst.Attribute(value=cst.Name("self"), attr=cst.Name(key))
        return updated

    def leave_Return(self, original: cst.Return, updated: cst.Return):
        if updated.value is not None and m.matches(updated.value, m.Name(self.values)):
            return updated.with_changes(value=cst.Name("self"))
        return updated

    def leave_AssignTarget(self, original, updated):
        if count_name_uses(original, self.values):
            self.unsafe = True
        return updated

    def leave_AugAssign(self, original, updated):
        if count_name_uses(original.target, self.values):
            self.unsafe = True
        return updated

    def leave_Name(self, original: cst.Name, updated: cst.Name):
        if original.value == self.cls and self.cls != "self" and id(original) not in self.protected:
            return cst.Name("self")
        return updated


# ----------------------------------------------------------------------------- API
def migrate_source(source: str, relpath: str, index: ProjectIndex) -> tuple[str, CodemodReport]:
    """Apply the codemod to one file's source. Returns (new_source, report)."""
    module = cst.parse_module(source)
    context = CodemodContext(filename=relpath, full_module_name=None)
    command = PydanticV2Codemod(context, index, relpath, source.splitlines())
    new_module = command.transform_module(module)
    new_source = new_module.code
    report = command.report
    if new_source == source:
        report.changes = []
    return new_source, report


def file_needs_codemod(source: str, index: ProjectIndex) -> bool:
    """Cheap pre-filter: only files mentioning pydantic or a known model are processed."""
    if "pydantic" in source:
        return True
    return any(name in source for name in index.model_names)
