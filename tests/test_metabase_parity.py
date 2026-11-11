"""Metabase parity capture uses dashboard identities and frozen source defaults, offline."""

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import yaml
from click.testing import CliRunner

from unshackle import build, verify
from unshackle.cli import main
from unshackle.http import Client
from unshackle.raw import RawStore
from unshackle.sources.metabase import MetabaseSource


def setup_repo(repo: Path, *, repeated=False):
    store = RawStore(repo, "metabase")
    card = {
        "id": 42, "name": "Amount", "display": "table",
        "dataset_query": {"type": "native", "native": {
            "query": "select cast({{value}} as integer) + {{bonus}} as amount",
            "template-tags": {"value": {"name": "value", "type": "number"},
                              "bonus": {"name": "bonus", "type": "number", "default": 3}},
        }},
        "result_metadata": [{"name": "amount", "display_name": "Total Amount", "base_type": "type/Integer", "source": "aggregation"}],
    }
    dashboard = {
        "id": 20, "name": "Example", "parameters": [
            {"id": "parameter-id", "slug": "engine", "type": "number/=", "default": [7], "options": {"case-sensitive": False}},
        ],
        "dashcards": [{"id": dcid, "card_id": 42, "row": i, "parameter_mappings": [
            {"card_id": 42, "parameter_id": "parameter-id", "target": ["variable", ["template-tag", "value"]]},
        ]} for i, dcid in enumerate([10, 11] if repeated else [10])],
    }
    store.put_json("cards/42.json", card)
    store.put_json("dashboards/20.json", dashboard)
    rebuild(repo)
    return store, dashboard, card


def rebuild(repo):
    return build.apply(MetabaseSource().parse(RawStore(repo, "metabase")), repo)


def mock_exports(monkeypatch, *, content=None, status=200):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        body = parse_qs(request.content.decode())
        assert body["format_rows"] == ["false"]
        assert body["pivot_results"] == ["false"]
        parameters = json.loads(body["parameters"][0])
        value = parameters[0]["value"][0] if parameters and content is None else 7
        return httpx.Response(status, content=content if content is not None else f"\ufeffTotal Amount\n{value + 3}\n".encode())

    client = Client("https://metabase.example.com", headers={"X-API-Key": "example-key"}, per_minute=0, max_retries=0,
                    transport=httpx.MockTransport(handler))
    monkeypatch.setattr(MetabaseSource, "client", lambda self: client)
    return calls


def test_cli_captures_distinct_dashcards_and_verifies_actual_notebook(tmp_path, monkeypatch):
    setup_repo(tmp_path, repeated=True)
    calls = mock_exports(monkeypatch)
    run = CliRunner().invoke(main, ["verify", "--init", "metabase", "--repo", str(tmp_path)])
    assert run.exit_code == 0, run.output
    assert "2 cases written" in run.output
    assert [r.url.path for r in calls] == [
        "/api/dashboard/20/dashcard/10/card/42/query/csv", "/api/dashboard/20/dashcard/11/card/42/query/csv",
    ]
    assert calls[0].headers["x-api-key"] == "example-key"
    assert json.loads(parse_qs(calls[0].content.decode())["parameters"][0]) == [{
        "id": "parameter-id", "type": "number/=", "value": [7], "target": ["variable", ["template-tag", "value"]],
        "options": {"case-sensitive": False},
    }]
    cases = [yaml.safe_load(p.read_text()) for p in sorted((tmp_path / "migration/parity").glob("*.yaml"))]
    assert [c["id"] for c in cases] == ["metabase-20-10", "metabase-20-11"]
    assert [c["target"]["tile"] for c in cases] == ["amount", "amount_2"]
    assert all(c["target"]["params"] == {"engine": 7} and c["strict"] for c in cases)
    assert cases[0]["columns"] == {"Total Amount": "amount"}
    assert "tile_engine = 7" in (tmp_path / "notebooks/example.ipynb").read_text()
    checked = CliRunner().invoke(main, ["verify", "--repo", str(tmp_path), "--url", "duckdb:///:memory:"])
    assert checked.exit_code == 0, checked.output
    assert len(list((tmp_path / "evals").glob("*.yaml"))) == 2


