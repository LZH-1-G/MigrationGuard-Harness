"""MigrationGuard command-line interface."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from migrationguard import __version__
from migrationguard.config import CONFIG_FILENAME, DEFAULT_TOML, MigrationGuardConfig, home_dir
from migrationguard.errors import MigrationGuardError
from migrationguard.models import Phase

app = typer.Typer(
    name="migrationguard",
    help="Evidence-driven agent harness for safe dependency migration (Pydantic v1 → v2).",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)
sandbox_app = typer.Typer(help="Inspect and prepare the execution sandbox.", no_args_is_help=True)
bench_app = typer.Typer(help="Reproducible migration benchmarks.", no_args_is_help=True)
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(bench_app, name="bench")

console = Console(highlight=False)
err = Console(stderr=True, highlight=False)

PHASE_STYLE = {
    "init": "dim", "analyze": "cyan", "baseline": "cyan", "plan": "cyan", "codemod": "blue",
    "verify": "magenta", "repair": "yellow", "review": "yellow", "decide": "bold",
}
LEVEL_STYLE = {"error": "red", "warning": "yellow", "info": "", "debug": "dim"}


def _version_cb(value: bool) -> None:
    if value:
        console.print(f"migrationguard {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    version: bool = typer.Option(False, "--version", callback=_version_cb, is_eager=True, help="Show version."),
) -> None:
    """MigrationGuard migrates a repository on an isolated branch and only accepts the
    result when deterministic evidence proves it is safe."""


def _sink(verbose: bool):
    def sink(event: dict[str, Any]) -> None:
        level = event["level"]
        if level == "debug" and not verbose:
            return
        kind = event["kind"]
        if kind in ("phase.start", "phase.end") and not verbose:
            return
        phase = event.get("phase") or "-"
        ts = event["ts"][11:19]
        style = LEVEL_STYLE.get(level, "")
        message = event["message"]
        if kind == "phase.transition":
            console.print(f"[dim]{ts}[/dim] [bold]▶ {message}[/bold]")
            return
        if kind == "gate":
            status = (event.get("data") or {}).get("status", "")
            icon = {"pass": "[green]✔[/green]", "fail": "[red]✘[/red]", "warn": "[yellow]![/yellow]", "skip": "[dim]–[/dim]"}.get(status, "•")
            console.print(f"[dim]{ts}[/dim] [{PHASE_STYLE.get(phase, '')}]{phase:>8}[/] {icon} {message}")
            return
        if kind == "decision":
            colour = "green" if "ACCEPTED" in message else "red"
            console.print(f"\n[bold {colour}]{message}[/bold {colour}]")
            return
        prefix = f"[{PHASE_STYLE.get(phase, '')}]{phase:>8}[/]"
        console.print(f"[dim]{ts}[/dim] {prefix} [{style}]{_escape(message)}[/{style}]" if style else f"[dim]{ts}[/dim] {prefix} {_escape(message)}")

    return sink


def _escape(text: str) -> str:
    return text.replace("[", "\\[")


def _orchestrator(verbose: bool = False, quiet: bool = False):
    from migrationguard.harness.orchestrator import Orchestrator

    return Orchestrator(sink=None if quiet else _sink(verbose))


def _parse_phase(value: Optional[str]) -> Optional[Phase]:
    if value is None:
        return None
    try:
        return Phase(value.lower())
    except ValueError:
        raise typer.BadParameter(f"unknown phase {value!r}; one of {[p.value for p in Phase if not p.terminal]}") from None


def _finish(state, as_json: bool) -> None:
    run_dir = Path(state.run_dir)
    if as_json:
        report = run_dir / "report.json"
        console.print_json(report.read_text() if report.exists() else state.model_dump_json())
    else:
        console.print()
        _summary_table(state)
        if (run_dir / "report.md").exists():
            console.print(f"\nReport: [bold]{run_dir / 'report.md'}[/bold]")
        if state.phase is Phase.ACCEPTED:
            console.print(f"Branch [bold]{state.branch}[/bold] in {state.repo} holds the accepted migration.\n"
                          f"Review it with `git diff {state.base_commit[:12]} {state.branch}` and merge when ready.")
        elif state.phase is Phase.PAUSED:
            console.print(f"[yellow]Paused:[/yellow] {state.pause_reason}\nResume with `migrationguard resume {state.run_id}`.")
    code = {Phase.ACCEPTED: 0, Phase.PAUSED: 3, Phase.REJECTED: 1}.get(state.phase, 2)
    raise typer.Exit(code)


def _summary_table(state) -> None:
    table = Table(title=f"Run {state.run_id}", show_header=False, title_justify="left")
    table.add_column(style="bold")
    table.add_column()
    colour = {"accepted": "green", "rejected": "red", "failed": "red", "paused": "yellow"}.get(state.phase.value, "")
    table.add_row("Outcome", f"[{colour}]{state.phase.value.upper()}[/{colour}]" if colour else state.phase.value)
    if state.decision:
        table.add_row("Reason", state.decision.reason)
    table.add_row("Iterations", str(state.iteration))
    table.add_row("Repair attempts", str(state.repair_attempts))
    table.add_row("Agent", state.agent_backend)
    table.add_row("Sandbox", state.sandbox_backend)
    table.add_row("Cost", f"${state.usage.cost_usd:.4f} ({state.usage.total_tokens:,} tokens, {state.agent_calls} agent call(s))")
    table.add_row("Active time", f"{state.active_seconds:.1f}s")
    console.print(table)


# ============================================================================ commands
@app.command()
def init(
    path: Path = typer.Argument(Path("."), help="Repository root."),
    force: bool = typer.Option(False, help="Overwrite an existing configuration."),
) -> None:
    """Write a commented migrationguard.toml into the repository."""
    target = path / CONFIG_FILENAME
    if target.exists() and not force:
        err.print(f"{target} already exists (use --force)")
        raise typer.Exit(1)
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    target.write_text(DEFAULT_TOML.format(python=python), encoding="utf-8")
    console.print(f"wrote {target}")


@app.command()
def analyze(
    path: Path = typer.Argument(Path("."), help="Repository root."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    show_all: bool = typer.Option(False, "--all", help="List every finding, not only those needing an agent."),
) -> None:
    """Static analysis + migration plan. Executes nothing and changes nothing."""
    from migrationguard.migrate.recipe import PydanticV2Recipe

    config = MigrationGuardConfig.load(path)
    recipe = PydanticV2Recipe(config.migration.target_requirement, config.migration.settings_requirement)
    analysis = recipe.analyze(path.resolve())
    plan = recipe.plan(analysis)
    if as_json:
        console.print_json(json.dumps({"summary": analysis.summary(), "plan": plan.model_dump(),
                                       "findings": [f.model_dump() for f in analysis.findings]}, default=str))
        return
    summary = analysis.summary()
    console.print(f"[bold]{summary['models']}[/bold] Pydantic model(s) in {summary['python_files']} Python file(s); "
                  f"[bold]{summary['findings']}[/bold] finding(s): [green]{summary['auto_fixable']} auto-fixable[/green], "
                  f"[yellow]{summary['unresolved']} need an agent[/yellow]")
    if analysis.pydantic_specs:
        console.print("Declared: " + ", ".join(f"{s} ({f})" for f, s in analysis.pydantic_specs))
    table = Table(title="Plan", title_justify="left")
    table.add_column("Kind")
    table.add_column("Step")
    table.add_column("Where")
    for step in plan.steps:
        table.add_row(step.kind, step.title, ", ".join(step.paths[:3]) + (" …" if len(step.paths) > 3 else ""))
    console.print(table)
    findings = analysis.findings if show_all else [f for f in analysis.findings if not f.auto_fixable]
    if findings:
        ft = Table(title="Findings" if show_all else "Findings requiring judgement", title_justify="left")
        for col in ("Location", "Rule", "Severity", "Message"):
            ft.add_column(col)
        for f in findings:
            ft.add_row(f"{f.path}:{f.line}", f.rule, f.severity.value, f.message)
        console.print(ft)


@app.command()
def run(
    path: Path = typer.Argument(Path("."), help="Git repository to migrate."),
    agent: Optional[str] = typer.Option(None, help="Agent backend: claude | none (default from config: claude)."),
    sandbox: Optional[str] = typer.Option(None, help="Sandbox backend: auto | docker | local."),
    model: Optional[str] = typer.Option(None, help="Claude model for the repair agent."),
    max_repairs: Optional[int] = typer.Option(None, "--max-repairs", help="Maximum repair iterations."),
    budget: Optional[float] = typer.Option(None, "--budget", help="Maximum agent spend in USD."),
    review: Optional[bool] = typer.Option(None, "--review/--no-review", help="Run the independent reviewer agent."),
    probes: Optional[bool] = typer.Option(None, "--probes/--no-probes", help="Differential behavioural probes."),
    stop_after: Optional[str] = typer.Option(None, "--stop-after", help="Pause after this phase (resume later)."),
    include_uncommitted: bool = typer.Option(False, "--include-uncommitted", help="Migrate uncommitted tracked changes too."),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Show agent tool calls and phase timings."),
    as_json: bool = typer.Option(False, "--json", help="Print the JSON report at the end."),
) -> None:
    """Migrate a repository. The original checkout is never modified; the result lands on
    branch migrationguard/<run-id>."""
    from migrationguard.harness.orchestrator import RunOptions

    try:
        state = _orchestrator(verbose, quiet=as_json).start(
            path,
            RunOptions(agent=agent, sandbox=sandbox, model=model, max_repair_iterations=max_repairs, budget_usd=budget,
                       reviewer=review, probes=probes, stop_after=_parse_phase(stop_after),
                       include_uncommitted=include_uncommitted),
        )
    except MigrationGuardError as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from None
    _finish(state, as_json)


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="Run id (prefix is enough)."),
    budget: Optional[float] = typer.Option(None, "--budget", help="Raise the USD budget before resuming."),
    max_repairs: Optional[int] = typer.Option(None, "--max-repairs"),
    stop_after: Optional[str] = typer.Option(None, "--stop-after"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Resume a paused, interrupted or crashed run from its last checkpoint."""
    from migrationguard.harness.orchestrator import RunOptions

    try:
        state = _orchestrator(verbose, quiet=as_json).resume(
            run_id, RunOptions(budget_usd=budget, max_repair_iterations=max_repairs, stop_after=_parse_phase(stop_after))
        )
    except MigrationGuardError as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from None
    _finish(state, as_json)


