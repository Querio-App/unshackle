"""Parameter IDs, not display slugs, determine dashboard filter identity."""

import ast
import json
import re
from pathlib import Path

import httpx
import pytest

from unshackle.http import Client
from unshackle.raw import RawStore
from unshackle.sources.metabase import MetabaseSource, _filter_binds, _parameter_names
from unshackle.target.notebooks import build_notebook


FIXTURE = Path(__file__).parent / "fixtures/metabase/colliding-filters.json"


def dashboard():
    return json.loads(FIXTURE.read_text())


def native_card():
    tags = ("origin", "destination", "suffixed", "normalized")
    return {"id": 1, "name": "Native", "display": "table", "dataset_query": {
        "type": "native", "native": {
            "query": "SELECT " + ", ".join("{{" + t + "}} AS " + t for t in tags),
            "template-tags": {t: {"type": "text", "name": t} for t in tags},
        },
    }}


def test_native_collisions_preserve_defaults_and_one_assignment(tmp_path):
    import duckdb

    d = dashboard()
    d["parameters"] = d["parameters"][:4]
    d["dashcards"] = d["dashcards"][:1]
    store = RawStore(tmp_path, "metabase")
    store.put_json("dashboards/17.json", d)
    store.put_json("cards/1.json", native_card())
    dash, = MetabaseSource().parse(store).dashboards
    nb = build_notebook(dash, "metabase")
    source = next(c["source"] for c in nb["cells"] if c["id"] == "parameters")
    assignments = [n.targets[0].id for n in ast.parse(source).body if isinstance(n, ast.Assign)]
    assert len(assignments) == len(set(assignments)) == 4
    assert {f.name: f.default for f in dash.filters} == {
        "country": "USA", "country_3": "MEX", "country_2": "CAN", "country_4": "FRA",
    }
    assert "TODO(unshackle)" not in source and not dash.tiles[0].todo
    sql = dash.tiles[0].sql.replace(":", "$")
    with duckdb.connect() as con:
        assert con.execute(sql, {f.name: f.default for f in dash.filters}).fetchone() == ("USA", "MEX", "CAN", "FRA")
        assert con.execute(sql, dict(country="BRA", country_3="USA", country_2="MEX", country_4="CAN")).fetchone() == (
            "BRA", "USA", "MEX", "CAN",
        )


def extract_dashboard(tmp_path, d):
    """Offline API double: compile each requested field predicate with its supplied probe."""
    compiled = []

    def handler(request):
        path = request.url.path
        if path == "/api/session/properties":
            return httpx.Response(200, json={"version": {"tag": "v0.64.1"}})
        if path == "/api/collection/tree":
            return httpx.Response(200, json=[])
        if path == "/api/search":
            return httpx.Response(200, json={"data": [{"model": "dashboard", "id": 17}], "total": 1})
        if path == "/api/dashboard/17":
            return httpx.Response(200, json=d)
        if path == "/api/card/1":
            return httpx.Response(200, json=native_card())
        if path == "/api/card/2":
            return httpx.Response(200, json={"id": 2, "name": "MBQL", "display": "table", "dataset_query": {
                "type": "query", "query": {"source-table": 1},
            }})
        if path == "/api/dataset/native":
            predicates = []
            body = json.loads(request.content)
            compiled.append(body)
            for p in body.get("parameters", []):
                col = f"c{p['target'][1][1]}"
                value = p["value"]
                if p["type"].startswith("date/"):
                    lo, hi = value.split("~")
                    predicates.append(f"{col} BETWEEN date '{lo}' AND date '{hi}'")
                elif p["type"] == "number/between":
                    predicates.append(f"{col} BETWEEN {value[0]} AND {value[1]}")
                else:
                    predicates.append(f"{col} = '{value[0]}'")
            sql = "SELECT * FROM filter_data" + (" WHERE " + " AND ".join(predicates) if predicates else "")
            return httpx.Response(200, json={"query": sql, "params": None})
        return httpx.Response(404, json="API endpoint does not exist.")

    client = Client("https://mb.example.com", per_minute=0, max_retries=0, transport=httpx.MockTransport(handler))
    store = RawStore(tmp_path, "metabase")
    MetabaseSource(client=client).extract(store)
    store.save()
    return store, compiled


