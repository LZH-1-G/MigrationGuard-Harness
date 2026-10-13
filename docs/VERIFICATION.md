# Release verification — MigrationGuard 0.1.0

Recorded 2026-10-13 on Linux (x86_64), Python 3.12.13 (uv-managed), Docker 29.8.2, Pydantic
1.10.26 (baseline) / 2.14.1 (target), Claude Agent SDK 0.2.165. Every result below comes from
an actual execution; nothing is estimated.

## Test suite

| Run | Result |
|---|---|
| `pytest` (working copy, with coverage) | **105 passed, 1 skipped, 0 failed** (200.8 s) |
| `pytest` in a fresh `git clone` after `pip install -e ".[dev]"` | **105 passed, 1 skipped, 0 failed** (167.6 s) |
| `MIGRATIONGUARD_LIVE_TESTS=1 pytest tests/integration/test_live_claude.py` | **1 passed** (123 s): real Claude migration of `billing`, accepted, hidden oracle passes |
| `MG_TEST_SANDBOX=local pytest tests/integration/test_pipeline.py` (earlier in the release cycle) | 17 passed: the full pipeline on the process sandbox |
| `ruff check src tests` | clean |

The single skip is the live test, which is opt-in because it spends money; its separate run
passed. Suite: 78 unit tests and 28 integration tests (real git worktrees, real uv
environments, real sandboxed pytest/probe executions; scripted agents instead of an LLM).

**Line coverage (host-side package): 82 %** (5149 statements, 933 missed).
Not measured: the in-sandbox runtime scripts (`mg_probe_runner.py`, `mg_pytest_plugin.py`,
which run in a separate interpreter inside the sandbox) and CLI subprocesses.
`agents/claude_sdk.py` is 22 % in the default suite and **84 %** under the live test.
`sandbox/docker.py` is 46 %: the image builder is exercised by `migrationguard sandbox
build-image`, not by the suite.

## Acceptance criteria

| # | Criterion | Evidence |
|---|---|---|
| 1 | Critical unit + integration tests pass | 105/105 non-live tests pass, in the working copy and in a fresh clone; live test passes |
| 2 | A real Claude-powered migration succeeds end to end | Benchmark Claude strategies, 15 runs: all accepted, 14 correct against the hidden oracle. The one false acceptance is `billing × claude-tests-only`: its tests were green, so the agent was never called. Standalone real runs, all accepted and oracle-correct: billing ($0.50), settings_service ($0.26), catalog ($0.30), the quota-interrupted webhooks run ($0.53), and the live pytest run. README `demo.sh --claude` from a fresh clone: accepted ($0.26; oracle not evaluated) |
| 3 | Differential verification detects a deliberately introduced regression | `test_deliberately_injected_regression_is_detected`: a default-value change injected after the codemod commit; tests stay green, `behavior.probes` fails with a `value` diff, run rejected. Natural case: `billing` codemod output is rejected (13 probe differences) although every visible test passes |
| 4 | Evidence gates reject unsafe migrations | 8 red-team agents × 5 cases: 32 runs where the adversary acted, 0 of 32 incorrect results accepted (a tests-only policy would accept 14). Integration test asserts the catching gate for each adversary |
| 5 | Checkpoint recovery after a simulated interruption | `kill`-style crash (`os._exit(137)` in `verify`) → `resume` → ACCEPTED; `--stop-after` pause/resume; interrupt during repair; budget exhaustion pause → resume with more budget; persistent transient failure → pause → resume. **Real outage:** the provider's usage limit paused webhooks run `20261013-013702-34f1fa` in `repair`; after the reset `migrationguard resume` completed it (ACCEPTED, hidden oracle passes) |
| 6 | Benchmarks execute and produce real metrics | 70 benchmark rows (55 free, 15 Claude) in `benchmarks/results.json` / `RESULTS.md` |
| 7 | Installable and runnable from the README | Fresh `git clone` → `pip install -e ".[agent]"` → `doctor` (all checks pass) → `sandbox build-image` (fell back to the no-registry strategy automatically) → `sandbox check` → `examples/demo.sh` (REJECTED as documented) → `examples/demo.sh --claude` (ACCEPTED, $0.26) → `pip install -e ".[dev]"`, `ruff`, `pytest` (105 passed). Wheel build checked: it contains the runtime scripts and the 5 benchmark cases; `bench list` works from an installed wheel |

## Security / isolation checks (executed)

* Docker sandbox: workspace not writable, only `out/` writable, no network, rootfs
  read-only, uid 65534, host secrets not visible (`test_docker_sandbox_*`, `sandbox check`).
* Local sandbox: workspace copy (host untouched), scrubbed environment, network namespace,
  timeout kills the process group (`test_local_sandbox_*`).
* Policy: 14 denial cases (tests, conftest, sitecustomize, `.pth`, pytest.ini, config, `.git`,
  `..`, absolute paths, symlink escape, non-writable files, size limit). In a real Claude
  run the policy denied the agent's attempt to add `tests/test_signing_nested_data.py`.
* Claude sessions: built-in tools removed (`tools=[]`), `dontAsk`, an allow-list, a
  `PreToolUse` deny hook, empty cwd, no setting sources.

## Bugs found and fixed during verification

* Quota errors from the model provider failed runs (terminal) instead of pausing them.
  They now raise `AgentQuotaExceeded` (not retried) → PAUSED; persistent transient errors
  also pause.
* The probe runner did not recognise PEP 604 unions (`X | None`): no `None`/coercion probes
  for such fields. Fixed, with a regression test covering both spellings. No benchmark
  case uses PEP 604 annotations, so no published metric is affected.
* `claude-no-codemod` left v1 calls in protected test files that no actor was allowed to
  migrate. Fixed with the audited tests-only codemod mode.
* The benchmark counted reviewer calls as "agent invoked"; it now counts repair attempts.
* The integrity gate flagged the codemod's own `setup.cfg` dependency rewrite; manifests are
  now audited as "only pydantic requirement lines changed".

## Known limitations

See README → Limitations and docs/benchmarks.md. In short: Pydantic v1→v2 on Python only;
probes cover Pydantic models (not settings) and compare parsed JSON (raw-format changes
need user probes; this is demonstrated by a test); behavioural coverage is bounded by
synthesised inputs + tests; the reviewer is non-deterministic; the local sandbox is weaker
than Docker; services such as databases are not provided in the sandbox; the benchmark is
small (5 cases, one run per cell).
