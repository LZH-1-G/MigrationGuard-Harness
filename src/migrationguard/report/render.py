"""Structured verification reports (Markdown for humans, JSON for machines).

The report puts the decision first, then the evidence it rests on: final gate table,
iteration history, agent claims *checked against* evidence, behaviour differences,
integrity findings, cost, and the checkpoint timeline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from migrationguard.artifacts import ArtifactStore
from migrationguard.harness.state import RunState
from migrationguard.harness.store import Store
from migrationguard.models import GateResult, VerificationResult

CLAIM_RULES: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("test",), ("tests.pass", "tests.differential", "tests.integrity")),
    (("behav", "regression", "semantic"), ("behavior.probes",)),
    (("deprecat", "warning"), ("runtime.deprecations",)),
    (("migrat", "v2", "pydantic 2", "all model", "converted"), ("static.residual", "deps.target")),
    (("compile", "syntax"), ("static.syntax",)),
]

STATUS_ICON = {"pass": "✅ pass", "fail": "❌ FAIL", "warn": "⚠️ warn", "skip": "➖ skip"}


def check_claim(text: str, gates: dict[str, GateResult]) -> tuple[bool | None, str]:
    lowered = text.lower()
    for keywords, gate_names in CLAIM_RULES:
        if any(k in lowered for k in keywords):
            relevant = [gates[g] for g in gate_names if g in gates]
            if not relevant:
                return None, "no evidence available"
            failed = [g.gate for g in relevant if g.status == "fail"]
            if failed:
                return False, "contradicted by " + ", ".join(failed)
            return True, "supported by " + ", ".join(g.gate for g in relevant)
    return None, "not mechanically checkable"


def build_report(state: RunState, store: Store, artifacts: ArtifactStore) -> dict[str, Any]:
    final: VerificationResult | None = None
    if state.last_verification and artifacts.exists(state.last_verification):
        final = VerificationResult.model_validate(artifacts.load_json(state.last_verification))
    gates = {g.gate: g for g in final.gates} if final else {}
    attempts = store.attempts(state.run_id)
    claims = []
    for attempt in attempts:
        for claim in json.loads(attempt.get("claims_json") or "[]"):
            # judge claims against the verification that followed the attempt
            follow = next((h for h in state.history if h.trigger == f"repair-{attempt['iteration']}"), None)
            judged_gates = gates
            if follow is not None and artifacts.exists(f"iter-{follow.iteration:02d}/verification.json"):
                v = VerificationResult.model_validate(artifacts.load_json(f"iter-{follow.iteration:02d}/verification.json"))
                judged_gates = {g.gate: g for g in v.gates}
            verdict, why = check_claim(claim["text"], judged_gates)
            claims.append({"attempt": attempt["iteration"], "role": attempt["role"], "claim": claim["text"],
                           "verified": verdict, "evidence": why})
    usage = store.usage_total(state.run_id)
    return {
        "run_id": state.run_id,
        "repo": state.repo,
        "branch": state.branch,
        "base_commit": state.base_commit,
        "outcome": state.decision.outcome if state.decision else state.phase.value,
        "decision": state.decision.model_dump() if state.decision else None,
        "failure_reason": state.failure_reason,
        "sandbox": {"backend": state.sandbox_backend, "isolation": state.sandbox_isolation},
        "agent_backend": state.agent_backend,
        "analysis": state.analysis_summary,
        "plan": state.plan.model_dump() if state.plan else None,
        "codemod": state.codemod_summary,
        "final_verification": final.model_dump(mode="json") if final else None,
        "history": [h.model_dump() for h in state.history],
        "attempts": [
            {k: a[k] for k in ("iteration", "role", "status", "summary", "commit_sha", "error")}
            | {"usage": json.loads(a["usage_json"]) if a.get("usage_json") else None}
            for a in attempts
        ],
        "claims": claims,
        "review": state.review.model_dump() if state.review else None,
        "usage": usage.model_dump() | {"total_tokens": usage.total_tokens},
        "agent_calls": state.agent_calls,
        "active_seconds": round(state.active_seconds, 1),
        "checkpoints": store.checkpoints(state.run_id),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    outcome = report["outcome"].upper()
    decision = report.get("decision") or {}
    lines.append(f"# MigrationGuard report — {outcome}")
    lines.append("")
    lines.append(f"**Decision:** {decision.get('reason') or report.get('failure_reason') or outcome}")
    lines.append("")
    lines.append("| | |\n|---|---|")
    lines.append(f"| Run | `{report['run_id']}` |")
    lines.append(f"| Repository | `{report['repo']}` |")
    lines.append(f"| Branch | `{report['branch']}` |")
    lines.append(f"| Base commit | `{report['base_commit'][:12]}` |")
    if decision.get("final_commit"):
        lines.append(f"| Final commit | `{decision['final_commit'][:12]}` |")
    lines.append(f"| Sandbox | {report['sandbox']['isolation']} |")
    lines.append(f"| Agent backend | {report['agent_backend']} |")
    usage = report["usage"]
    lines.append(f"| Agent cost | ${usage['cost_usd']:.4f} · {usage['total_tokens']:,} tokens · {report['agent_calls']} call(s) |")
    lines.append(f"| Active time | {report['active_seconds']}s |")
    lines.append("")

    final = report.get("final_verification")
    if final:
        lines.append(f"## Evidence gates (iteration {final['iteration']}, commit `{final['commit'][:12]}`)")
        lines.append("")
        lines.append("| Gate | Status | Blocking | Summary |\n|---|---|---|---|")
        for g in final["gates"]:
            summary = g["summary"].replace("|", "\\|").replace("\n", " ")[:220]
            lines.append(f"| `{g['gate']}` | {STATUS_ICON.get(g['status'], g['status'])} | {'yes' if g['blocking'] else 'no'} | {summary} |")
        lines.append("")
        for g in final["gates"]:
            d = g.get("details") or {}
            if g["gate"] == "behavior.probes" and (d.get("diffs") or d.get("accepted")):
                lines.append("### Behavioural differences")
                lines.append("")
                for diff in (d.get("diffs") or [])[:15]:
                    lines.append(f"- **{diff['probe_id']}** `{diff['kind']}` — baseline: `{_short(diff['baseline'])}` → migrated: `{_short(diff['target'])}`")
                for diff in (d.get("accepted") or [])[:15]:
                    lines.append(f"- {diff['probe_id']} `{diff['kind']}` — accepted: {diff['accepted_reason']}")
                lines.append("")
            if g["gate"] == "tests.integrity" and d.get("violations"):
                lines.append("### Test-integrity violations")
                lines.append("")
                lines.extend(f"- {v}" for v in d["violations"][:20])
                lines.append("")
            if g["gate"] in ("tests.pass", "tests.differential") and g["status"] == "fail":
                lines.append(f"### {g['gate']} details")
                lines.append("")
                for item in (d.get("failing") or [])[:10]:
                    lines.append(f"- `{item['nodeid']}` → {item['outcome']}")
                for item in (d.get("regressions") or [])[:10]:
                    lines.append(f"- regression: `{item}`")
                for item in (d.get("missing") or [])[:10]:
                    lines.append(f"- missing: `{item}`")
                lines.append("")
            if g["gate"] == "static.residual" and d.get("findings"):
                lines.append("### Residual v1 API usage")
                lines.append("")
                lines.extend(f"- `{f}`" for f in d["findings"][:20])
                lines.append("")

    if report["history"]:
        lines.append("## Iterations")
        lines.append("")
        lines.append("| # | Trigger | Commit | Result | Failing gates |\n|---|---|---|---|---|")
        for h in report["history"]:
            lines.append(f"| {h['iteration']} | {h['trigger']} | `{h['commit'][:10]}` | {'pass' if h['passed'] else 'fail'} | {', '.join(h['failing_gates']) or '—'} |")
        lines.append("")

    if report["claims"]:
        lines.append("## Agent claims vs. evidence")
        lines.append("")
        lines.append("Claims are recorded but never used for acceptance.")
        lines.append("")
        lines.append("| Attempt | Claim | Verdict | Evidence |\n|---|---|---|---|")
        for c in report["claims"]:
            verdict = {True: "✅ supported", False: "❌ contradicted", None: "❔ unverifiable"}[c["verified"]]
            lines.append(f"| {c['attempt']} | {c['claim'][:120]} | {verdict} | {c['evidence']} |")
        lines.append("")

    if report.get("review"):
        r = report["review"]
        lines.append(f"## Independent review: {r['verdict']}")
        lines.append("")
        lines.append(r["summary"])
        lines.extend(f"- [{i['severity']}] `{i['path']}:{i['line']}` {i['explanation']}" for i in r.get("issues", []))
        lines.append("")

    analysis = report.get("analysis") or {}
    if analysis:
        lines.append("## Analysis & plan")
        lines.append("")
        lines.append(f"{analysis.get('models', 0)} model(s) in {analysis.get('python_files', 0)} file(s); "
                     f"{analysis.get('findings', 0)} finding(s): {analysis.get('auto_fixable', 0)} handled by deterministic codemods, "
                     f"{analysis.get('unresolved', 0)} requiring an agent.")
        lines.append("")
        plan = report.get("plan") or {}
        for step in plan.get("steps", [])[:40]:
            lines.append(f"- **[{step['kind']}]** {step['title']}" + (f" — {step['detail'][:200]}" if step.get("detail") else ""))
        lines.append("")

    if report["attempts"]:
        lines.append("## Agent attempts")
        lines.append("")
        lines.append("| # | Role | Status | Cost | Tokens | Summary |\n|---|---|---|---|---|---|")
        for a in report["attempts"]:
            u = a.get("usage") or {}
            tokens = sum(u.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"))
            lines.append(f"| {a['iteration']} | {a['role']} | {a['status']} | ${u.get('cost_usd', 0):.4f} | {tokens:,} | {(a.get('summary') or '').replace('|', '/')[:160]} |")
        lines.append("")

    lines.append("## Checkpoints")
    lines.append("")
    lines.append(" → ".join(f"{c['phase']}" for c in report["checkpoints"]))
    lines.append("")
    return "\n".join(lines)


def _short(value: Any, limit: int = 140) -> str:
    text = json.dumps(value, default=str, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def write_reports(state: RunState, store: Store, artifacts: ArtifactStore) -> dict[str, Path]:
    report = build_report(state, store, artifacts)
    run_dir = Path(state.run_dir)
    json_path = run_dir / "report.json"
    md_path = run_dir / "report.md"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}
