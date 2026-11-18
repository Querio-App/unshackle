import json
from pathlib import Path

import pytest

from unshackle.mcp.repository import Repository
from unshackle.ir import Dashboard, Tile, Encoding
from unshackle.target.notebooks import render_dashboard


def test_confinement_and_layout(tmp_path):
    (tmp_path / 'unshackle.toml').write_text('[paths]\nknowledge="docs/context"\n')
    (tmp_path / 'docs/context').mkdir(parents=True)
    (tmp_path / 'docs/context/readme.md').write_text('hello TODO(unshackle): review')
    (tmp_path / '.env').write_text('secret')
    (tmp_path / 'docs/context/secret.md').symlink_to(tmp_path / '.env')
    repo = Repository(tmp_path)
    assert repo.files() == ['docs/context/readme.md']
    assert repo.search('hello')[0]['has_todo']
    for p in ('../.env', '.env', str(tmp_path / '.env'), 'docs/context/secret.md'):
        with pytest.raises(ValueError):
            repo.read(p)
    (tmp_path / 'unshackle.toml').write_text('[paths]\nknowledge="../"\n')
    with pytest.raises(ValueError):
        Repository(tmp_path)


def test_notebooks_static_and_legacy(tmp_path):
    (tmp_path / 'notebooks').mkdir()
    path = tmp_path / 'notebooks/test.ipynb'
    d = Dashboard('Test', 'test', tiles=[Tile('x', mark='bar', sql='select 1 as x', encodings={'x': Encoding('x', 'quantitative')})])
    path.write_text(render_dashboard(d, 'test'))
    read = Repository(tmp_path).read('notebooks/test.ipynb')
    assert read['tiles'][0]['sql'].strip() == 'select 1 as x'
    assert read['tiles'][0]['chart_spec']['mark']['type'] == 'bar'
    nb = json.loads(path.read_text())
    for cell in nb['cells']:
        cell['metadata'].pop('unshackle', None)
    path.write_text(json.dumps(nb))
    assert Repository(tmp_path).read('notebooks/test.ipynb')['tiles'][0]['chart_spec']['mark']['type'] == 'bar'
    nb['cells'][-1]['source'] = "x_chart = __import__('os').system('touch should-not-exist')"
    path.write_text(json.dumps(nb))
    read = Repository(tmp_path).read('notebooks/test.ipynb')
    assert read['has_todo']
    assert '__import__' in read['tiles'][0]['chart_spec']['source_expression']
    assert not Path('should-not-exist').exists()


def test_overview_hides_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('UNSHACKLE_WAREHOUSE_URL', 'postgresql://user:secret@example.invalid/db')
    overview = Repository(tmp_path).overview()
    assert overview['warehouse'] == {'configured': True, 'connection_verified': False, 'engine': 'postgresql'}
    assert 'secret' not in json.dumps(overview)


def test_generated_ossie_and_metricflow_counts_deduplicate(tmp_path):
    from unshackle.build import apply
    from unshackle.ir import Bundle, SemanticModel, Measure, Metric
    (tmp_path / 'dbt_project.yml').write_text('name: example\nversion: 1.0.0\n')
    bundle = Bundle('test', semantic_models=[SemanticModel('orders', 'orders', measures=[Measure('revenue', expr='amount')])],
                    metrics=[Metric('revenue', measure='revenue')])
    apply(bundle, tmp_path)
    overview = Repository(tmp_path).overview()
    assert overview['models'] == 1
    assert overview['metrics'] == 1


def test_other_connection_is_flagged(tmp_path):
    (tmp_path / 'queries').mkdir()
    (tmp_path / 'queries/other.sql').write_text('-- datasource: other_warehouse\nselect 1')
    result = Repository(tmp_path).read('queries/other.sql')
    assert result['datasource'] == 'other_warehouse'
    assert 'server warehouse' in result['connection_warning']
