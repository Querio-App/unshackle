"""The live Tableau test's run names (tests/live/tableau/): a run deletes only its own content, and the sweep only
what runs older than the limit left behind, so runs can go on at the same time on one site. No network."""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

LIVE = Path(__file__).parent / "live" / "tableau"


@pytest.fixture
def bootstrap(monkeypatch):
    monkeypatch.setenv("UNSHACKLE_LIVE_RUN", _rid(0))
    monkeypatch.delenv("UNSHACKLE_LIVE_TABLEAU_PROJECT", raising=False)
    for name in ("tableau_site", "tableau_bootstrap_runs"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("tableau_bootstrap_runs", LIVE / "bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("tableau_site", None)


def _rid(hours_ago: float, tag: str = "a1b2") -> str:
    return f"{datetime.now(timezone.utc) - timedelta(hours=hours_ago):%Y%m%d%H%M}-{tag}"


class FakeSite:
    """The REST lists the teardown reads, and the DELETEs it sends."""

    def __init__(self, projects: list[str], groups: list[str], hosts: list[str]):
        self.lists = {
            "/projects": ("projects", "project", [{"id": f"p{i}", "name": n} for i, n in enumerate(projects)]),
            "/groups": ("groups", "group", [{"id": f"g{i}", "name": n} for i, n in enumerate(groups)]),
            "/databases": ("databases", "database", [{"id": f"d{i}", "name": "dunder_mifflin", "hostName": h} for i, h in enumerate(hosts)]),
            "/workbooks": ("workbooks", "workbook", []),
            "/datasources": ("datasources", "datasource", []),
        }
        self.deleted: list[str] = []

    def items(self, path, key, item, **params):
        return self.lists[path][2]

    def api(self, method, path, **kw):
        assert method == "DELETE"
        self.deleted.append(path)


def test_names_carry_the_run_id(bootstrap):
    run = _rid(0)
    assert bootstrap.RUN == run
    assert (bootstrap.PROJECT, bootstrap.GROUP, bootstrap.WAREHOUSE_HOST) == (
        f"unshackle-live-{run}", f"unshackle-managers-{run}", f"warehouse-{run}.example.com")
    assert f"ISMEMBEROF('unshackle-managers-{run}')" in next(c[5] for c in bootstrap.CALCS if "ISMEMBEROF" in c[5])
    assert f"server='warehouse-{run}.example.com'" in bootstrap.datasource_tds()


def test_teardown_deletes_only_this_run(bootstrap):
    mine, other, old = _rid(0), _rid(0, "ffff"), _rid(30)
    site = FakeSite([f"unshackle-live-{r}" for r in (mine, other, old)] + ["Samples"],
                    [f"unshackle-managers-{r}" for r in (mine, other, old)] + ["All Users"],
                    [f"warehouse-{r}.example.com" for r in (mine, other, old)] + ["db.example.org"])
    bootstrap.teardown(site)
    assert site.deleted == ["/projects/p0", "/groups/g0", "/databases/d0"]


def test_sweep_deletes_stale_and_legacy_runs_only(bootstrap):
    mine, running, old = _rid(0), _rid(2, "ffff"), _rid(30)
    site = FakeSite([f"unshackle-live-{r}" for r in (mine, running, old)] + ["unshackle-live", "unshackle-live-notes", "Samples"],
                    [f"unshackle-managers-{r}" for r in (mine, running, old)] + ["unshackle-managers", "All Users"],
                    [f"warehouse-{r}.example.com" for r in (mine, running, old)] + ["warehouse.example.com", "db.example.org"])
    bootstrap.sweep(site)
    # 30 hours old and the names from before run ids; not this run, not a 2-hour-old run, not other content
    assert site.deleted == ["/projects/p2", "/projects/p3", "/groups/g2", "/groups/g3", "/databases/d2", "/databases/d3"]
    site.deleted.clear()
    bootstrap.sweep(site, older_than=timedelta(hours=1))
    assert "/projects/p1" in site.deleted and "/projects/p0" not in site.deleted


def test_run_id_is_checked(monkeypatch):
    monkeypatch.setenv("UNSHACKLE_LIVE_RUN", "bad id; drop")
    sys.modules.pop("tableau_site", None)
    spec = importlib.util.spec_from_file_location("tableau_site", LIVE / "tableau_site.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(ValueError):
        module.run_id()
    sys.modules.pop("tableau_site", None)