def _store():
    from migrationguard.harness.store import Store

    return Store(home_dir() / "migrationguard.db")


def _run_row(store, run_id: str) -> dict:
    row = store.get_run(run_id)
    if row is None:
        err.print(f"[red]unknown run {run_id}[/red]")
        raise typer.Exit(2)
    return row


@app.command()
def status(
    run_id: Optional[str] = typer.Argument(None, help="Show one run in detail."),
    limit: int = typer.Option(20, help="Number of runs to list."),
    quiet: bool = typer.Option(False, "-q", "--quiet", help="Only print run ids (newest first)."),
) -> None:
    """List runs, or show the checkpoints / evidence / attempts of one run."""
    store = _store()
    if run_id is None and quiet:
        for r in store.list_runs(limit):
            print(r["id"])
        return
    if run_id is None:
        table = Table(title="Runs", title_justify="left")
        table.add_column("Run", no_wrap=True, min_width=22)
        for col in ("Phase", "Repository", "Updated"):
            table.add_column(col)
        for r in store.list_runs(limit):
            table.add_row(r["id"], r["phase"], r["repo"], r["updated_at"][:19])
        console.print(table)
        return
    row = _run_row(store, run_id)
    console.print(f"[bold]{row['id']}[/bold]  phase=[bold]{row['phase']}[/bold]  repo={row['repo']}  branch={row['branch']}")
    cps = store.checkpoints(row["id"])
    console.print("Checkpoints: " + " → ".join(f"{c['seq']}:{c['phase']}" for c in cps))
    ev = store.evidence(row["id"])
    if ev:
        last = max(e["iteration"] for e in ev)
        table = Table(title=f"Evidence (iteration {last})", title_justify="left")
        for col in ("Gate", "Status", "Blocking", "Summary"):
            table.add_column(col)
        for e in [e for e in ev if e["iteration"] == last]:
            colour = {"pass": "green", "fail": "red", "warn": "yellow"}.get(e["status"], "dim")
            table.add_row(e["gate"], f"[{colour}]{e['status']}[/{colour}]", "yes" if e["blocking"] else "no", e["summary"][:110])
        console.print(table)
    attempts = store.attempts(row["id"])
    if attempts:
        table = Table(title="Agent attempts", title_justify="left")
        for col in ("#", "Role", "Status", "Summary"):
            table.add_column(col)
        for a in attempts:
            table.add_row(str(a["iteration"]), a["role"], a["status"], (a.get("summary") or a.get("error") or "")[:100])
        console.print(table)
    usage = store.usage_total(row["id"])
    console.print(f"Agent usage: ${usage.cost_usd:.4f}, {usage.total_tokens:,} tokens")


