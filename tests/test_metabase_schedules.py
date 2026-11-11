"""Offline subscription exports: channel settings, parameter identity and safe writes."""

import json
import shutil
from pathlib import Path

import httpx
import pytest
import yaml

from unshackle import build
from unshackle.http import Client
from unshackle.ir import Bundle, Dashboard, Tile
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
    assert weekly.channels[0].details["attachment_only"] is True
    assert weekly.channels[1].details["include_pdf"] is True
    assert weekly.channels[2].cadence["frame"] == "last"
    assert weekly.cards == store.get_json("pulses/42.json")["cards"]
    assert all(c.timezone is None and "timezone" in c.todo for c in weekly.channels)
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
    assert first.channels[2].timezone is None and first.channels[2].source_config["timezone"] == "Europe/Bucharest"
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
