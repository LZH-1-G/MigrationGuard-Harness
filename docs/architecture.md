# Architecture

MigrationGuard is a harness: it owns the control flow, the state, the execution
environment and the acceptance decision. Agents are workers inside it with a narrow,
policy-checked tool surface.

```
                  ┌──────────────────────────── migrationguard CLI ─────────────────────────────┐
                  │  run · resume · status · logs · report · diff · analyze · bench · sandbox     │
                  └───────────────────────────────────────┬──────────────────────────────────────┘
                                                          │
┌─────────────────────────────────── Orchestrator (harness/orchestrator.py) ───────────────────────────────────┐
│ explicit state machine · checkpoint after every transition · retries (transient) · budget · pause/resume      │
│                                                                                                               │
│  INIT → ANALYZE → BASELINE → PLAN → CODEMOD → VERIFY ⇄ REPAIR        VERIFY → REVIEW → DECIDE → ACCEPTED       │
│                                                ↑________________________|  (reject → REPAIR)      → REJECTED  │
│  any phase → PAUSED (budget, interrupt, --stop-after; resumable) | FAILED (non-transient error)               │
└──────┬──────────────┬─────────────────┬────────────────────┬─────────────────────┬────────────────────────────┘
       │              │                 │                    │                     │
 ┌─────▼─────┐  ┌─────▼──────┐   ┌──────▼───────┐   ┌────────▼─────────┐   ┌───────▼────────┐
 │ migrate/  │  │ sandbox/   │   │ verify/      │   │ agents/          │   │ harness/store  │
 │ analysis  │  │ git        │   │ gates        │   │ claude_sdk       │   │ SQLite (WAL):  │
 │ codemod   │  │ worktrees  │   │ compare      │   │ tools (policy)   │   │ runs           │
 │ deps      │  │ envs (uv)  │   │ (pure fns)   │   │ prompts/context  │   │ checkpoints    │
 │ recipe    │  │ docker /   │   │              │   │ scripted /       │   │ events         │
 │           │  │ local      │   │              │   │ red-team         │   │ attempts       │
 │           │  │ policy     │   │              │   │                  │   │ evidence       │
 │           │  │ runtime/*  │   │              │   │                  │   │ usage          │
 └───────────┘  └────────────┘   └──────────────┘   └──────────────────┘   └────────────────┘
```

## Phases

| Phase | What happens | Deterministic? |
|---|---|---|
| `init` | Create two git worktrees from the base commit: **target** (branch `migrationguard/<run>`) and **baseline** (detached). The user's checkout is never touched. | yes |
| `analyze` | Build a project index (models, transitive model bases, field types, function return types) and dry-run the codemod to classify every v1 usage as auto-fixable or needing judgement. | yes |
| `baseline` | Resolve + install a **pydantic<2** environment with uv, run the test suite in the sandbox, run the probe *discoverer* which synthesises inputs for every model and records outcomes. This is the oracle. | yes |
| `plan` | Explicit plan: dependency step, one codemod step per rule, one agent step per file with unresolved findings. | yes |
| `codemod` | Apply libcst codemods + format-preserving manifest rewrites, commit. | yes |
| `verify` | Provision the target environment and evaluate every evidence gate (see [verification.md](verification.md)). | yes |
| `repair` | Claude repair agent with an evidence-first prompt and policy-checked tools; commit the attempt. | LLM |
| `review` | Independent, read-only reviewer agent; veto-only. | LLM |
| `decide` | Accept iff no blocking gate failed; write `report.md` / `report.json` / `migration.patch`. | yes |

The LLM is only used where judgement is required (unresolved findings, failing gates) and
for the independent review. Everything that can be computed is computed.

## State, checkpoints and recovery

`RunState` (a Pydantic model) holds the phase, iteration counters, plan, history of
verifications, review verdict, usage and budget counters. After every transition the
orchestrator writes a checkpoint row containing the **complete state** and the **target
worktree commit**. Consequences:

