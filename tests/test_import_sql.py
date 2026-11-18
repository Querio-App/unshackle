import json
from pathlib import Path

import pytest

from unshackle import build
from unshackle.imports.sql import parse

FIXTURE = Path(__file__).parent / "fixtures/import_sql"


def tiles(bundle):
    return [tile for dashboard in bundle.dashboards for tile in dashboard.tiles]


def test_static_sql_and_python_with_source_and_lines():
    bundle = parse([FIXTURE])
    assert len(bundle.dashboards) == 3
    extracted = tiles(bundle)
    assert len(extracted) == 10
    executable = [tile.sql for tile in extracted if tile.sql]
    assert len(executable) == 6
    assert "SELECT customer_id, amount FROM orders" in executable
    assert "SELECT id FROM orders" in executable
    uncertain = [tile for tile in extracted if tile.todo]
    assert len(uncertain) == 4
    assert all(not tile.sql for tile in uncertain)
    assert any("table_name" in tile.source_config["source_expression"] for tile in uncertain)
    assert all(f'embedded.py:{tile.source_config["line"]}' in tile.todo for tile in uncertain)
    assert str(FIXTURE.absolute()) not in bundle.to_json()
    assert bundle.to_json() == parse([FIXTURE]).to_json()


def test_python_is_never_executed(tmp_path):
    marker = tmp_path / "executed"
    path = tmp_path / "danger.py"
    path.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\nq = "SELECT 1"\n')
    bundle = parse([path])
    assert not marker.exists()
    build.apply(bundle, tmp_path / "out")
    assert not marker.exists()
    assert tiles(bundle)[0].sql == "SELECT 1"


@pytest.mark.parametrize("source", [
    "DELETE FROM orders", "SELECT 1; SELECT 2", "SELECT * INTO saved FROM orders",
    "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x", "SELECT FROM",
    "SELECT * FROM {{ ref('orders') }}", "SELECT * FROM orders WHERE id = :id",
])
def test_unsupported_sql_keeps_source_and_todo(tmp_path, source):
    path = tmp_path / "query.sql"
    path.write_text(source)
    bundle = parse([path])
    tile, = tiles(bundle)
    assert not tile.sql and tile.todo
    assert tile.source_config["source_expression"] == source
    build.apply(bundle, tmp_path / "out")
    notebook, = (tmp_path / "out/notebooks").glob("*.ipynb")
    cells = json.loads(notebook.read_text())["cells"]
    markdown = "\n".join("".join(c["source"]) for c in cells if c["cell_type"] == "markdown")
    assert source in markdown
    assert "TODO(unshackle)" in notebook.read_text()


def test_syntax_error_keeps_whole_python_source(tmp_path):
    path = tmp_path / "broken.py"
    source = 'q = "SELECT 1"\nif (\n'
    path.write_text(source)
    tile, = tiles(parse([path]))
    assert "broken.py:2" in tile.todo and "syntax error" in tile.todo
    assert tile.source_config["source_expression"] == source


def test_same_names_paths_reruns_hand_edits_and_layout(tmp_path):
    repo = tmp_path / "out"
    repo.mkdir()
    (repo / "unshackle.toml").write_text('[paths]\nnotebooks = "analysis"\n')
    build.apply(parse([FIXTURE]), repo)
    paths = sorted((repo / "analysis").glob("*.ipynb"))
    assert len(paths) == 3
    before = {p.name: p.read_bytes() for p in paths}
    build.apply(parse([FIXTURE]), repo)
    assert before == {p.name: p.read_bytes() for p in paths}
    edited = paths[0]
    edited.write_text(edited.read_text().replace('"nbformat_minor": 5', '"nbformat_minor": 4'))
    result = build.apply(parse([FIXTURE]), repo)
    assert str(edited.relative_to(repo)) in result.writes.kept_hand_edits


def test_query_sinks_unknown_values_nested_text_and_unrelated_strings(tmp_path):
    path = tmp_path / "queries.py"
    path.write_text('''about = "nothing to do with SQL"
cursor.execute(text("SELECT 1"))
pd.read_sql(sql=query, con=engine)
q = "SELECT " + field_name + " FROM orders"
''')
    extracted = tiles(parse([path]))
    assert len(extracted) == 3
    assert extracted[0].sql == "SELECT 1"
    assert extracted[1].todo and extracted[2].todo


def test_source_symlinks_and_hidden_directories_are_not_followed(tmp_path):
    hidden = tmp_path / ".private"
    hidden.mkdir()
    (hidden / "secret.sql").write_text("SELECT 1")
    (tmp_path / "link.sql").symlink_to(hidden / "secret.sql")
    assert not parse([tmp_path]).dashboards
    with pytest.raises(ValueError, match="symlinks"):
        parse([tmp_path / "link.sql"])


@pytest.mark.parametrize("expression", [
    '"SELECT a FROM orders".replace("a", "b")',
    '"SELECT a FROM orders"[:8]',
    '"SELECT a FROM orders" if condition else "SELECT b FROM orders"',
    'build_query(table_name)',
])
def test_dynamic_calls_and_expression_transformations_are_not_silently_literals(tmp_path, expression):
    path = tmp_path / "dynamic.py"
    path.write_text(f'cursor.execute({expression})\n')
    tile, = tiles(parse([path]))
    assert not tile.sql and tile.todo
    assert tile.source_config["source_expression"] == expression


def test_source_paths_are_stable_after_copy_and_input_order(tmp_path):
    import shutil
    shutil.copytree(FIXTURE, tmp_path / "copy")
    assert parse([FIXTURE]).to_json() == parse([tmp_path / "copy"]).to_json()
    paths = list(FIXTURE.rglob("*.sql"))
    assert parse(paths).to_json() == parse(paths[::-1]).to_json()
