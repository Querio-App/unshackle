"""Offline subscription exports: channel settings, parameter identity and safe writes."""

import json
import shutil
from pathlib import Path

import httpx
import pytest
import yaml

from unshackle import build
from unshackle.http import Client
from unshackle.ir import Bundle, Dashboard, DashboardFilter, Schedule, Tile
from unshackle.raw import RawStore
from unshackle.sources.metabase import MetabaseSource, _filter_binds, _parameter_names

FIXTURES = Path(__file__).parent / "fixtures/metabase_schedules"


def parse(tmp_path):
    shutil.copytree(FIXTURES, tmp_path / "migration/raw/metabase")
    store = RawStore(tmp_path, "metabase")
    return store, MetabaseSource().parse(store)


def test_pulses_keep_independent_channels_and_card_flags(tmp_path):
    store, bundle = parse(tmp_path)
    weekly, limited, archived = bundle.schedules
    assert weekly.source_ref == "metabase:pulse/42" and weekly.dashboard_ref == "metabase:dashboard/2"
    assert weekly.skip_if_empty is True and weekly.archived is False
    assert [(c.type, c.enabled, c.cadence["type"]) for c in weekly.channels] == [
        ("email", True, "hourly"), ("email", False, "weekly"), ("slack", True, "monthly")]
    assert weekly.channels[0].cadence["hour"] == 0
    assert weekly.channels[0].details["emails"] == ["external@example.com"]
    assert weekly.channels[0].details["recipient_emails"] == ["external@example.com", "analyst@example.com"]
    assert weekly.channels[0].details["attachment_only"] is True
    assert weekly.channels[1].details["include_pdf"] is True
    assert weekly.channels[1].details["recipient_emails"] == ["weekly@example.com"]
    assert weekly.channels[2].cadence["frame"] == "last"
    assert weekly.cards == store.get_json("pulses/42.json")["cards"]
    assert all("timezone" in c.todo for c in weekly.channels)
    assert all(c.timezone is None for c in weekly.channels)
    assert limited.cards is None and limited.channels[0].recipients is None
    assert "recipients" in limited.channels[0].todo and "completeness" in limited.todo
    assert archived.archived is True and archived.cards == [] and archived.channels[0].recipients == []
    assert archived.channels[0].source_config["schedule_calendar"] == "example-calendar"
    assert archived.channels[0].details["delivery_window_minutes"] == 15
    assert "unsupported" in archived.channels[0].todo
    assert Bundle.from_json(bundle.to_json()) == bundle
    assert Bundle.from_json('{"tool":"old"}').schedules == []


def test_subscription_parameters_use_dashboard_ids_and_expanded_names(tmp_path):
    store, bundle = parse(tmp_path)
    dashboard = store.get_json("dashboards/2.json")
    params = {p["id"]: p for p in dashboard["parameters"]}
    names = _parameter_names(params)
    first, limited, _ = bundle.schedules
    assert first.parameters == {
        names["region-a"]: "West", f'{names["dates"]}_from': "2026-01-01",
        f'{names["dates"]}_to': "2026-01-31", f'{names["quantity"]}_min': 0, f'{names["quantity"]}_max': 10,
    }
    actual = {f.name for f in bundle.dashboards[0].filters}
    assert set(first.parameters) <= actual
    binding = next(p for p in first.parameter_bindings if p["source_id"] == "region-b")
    assert binding["status"] == "unresolved" and binding["value"] == ["East", "North"]
    assert binding["filter_names"] == [n for n, _, _ in _filter_binds(params["region-b"], names["region-b"])]
    assert limited.parameters == {names["region-a"]: None, names["region-b"]: None}
    assert "relative" in limited.todo and "unknown-parameter" in limited.todo


