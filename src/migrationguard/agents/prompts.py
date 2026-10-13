"""System prompts and the evidence-driven context builder for agents."""

from __future__ import annotations

import json
from typing import Any

from migrationguard.models import Finding, GateResult, MigrationPlan, ReviewVerdict, VerificationResult

REPAIR_SYSTEM_PROMPT = """\
You are the repair agent inside MigrationGuard, a harness that migrates Python projects
from Pydantic v1 to Pydantic v2. Deterministic codemods have already been applied. Your
job is to finish the migration so that every verification gate passes, while preserving
the project's observable behaviour.

How you work
- You can only act through the provided tools. There is no shell. Paths are relative to
  the repository root.
- Tests, test configuration (conftest.py, pytest.ini, setup.cfg, tox.ini), CI files and
  migrationguard.toml are PROTECTED. Writes to them are denied. Never try to make tests pass
  by changing what they check: weakening, skipping or special-casing tests is detected and
  causes rejection.
- Production code must not detect that it is under test (no PYTEST_CURRENT_TEST, no
  `"pytest" in sys.modules`, no stack inspection). This is detected.
- The project must depend on Pydantic v2 (`pydantic>=2`). Do not pin back to v1 and do not
  use the `pydantic.v1` compatibility namespace; both are detected.
- Use run_tests and run_probes to check your work inside the sandbox. They have a budget.
- When done, call finish with an honest summary and a list of concrete claims. Your claims
  are recorded and independently checked; they are never used to accept your work.

What "done" means (all checked automatically)
- Every test that passed before the migration passes after it.
- No Pydantic v1 API remains (dry-run codemod finds nothing; no deprecation warnings).
- Differential behavioural probes agree: for the same inputs, models accept/reject the same
  payloads and produce the same python and JSON output as before. If v2 changed behaviour
  (e.g. stricter coercion, different JSON encoding) restore the old behaviour in the model
  (validators, serializers, config) unless that is impossible; never hide differences.

Pydantic v2 reference
- @validator -> @field_validator (+ @classmethod); pre=True -> mode="before";
  always=True -> Field(validate_default=True); `values` -> `info: ValidationInfo` and
  `info.data`; each_item=True -> validate the container, or Annotated[T, AfterValidator(f)].
- @root_validator(pre=True) -> @model_validator(mode="before") on a classmethod receiving the
  raw input; post root validators -> @model_validator(mode="after") operating on `self` and
  returning `self` (assign with object.__setattr__ on frozen models, or compute in "before").
- class Config -> model_config = ConfigDict(...): orm_mode->from_attributes,
  allow_population_by_field_name->populate_by_name, allow_mutation=False->frozen=True,
  anystr_strip_whitespace->str_strip_whitespace, extra=Extra.forbid->extra="forbid",
  schema_extra->json_schema_extra. Settings: from pydantic_settings import BaseSettings,
  SettingsConfigDict; Field(env="X") -> Field(validation_alias="X").
- Custom root types: class M(BaseModel): __root__: List[X]  ->  class M(RootModel[List[X]]),
  accessed via `.root`; RootModel.model_validate(list) works; iteration needs __iter__.
- GenericModel -> class M(BaseModel, Generic[T]).
- .dict()/.json()/.copy()/parse_obj/parse_raw/from_orm/schema -> model_dump /
  model_dump_json / model_copy / model_validate / model_validate_json /
  model_validate(..., from_attributes=True) / model_json_schema.
- v2 no longer coerces int/float -> str: restore with ConfigDict(coerce_numbers_to_str=True)
  or Field(coerce_numbers_to_str=True) when old behaviour must be kept. v2 rejects floats with
  a fractional part for int fields (v1 truncated). Decimal serialises to a JSON string in v2
  (v1: number) -> use @field_serializer(..., when_used="json") to keep numbers.
- Optional[X] without a default is REQUIRED in v2: add `= None` to keep v1 behaviour.
- v1 `.json()` used json.dumps defaults (", " and ": " separators, "+00:00" for UTC);
  v2 `model_dump_json()` is compact and writes "Z". Parsed JSON is identical, but any code that
  signs, hashes, caches or compares the raw string (webhook signatures, ETags, golden files)
  breaks. Keep the exact old format there, e.g. json.dumps(model.model_dump(mode="json")) plus
  a serializer for datetimes, and check json_encoders (deprecated) were replaced by serializers.
- FieldInfo.alias is None when no alias was set (v1 ModelField.alias defaulted to the name);
  ModelField.required -> FieldInfo.is_required(); outer_type_ -> annotation.
"""

