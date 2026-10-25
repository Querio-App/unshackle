"""A Tableau Cloud (or Server) site for live tests: a signed-in REST session, run names, view data and
VizQL Data Service queries. Shared by tests/live/tableau/bootstrap.py and any live Tableau test.

Live runs can go on at the same time (several agents, one site): each run has an id, and everything it
creates on the site is named after it, so it only deletes its own content. Each process needs its own
Personal Access Token, because one PAT holds one session: a second sign-in with the same token signs the
first one out.

Environment: TABLEAU_SERVER_URL, TABLEAU_SITE (content URL), TABLEAU_PAT_NAME, TABLEAU_PAT_SECRET,
TABLEAU_API_VERSION (default 3.24), UNSHACKLE_LIVE_RUN (the run id; generated when unset),
UNSHACKLE_LIVE_TABLEAU_PROJECT (the name prefix, default `unshackle-live`).
"""

from __future__ import annotations

import io
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from xml.sax.saxutils import quoteattr

import httpx

PREFIX = os.environ.get("UNSHACKLE_LIVE_TABLEAU_PROJECT", "unshackle-live")
RUN_ID = re.compile(r"(\d{12})-[0-9a-f]{4}")  # UTC minute + random suffix: its age is in its name


def run_id() -> str:
    """This process's run id: UNSHACKLE_LIVE_RUN, or a new one (kept in the environment, so every module and the
    unshackle CLI subprocesses see the same)."""
    rid = os.environ.get("UNSHACKLE_LIVE_RUN")
    if not rid:
        rid = f"{datetime.now(timezone.utc):%Y%m%d%H%M}-{secrets.token_hex(2)}"
        os.environ["UNSHACKLE_LIVE_RUN"] = rid
    if not re.fullmatch(r"[A-Za-z0-9-]{1,40}", rid):
        raise ValueError(f"UNSHACKLE_LIVE_RUN must be letters, digits and dashes: {rid!r}")
    return rid


def run_age(name: str) -> timedelta | None:
    """How old the run that created `name` is, from the run id in it; None if it has none."""
    m = RUN_ID.search(name)
    if not m:
        return None
    started = datetime.strptime(m.group(1), "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - started


class Site:
    """A signed-in REST session (PAT from TABLEAU_PAT_NAME / TABLEAU_PAT_SECRET)."""

    def __init__(self):
        self.server = os.environ["TABLEAU_SERVER_URL"].rstrip("/")
        self.content_url = os.environ.get("TABLEAU_SITE", "")
        self.version = os.environ.get("TABLEAU_API_VERSION") or "3.24"
        self.http = httpx.Client(base_url=self.server, timeout=300, headers={"Accept": "application/json"})
        creds = self.call("POST", f"/api/{self.version}/auth/signin", json={"credentials": {
            "personalAccessTokenName": os.environ["TABLEAU_PAT_NAME"],
            "personalAccessTokenSecret": os.environ["TABLEAU_PAT_SECRET"],
            "site": {"contentUrl": self.content_url},
        }})["credentials"]
        self.http.headers["X-Tableau-Auth"] = creds["token"]
        self.site_id = creds["site"]["id"]
        self.user_id = creds["user"]["id"]

    def call(self, method: str, path: str, **kw: Any) -> Any:
        r = self.http.request(method, path, **kw)
        if r.status_code == 401 and "X-Tableau-Auth" in self.http.headers:
            raise RuntimeError(f"{method} {path.split('?')[0]} -> 401: signed out. If another process signed in with the same "
                               f"Personal Access Token, give each live run its own PAT (tests/live/README.md). {r.text[:300]}")
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path.split('?')[0]} -> {r.status_code}: {r.text[:600]}")
        return r.json() if r.headers.get("content-type", "").startswith("application/json") and r.content else r

    def api(self, method: str, path: str, **kw: Any) -> Any:
        return self.call(method, f"/api/{self.version}/sites/{self.site_id}{path}", **kw)

    def items(self, path: str, key: str, item: str, **params: Any) -> list[dict[str, Any]]:
        """Every item of a REST list, all pages."""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.api("GET", path, params={"pageSize": 1000, "pageNumber": page, **params})
            found = (data.get(key) or {}).get(item) or []
            out += [found] if isinstance(found, dict) else found
            total = int((data.get("pagination") or {}).get("totalAvailable") or 0)
            if len(out) >= total or not found:
                return out
            page += 1

    def close(self) -> None:
        try:
            self.call("POST", f"/api/{self.version}/auth/signout")
        finally:
            self.http.close()

    def __enter__(self) -> "Site":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def publish(self, kind: str, name: str, project_id: str, filename: str, content: bytes) -> dict[str, Any]:
        """POST .../{workbooks|datasources}?overwrite=true as multipart/mixed (request_payload + file)."""
        tag = kind[:-1]
        payload = f"<tsRequest><{tag} name={quoteattr(name)}><project id='{project_id}' /></{tag}></tsRequest>"
        boundary = uuid.uuid4().hex
        body = io.BytesIO()
        body.write(f"--{boundary}\r\nContent-Disposition: name=\"request_payload\"\r\nContent-Type: text/xml\r\n\r\n{payload}\r\n".encode())
        body.write(f"--{boundary}\r\nContent-Disposition: name=\"tableau_{tag}\"; filename=\"{filename}\"\r\n"
                   f"Content-Type: application/octet-stream\r\n\r\n".encode())
        body.write(content)
        body.write(f"\r\n--{boundary}--\r\n".encode())
        extra = "&skipConnectionCheck=true" if kind == "workbooks" else ""
        return self.api("POST", f"/{kind}?overwrite=true{extra}", content=body.getvalue(),
                        headers={"Content-Type": f"multipart/mixed; boundary={boundary}"})[tag]

    def view_data(self, view_id: str, **filters: str) -> str:
        """Tableau's own data for a view as CSV (GET /views/{id}/data); `filters` set filters and parameters by caption."""
        params = {f"vf_{k}": v for k, v in filters.items()}
        return self.api("GET", f"/views/{view_id}/data", params={**params, "maxAge": 1}, headers={"Accept": "*/*"}).text

    def query(self, datasource_luid: str, fields: list[dict[str, Any]], filters: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """VizQL Data Service: Tableau computes `fields` on a published data source. A field is {"fieldCaption": ...}, with
        "function" (SUM, AVG, ...) or "calculation" (any Tableau formula, LODs included)."""
        query: dict[str, Any] = {"fields": fields}
        if filters:
            query["filters"] = filters
        resp = self.call("POST", "/api/v1/vizql-data-service/query-datasource",
                         json={"datasource": {"datasourceLuid": datasource_luid}, "query": query})
        return (resp if isinstance(resp, dict) else resp.json())["data"]  # not always sent as application/json

    def wait_for_metadata(self, workbook: str, timeout: float = 600) -> None:
        """The Metadata API indexes new content with a delay; wait until it knows the workbook."""
        query = "query($l: [String]) { workbooksConnection(filter: {luidWithin: $l}) { nodes { luid } } }"
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            data = self.call("POST", "/api/metadata/graphql", json={"query": query, "variables": {"l": [workbook]}})
            if ((data.get("data") or {}).get("workbooksConnection") or {}).get("nodes"):
                return
            time.sleep(10)
        raise TimeoutError(f"the Metadata API did not index workbook {workbook} within {timeout:.0f}s")
