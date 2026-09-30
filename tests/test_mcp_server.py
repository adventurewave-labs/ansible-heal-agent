"""The read-only MCP server: protocol, tools, and that it never writes."""

from __future__ import annotations

import io
import json
import subprocess
import sys

from agent import mcp_server as m


def _call(name, args=None, mid=1):
    return m.handle({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                     "params": {"name": name, "arguments": args or {}}})


def test_initialize_and_list():
    init = m.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18"}})["result"]
    assert init["capabilities"]["tools"] == {"listChanged": False}
    assert init["serverInfo"]["name"] == "ansible-heal-agent"
    tools = m.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"diagnose", "explain_decline", "list_failure_classes"}
    assert all(t["annotations"]["readOnlyHint"] for t in tools)
    assert not any("apply" in n or "commit" in n for n in names)


def test_notifications_get_no_response_and_unknowns_error():
    assert m.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert m.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"] == -32601
    assert _call("apply_fix")["error"]["code"] == -32602


def test_diagnose_reports_findings_and_writes_nothing(scratch_repo):
    before = subprocess.run(["git", "status", "--porcelain"], cwd=scratch_repo,
                            capture_output=True, text=True).stdout
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=scratch_repo,
                          capture_output=True, text=True).stdout
    res = _call("diagnose", {"repo": str(scratch_repo)})["result"]
    assert res["isError"] is False
    data = res["structuredContent"]
    assert data["green"] is False
    types = sorted(f["failure"]["type"] for f in data["findings"])
    assert "undefined_variable" in types
    assert any(f["diff"] for f in data["findings"])
    assert json.loads(res["content"][0]["text"]) == data
    after = subprocess.run(["git", "status", "--porcelain"], cwd=scratch_repo,
                           capture_output=True, text=True).stdout
    assert after == before
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=scratch_repo,
                          capture_output=True, text=True).stdout == head


def test_explain_decline(scratch_repo):
    res = _call("explain_decline", {"repo": str(scratch_repo),
                                    "failure": {"type": "meteor_strike"}})
    data = res["result"]["structuredContent"]
    assert data["verdict"] == "declines" and "meteor_strike" in data["explanation"]

    ok = _call("explain_decline", {"repo": str(scratch_repo), "failure": {
        "type": "undefined_variable", "variable": "brand_new_thing"}})
    assert ok["result"]["structuredContent"]["verdict"] == "would fix"


def test_bad_arguments_are_tool_errors_not_crashes(tmp_path):
    for args in ({}, {"repo": "relative/path"}, {"repo": str(tmp_path / "missing")},
                 {"repo": str(tmp_path), "runner": "prod"}):
        res = _call("diagnose", args)["result"]
        assert res["isError"] is True, args


def test_list_failure_classes():
    data = _call("list_failure_classes")["result"]["structuredContent"]
    assert {c["rule"] for c in data["classes"]} >= {"AHA001", "AHA004"}


def test_stdio_loop_keeps_stdout_pure():
    stdin = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        '\n'
        'not json\n'
        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/call",'
        '"params":{"name":"list_failure_classes","arguments":{}}}\n')
    out = io.StringIO()
    m.serve(stdin, out)
    lines = [json.loads(ln) for ln in out.getvalue().splitlines()]
    assert [ln.get("id") for ln in lines] == [1, None, 2]
    assert lines[1]["error"]["code"] == -32700


def test_cli_subprocess_round_trip():
    proc = subprocess.run(
        [sys.executable, "-m", "agent.cli", "mcp"],
        input='{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n',
        capture_output=True, text=True, timeout=60)
    (line,) = proc.stdout.splitlines()
    assert json.loads(line)["id"] == 7