@app.command()
def logs(
    run_id: str = typer.Argument(...),
    follow: bool = typer.Option(False, "-f", "--follow", help="Keep printing new events."),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Include debug events (agent tool calls)."),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON lines."),
) -> None:
    """Print the structured event log of a run."""
    store = _store()
    row = _run_row(store, run_id)
    sink = _sink(verbose)
    last = 0
    while True:
        for e in store.events(row["id"], after_id=last):
            last = e["id"]
            event = {"ts": e["ts"], "phase": e["phase"], "kind": e["kind"], "level": e["level"], "message": e["message"],
                     "data": json.loads(e["data_json"]) if e["data_json"] else None}
            if as_json:
                print(json.dumps(event, default=str))
            else:
                sink(event)
        if not follow:
            break
        current = store.get_run(row["id"])
        if current and Phase(current["phase"]).terminal:
            break
        time.sleep(1.0)


@app.command()
def report(
    run_id: str = typer.Argument(...),
    fmt: str = typer.Option("md", "--format", help="md | json"),
    raw: bool = typer.Option(False, "--raw", help="Print markdown source instead of rendering it."),
) -> None:
    """Show the verification report of a finished run."""
    store = _store()
    row = _run_row(store, run_id)
    path = Path(row["run_dir"]) / ("report.json" if fmt == "json" else "report.md")
    if not path.exists():
        err.print("no report yet (the run has not reached a decision)")
        raise typer.Exit(1)
    text = path.read_text(encoding="utf-8")
    if fmt == "json" or raw:
        print(text)
    else:
        console.print(Markdown(text))