def test_cross_wired_notebook_parameters_are_not_repaired_by_pinning(tmp_path, monkeypatch):
    store, d, card = setup_repo(tmp_path)
    # Equal defaults still need distinct parameter identities: comparing values alone misses the swap.
    d["parameters"] = [{"id": name, "slug": name, "type": "string/=", "default": ["USA"]}
                       for name in ("origin", "destination")]
    d["dashcards"][0]["parameter_mappings"] = [
        {"card_id": 42, "parameter_id": name, "target": ["variable", ["template-tag", name]]}
        for name in ("origin", "destination")
    ]
    card["dataset_query"]["native"] = {
        "query": "select {{origin}} as origin, {{destination}} as destination",
        "template-tags": {name: {"name": name, "type": "text"} for name in ("origin", "destination")},
    }
    card["result_metadata"] = [{"name": name, "display_name": name.title(), "base_type": "type/Text"}
                               for name in ("origin", "destination")]
    store.put_json("cards/42.json", card)
    store.put_json("dashboards/20.json", d)
    rebuild(tmp_path)
    path = tmp_path / "notebooks/example.ipynb"
    nb = json.loads(path.read_text())
    for cell in nb["cells"]:
        if cell["id"].endswith("-sql"):
            cell["source"] = [line.replace("'origin': origin, 'destination': destination",
                                           "'origin': destination, 'destination': origin") for line in cell["source"]]
    path.write_text(json.dumps(nb))
    calls = mock_exports(monkeypatch, content=b"Origin,Destination\nUSA,USA\n")
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert not written and any("mapping" in s for s in skipped)
    assert not calls


def test_input_column_occurrence_is_not_proof_of_output_alias(tmp_path, monkeypatch):
    store, d, card = setup_repo(tmp_path)
    d["parameters"] = []
    d["dashcards"][0]["parameter_mappings"] = []
    card["dataset_query"]["native"] = {"query": 'SELECT "amount" AS total FROM t', "template-tags": {}}
    card["result_metadata"] = [{"name": "total", "lib/desired-column-alias": "amount", "display_name": "Grand Total", "base_type": "type/Integer"}]
    store.put_json("cards/42.json", card)
    store.put_json("dashboards/20.json", d)
    rebuild(tmp_path)
    mock_exports(monkeypatch, content=b"Grand Total\n10\n")
    written, skipped = verify.init_cases(tmp_path, "metabase")
    if written:
        assert yaml.safe_load(written[0].read_text())["columns"] == {"Grand Total": "total"}
    else:
        assert any("projection" in s for s in skipped)


def test_joined_mbql_projection_maps_desired_alias_and_range_defaults(tmp_path, monkeypatch):
    import shutil

    fixture = Path(__file__).parent / "fixtures/metabase"
    shutil.copytree(fixture, tmp_path / "migration/raw/metabase")
    store = RawStore(tmp_path, "metabase")
    d = store.get_json("dashboards/2.json")
    d["dashcards"] = [dc for dc in d["dashcards"] if dc["id"] == 39]
    store.put_json("dashboards/2.json", d)
    rebuild(tmp_path)
    card = store.get_json("cards/43.json")
    headers = [c["display_name"] for c in card["result_metadata"]]
    calls = mock_exports(monkeypatch, content=(",".join(headers) + "\nDwight Schrute,95,1\n").encode())
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert len(written) == 1, skipped
    case = yaml.safe_load(written[0].read_text())
    assert case["columns"][headers[0]] == "accounts__via__account_id__account_manager"
    assert case["target"]["params"] == {"status": None, "purchased_from": "2025-01-01", "purchased_to": "2025-01-31",
                                         "quantity_min": None, "quantity_max": None}
    params = json.loads(parse_qs(calls[0].content.decode())["parameters"][0])
    assert params[1]["id"] == "c9d0e1f2" and params[1]["value"] == "2025-01-01~2025-01-31"
    assert params[0]["value"] is None and params[2]["value"] is None


def test_scope_and_same_card_on_another_dashboard_use_distinct_baselines(tmp_path, monkeypatch):
    store, d, _ = setup_repo(tmp_path)
    d["id"] = 21
    d["parameters"][0]["default"] = [8]
    store.put_json("dashboards/21.json", d)
    (tmp_path / "unshackle.toml").write_text('[paths]\nnotebooks = "analysis"\n')
    rebuild(tmp_path)
    calls = mock_exports(monkeypatch)
    args = ["verify", "--init", "metabase", "--repo", str(tmp_path)]
    run = CliRunner().invoke(main, args + ["--only", "dashboard:21"])
    assert run.exit_code == 0 and "1 cases written" in run.output, run.output
    assert [r.url.path for r in calls] == ["/api/dashboard/21/dashcard/10/card/42/query/csv"]
    run = CliRunner().invoke(main, args)
    assert run.exit_code == 0 and "1 cases written" in run.output and "exists, kept" in run.output, run.output
    cases = [yaml.safe_load(p.read_text()) for p in sorted((tmp_path / "migration/parity").glob("*.yaml"))]
    assert [c["target"]["params"] for c in cases] == [{"engine": 7}, {"engine": 8}]
    assert len({c["source"]["csv"] for c in cases}) == 2
    assert len({c["target"]["notebook"] for c in cases}) == 2
    assert all(c["target"]["notebook"].startswith("analysis/") for c in cases)