def test_schedule_writer_uses_layout_and_preserves_hand_edits(tmp_path):
    _, bundle = parse(tmp_path)
    (tmp_path / "unshackle.toml").write_text('[paths]\nschedules = "delivery"\nnotebooks = "reports"\n')
    bundle.dashboards[0].slug = "renamed-report"
    dry = build.apply(bundle, tmp_path, dry_run=True)
    path = tmp_path / "delivery/metabase-pulse-42.yml"
    assert "delivery/metabase-pulse-42.yml" in dry.writes.written and not path.exists()
    build.apply(bundle, tmp_path)
    doc = yaml.safe_load(path.read_text())
    assert doc["notebook"] == "reports/renamed-report.ipynb"
    assert doc["dashboard_name"] == "Client reporting"
    assert doc["dashboard_url"] == ""
    assert doc["source"] == "metabase:pulse/42" and doc["channels"][1]["enabled"] is False
    assert "TODO(unshackle)" in path.read_text()
    assert doc["source_config"]["completeness"] == "unknown"
    path.write_text(path.read_text() + "# hand edit\n")
    bundle.schedules[0].name = "Changed source name"
    assert "delivery/metabase-pulse-42.yml" in build.apply(bundle, tmp_path).writes.kept_hand_edits
    assert path.read_text().endswith("# hand edit\n")


def test_pulse_extraction_is_get_only_and_does_not_drop_details(tmp_path):
    from unshackle.sources.metabase import _extract_pulses

    pulse = json.loads((FIXTURES / "pulses/42.json").read_text())
    pulse["creator"] = {"email": "ignored@example.com", "password": "example-secret"}
    pulse["channels"][0]["details"]["api_key"] = "example-secret"
    pulse["channels"][0]["details"]["future_option"] = {"value": 15, "token": "example-secret"}
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/api/pulse"
        assert dict(request.url.params) in ({"archived": "false", "dashboard_id": "2"}, {"archived": "true", "dashboard_id": "2"})
        return httpx.Response(200, json=[pulse] if request.url.params["archived"] == "false" else [])

    client = Client("https://metabase.example.com", transport=httpx.MockTransport(handler), per_minute=0)
    store = RawStore(tmp_path, "metabase")
    errors = []
    _extract_pulses(client.get_json, store, [2], errors)
    assert len(calls) == 2 and errors == []
    saved = store.get_json("pulses/42.json")
    assert saved["channels"][0]["details"]["attachment_only"] is True
    assert saved["channels"][0]["details"]["future_option"]["value"] == 15
    assert "example-secret" not in store.get_text("pulses/42.json")
    assert "creator" not in saved
    assert store.get_json("_pulses.json")["completeness"] == "unknown"


@pytest.mark.parametrize("response", [403, 404, "invalid", "empty"])
def test_incomplete_lists_retain_earlier_pulses(tmp_path, response):
    from unshackle.sources.metabase import _extract_pulses

    store, _ = parse(tmp_path)
    store.rescan()
    store.save()
    store.begin()

    def handler(request):
        return httpx.Response(response, json={"error": "example-secret"}) if isinstance(response, int) else httpx.Response(200, json={} if response == "invalid" else [])

    client = Client("https://metabase.example.com", transport=httpx.MockTransport(handler), per_minute=0)
    errors = []
    _extract_pulses(client.get_json, store, None, errors)
    store.put_json("_errors.json", errors)
    store.save()
    assert store.exists("pulses/42.json")
    assert store.get_json("_pulses.json")["retained_ids"] == [42, 43, 44]
    assert bool(errors) == (response != "empty")
    assert "example-secret" not in json.dumps(errors)


def test_card_only_scope_never_fetches_all_subscriptions(tmp_path):
    from unshackle.sources.metabase import _extract_pulses

    def unexpected(*args, **kwargs):
        pytest.fail("card-only extraction must not list pulses")

    store = RawStore(tmp_path, "metabase")
    _extract_pulses(unexpected, store, [], [])
    assert store.get_json("_pulses.json")["requests"] == []


