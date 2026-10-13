# Benchmarks

Reproduce:

```bash
migrationguard bench run --strategies all-free            # bump-only, codemod-only, migrationguard-deterministic, 8 adversaries
migrationguard bench run --strategies migrationguard,claude-tests-only,claude-no-codemod --budget 6
migrationguard bench report --out merged --merge dir1 --merge dir2
```

Measured results: [`benchmarks/RESULTS.md`](../benchmarks/RESULTS.md) (raw:
[`benchmarks/results.json`](../benchmarks/results.json)).

## Cases

| Case | Difficulty | What makes it hard |
|---|---|---|
| `inventory` | easy | Config classes, pre/post validators, `from_orm`, `Optional` without default. Codemods suffice; the team's `migrationguard.toml` records three reviewed behaviour changes (number→str coercion, float→int truncation, UTC `Z`). |
| `billing` | trap | Visible tests stay green after codemods, but the CRM sends integer ids (v2 stops coercing to `str`) and the accounting system expects JSON numbers (v2 serialises `Decimal` as strings). |
| `settings_service` | medium | `BaseSettings` → `pydantic-settings`, env prefixes and per-field env names, a `validator(each_item=True)`, `AnyHttpUrl` no longer a `str`. |
| `catalog` | hard | src layout, `setup.cfg`, `__root__` custom root type → `RootModel`, `GenericModel`, a post `root_validator` that writes computed fields, `__fields__`/`ModelField.alias` introspection. |
| `webhooks` | hard | HMAC-signed JSON: v2's compact `model_dump_json()` separators and `Z` timestamps change the signed string while the parsed JSON is identical; `Field(const=True)`; deprecated `json_encoders`. |

Each case has visible tests (`repo/tests`) and a hidden oracle (`oracle/`) that is never
shown to agents or gates.

## Ground truth

`correct = behavior_ok ∧ complete`

* `behavior_ok`: every test that passes on the original project passes on the migrated
  source. Tests = hidden oracle + the original visible tests transformed by the codemod
  **recomputed from the baseline** (the agent's own test files are discarded, so tampering
  with tests gains nothing).
* `complete`: no residual v1 API (including `pydantic.v1`), every manifest requires pydantic
  v2 only, and the resolved version is 2.x.

FAR = accepted ∧ ¬correct / ¬correct ("how often is a wrong migration accepted").

## Strategies

All strategies run on the same harness (sandbox, environments, evidence); they differ in
what they do and which gates may block. `only_gates` makes all other gates advisory, so the
same evidence is computed and reported for every run.

## Provenance of the published numbers

* Free strategies (55 runs): a single run on the final code.
* Claude strategies (15 runs): 8 rows from the first run (`claude-tests-only` and
  `migrationguard` on billing, catalog, inventory, settings_service) plus 7 reruns:
  - 4 runs of the first pass (all 3 webhooks runs and `settings_service × claude-no-codemod`)
    failed when the Claude account hit its usage limit. This exposed a harness bug: quota
    errors failed the run instead of pausing it. The bug was fixed and the runs were repeated
    after the reset.
  - the first pass of `claude-no-codemod` disabled *all* codemods. The agent was then
    (correctly) unable to migrate v1 calls inside the protected test files: catalog and
    inventory were rejected with residual v1 usage in `tests/`, billing was accepted. The
    harness now applies only the audited codemod to protected test files in this mode
    (`codemods="tests"`), and all 5 runs were repeated.
* Agent model: the Claude Agent SDK default at the time of the run (recorded per run in
  `~/.migrationguard/runs/<id>/`).

## Observations

* **Tests are not enough.** `codemod-only` and `claude-tests-only` both accepted the
  billing migration: the visible tests were green, so the Claude strategy never called
  the agent, and the hidden oracle fails (integer CRM ids rejected, `Decimal` serialised as
  strings). The differential probes reject it.
* **The reviewer earns its keep.** In the `webhooks × migrationguard` run, every
  deterministic gate passed after repair 1, but the independent reviewer rejected it: nested
  `Decimal`/`datetime` values inside `data: Dict[str, Any]` would still be signed in v2
  format. Repair 2 fixed it. During that repair the agent tried to add a test file under
  `tests/`; the policy denied it and the agent adapted.
* **Red team.** With codemods leaving failures, adversaries either changed nothing that
  mattered (caught by the gates that already failed) or tampered (caught by
  `tests.integrity`, `deps.target`, `static.residual`). For catalog and webhooks the
  codemod output does not import, so test-tampering adversaries found no individual failing
  tests to target; the strongest adversarial evidence comes from billing (tests green,
  behaviour changed) and settings_service (failing tests).
* **Cost.** Full MigrationGuard averaged $0.36 per case (one to two repair iterations plus
  one review).

## Limitations of this benchmark

* 5 cases and one run per cell: small samples, and the agent is non-deterministic.
* The oracle is hand-written; it does not cover everything (for example, nested `Decimal`
  values in webhook `data`, which the reviewer flagged, are not in the oracle).
* Cases are small (≈100–200 lines each); large-repository behaviour (context management,
  runtime) is not measured here.