@pytest.mark.parametrize("args, error", [
    (["--only", "dashboard:20"], "--init metabase"),
    (["--init", "tableau", "--only", "dashboard:20"], "--init metabase"),
    (["--init", "metabase", "--only", "card:42"], "dashboard:<id>"),
    (["--init", "metabase", "--only", "dashboard:99"], "not in the build"),
])
def test_cli_rejects_invalid_scope(tmp_path, monkeypatch, args, error):
    setup_repo(tmp_path)
    calls = mock_exports(monkeypatch)
    run = CliRunner().invoke(main, ["verify", "--repo", str(tmp_path), *args])
    assert run.exit_code != 0 and error in run.output
    assert not calls


def test_reruns_preserve_edits_and_reuse_csvs_without_api_access(tmp_path, monkeypatch):
    setup_repo(tmp_path)
    calls = mock_exports(monkeypatch)
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert len(written) == 1, skipped
    path = written[0]
    csv_path = tmp_path / yaml.safe_load(path.read_text())["source"]["csv"]
    path.write_text(path.read_text() + "# hand edited\n")
    csv_path.write_text("Total Amount\n11\n")
    saved_case, saved_csv = path.read_bytes(), csv_path.read_bytes()
    monkeypatch.setattr(MetabaseSource, "client", lambda self: pytest.fail("existing baseline must not need credentials"))
    assert not verify.init_cases(tmp_path, "metabase")[0]
    assert path.read_bytes() == saved_case and csv_path.read_bytes() == saved_csv
    path.unlink()
    assert len(verify.init_cases(tmp_path, "metabase")[0]) == 1
    assert csv_path.read_bytes() == saved_csv and len(calls) == 1


def test_saved_baseline_does_not_acquire_changed_defaults(tmp_path, monkeypatch):
    store, d, _ = setup_repo(tmp_path)
    calls = mock_exports(monkeypatch)
    written, _ = verify.init_cases(tmp_path, "metabase")
    written[0].unlink()
    csv_path = tmp_path / "migration/parity/data/metabase-20-10.csv"
    before = csv_path.read_bytes()
    d["parameters"][0]["default"] = [8]
    store.put_json("dashboards/20.json", d)
    # Remove the generated notebook so build can write the new default, independently of edit preservation.
    (tmp_path / "notebooks/example.ipynb").unlink()
    rebuild(tmp_path)
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert not written and any("capture context missing or changed" in s for s in skipped)
    assert len(calls) == 1 and csv_path.read_bytes() == before


def test_existing_csv_without_capture_metadata_is_kept(tmp_path, monkeypatch):
    setup_repo(tmp_path)
    path = tmp_path / "migration/parity/data/metabase-20-10.csv"
    path.parent.mkdir(parents=True)
    path.write_text("Total Amount\n10\n")
    calls = mock_exports(monkeypatch)
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert not written and any("capture context missing or changed" in s for s in skipped)
    assert not calls and path.read_text() == "Total Amount\n10\n"


@pytest.mark.parametrize("content, error", [
    (b"", "no data rows"),
    (b"Total Amount\n", "no data rows"),
    (b"Total Amount,Extra\n10,1\n", "complete, unique"),
    (b"Unknown\n10\n", "unambiguous"),
    (b"Total Amount\n10,1\n", "inconsistent"),
    (b'"unterminated', "unexpected end"),
    (b'{"error":"query failed"}', "no data rows"),
])
def test_invalid_exports_leave_no_baseline_or_case(tmp_path, monkeypatch, content, error):
    setup_repo(tmp_path)
    mock_exports(monkeypatch, content=content)
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert not written and any(error in s for s in skipped)
    assert not list((tmp_path / "migration/parity").rglob("*.csv"))
    assert not list((tmp_path / "migration/parity").glob("*.yaml"))


