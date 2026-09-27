"""Read-only MCP server — ``ansible-heal mcp``.

Exposes the agent to MCP clients (Claude Code, Claude Desktop, any agent
harness) over stdio, so another agent can ask *what is broken and what would
you change* without being handed the power to change it.

Tools — all read-only. There is deliberately no apply tool: committing to an
operator's repository stays a decision made at a terminal, not one delegated to
whichever model is on the other end of the pipe.

``diagnose``
    Dry-run the heal loop on a repository and return every finding: failure,
    diagnosis, proposed diff, and why a fix was declined or blocked.
``explain_decline``
    Diagnose one failure dict (as returned by ``diagnose``) and explain the
    agent's decision in prose.
``list_failure_classes``
    The failure classes and SARIF rule ids the agent knows.

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout (the MCP stdio
transport). Stdlib only. Everything the agent itself prints goes to stderr so
stdout carries protocol messages and nothing else.

Register it with a client, e.g. Claude Code::

    claude mcp add ansible-heal -- ansible-heal mcp
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any, TextIO

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "ansible-heal-agent", "version": "0.1.0"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "diagnose",
        "description": (
            "Dry-run ansible-heal-agent against an Ansible repository. Runs the "
            "pipeline, diagnoses each failure, and returns the proposed fix as a "
            "unified diff — or why it declines. Never writes to or commits in "
            "the repository."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string",
                         "description": "Absolute path to the repository."},
                "playbook": {"type": "string",
                             "default": "ansible/playbooks/site.yml"},
                "runner": {"type": "string", "enum": ["mock", "real"],
                           "default": "mock"},
            },
            "required": ["repo"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "explain_decline",
        "description": (
            "Given one failure object from `diagnose`, explain what the agent "
            "would do about it and why — including why it refuses, if it does."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "failure": {"type": "object",
                            "description": "A failure dict, e.g. "
                                           "{\"type\": \"undefined_variable\", "
                                           "\"variable\": \"app_port\"}"},
            },
            "required": ["repo", "failure"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "list_failure_classes",
        "description": "The failure classes ansible-heal-agent handles, with SARIF rule ids.",
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
    },
]


class ToolError(Exception):
    """A tool-level failure, reported as ``isError`` content, not a protocol error."""


def _repo(args: dict) -> Path:
    raw = args.get("repo")
    if not isinstance(raw, str) or not raw:
        raise ToolError("'repo' must be a non-empty path")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ToolError("'repo' must be an absolute path")
    if not path.is_dir():
        raise ToolError(f"not a directory: {path}")
    return path.resolve()


@contextlib.contextmanager
def _isolated(repo: Path, runner: str = "mock"):
    """Point the agent at ``repo`` with artefacts outside it, then restore."""
    import os
    import tempfile

    from agent import config, diagnoser

    saved_env = os.environ.get("PIPELINE_RUNNER")
    saved_out = config.output_root_override()
    scratch = tempfile.mkdtemp(prefix="ansible-heal-mcp-")
    os.environ["PIPELINE_RUNNER"] = runner
    config.set_output_root(scratch)
    # Untrusted by default, exactly like --dry-run: the repo's own inventory
    # plugins do not run.
    diagnoser.set_trust_repo(False)
    try:
        with config.repo_root_override(repo), contextlib.redirect_stdout(sys.stderr):
            yield
    finally:
        config.set_output_root(saved_out)
        if saved_env is None:
            os.environ.pop("PIPELINE_RUNNER", None)
        else:
            os.environ["PIPELINE_RUNNER"] = saved_env


def _public(d: dict) -> dict:
    """Drop callables and internal keys so the payload is plain JSON."""
    return json.loads(json.dumps(d, default=str))


def tool_diagnose(args: dict) -> dict:
    from agent.core import MODE_DRY_RUN, heal
    runner = args.get("runner", "mock")
    if runner not in ("mock", "real"):
        raise ToolError("runner must be 'mock' or 'real'")
    repo = _repo(args)
    with _isolated(repo, runner):
        result = heal(playbook=args.get("playbook") or "ansible/playbooks/site.yml",
                      use_llm=False, mode=MODE_DRY_RUN)
    findings = [{
        "failure": _public(p.failure),
        "diagnosis": (p.diagnosis or {}).get("diagnosis"),
        "failure_type": (p.diagnosis or {}).get("failure_type"),
        "target_file": p.target_file,
        "diff": p.diff or None,
        "blocked_reason": p.blocked_reason,
        "declined_reason": (p.diagnosis or {}).get("_no_fix_reason"),
    } for p in result.proposals]
    return {"repo": str(repo), "pipeline_exit_code": result.final_exit_code,
            "green": result.final_exit_code == 0, "findings": findings}


def tool_explain_decline(args: dict) -> dict:
    from agent import diagnoser
    failure = args.get("failure")
    if not isinstance(failure, dict) or not failure.get("type"):
        raise ToolError("'failure' must be an object with a 'type'")
    repo = _repo(args)
    with _isolated(repo):
        diag = diagnoser.fallback_diagnose(failure)
    fix = diag.get("fix") or {}
    if fix.get("action", "none") == "none":
        verdict = "declines"
        explanation = diag.get("_no_fix_reason") or "no automated fix is available"
    else:
        verdict = "would fix"
        explanation = (f"{diag.get('diagnosis', '')} Fix: {fix.get('action')} on "
                       f"{fix.get('target_file')} — {fix.get('rationale', '')}").strip()
    return {"verdict": verdict, "explanation": explanation,
            "failure_type": diag.get("failure_type"), "fix": _public(fix)}


def tool_list_failure_classes(_args: dict) -> dict:
    from agent import sarif
    return {"classes": [{"rule": rid, "name": r["name"], "summary": r["short"],
                         "failure_types": [t for t in r["types"].split(",") if t]}
                        for rid, r in sarif.RULES.items()]}


HANDLERS = {
    "diagnose": tool_diagnose,
    "explain_decline": tool_explain_decline,
    "list_failure_classes": tool_list_failure_classes,
}


def handle(message: dict) -> dict | None:
    """Handle one JSON-RPC message; return the response, or None for notifications."""
    method = message.get("method")
    mid = message.get("id")
    is_request = "id" in message

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code: int, text: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}

    if not is_request:
        return None  # notifications/initialized, cancellations: nothing to say
    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        return ok({
            "protocolVersion": requested or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": "Read-only. Use `diagnose` on an absolute repo path; "
                            "nothing is written to it.",
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            return err(-32602, f"unknown tool: {name}")
        try:
            data = handler(params.get("arguments") or {})
        except ToolError as e:
            return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception as e:  # noqa: BLE001 - a tool crash must not kill the server
            return ok({"content": [{"type": "text",
                                    "text": f"{type(e).__name__}: {e}"}],
                       "isError": True})
        return ok({"content": [{"type": "text", "text": json.dumps(data, indent=2)}],
                   "structuredContent": data, "isError": False})
    return err(-32601, f"method not found: {method}")


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """Run the stdio loop until EOF."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as e:
            response: dict | None = {"jsonrpc": "2.0", "id": None,
                                     "error": {"code": -32700, "message": f"parse error: {e}"}}
        else:
            if not isinstance(message, dict):
                response = {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32600, "message": "invalid request"}}
            else:
                # Anything the agent prints while handling goes to stderr.
                with contextlib.redirect_stdout(io.StringIO()) as noise:
                    response = handle(message)
                if noise.getvalue():
                    sys.stderr.write(noise.getvalue())
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