def test_secret_projection_and_malformed_metadata_survive_offline_build(tmp_path):
    store, _ = parse(tmp_path)
    p = store.get_json("pulses/42.json")
    p["channels"][0]["details"]["future_setting"] = {"format": "example-format", "password": "example-secret"}
    p["channels"][0]["details"]["endpoint"] = "https://user:example-secret@delivery.example.com/?token=example-secret"
    p["channels"][0]["recipients"] = {"unexpected": "shape"}
    p["channels"][1]["details"] = "unexpected shape"
    p["channels"][2]["timezone"] = "Europe/Bucharest"  # unverified scheduling semantics; do not guess
    p["cards"] = [10]
    store.put_json("pulses/42.json", p)
    bundle = MetabaseSource().parse(store)
    first = bundle.schedules[0]
    assert first.cards is None and first.channels[0].recipients is None
    assert first.source_config["_unsupported"]["cards"] == [10]
    assert first.channels[1].source_config["unsupported_details"] == "unexpected shape"
    assert first.channels[2].timezone == "Europe/Bucharest" and first.channels[2].source_config["timezone"] == "Europe/Bucharest"
    assert "example-secret" not in bundle.to_json()
    build.apply(bundle, tmp_path)
    output = (tmp_path / "schedules/metabase-pulse-42.yml").read_text()
    assert "example-secret" not in output and "example-format" in output
    assert "malformed" in output and "unsupported" in output


@pytest.mark.parametrize("overrides", [
    [{"id": "region-a", "value": "West"}, {"id": "region-a", "value": "East"}],
    [{"id": "region-a", "value": {"unexpected": "shape"}}],
    [{"id": "dates", "value": "2026-13-45"}],
    [{"id": "quantity", "value": [1, 2, 3]}],
    [{"id": "region-a", "value": "West", "options": {"unknown": True}}],
])
def test_unresolved_overrides_never_become_runnable_parameters(tmp_path, overrides):
    store, _ = parse(tmp_path)
    p = store.get_json("pulses/42.json")
    p["parameters"] = overrides
    store.put_json("pulses/42.json", p)
    first = MetabaseSource().parse(store).schedules[0]
    assert first.parameters == {}
    assert all(b["status"] == "unresolved" for b in first.parameter_bindings)
    assert first.source_config["parameters"] == overrides


def test_schedule_references_follow_notebook_path_collisions(tmp_path):
    _, bundle = parse(tmp_path)
    other = Bundle(tool="other", dashboards=[Dashboard("Client reporting", "client-reporting"),
                                             Dashboard("Already suffixed", "client-reporting-metabase")])
    build.apply(other, tmp_path)
    result = build.apply(bundle, tmp_path)
    doc = yaml.safe_load((tmp_path / "schedules/metabase-pulse-42.yml").read_text())
    assert doc["notebook"] == "notebooks/client-reporting-metabase.metabase.ipynb"
    assert doc["notebook"] in result.writes.written
    assert (tmp_path / doc["notebook"]).exists()


def test_missing_dashboard_keeps_schedule_with_todo(tmp_path):
    _, bundle = parse(tmp_path)
    bundle.dashboards = []
    build.apply(bundle, tmp_path)
    doc = yaml.safe_load((tmp_path / "schedules/metabase-pulse-42.yml").read_text())
    assert doc["notebook"] is None and "no migrated notebook" in doc["todo"]


def test_schedule_parameters_execute_with_notebook_reserved_names(tmp_path):
    import pandas as pd
    from sqlalchemy import create_engine, text

    store, _ = parse(tmp_path)
    raw = store.get_json("dashboards/2.json")
    slugs = ["engine", "tile_engine", "class", "tile_class"]
    raw["parameters"] = [{"id": str(i), "slug": name, "type": "category", "default": ["default"]}
                         for i, name in enumerate(slugs)]
    store.put_json("dashboards/2.json", raw)
    pulse = store.get_json("pulses/42.json")
    pulse["parameters"] = [{"id": str(i), "value": [f"value-{i}"]} for i in range(4)]
    store.put_json("pulses/42.json", pulse)
    bundle = MetabaseSource().parse(store)
    bundle.dashboards[0].tiles = [Tile("result", kind="table", sql="SELECT " + ", ".join(f":{n} AS c{i}" for i, n in enumerate(slugs)))]
    build.apply(bundle, tmp_path)
    spec = yaml.safe_load((tmp_path / "schedules/metabase-pulse-42.yml").read_text())
    assert spec["parameters"] == {"tile_engine": "value-0", "tile_engine_2": "value-1", "tile_class": "value-2", "tile_class_2": "value-3"}
    assert [b["notebook_names"] for b in spec["parameter_bindings"]] == [[n] for n in spec["parameters"]]
    nb = json.loads((tmp_path / spec["notebook"]).read_text())
    ns = {"pd": pd, "text": text, "engine": create_engine("duckdb:///:memory:")}
    for cell in nb["cells"]:
        if cell["cell_type"] == "code" and cell["id"] != "setup":
            exec("".join(cell["source"]), ns)
            if "parameters" in cell["metadata"].get("tags", []):
                ns.update(spec["parameters"])  # Papermill injects these after the parameters cell.
    assert ns["result"].iloc[0].tolist() == [f"value-{i}" for i in range(4)]