@pytest.mark.parametrize(("status", "body", "error"), [
    (403, "Download permission denied", "403: Download permission denied"),
    (200, '"unterminated', "unexpected end of data"),
])
def test_export_failure_is_visible_and_other_cards_continue_with_one_client(tmp_path, monkeypatch, status, body, error):
    setup_repo(tmp_path, repeated=True)
    clients = []
    requests = []

    def handler(request):
        requests.append(request)
        if "/dashcard/10/" in request.url.path:
            return httpx.Response(status, text=body)
        return httpx.Response(200, content=b"Total Amount\n10\n")

    client = Client("https://metabase.example.com", per_minute=0, max_retries=0, transport=httpx.MockTransport(handler))

    def get_client(self):
        clients.append(self)
        return client

    monkeypatch.setattr(MetabaseSource, "client", get_client)
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert len(written) == 1 and written[0].stem == "metabase-20-11"
    assert any(error in s for s in skipped)
    assert len(requests) == 2 and clients[0] is clients[1]
    assert not (tmp_path / "migration/parity/data/metabase-20-10.csv").exists()


@pytest.mark.parametrize("edit, error", [
    ("missing", "No such file"),
    ("identity", "identity missing"),
    ("sql", "SQL differs"),
    ("default", "defaults differ"),
    ("nonliteral", "malformed node"),
    ("missing_default", "tile_engine"),
    ("missing_mapping", "mapping differs"),
])
def test_actual_notebook_is_validated_before_capture(tmp_path, monkeypatch, edit, error):
    setup_repo(tmp_path)
    path = tmp_path / "notebooks/example.ipynb"
    if edit == "missing":
        path.unlink()
    else:
        nb = json.loads(path.read_text())
        for cell in nb["cells"]:
            if edit == "identity":
                cell["metadata"].pop("unshackle", None)
            source = "".join(cell["source"])
            if edit == "sql":
                source = source.replace("+ 3 as amount", "+ 4 as amount")
            elif edit == "default":
                source = source.replace("tile_engine = 7", "tile_engine = 8")
            elif edit == "nonliteral":
                source = source.replace("tile_engine = 7", "tile_engine = int('7')")
            elif edit == "missing_default":
                source = source.replace("tile_engine = 7", "# no default")
            elif edit == "missing_mapping":
                source = source.replace(", params={'engine': tile_engine}", "")
            cell["source"] = source.splitlines(keepends=True)
        path.write_text(json.dumps(nb))
    before = path.read_bytes() if path.exists() else None
    calls = mock_exports(monkeypatch)
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert not written and any(error in s for s in skipped), skipped
    assert not calls
    if before is not None:
        assert path.read_bytes() == before


@pytest.mark.parametrize("kind, default, error", [
    ("date/all-options", "past30days", "relative date"),
    ("number/between", [1, 2, 3], "range default"),
    ("string/=", ["USA", "MEX"], "multi-value"),
    ("temporal-unit", "month", "unsupported type"),
])
def test_unsupported_filter_semantics_skip_before_export(tmp_path, monkeypatch, kind, default, error):
    store, d, _ = setup_repo(tmp_path)
    d["parameters"][0].update(type=kind, default=default)
    store.put_json("dashboards/20.json", d)
    rebuild(tmp_path)
    calls = mock_exports(monkeypatch)
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert not written and any(error in s for s in skipped), skipped
    assert not calls


def test_missing_columns_are_visible_before_export(tmp_path, monkeypatch):
    store, _, card = setup_repo(tmp_path)
    card["result_metadata"] = []
    store.put_json("cards/42.json", card)
    rebuild(tmp_path)
    calls = mock_exports(monkeypatch)
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert not written and any("missing result column metadata" in s for s in skipped)
    assert not calls


def test_normalization_colliding_aliases_cannot_false_pass_by_column_order(tmp_path, monkeypatch):
    store, d, card = setup_repo(tmp_path)
    d["parameters"] = []
    d["dashcards"][0]["parameter_mappings"] = []
    card["dataset_query"]["native"] = {"query": 'select 1 AS "A B", 2 AS "A-B"', "template-tags": {}}
    card["result_metadata"] = [{"name": n, "display_name": n, "base_type": "type/Integer"} for n in ("A B", "A-B")]
    store.put_json("cards/42.json", card)
    store.put_json("dashboards/20.json", d)
    rebuild(tmp_path)
    calls = mock_exports(monkeypatch, content=b"A-B,A B\n1,2\n")
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert not written and any("collide after normalization" in s for s in skipped)
    assert not calls