@app.command()
def diff(run_id: str = typer.Argument(...), stat: bool = typer.Option(False, "--stat")) -> None:
    """Show the migration diff of a run (base commit → migrated branch)."""
    from migrationguard.sandbox.git import git

    store = _store()
    row = _run_row(store, run_id)
    args = ["diff", "--no-color"] + (["--stat"] if stat else []) + [row["base_commit"], row["branch"]]
    print(git(Path(row["repo"]), *args), end="")


@app.command()
def cleanup(
    run_id: str = typer.Argument(...),
    delete_branch: bool = typer.Option(False, "--delete-branch", help="Also delete the migrationguard branch."),
) -> None:
    """Remove a run's worktrees (evidence, reports and the branch are kept)."""
    from migrationguard.sandbox.git import GitRepo, WorktreeManager

    store = _store()
    row = _run_row(store, run_id)
    WorktreeManager(GitRepo(Path(row["repo"])), Path(row["run_dir"])).remove(keep_branch=not delete_branch, branch=row["branch"])
    console.print(f"removed worktrees of {row['id']}" + (" and its branch" if delete_branch else ""))


@app.command()
def doctor() -> None:
    """Check the local toolchain: git, uv, Python, Docker, runtime image, Claude Agent SDK."""
    import shutil

    from migrationguard.agents.claude_sdk import sdk_available
    from migrationguard.sandbox.docker import docker_daemon_ok, image_exists
    from migrationguard.sandbox.envs import find_python, uv_binary
    from migrationguard.sandbox.local import _netns_available

    config = MigrationGuardConfig()
    rows: list[tuple[str, bool, str]] = []
    rows.append(("git", shutil.which("git") is not None, shutil.which("git") or "not found"))
    try:
        rows.append(("uv", True, uv_binary()))
    except Exception as exc:
        rows.append(("uv", False, str(exc)))
    try:
        rows.append((f"python {config.environment.python}", True, str(find_python(config.environment.python, install=False))))
    except Exception as exc:
        rows.append((f"python {config.environment.python}", False, f"{exc} (it is installed on first run)"))
    ok, why = docker_daemon_ok()
    rows.append(("docker daemon", ok, why))
    image = config.image_for()
    rows.append(("runtime image", image_exists(image), image if image_exists(image) else f"{image} missing: migrationguard sandbox build-image"))
    rows.append(("network namespace (local sandbox)", _netns_available(), "unshare -rn"))
    ok, why = sdk_available()
    rows.append(("claude-agent-sdk", ok, why))
    rows.append(("claude CLI", shutil.which("claude") is not None, shutil.which("claude") or "bundled with the SDK or install Claude Code"))
    table = Table(title="MigrationGuard doctor", title_justify="left")
    for col in ("Check", "OK", "Detail"):
        table.add_column(col)
    for name, good, detail in rows:
        table.add_row(name, "[green]yes[/green]" if good else "[yellow]no[/yellow]", detail)
    console.print(table)
    console.print(f"home: {home_dir()}")