def test_business_urls_survive_but_credential_urls_are_unresolved(tmp_path):
    store, _ = parse(tmp_path)
    pulse = store.get_json("pulses/42.json")
    pulse["parameters"] = [{"id": "region-a", "value": ["https://example.com"]},
                           {"id": "region-b", "value": ["https://user:example-secret@example.com"]}]
    pulse["channels"][0]["details"]["subject"] = "Results for https://example.com"
    store.put_json("pulses/42.json", pulse)
    bundle = MetabaseSource().parse(store)
    first = bundle.schedules[0]
    assert list(first.parameters.values()) == ["https://example.com"]
    assert [b["status"] for b in first.parameter_bindings] == ["translated", "unresolved"]
    assert first.channels[0].details["subject"] == "Results for https://example.com"
    assert "example-secret" not in bundle.to_json()


def test_schedule_comments_are_single_line_even_with_source_newlines(tmp_path):
    _, bundle = parse(tmp_path)
    bundle.schedules[0].channels[0].source_ref = "channel\ninvalid: ["
    build.apply(bundle, tmp_path)
    doc = yaml.safe_load((tmp_path / "schedules/metabase-pulse-42.yml").read_text())
    assert doc["channels"][0]["source_ref"] == "channel\ninvalid: ["


def test_report_includes_subscription_readiness_summary(tmp_path):
    _, bundle = parse(tmp_path)
    build.apply(bundle, tmp_path)
    report = (tmp_path / "migration/report.md").read_text()
    assert "## Subscription readiness (3)" in report
    assert "| metabase | Client weekly report | Client reporting | 3 | 2 | 3 | 0 | 1 |" in report
    assert "| metabase | Limited visibility example | Client reporting | 1 | 1 | 0 | 1 | 2 |" in report


@pytest.mark.parametrize("dry_run", [False, True])
def test_kept_notebook_marks_schedule_bindings_unresolved(tmp_path, dry_run):
    store, _ = parse(tmp_path)
    raw = store.get_json("dashboards/2.json")
    raw["parameters"] = [{"id": "region-a", "slug": "engine", "type": "category", "default": ["West"]}]
    store.put_json("dashboards/2.json", raw)
    pulse = store.get_json("pulses/42.json")
    pulse["parameters"] = [{"id": "region-a", "value": ["West"]}]
    store.put_json("pulses/42.json", pulse)
    bundle = MetabaseSource().parse(store)
    bundle.dashboards[0].tiles = [Tile("result", kind="table", sql="SELECT :engine AS region")]
    build.apply(bundle, tmp_path)
    notebook = tmp_path / "notebooks/client-reporting.ipynb"
    assert "| metabase | Client weekly report | Client reporting | 3 | 2 | 3 | 0 | 0 |" in (
        tmp_path / "migration/report.md"
    ).read_text()
    manual = notebook.read_text().replace("tile_engine", "manual_engine")
    notebook.write_text(manual)
    pulse["parameters"][0]["value"] = ["East"]
    store.put_json("pulses/42.json", pulse)
    bundle = MetabaseSource().parse(store)
    bundle.dashboards[0].tiles = [Tile("result", kind="table", sql="SELECT :engine AS region")]
    result = build.apply(bundle, tmp_path, dry_run=dry_run)
    assert "notebooks/client-reporting.ipynb" in result.writes.kept_hand_edits
    assert notebook.read_text() == manual
    if dry_run:
        from unshackle.build import render_all
        from unshackle.target.writer import Target

        content = render_all(bundle, Target(tmp_path, dry_run=True, tool="metabase"))["schedules/metabase-pulse-42.yml"][0]
    else:
        content = (tmp_path / "schedules/metabase-pulse-42.yml").read_text()
    doc = yaml.safe_load(content)
    assert doc["parameters"] == {}
    binding, = doc["parameter_bindings"]
    assert binding["status"] == "unresolved" and binding["notebook_names"] == []
    assert binding["value"] == ["East"] and binding["filter_names"] == ["engine"]
    assert "kept notebook" in doc["todo"] and "reconcile" in binding["todo"]
    assert bundle.schedules[0].parameter_bindings[0]["status"] == "translated"
    if not dry_run:
        expected = "| metabase | Client weekly report | Client reporting | 3 | 2 | 3 | 0 | 1 |"
        assert expected in result.report_path.read_text()
        assert expected in build.write_report(tmp_path).read_text()
        stored = build.load_bundles(tmp_path)[0]
        assert stored.schedules[0].parameter_bindings[0]["status"] == "translated"


