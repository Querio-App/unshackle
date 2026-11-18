#!/usr/bin/env python3
"""Run one real client against the demo using temporary configuration.

This calls the selected client's model service and may consume its subscription.
Raw transcripts remain outside the source repo; review/sanitize before publishing.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent


def prompt() -> str:
    questions = json.loads((HERE / "questions.json").read_text())
    return (
        "This is an acceptance test of the unshackle MCP server. Use only its overview, search, read, "
        "and run_query tools. Do not use shell, direct filesystem access, web search, other MCP servers, "
        "or subagents. First inspect overview and search/read the context to establish the business "
        "definition of recognized revenue. Then execute SQL through run_query to answer all five "
        "questions; do not infer numbers without querying. TODO-marked dynamic SQL is not a usable query. "
        "Return only a JSON object mapping q1..q5 to arrays of result rows, with numeric amounts as "
        "JSON numbers. Do not include SQL or column headers in the final JSON.\n\n"
        + "\n".join(f'{q["id"]}: {q["question"]}' for q in questions)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client", choices=["claude", "codex", "cursor"])
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--executable", type=Path, required=True, help="Absolute unshackle executable")
    parser.add_argument("--output", type=Path, required=True, help="Private evidence directory outside source repo")
    parser.add_argument("--timeout", type=int, default=360)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    text = prompt()
    (out / "prompt.txt").write_text(text)
    server = {"command": str(args.executable.resolve()), "args": ["mcp", "--repo", str(args.repo.resolve())]}
    env = dict(os.environ)
    # MCP reads only the fixture's .env, not a caller's unrelated warehouse.
    env.pop("UNSHACKLE_WAREHOUSE_URL", None)
    executable = {"claude": "claude", "codex": "codex", "cursor": "cursor-agent"}[args.client]
    version = subprocess.check_output([executable, "--version"], text=True).strip()
    source_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE, text=True).strip()
    with tempfile.TemporaryDirectory(prefix="unshackle-client-") as temporary:
        workspace = Path(temporary)
        mcp = workspace / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"unshackle": server}}))
        if args.client == "claude":
            command = [executable, "-p", text, "--output-format", "stream-json", "--verbose",
                       "--strict-mcp-config", "--mcp-config", str(mcp), "--setting-sources", "",
                       "--no-session-persistence", "--disable-slash-commands", "--tools", "",
                       "--allowedTools", "mcp__unshackle__overview,mcp__unshackle__search,mcp__unshackle__read,mcp__unshackle__run_query"]
        elif args.client == "codex":
            config = "mcp_servers.unshackle=" + "{command=" + json.dumps(server["command"]) + ",args=" + json.dumps(server["args"]) + "}"
            command = [executable, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                       "--sandbox", "read-only", "--json", "-c", config,
                       "-c", 'mcp_servers.unshackle.default_tools_approval_mode="approve"', text]
        else:
            config_dir = workspace / "cursor-config"
            config_dir.mkdir()
            env["CURSOR_CONFIG_DIR"] = str(config_dir)
            # Reuse auth identity through the client's own keychain. Never copy token values
            # into MCP config or emit them in evidence. Preserve selected model preferences.
            original = Path.home() / ".cursor/cli-config.json"
            existing = json.loads(original.read_text()) if original.exists() else {}
            config = {k: existing[k] for k in ("authInfo", "model", "selectedModel", "modelParameters") if k in existing}
            config.update({"version": 1, "editor": {"vimMode": False}, "permissions": {"allow": [f"Mcp(unshackle:{tool})" for tool in ("overview", "search", "read", "run_query")], "deny": ["Shell(*)", "Read(**)", "Write(**)", "WebFetch(*)"]},
                           "notifications": False, "display": {"showStatusIndicators": False}})
            (config_dir / "cli-config.json").write_text(json.dumps(config))
            (config_dir / "mcp.json").write_text(mcp.read_text())
            (workspace / ".cursor").mkdir()
            shutil.copyfile(mcp, workspace / ".cursor/mcp.json")
            command = [executable, "-p", text, "--output-format", "stream-json",
                       "--approve-mcps", "--trust", "--workspace", str(workspace)]
        metadata = {"client": args.client, "version": version, "source_commit": source_commit,
                    "transport": "stdio", "config": "temporary, no persistent user configuration writes"}
        with (out / "stdout.jsonl").open("w") as stdout, (out / "stderr.txt").open("w") as stderr:
            try:
                result = subprocess.run(command, cwd=workspace, env=env, stdout=stdout, stderr=stderr, timeout=args.timeout)
                metadata["exit_code"] = result.returncode
            except subprocess.TimeoutExpired:
                metadata["timed_out"] = True
        (out / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata))
    print(f"Private transcript: {out}")


if __name__ == "__main__":
    main()
