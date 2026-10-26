"""Every missing-extra error names the extra and both install commands, in one format."""

import importlib.util
import re
import sys

import pytest
from click.testing import CliRunner

from unshackle.cli import main
from unshackle.sources import _tableau_data, looker, qlik, quicksight

FORMAT = re.compile(r'''^.+ needs the `(\w+)` extra: uv tool install --force "unshackle\[run,\1\]" \(from a checkout: uv pip install -e '\.\[\1\]'\)''')


def _hide(monkeypatch, *modules):
    for m in modules:
        monkeypatch.setitem(sys.modules, m, None)  # `import m` raises ImportError


def test_tableau_with_data(monkeypatch):
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda m, *a: None if m == "tableauhyperapi" else real(m, *a))
    with pytest.raises(Exception) as e:
        _tableau_data.check_installed()
    assert FORMAT.match(str(e.value)).group(1) == "tableau"
    assert str(e.value).endswith("(missing: tableauhyperapi)")


def test_looker(monkeypatch):
    _hide(monkeypatch, "lkml")
    with pytest.raises(Exception) as e:
        looker._Parser._load_lookml(None)
    assert FORMAT.match(str(e.value)).group(1) == "looker"


def test_qlik(monkeypatch):
    _hide(monkeypatch, "websockets", "websockets.exceptions", "websockets.sync.client")
    with pytest.raises(Exception) as e:
        qlik.Engine("wss://example.com/app/x", "token")
    assert FORMAT.match(str(e.value)).group(1) == "qlik"


def test_quicksight(monkeypatch):
    _hide(monkeypatch, "boto3")
    with pytest.raises(Exception) as e:
        quicksight.QuickSightSource().client()
    assert FORMAT.match(str(e.value)).group(1) == "quicksight"


def test_tools_lists_extras():
    out = CliRunner().invoke(main, ["tools"]).output
    lines = {line.split()[0]: line for line in out.splitlines()}
    for tool in ("tableau", "looker", "qlik", "quicksight"):
        assert lines[tool].endswith(f"(install unshackle[run,{tool}])")
    assert "install" not in lines["lightdash"]