def test_report_reads_preserved_delivery_with_layout_and_collisions(tmp_path):
    (tmp_path / "unshackle.toml").write_text('[paths]\nschedules = "delivery"\nnotebooks = "reports"\n')
    other = Bundle(tool="other", schedules=[Schedule(
        "Other", "shared:subscription", parameter_bindings=[
            {"filter_names": [], "status": "unresolved"}, {"filter_names": [], "status": "unresolved"},
        ],
    )])
    build.apply(other, tmp_path)
    bundle = Bundle(tool="metabase", dashboards=[Dashboard(
        "Example", "example", source_ref="dashboard", filters=[DashboardFilter("region")],
    )], schedules=[
        Schedule("Translated", "shared:subscription", "dashboard", parameters={"region": "West"},
                 parameter_bindings=[{"filter_names": ["region"], "status": "translated", "value": "West"}]),
        Schedule("Unresolved", "shared/subscription", "dashboard", parameter_bindings=[
            {"filter_names": [], "status": "unresolved", "todo": "source mapping unavailable"},
        ]),
        Schedule("No bindings", "shared:empty", "dashboard"),
    ])
    source_ir = bundle.to_json()
    result = build.apply(bundle, tmp_path)
    assert result.writes.renamed["delivery/shared-subscription.yml"] == "delivery/shared-subscription.metabase.yml"
    notebook = tmp_path / "reports/example.ipynb"
    manual = notebook.read_text().replace('"nbformat_minor": 5', '"nbformat_minor": 4')
    assert manual != notebook.read_text()
    notebook.write_text(manual)
    delivery = tmp_path / "delivery/shared-subscription.metabase.yml"
    doc = yaml.safe_load(delivery.read_text())
    doc["parameter_bindings"][0]["status"] = "default"
    protected = yaml.safe_dump(doc) + "# hand edit\n"
    delivery.write_text(protected)

    result = build.apply(bundle, tmp_path)
    assert notebook.read_text() == manual and delivery.read_text() == protected
    assert "delivery/shared-subscription.metabase.yml" in result.writes.kept_hand_edits
    unresolved = yaml.safe_load((tmp_path / "delivery/shared-subscription-2.yml").read_text())
    assert unresolved["source"] == "shared/subscription"
    assert unresolved["parameter_bindings"][0]["status"] == "unresolved"
    assert "source mapping unavailable" in unresolved["parameter_bindings"][0]["todo"]
    assert "kept notebook" in unresolved["todo"]
    assert yaml.safe_load((tmp_path / "delivery/shared-empty.yml").read_text())["parameter_bindings"] == []
    assert yaml.safe_load((tmp_path / "delivery/shared-subscription.yml").read_text())["name"] == "Other"
    for report in (result.report_path.read_text(), build.write_report(tmp_path).read_text()):
        assert "| metabase | Translated | Example | 0 | 0 | 0 | 0 | 0 |" in report
        assert "| metabase | Unresolved | Example | 0 | 0 | 0 | 0 | 1 |" in report
        assert "| metabase | No bindings | Example | 0 | 0 | 0 | 0 | 0 |" in report
        assert "| other | Other | (unknown) | 0 | 0 | 0 | 0 | 2 |" in report
    assert bundle.to_json() == source_ir
    assert next(b for b in build.load_bundles(tmp_path) if b.tool == "metabase").to_json() == source_ir