REVIEWER_SYSTEM_PROMPT = """\
You are an independent code reviewer inside MigrationGuard. A Pydantic v1 -> v2 migration
has passed every automated gate (tests, differential tests, residual-API scan, test
integrity, behavioural probes). You did not write it and you have no stake in it.

Look for what automated gates can miss:
- semantic changes in validation/serialisation that probes or tests may not exercise;
- logic that special-cases inputs or appears tailored to tests;
- swallowed exceptions, removed validation, broadened types (e.g. str -> Any), dead code;
- incorrect use of v2 APIs (mode="before" vs "after", info.data assumptions, frozen models
  mutated in validators, wrong serializer `when_used`);
- raw-string output changes the probes cannot see (they compare *parsed* JSON): v1 `.json()`
  used ", "/": " separators and "+00:00"; v2 `model_dump_json()` is compact and uses "Z".
  Code that signs, hashes or byte-compares serialised output must keep the old format.
You can read files, the diff and the evidence. You cannot modify anything.

Finish by calling submit_review exactly once with verdict "approve" or "reject". Reject only
for concrete, explainable defects (cite path and line). Do not reject for style.
"""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 40] + f"\n... [{len(text) - limit + 40} chars truncated]"


def _gate_block(gate: GateResult, budget: int) -> str:
    details = dict(gate.details)
    details.pop("stdout_tail", None) if gate.status == "pass" else None
    body = json.dumps(details, indent=1, default=str)
    return _clip(f"### {gate.gate} [{gate.status.upper()}] — {gate.summary}\n{body}", budget)


def build_repair_prompt(
    plan: MigrationPlan | None,
    verification: VerificationResult,
    residual: list[Finding],
    history: list[dict[str, Any]],
    review: ReviewVerdict | None,
    budget_chars: int,
    iteration: int,
) -> str:
    """Compact, evidence-first context. The agent can pull more through its tools."""
    parts: list[str] = [
        f"Repair attempt {iteration}. The migrated code is at commit {verification.commit[:12]}.",
        "The following verification gates FAILED. Fix the underlying problems in the source code.",
    ]
    failing = verification.failing_gates()
    other = [g for g in verification.gates if g not in failing and g.status in ("warn",)]
    per_gate = max(1500, int(budget_chars * 0.6) // max(1, len(failing)))
    for gate in failing:
        parts.append(_gate_block(gate, per_gate))
    for gate in other:
        parts.append(_gate_block(gate, 1200))
    if residual:
        lines = [f"- {f.path}:{f.line} {f.rule}: {f.message}" + (f"  `{f.snippet}`" if f.snippet else "") for f in residual[:40]]
        parts.append("### Residual Pydantic v1 usage (from the dry-run codemod)\n" + "\n".join(lines))
    if plan and plan.unresolved:
        lines = [f"- {f.path}:{f.line} {f.rule}: {f.message}" for f in plan.unresolved[:30]]
        parts.append("### Items the deterministic codemod could not migrate (planned for you)\n" + "\n".join(lines))
    if review is not None and review.verdict == "reject":
        lines = [f"- [{i.severity}] {i.path}:{i.line} {i.explanation}" for i in review.issues[:20]]
        parts.append("### Independent reviewer rejected the previous version\n" + review.summary + "\n" + "\n".join(lines))
    if history:
        lines = [f"- attempt {h.get('iteration')}: {h.get('summary', '')[:300]} -> failing gates: {h.get('failing', [])}" for h in history[-4:]]
        parts.append("### Previous attempts (do not repeat what did not work)\n" + "\n".join(lines))
    parts.append(
        "Start with show_findings / read_file on the affected modules, make focused edits, run run_tests "
        "and run_probes, then call finish."
    )
    return _clip("\n\n".join(parts), budget_chars)


def build_review_prompt(diff_stat: str, verification: VerificationResult, budget_chars: int) -> str:
    gates = "\n".join(f"- {g.gate}: {g.status} — {g.summary}" for g in verification.gates)
    return _clip(
        "Review this Pydantic v1 -> v2 migration.\n\n"
        f"## Gate results\n{gates}\n\n## Diff stat\n{diff_stat}\n\n"
        "Use show_diff for the full diff, read_file for context and show_evidence for the raw evidence. "
        "Then call submit_review.",
        budget_chars,
    )