def test_normalization_colliding_headers_are_rejected(tmp_path, monkeypatch):
    store, d, card = setup_repo(tmp_path)
    d["parameters"] = []
    d["dashcards"][0]["parameter_mappings"] = []
    card["dataset_query"]["native"] = {"query": "select 1 as a, 2 as b", "template-tags": {}}
    card["result_metadata"] = [{"name": n, "display_name": title, "base_type": "type/Integer"}
                               for n, title in (("a", "A-B"), ("b", "A B"))]
    store.put_json("cards/42.json", card)
    store.put_json("dashboards/20.json", d)
    rebuild(tmp_path)
    mock_exports(monkeypatch, content=b"A-B,A B\n1,2\n")
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert not written and any("complete, unique" in s for s in skipped)


@pytest.mark.parametrize("sql", [
    'with c as (select "amount" as total from t) select * from c',
    'select * from (select "amount" as total from t) q',
    'select (select "amount" as total from t) as total',
    'select "amount" /* AS total */ from t',
])
def test_uncertain_output_projection_is_skipped(tmp_path, monkeypatch, sql):
    store, d, card = setup_repo(tmp_path)
    d["parameters"] = []
    d["dashcards"][0]["parameter_mappings"] = []
    card["dataset_query"]["native"] = {"query": sql, "template-tags": {}}
    card["result_metadata"] = [{"name": "total", "lib/desired-column-alias": "amount", "display_name": "Total", "base_type": "type/Integer"}]
    store.put_json("cards/42.json", card)
    store.put_json("dashboards/20.json", d)
    rebuild(tmp_path)
    calls = mock_exports(monkeypatch)
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert not written and any("uncertain output projection" in s for s in skipped)
    assert not calls


@pytest.mark.parametrize("mutation", ["wrong_dataframe", "uncalled_function", "reassigned_sql"])
def test_notebook_requires_generated_top_level_execution_shape(tmp_path, monkeypatch, mutation):
    setup_repo(tmp_path)
    path = tmp_path / "notebooks/example.ipynb"
    nb = json.loads(path.read_text())
    cell = next(c for c in nb["cells"] if c["id"] == "amount-sql")
    source = "".join(cell["source"])
    if mutation == "wrong_dataframe":
        source = source.replace("amount = pd.read_sql", "wrong = pd.read_sql")
    elif mutation == "uncalled_function":
        source = source.replace("amount = pd.read_sql", "def unused():\n    amount = pd.read_sql")
    else:
        source = source.replace("amount = pd.read_sql", "amount_sql = amount_sql.replace(' + 3', ' + 993')\namount = pd.read_sql")
    cell["source"] = source.splitlines(keepends=True)
    path.write_text(json.dumps(nb))
    calls = mock_exports(monkeypatch)
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert not written and any("shape" in s for s in skipped)
    assert not calls


@pytest.mark.parametrize("key", ["dashboard_id", "dashcard_id", "card_id"])
@pytest.mark.parametrize("value", [None, 0, -1, True, "20", "x/../../../../../escaped"])
def test_loaded_parity_context_rejects_invalid_ids_before_paths(tmp_path, monkeypatch, key, value):
    setup_repo(tmp_path)
    path = tmp_path / "migration/ir/metabase.json"
    doc = json.loads(path.read_text())
    doc["dashboards"][0]["tiles"][0]["source_config"]["metabase_parity"][key] = value
    path.write_text(json.dumps(doc))
    calls = mock_exports(monkeypatch)
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert not written and any("positive integer" in s for s in skipped)
    assert not calls and not (tmp_path / "migration/parity").exists()


def test_raw_path_like_dashboard_id_is_not_used_in_parity_paths(tmp_path, monkeypatch):
    store, d, _ = setup_repo(tmp_path)
    d["id"] = "x/../../../../../escaped"
    store.put_json("dashboards/20.json", d)
    bundle = MetabaseSource().parse(store)
    tile = bundle.dashboards[0].tiles[0]
    assert "parity_csv" not in tile.source_config
    build.apply(bundle, tmp_path)
    calls = mock_exports(monkeypatch)
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert not written and any("positive integer" in s for s in skipped)
    assert not calls


@pytest.mark.parametrize("name", ["amount_sql", "tile_engine", "amount"])
def test_other_cells_cannot_reassign_parity_inputs(tmp_path, monkeypatch, name):
    setup_repo(tmp_path)
    path = tmp_path / "notebooks/example.ipynb"
    nb = json.loads(path.read_text())
    nb["cells"].append({"cell_type": "code", "id": "edit", "metadata": {}, "source": f"{name} = str({name})"})
    path.write_text(json.dumps(nb))
    calls = mock_exports(monkeypatch)
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert not written and any("reassigns" in s for s in skipped)
    assert not calls