@pytest.mark.parametrize("contents", [None, "invalid: [", "{}", "parameter_bindings: []\nsource: another",
                                      "source: shared\nparameter_bindings: [{}]",
                                      "source: shared\nparameter_bindings: [{status: []}]"])
def test_report_marks_unreadable_delivery_bindings_unknown(tmp_path, contents):
    bundle = Bundle(tool="metabase", schedules=[Schedule("Example", "shared")])
    build.apply(bundle, tmp_path)
    delivery = tmp_path / "schedules/shared.yml"
    if contents is None:
        delivery.unlink()
    else:
        delivery.write_text(contents)
    assert "| metabase | Example | (unknown) | 0 | 0 | 0 | 0 | unknown |" in build.write_report(tmp_path).read_text()


def test_report_reads_identical_delivery_shared_by_tools(tmp_path):
    schedule = Schedule("Example", "shared", parameter_bindings=[{"filter_names": [], "status": "unresolved"}])
    build.apply(Bundle(tool="other", schedules=[schedule]), tmp_path)
    result = build.apply(Bundle(tool="metabase", schedules=[schedule]), tmp_path)
    assert "schedules/shared.yml" in result.writes.unchanged
    assert not result.writes.renamed
    assert not (tmp_path / "schedules/shared.metabase.yml").exists()
    report = build.write_report(tmp_path).read_text()
    for tool in ("metabase", "other"):
        assert f"| {tool} | Example | (unknown) | 0 | 0 | 0 | 0 | 1 |" in report


@pytest.mark.parametrize("value", [7, {}, "region-a", False])
def test_malformed_parameters_container_is_retained_without_aborting_parse(tmp_path, value):
    store, _ = parse(tmp_path)
    pulse = store.get_json("pulses/42.json")
    pulse["parameters"] = value
    store.put_json("pulses/42.json", pulse)
    bundle = MetabaseSource().parse(store)
    first, limited, archived = bundle.schedules
    assert first.parameters == {} and first.source_config["parameters"] == value
    assert "parameters container" in first.todo
    assert limited.source_ref == "metabase:pulse/43" and archived.source_ref == "metabase:pulse/44"


@pytest.mark.parametrize("value", [["region-a"], {"id": "region-a"}, 7, None, ""])
def test_malformed_parameter_id_is_retained_without_aborting_parse(tmp_path, value):
    store, _ = parse(tmp_path)
    pulse = store.get_json("pulses/42.json")
    pulse["parameters"] = [{"id": value, "value": ["West"]}]
    store.put_json("pulses/42.json", pulse)
    bundle = MetabaseSource().parse(store)
    first = bundle.schedules[0]
    assert len(bundle.schedules) == 3 and first.parameters == {}
    binding, = first.parameter_bindings
    assert binding["source_id"] == value and binding["status"] == "unresolved"
    assert "invalid source parameter ID" in binding["todo"]


@pytest.mark.parametrize("field,value", [
    ("schedule_day", ["mon"]), ("schedule_type", {"type": "daily"}),
    ("schedule_frame", ["last"]), ("schedule_hour", {"hour": 9}),
])
def test_malformed_cadence_scalar_is_retained_without_aborting_parse(tmp_path, field, value):
    store, _ = parse(tmp_path)
    pulse = store.get_json("pulses/42.json")
    pulse["channels"][0][field] = value
    store.put_json("pulses/42.json", pulse)
    bundle = MetabaseSource().parse(store)
    assert len(bundle.schedules) == 3
    channel = bundle.schedules[0].channels[0]
    assert channel.cadence[field.removeprefix("schedule_")] == value
    assert channel.source_config[field] == value and "unsupported" in channel.todo
    build.apply(bundle, tmp_path)
    assert (tmp_path / "schedules/metabase-pulse-44.yml").exists()
