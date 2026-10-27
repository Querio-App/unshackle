"""One install covers every source; the only extra (`hyper`) is named, with its install commands, where it's needed."""

import re
import sys

import pytest
from click.testing import CliRunner

from unshackle.cli import main
from unshackle.sources import _tableau_data

FORMAT = re.compile(r'''^.+ needs the `(\w+)` extra: pip install "unshackle\[\1\]" \(or uv tool install --force "unshackle\[\1\]"; from a checkout: uv pip install -e '\.\[\1\]'\)$''')


def test_hyper(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "tableauhyperapi", None)  # `import tableauhyperapi` raises ImportError
    with pytest.raises(ImportError) as e:
        _tableau_data._from_hyper(tmp_path / "x.hyper", ["Extract", "Extract"], [], tmp_path / "out.parquet", tmp_path)
    assert FORMAT.match(str(e.value)).group(1) == "hyper"


def test_base_install_covers_every_source():
    for module in ("lkml", "websockets", "boto3", "duckdb", "duckdb_engine", "openpyxl", "altair", "pandas", "nbclient"):
        __import__(module)


def test_tools_names_the_only_extra():
    out = CliRunner().invoke(main, ["tools"]).output
    lines = {line.split()[0]: line for line in out.splitlines()}
    assert lines["tableau"].endswith("(copying .hyper extracts needs unshackle[hyper])")
    for tool in ("looker", "qlik", "quicksight", "lightdash"):
        assert "unshackle[" not in lines[tool]