def test_extract_parse_and_notebook_keep_independent_filter_values(tmp_path):
    import duckdb

    store, compiled = extract_dashboard(tmp_path, dashboard())
    expected_names = {
        "origin": "country", "destination": "country_3", "suffixed": "country_2", "normalized": "country_4",
        "date": "purchased", "date_from": "purchased_from_2", "range": "quantity", "range_min": "quantity_min_2",
        "other_date": "purchased_2", "other_range": "quantity_2", "unused": "unmapped",
    }
    assert store.get_json("dashboards/17/parameter-names.json") == expected_names
    probes = [p["value"] for p in compiled[1]["parameters"]]
    assert probes == [
        ["unshackle__country"], ["unshackle__country_3"], ["unshackle__country_2"], ["unshackle__country_4"],
        "1905-02-03~1905-04-05", ["unshackle__purchased_from_2"], [-901240.5, 901240.5],
        ["unshackle__quantity_min_2"], "1909-02-03~1909-04-05", [-901243.5, 901243.5],
    ]
    dash, = MetabaseSource().parse(store).dashboards
    assert all(not t.todo for t in dash.tiles)
    nb = build_notebook(dash, "metabase")
    source = next(c["source"] for c in nb["cells"] if c["id"] == "parameters")
    assignments = [n.targets[0].id for n in ast.parse(source).body if isinstance(n, ast.Assign)]
    assert len(assignments) == len(set(assignments)) == 15
    # Keep the legitimate warning for the unmapped default, and no false warning for bound filters.
    assert source.count("TODO(unshackle)") == 1 and "dashboard filter unmapped" in source
    defaults = {}
    exec(source, {}, defaults)
    assert defaults == {
        "country": "USA", "country_3": "MEX", "country_2": "CAN", "country_4": "FRA",
        "purchased_from": "2025-01-01", "purchased_to": "2025-01-31", "purchased_from_2": "custom",
        "quantity_min": 5, "quantity_max": 15, "quantity_min_2": "small",
        "purchased_2_from": "2026-02-01", "purchased_2_to": "2026-02-28",
        "quantity_2_min": 20, "quantity_2_max": 30, "unmapped": "keep this TODO",
    }
    with duckdb.connect() as con:
        con.execute("""CREATE TABLE filter_data AS SELECT * FROM (VALUES
            (1, 'USA', 'MEX', 'CAN', 'FRA', date '2025-01-10', 'custom', 10, 'small', date '2026-02-10', 25),
            (2, 'MEX', 'USA', 'FRA', 'CAN', date '2025-03-10', 'custom', 17, 'small', date '2026-04-10', 40)
        ) t(id, c1, c2, c3, c4, c5, c6, c7, c8, c9, c10)""")

        def run(tile, values):
            sql = tile.sql.replace(":", "$")
            binds = set(re.findall(r"\$(\w+)", sql))
            return con.execute(sql, {k: values[k] for k in binds}).fetchall()

        native, all_filters, subset = dash.tiles
        assert run(native, defaults) == [("USA", "MEX", "CAN", "FRA")]
        assert [r[0] for r in run(all_filters, defaults)] == [1]
        changed = {**defaults, "country": "MEX", "country_3": "USA", "country_2": "FRA", "country_4": "CAN",
                   "purchased_from": "2025-03-01", "purchased_to": "2025-03-31", "quantity_min": 16, "quantity_max": 18,
                   "purchased_2_from": "2026-04-01", "purchased_2_to": "2026-04-30", "quantity_2_min": 35, "quantity_2_max": 45}
        assert run(native, changed) == [("MEX", "USA", "FRA", "CAN")]
        assert [r[0] for r in run(all_filters, changed)] == [2]
        assert [r[0] for r in run(subset, defaults)] == [1]
        assert [r[0] for r in run(subset, changed)] == [2]
        for name in ("country", "country_3", "country_2", "country_4", "purchased_from_2", "quantity_min_2"):
            assert run(all_filters, {**defaults, name: "no match"}) == []
        assert [r[0] for r in run(all_filters, {k: None for k in defaults})] == [1, 2]


