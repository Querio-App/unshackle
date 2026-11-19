import sys
from types import SimpleNamespace

from click.testing import CliRunner

from unshackle.cli import main
from unshackle.ir import Bundle


def test_non_bi_import_writes_immediately(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    target = tmp_path / 'target'
    monkeypatch.setitem(sys.modules, 'unshackle.imports.dbt', SimpleNamespace(parse=lambda p: Bundle('dbt')))
    result = CliRunner().invoke(main, ['import', 'dbt', str(project), '--repo', str(target)])
    assert result.exit_code == 0, result.output
    assert (target / 'migration/report.md').exists()


def test_import_argument_validation(tmp_path, monkeypatch):
    monkeypatch.delenv('UNSHACKLE_WAREHOUSE_URL', raising=False)
    runner = CliRunner()
    for args, expected in [(['tableau'], 'at least one path'), (['dbt'], 'exactly one'),
                           (['sql'], 'at least one'), (['warehouse'], 'UNSHACKLE_WAREHOUSE_URL')]:
        result = runner.invoke(main, ['import', *args, '--repo', str(tmp_path)])
        assert result.exit_code != 0
        assert expected in result.output