def test_loaded_csv_path_cannot_escape_baseline_directory(tmp_path, monkeypatch):
    setup_repo(tmp_path)
    path = tmp_path / "migration/ir/metabase.json"
    doc = json.loads(path.read_text())
    doc["dashboards"][0]["tiles"][0]["source_config"]["parity_csv"] = "../../escaped.csv"
    path.write_text(json.dumps(doc))
    calls = mock_exports(monkeypatch)
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert not written and any("baseline path does not match" in s for s in skipped)
    assert not calls


@pytest.mark.parametrize("redirect", ["case", "csv", "metadata"])
def test_parity_paths_cannot_redirect_outside_intended_directory(tmp_path, monkeypatch, redirect):
    setup_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("keep me")
    if redirect == "case":
        path = tmp_path / "migration/parity/metabase-20-10.yaml"
    else:
        path = tmp_path / f"migration/parity/data/metabase-20-10.{ 'csv' if redirect == 'csv' else 'json'}"
    path.parent.mkdir(parents=True)
    path.symlink_to(outside)
    calls = mock_exports(monkeypatch)
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert not written and any("redirects outside" in s for s in skipped)
    assert not calls and outside.read_text() == "keep me"


def test_export_hook_rejects_bad_identity_before_client_creation(monkeypatch):
    monkeypatch.setattr(MetabaseSource, "client", lambda self: pytest.fail("invalid identity must not create a client"))
    with pytest.raises(ValueError, match="positive integer"):
        MetabaseSource().export_parity({"dashboard_id": True, "dashcard_id": 10, "card_id": 42})


def test_colliding_filter_slugs_use_shared_id_mapping_in_parity(tmp_path, monkeypatch):
    fixture = Path(__file__).parent / "fixtures/metabase/colliding-filters.json"
    d = json.loads(fixture.read_text())
    d["parameters"] = d["parameters"][:4]
    d["dashcards"] = d["dashcards"][:1]
    names = ("origin", "destination", "suffixed", "normalized")
    card = {"id": 1, "name": "Native", "display": "table", "dataset_query": {"type": "native", "native": {
        "query": "select " + ", ".join("{{" + name + "}} as " + name for name in names),
        "template-tags": {name: {"type": "text", "name": name} for name in names},
    }}, "result_metadata": [{"name": name, "display_name": name.title(), "base_type": "type/Text"} for name in names]}
    store = RawStore(tmp_path, "metabase")
    store.put_json("dashboards/17.json", d)
    store.put_json("cards/1.json", card)
    rebuild(tmp_path)
    calls = mock_exports(monkeypatch, content=b"Origin,Destination,Suffixed,Normalized\nUSA,MEX,CAN,FRA\n")
    written, skipped = verify.init_cases(tmp_path, "metabase")
    assert len(written) == 1, skipped
    case = yaml.safe_load(written[0].read_text())
    assert case["target"]["params"] == {"country": "USA", "country_3": "MEX", "country_2": "CAN", "country_4": "FRA"}
    parameters = json.loads(parse_qs(calls[0].content.decode())["parameters"][0])
    assert [(p["id"], p["value"]) for p in parameters] == [(p["id"], p["default"]) for p in d["parameters"]]
    assert [r.status for r in verify.run_checks(tmp_path, "duckdb:///:memory:")] == ["pass"]


def test_cli_loads_credentials_from_selected_repo(tmp_path, monkeypatch):
    import os

    setup_repo(tmp_path)
    monkeypatch.delenv("METABASE_URL", raising=False)
    monkeypatch.delenv("METABASE_API_KEY", raising=False)
    (tmp_path / ".env").write_text("METABASE_URL=https://metabase.example.com\nMETABASE_API_KEY=example-key\n")

    def export(self, context):
        assert os.environ["METABASE_URL"] == "https://metabase.example.com"
        assert os.environ["METABASE_API_KEY"] == "example-key"
        return b"Total Amount\n10\n"

    monkeypatch.setattr(MetabaseSource, "export_parity", export)
    run = CliRunner().invoke(main, ["verify", "--init", "metabase", "--repo", str(tmp_path)])
    assert run.exit_code == 0 and "1 cases written" in run.output, run.output