* `resume` reloads the latest checkpoint, `git reset --hard` the target worktree to the
  checkpoint commit (discarding half-finished work of the interrupted phase) and re-enters
  the phase. Agent attempts that were running are marked `interrupted`.
* A hard crash (`kill -9`) loses at most the current phase's work. This is tested by
  killing the CLI process with `MIGRATIONGUARD_FAULT=verify:crash` and resuming it.
* `TransientError`s (dependency index outages, sandbox hiccups, API rate limits) are
  retried with exponential backoff and full jitter; the worktree is reset to the phase's
  starting commit between attempts.
* Budget exhaustion (USD, tokens, agent calls, wall time) **pauses** the run; resume with a
  higher budget (`migrationguard resume <id> --budget 20`).

Every phase handler is idempotent with respect to the checkpointed state, which is what
makes re-entry safe.

## Environments

`sandbox/envs.py` resolves a full lock with `uv pip compile` (optionally
`--exclude-newer` for reproducibility) and installs it with `uv pip install --target` into a
directory addressed by `sha256(python version + lock)`. The sandbox mounts that directory
read-only on `PYTHONPATH`. Consequences: no network is needed during test execution,
environments are shared across runs and benchmark cases, and the exact lock is stored as
evidence. Interpreters are uv-managed CPython builds, never virtualenvs (a virtualenv would
leak its site-packages into the sandbox).

## Sandboxes

Both backends run the same `ExecSpec` (argv with `${bind}` placeholders, named binds,
environment, timeout):

| | Docker (`sandbox/docker.py`) | Local (`sandbox/local.py`) |
|---|---|---|
| filesystem | workspace + deps mounted **read-only**, read-only rootfs, tmpfs `/tmp`, only `out/` writable | workspace **copied** to a throwaway dir before each run |
| network | `--network none` | unprivileged network namespace (`unshare -rn`) when available |
| privileges | uid 65534, `--cap-drop ALL`, `no-new-privileges` | normal user |
| resources | `--memory`, `--cpus`, `--pids-limit` | rlimits (CPU, address space, file size); process group killed on timeout |
| environment | fresh, no host variables | scrubbed (no API keys, tokens, proxies) |

The runtime image can be built from `python:<v>-slim` or — without any registry access —
from a uv-managed standalone CPython plus the host's glibc libraries (`FROM scratch`).

## Agents

`agents/claude_sdk.py` runs Claude through the **Claude Agent SDK** with:

* `tools=[]` — all built-in Claude Code tools (Bash, Read, Write, Edit, WebFetch, …) removed;
* in-process MCP tools (`mcp__mg__*`) implemented by `agents/tools.py` — read/search/list,
  `write_file`/`replace_in_file` through the `PermissionPolicy`, `run_tests`/`run_probes`
  executed **in the sandbox**, `show_findings`/`show_diff`/`show_evidence`, `finish`;
* `permission_mode="dontAsk"`, explicit `allowed_tools`, `disallowed_tools` for every
  built-in, and a `PreToolUse` hook that denies anything else (defense in depth);
* an empty scratch `cwd` and `setting_sources=[]` (no user settings, CLAUDE.md or hooks);
* `max_turns` / `max_budget_usd` per session; usage and cost recorded per attempt.

The reviewer gets read-only tools plus `submit_review`. Its worktree is checked after the
session; any modification is discarded and logged.

### Context management

The repair prompt is built from evidence, not from the whole repository: failing gates
with their details (failing tests with tracebacks, probe diffs with baseline vs migrated
values, residual findings with code snippets), items the codemod planned for the agent,
the reviewer's rejection (if any) and a summary of previous attempts. It is clipped to
`agent.prompt_evidence_chars`; the agent pulls more through tools on demand (`read_file`
with line ranges, `search`, `show_evidence`).

## Extending to other migrations

A recipe provides `analyze`, `plan`, `apply`, `residual` and `expected_for` (see
`migrate/recipe.py`). The harness, sandbox, gates (tests, differential, integrity, scope,
probes) and agents are recipe-agnostic; only `static.residual`, `deps.target` and the
probe runner are Pydantic-specific.