@pytest.mark.parametrize("reverse", [False, True])
def test_expanded_names_also_reserve_existing_suffixes(reverse):
    ps = dashboard()["parameters"] + [
        {"id": "date_suffix", "slug": "purchased_2_from", "type": "string/="},
        {"id": "range_suffix", "slug": "quantity_2_min", "type": "string/="},
        {"id": "hyphen", "slug": "country-code", "type": "string/="},
        {"id": "underscore", "slug": "country_code", "type": "string/="},
    ]
    params = {p["id"]: p for p in (ps[::-1] if reverse else ps)}
    names = _parameter_names(params)
    emitted = [n for pid, p in params.items() for n, _, _ in _filter_binds(p, names[pid])]
    assert len(emitted) == len(set(emitted))
    assert names["date_suffix"] == "purchased_2_from" and names["range_suffix"] == "quantity_2_min"
    assert {names["date"], names["other_date"]} == {"purchased", "purchased_3"}
    assert {names["range"], names["other_range"]} == {"quantity", "quantity_3"}
    assert {names["hyphen"], names["underscore"]} == {"country_code", "country_code_2"}


def test_legacy_export_keeps_ambiguous_sentinels_and_recovers_unambiguous_tile(tmp_path):
    store, _ = extract_dashboard(tmp_path, dashboard())
    # Reconstruct the old exporter: all normalized colliding text filters used the same marker.
    names = store.get_json("dashboards/17/parameter-names.json")
    old_names = {"destination": "country", "normalized": "country", "date_from": "purchased_from", "range_min": "quantity_min"}
    for rel in ("dashboards/17/102.sql", "dashboards/17/103.sql"):
        sql = store.get_text(rel)
        for pid, old in old_names.items():
            sql = sql.replace(f"'unshackle__{names[pid]}'", f"'unshackle__{old}'")
        store.put_text(rel, sql)
    store.path("dashboards/17/parameter-names.json").unlink()
    dash, = MetabaseSource().parse(store).dashboards
    native, ambiguous, subset = dash.tiles
    assert not native.todo  # native mappings still identify the parameters by ID
    assert ambiguous.sql.count("'unshackle__country'") == 3
    assert ":country " not in ambiguous.sql and ":country_3 " not in ambiguous.sql and ":country_4 " not in ambiguous.sql
    assert ambiguous.todo.count("ambiguous sentinel") == 3 and "re-run extract" in ambiguous.todo
    assert ":purchased_from_2" in ambiguous.sql and ":quantity_min_2" in ambiguous.sql  # unique old text probes
    assert ":country_3" in subset.sql and not subset.todo  # one colliding ID mapped here: recovery is exact
    assert ":purchased_2_from" in subset.sql and ":quantity_2_min" in subset.sql  # index-based probes never collided


def test_native_empty_default_keeps_its_own_variable_and_legitimate_todo(tmp_path):
    d = dashboard()
    d["parameters"] = d["parameters"][:4]
    d["parameters"][1]["default"] = None
    d["dashcards"] = d["dashcards"][:1]
    store, _ = extract_dashboard(tmp_path, d)
    dash, = MetabaseSource().parse(store).dashboards
    source = next(c["source"] for c in build_notebook(dash, "metabase")["cells"] if c["id"] == "parameters")
    defaults = {}
    exec(source, {}, defaults)
    assert defaults == {"country": "USA", "country_3": None, "country_2": "CAN", "country_4": "FRA"}
    assert source.count("country_3 =") == 1 and "TODO(unshackle)" not in source
    assert "dashboard filter country_3 has no default" in dash.tiles[0].todo


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("unsupported", [False, True])
def test_one_parameter_mapped_to_multiple_fields_is_bound_once(tmp_path, legacy, unsupported):
    d = dashboard()
    d["dashcards"] = [d["dashcards"][1]]
    d["dashcards"][0]["parameter_mappings"] = [
        {"parameter_id": "origin", "target": ["dimension", ["field", field, None]]} for field in (1, 2)
    ]
    store, _ = extract_dashboard(tmp_path, d)
    if legacy:
        store.path("dashboards/17/parameter-names.json").unlink()
    if unsupported:
        rel = "dashboards/17/102.sql"
        store.put_text(rel, store.get_text(rel).replace("c2 =", "UPPER(c2) ="))
    dash, = MetabaseSource().parse(store).dashboards
    tile, = dash.tiles
    assert "(c1 = :country OR :country IS NULL)" in tile.sql
    if unsupported:
        assert "UPPER(c2) = 'unshackle__country'" in tile.sql and "compiled to an expression unshackle can't bind" in tile.todo
    else:
        assert "(c2 = :country OR :country IS NULL)" in tile.sql and not tile.todo
