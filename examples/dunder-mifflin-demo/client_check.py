#!/usr/bin/env python3
"""Reuse the isolated real-client harness with the eight team-demo questions."""
import importlib.util
import json
from pathlib import Path

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('client_matrix',HERE.parent/'mcp-demo/client_matrix.py')
harness=importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def prompt():
    questions=json.loads((HERE/'questions.json').read_text())
    return (
        'Use only the unshackle MCP overview, search, read and run_query tools. '
        'Do not use shell, filesystem, web, other servers or subagents. Inspect overview, '
        'search recognized revenue, and read knowledge/team-demo.md before querying. '
        'Answer every question with actual run_query SQL results. Scenario and history are separate. '
        'Return only a JSON object q1..q8 mapping to arrays of result rows, with numeric values as JSON numbers.\n'
        +'\n'.join(q['id']+': '+q['question'] for q in questions)
    )

harness.prompt=prompt
if __name__=='__main__': harness.main()