# ---------------------------------------------------------------------------- sandbox
@sandbox_app.command("check")
def sandbox_check(backend: str = typer.Option("auto", help="auto | docker | local")) -> None:
    """Show which sandbox would be used and run a smoke test inside it."""
    from migrationguard.sandbox.base import Bind, ExecSpec
    from migrationguard.sandbox.envs import find_python
    from migrationguard.sandbox.runner import make_sandbox

    config = MigrationGuardConfig.model_validate({"sandbox": {"backend": backend}})
    sb = make_sandbox(config, log=lambda m: console.print(f"[yellow]{m}[/yellow]"))
    console.print(f"backend: [bold]{sb.name}[/bold] — {sb.isolation}")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        ws.mkdir()
        (ws / "probe.py").write_text(
            "import os, socket\n"
            "try:\n    open('escape.txt','w').write('x'); print('workspace writable: YES')\n"
            "except OSError: print('workspace writable: no')\n"
            "try:\n    socket.create_connection(('1.1.1.1', 53), timeout=2); print('network: YES')\n"
            "except OSError: print('network: no')\n"
            "print('uid', os.getuid())\n"
        )
        result = sb.run(ExecSpec(argv=["${python}", "${workspace}/probe.py"], binds={"workspace": Bind(ws)}, timeout=60,
                                 interpreter=find_python(config.environment.python)))
        console.print(result.stdout.strip() or result.stderr.strip())
        console.print("host workspace modified: " + ("YES" if (ws / "escape.txt").exists() else "no"))


@sandbox_app.command("build-image")
def sandbox_build_image(
    python: Optional[str] = typer.Option(None, help="Python version (default from config)."),
    strategy: str = typer.Option("auto", help="auto | dockerfile | standalone"),
) -> None:
    """Build the Docker runtime image used by the docker sandbox."""
    from migrationguard.sandbox.docker import build_runtime_image

    version = python or MigrationGuardConfig().environment.python
    try:
        tag = build_runtime_image(version, strategy=strategy, log=lambda m: console.print(m))
    except MigrationGuardError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    console.print(f"[green]ready:[/green] {tag}")


# ---------------------------------------------------------------------------- bench
@bench_app.command("list")
def bench_list() -> None:
    """List benchmark cases and strategies."""
    from migrationguard.bench.cases import load_cases
    from migrationguard.bench.strategies import STRATEGIES

    table = Table(title="Cases", title_justify="left")
    for col in ("Case", "Difficulty", "Traps"):
        table.add_column(col)
    for case in load_cases():
        table.add_row(case.name, case.difficulty, "; ".join(case.traps) or "—")
    console.print(table)
    st = Table(title="Strategies", title_justify="left")
    st.add_column("Strategy")
    st.add_column("Description")
    for name, s in STRATEGIES.items():
        st.add_row(name, s.description)
    console.print(st)


@bench_app.command("run")
def bench_run(
    cases: Optional[str] = typer.Option(None, help="Comma-separated case names (default: all)."),
    strategies: str = typer.Option("bump-only,codemod-only,migrationguard-deterministic",
                                   help="Comma-separated strategies (see `bench list`)."),
    out: Path = typer.Option(Path("bench-results"), help="Output directory."),
    sandbox: str = typer.Option("auto", help="Sandbox backend."),
    budget: float = typer.Option(5.0, help="Per-run agent budget in USD (Claude strategies)."),
    model: Optional[str] = typer.Option(None, help="Claude model for agent strategies."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Run the benchmark matrix and write results.json + results.md."""
    from migrationguard.bench.runner import BenchConfig, run_benchmark

    cfg = BenchConfig(cases=[c for c in (cases or "").split(",") if c] or None,
                      strategies=[s for s in strategies.split(",") if s], out=out, sandbox=sandbox,
                      budget_usd=budget, model=model, verbose=verbose)
    results = run_benchmark(cfg, log=lambda m: console.print(m))
    console.print(f"\n[bold]{len(results.rows)} run(s)[/bold] → {out / 'results.md'}")
    console.print(Markdown((out / "results.md").read_text()))


@bench_app.command("report")
def bench_report(
    out: Path = typer.Option(Path("bench-results")),
    merge: Optional[list[Path]] = typer.Option(None, "--merge", help="Merge results.json from these directories into OUT."),
) -> None:
    """Re-render results.md from results.json (optionally merging several result directories)."""
    from migrationguard.bench.runner import BenchResults, render_results

    if merge:
        parts = [BenchResults.model_validate_json((d / "results.json").read_text()) for d in merge]
        results = parts[0].model_copy(deep=True)
        for extra in parts[1:]:
            results.rows.extend(extra.rows)
            for key, value in extra.environment.items():
                if isinstance(value, list):
                    results.environment[key] = sorted(set(results.environment.get(key, [])) | set(value))
        out.mkdir(parents=True, exist_ok=True)
        (out / "results.json").write_text(results.model_dump_json(indent=2))
    results = BenchResults.model_validate_json((out / "results.json").read_text())
    (out / "results.md").write_text(render_results(results))
    console.print(Markdown((out / "results.md").read_text()))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
