"""Claude Agent SDK backend.

Security model
--------------
* ``tools=[]`` removes every built-in Claude Code tool (Bash, Read, Write, Edit, WebFetch...).
  The agent only gets MigrationGuard's in-process MCP tools (``mcp__mg__*``), which are
  confined to the worktree and pass through the permission policy.
* ``permission_mode="dontAsk"`` + an explicit ``allowed_tools`` list + ``disallowed_tools``
  for every built-in + a ``PreToolUse`` hook all deny anything outside the allow-list
  (defense in depth: any one of them is sufficient).
* ``cwd`` is an empty scratch directory and ``setting_sources=[]`` so no user/project
  settings, CLAUDE.md files or hooks are loaded.
* ``max_turns`` and ``max_budget_usd`` bound every session; the harness budget tracker
  bounds the run.
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator, Callable
from typing import Any

import anyio

from migrationguard.agents.base import AgentTask
from migrationguard.errors import AgentQuotaExceeded, AgentUnavailable, TransientError
from migrationguard.models import AgentResult, AgentUsage

BUILTIN_TOOLS = [
    "Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep",
    "WebFetch", "WebSearch", "Task", "TodoWrite", "ExitPlanMode", "SlashCommand", "Skill", "Agent",
]

SERVER = "mg"


QUOTA_MARKERS = ("session limit", "usage limit", "hit your limit", "you've hit your", "credit balance", "quota")
TRANSIENT_MARKERS = ("rate limit", "rate_limit", "overloaded", "529", "503", "timeout", "timed out", "connection")


def classify_sdk_error(exc: BaseException) -> None:
    """Re-raise SDK failures as harness error types (quota → pause, transient → retry)."""
    text = str(exc)
    lowered = text.lower()
    if any(marker in lowered for marker in QUOTA_MARKERS):
        raise AgentQuotaExceeded(f"Claude usage limit reached: {text[:300]}") from exc
    try:
        from claude_agent_sdk import CLIConnectionError, ProcessError
    except ImportError:  # pragma: no cover
        CLIConnectionError = ProcessError = ()  # type: ignore[assignment,misc]
    if isinstance(exc, (CLIConnectionError, ProcessError)) or any(m in lowered for m in TRANSIENT_MARKERS):
        raise TransientError(f"Claude Agent SDK session failed: {text[:300]}") from exc


def sdk_available() -> tuple[bool, str]:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False, "claude-agent-sdk is not installed (pip install 'migrationguard[agent]')"
    return True, "claude-agent-sdk available"


def _text(result: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": result}]}


def build_tools(toolbox, role: str):
    """Wrap the toolbox as SDK MCP tools. Reviewers get read-only tools only."""
    from claude_agent_sdk import tool

    tools = []

    @tool("list_files", "List repository files matching a glob (default **/*.py).", {"pattern": str})
    async def list_files(args):
        return _text(toolbox.list_files(args.get("pattern") or "**/*.py"))

    @tool(
        "read_file",
        "Read a file with line numbers. Optional start_line/end_line (1-based, inclusive).",
        {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"},
                                           "end_line": {"type": "integer"}}, "required": ["path"]},
    )
    async def read_file(args):
        return _text(toolbox.read_file(args["path"], args.get("start_line") or 1, args.get("end_line")))

    @tool(
        "search",
        "Regex search over Python files. Returns path:line: text.",
        {"type": "object", "properties": {"pattern": {"type": "string"}, "glob": {"type": "string"}}, "required": ["pattern"]},
    )
    async def search(args):
        return _text(toolbox.search(args["pattern"], args.get("glob") or "**/*.py"))

    @tool("show_diff", "Show the full diff of the migration so far against the original code.", {})
    async def show_diff(args):
        return _text(toolbox.show_diff())

    @tool("show_evidence", "Show the latest verification evidence (all gates, with details).", {})
    async def show_evidence(args):
        return _text(toolbox.show_evidence())

    tools += [list_files, read_file, search, show_diff, show_evidence]

    if role == "repair":

        @tool("show_findings", "Re-run the dry-run codemod on the current code and list remaining v1 usages.", {})
        async def show_findings(args):
            return _text(await anyio.to_thread.run_sync(toolbox.show_findings))

        @tool(
            "write_file",
            "Create or overwrite a file (policy-checked; tests and test config are protected).",
            {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
             "required": ["path", "content"]},
        )
        async def write_file(args):
            return _text(toolbox.write_file(args["path"], args["content"]))

        @tool(
            "replace_in_file",
            "Replace one exact, unique occurrence of `old` with `new` in a file (policy-checked).",
            {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
             "required": ["path", "old", "new"]},
        )
        async def replace_in_file(args):
            return _text(toolbox.replace_in_file(args["path"], args["old"], args["new"]))

        @tool(
            "run_tests",
            "Run the test suite (or the given pytest node ids) in the sandbox against the migrated code.",
            {"type": "object", "properties": {"node_ids": {"type": "array", "items": {"type": "string"}}}},
        )
        async def run_tests(args):
            return _text(await anyio.to_thread.run_sync(lambda: toolbox.run_tests(args.get("node_ids") or [])))

        @tool("run_probes", "Run the differential behavioural probes (baseline vs current code).", {})
        async def run_probes(args):
            return _text(await anyio.to_thread.run_sync(toolbox.run_probes))

        @tool(
            "finish",
            "Finish the attempt with an honest summary and concrete claims (they will be verified).",
            {"type": "object", "properties": {"summary": {"type": "string"}, "claims": {"type": "array", "items": {"type": "string"}}},
             "required": ["summary"]},
        )
        async def finish(args):
            return _text(toolbox.finish(args["summary"], args.get("claims") or []))

        tools += [show_findings, write_file, replace_in_file, run_tests, run_probes, finish]
    else:

        @tool(
            "submit_review",
            "Submit the review verdict exactly once.",
            {
                "type": "object",
                "properties": {
                    "verdict": {"type": "string", "enum": ["approve", "reject"]},
                    "summary": {"type": "string"},
                    "issues": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "line": {"type": "integer"},
                                "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                                "explanation": {"type": "string"},
                            },
                            "required": ["explanation"],
                        },
                    },
                },
                "required": ["verdict", "summary"],
            },
        )
        async def submit_review(args):
            return _text(toolbox.submit_review(args["verdict"], args.get("summary", ""), args.get("issues") or []))

        tools.append(submit_review)
    return tools


class ClaudeAgentBackend:
    name = "claude"

    def __init__(self, model: str | None = None, reviewer_model: str | None = None,
                 log: Callable[[str, dict[str, Any]], None] = lambda kind, data: None):
        ok, why = sdk_available()
        if not ok:
            raise AgentUnavailable(why)
        self.model = model
        self.reviewer_model = reviewer_model or model
        self.log = log

    def repair(self, task: AgentTask) -> AgentResult:
        return anyio.run(self._session, task, "repair")

    def review(self, task: AgentTask) -> AgentResult:
        task.toolbox.read_only = True
        return anyio.run(self._session, task, "reviewer")

    async def _session(self, task: AgentTask, role: str) -> AgentResult:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            HookMatcher,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
            create_sdk_mcp_server,
            query,
        )

        tools = build_tools(task.toolbox, "repair" if role == "repair" else "reviewer")
        server = create_sdk_mcp_server(name=SERVER, version="1.0.0", tools=tools)
        allowed = [f"mcp__{SERVER}__{t.name}" for t in tools]
        denied_outside: list[str] = []

        async def pre_tool_use(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
            name = input_data.get("tool_name", "")
            self.log("agent.tool", {"role": role, "tool": name})
            if name not in allowed:
                denied_outside.append(name)
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                               "permissionDecisionReason": "tool not allowed by MigrationGuard"}}
            return {}

        scratch = tempfile.mkdtemp(prefix="mg-agent-cwd-")
        options = ClaudeAgentOptions(
            tools=[],
            mcp_servers={SERVER: server},
            allowed_tools=allowed,
            disallowed_tools=BUILTIN_TOOLS,
            permission_mode="dontAsk",
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])]},
            system_prompt=task.system_prompt,
            max_turns=task.max_turns,
            max_budget_usd=max(0.05, task.max_budget_usd),
            model=task.model or (self.model if role == "repair" else self.reviewer_model),
            cwd=scratch,
            setting_sources=[],
        )

        async def prompt_stream() -> AsyncIterator[dict[str, Any]]:
            yield {"type": "user", "message": {"role": "user", "content": task.prompt}, "parent_tool_use_id": None}

        result = AgentResult(role="repair" if role == "repair" else "reviewer", status="finished")
        texts: list[str] = []
        try:
            async for message in query(prompt=prompt_stream(), options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            texts.append(block.text.strip())
                            self.log("agent.text", {"role": role, "text": block.text.strip()[:600]})
                        elif isinstance(block, ToolUseBlock):
                            result.tool_calls += 1
                elif isinstance(message, ResultMessage):
                    usage = message.usage or {}
                    result.usage = AgentUsage(
                        input_tokens=int(usage.get("input_tokens") or 0),
                        output_tokens=int(usage.get("output_tokens") or 0),
                        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                        cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
                        cost_usd=float(message.total_cost_usd or 0.0),
                        turns=int(message.num_turns or 0),
                    )
                    subtype = message.subtype or ""
                    if "max_turns" in subtype:
                        result.status = "turn_limit"
                    elif "budget" in subtype:
                        result.status = "budget"
                    elif message.is_error:
                        result.status = "error"
                        result.error = (message.result or subtype)[:1000]
        except Exception as exc:
            classify_sdk_error(exc)
            raise
        toolbox = task.toolbox
        result.summary = toolbox.finish_summary or (texts[-1][:2000] if texts else "")
        result.claims = list(toolbox.claims)
        result.files_written = sorted(toolbox.files_written)
        result.denied_tool_calls = toolbox.denied + len(denied_outside)
        result.review = toolbox.verdict
        return result
