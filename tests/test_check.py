"""`unshackle check`, the early check in `inventory`, `.env` and `unshackle tools <tool>`. HTTP is mocked."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from unshackle import connect, envfile
from unshackle.cli import main
from unshackle.http import Client, SourceError
from unshackle.resources import data_dir
from unshackle.sources import TOOLS, source_class
from unshackle.sources.lightdash import LightdashSource
from unshackle.sources.metabase import MetabaseSource
from unshackle.sources.powerbi import PowerBISource
from unshackle.sources.quicksight import QuickSightSource
from unshackle.sources.tableau import TableauSource


def _client(handler, base="https://bi.example.com") -> Client:
    return Client(base, transport=httpx.MockTransport(handler), per_minute=0, max_retries=0)


def _reply(status: int, body: object = None, text: str = ""):
    def handler(request: httpx.Request) -> httpx.Response:
        if text:
            return httpx.Response(status, text=text, headers={"content-type": "text/html"})
        return httpx.Response(status, json=body if body is not None else {})
    return handler


def _problem(src) -> str:
    with pytest.raises(SourceError) as e:
        connect.check(src)
    return str(e.value)


# ------------------------------------------------------------------ Tableau


@pytest.fixture
def pat(monkeypatch):
    monkeypatch.setenv("TABLEAU_PAT_NAME", "migration")
    monkeypatch.setenv("TABLEAU_PAT_SECRET", "not-a-secret")


def test_tableau_signs_in_and_out(pat):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/auth/signin"):
            assert json.loads(request.content)["credentials"]["site"]["contentUrl"] == "acme"
            return httpx.Response(200, json={"credentials": {"token": "t", "site": {"id": "s1", "contentUrl": "acme"}}})
        return httpx.Response(204)

    src = TableauSource(client=_client(handler), server_url="https://tableau.example.com", site="acme")
    assert connect.check(src) == "signed in to site 'acme' on https://tableau.example.com"
    assert calls == ["/api/3.24/auth/signin", "/api/3.24/auth/signout"]


def test_tableau_401_names_site_and_token(pat):
    body = {"error": {"summary": "Signin Error", "detail": "Error signing in to Tableau Server", "code": "401001"}}
    msg = _problem(TableauSource(client=_client(_reply(401, body)), server_url="https://tableau.example.com", site="Acme Corp"))
    assert "site 'Acme Corp'" in msg and "#/site/" in msg and "display name" in msg
    assert "TABLEAU_PAT_NAME / TABLEAU_PAT_SECRET" in msg and "expire" in msg


@pytest.mark.parametrize("handler", [_reply(200, text="<html>sign in</html>"), _reply(404, text="not found")])
def test_tableau_wrong_server_asks_for_the_pod_address(pat, handler):
    msg = _problem(TableauSource(client=_client(handler), server_url="https://tableau.example.com", site="acme"))
    assert "pod address" in msg and "Tableau Cloud Manager" in msg and "TABLEAU_SERVER_URL" in msg


# ------------------------------------------------------------------ generic HTTP answers


def _metabase(handler) -> MetabaseSource:
    return MetabaseSource(client=_client(handler))


def test_check_ok_names_the_user():
    assert connect.check(_metabase(_reply(200, {"common_name": "Migration Bot"}))) == "https://bi.example.com as Migration Bot"


def test_401_names_the_credential_variables():
    msg = _problem(_metabase(_reply(401, text="Unauthenticated")))
    assert "rejected the credentials (401)" in msg and "METABASE_API_KEY" in msg and "unshackle tools metabase" in msg


def test_expired_token_is_called_expired():
    msg = _problem(_metabase(_reply(401, {"message": "Token has expired"})))
    assert "token has expired" in msg and "METABASE_API_KEY" in msg


def test_403_is_a_permission_problem():
    msg = _problem(_metabase(_reply(403, {"message": "You don't have permissions to do that."})))
    assert "refused access (403)" in msg and "skills/source-metabase" in msg


def test_404_points_at_the_server_variable():
    assert "check METABASE_URL" in _problem(_metabase(_reply(404, text="Not Found")))


def test_web_page_instead_of_api():
    assert "check METABASE_URL" in _problem(_metabase(_reply(200, text="<html>login</html>")))


def test_unreachable_server():
    def handler(request):
        raise httpx.ConnectError("name not known", request=request)

    msg = _problem(_metabase(handler))
    assert msg.startswith("cannot reach bi.example.com: check METABASE_URL")


def test_missing_variable_says_where_to_look(monkeypatch):
    monkeypatch.delenv("METABASE_URL", raising=False)
    assert "unshackle tools metabase" in _problem(MetabaseSource())


# ------------------------------------------------------------------ tool-specific answers


def test_lightdash_wrong_project():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/user":
            return httpx.Response(200, json={"results": {"firstName": "Migration"}})
        return httpx.Response(404, json={"error": {"name": "NotFoundError"}})

    msg = _problem(LightdashSource(client=_client(handler), project="00000000-0000-0000-0000-000000000000"))
    assert "LIGHTDASH_PROJECT=00000000" in msg and "/projects/" in msg


def test_powerbi_entra_error_codes(monkeypatch):
    for v in ("POWERBI_ACCESS_TOKEN", "POWERBI_FABRIC_ACCESS_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("POWERBI_TENANT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("POWERBI_CLIENT_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("POWERBI_CLIENT_SECRET", "not-a-secret")
    body = {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided."}
    src = PowerBISource(transport=httpx.MockTransport(_reply(401, body)))
    msg = _problem(src)
    assert "AADSTS7000215" in msg and "POWERBI_CLIENT_SECRET" in msg and "Secret ID" in msg


class _AwsError(Exception):
    def __init__(self, code: str):
        super().__init__(f"An error occurred ({code})")
        self.response = {"Error": {"Code": code}}


class _QuickSight:
    def __init__(self, code: str):
        self.code = code

    def list_dashboards(self, **kw):
        raise _AwsError(self.code)


@pytest.mark.parametrize("code,expect", [("ExpiredTokenException", "aws sso login"),
                                         ("AccessDeniedException", "IAM policy"),
                                         ("ResourceNotFoundException", "QUICKSIGHT_REGION")])
def test_quicksight_aws_error_codes(code, expect):
    msg = _problem(QuickSightSource(client=_QuickSight(code), account="123456789012", region="us-east-1"))
    assert code in msg and expect in msg


# ------------------------------------------------------------------ CLI


def test_inventory_fails_early_with_the_same_message(tmp_path: Path, monkeypatch):
    called = []
    monkeypatch.setattr(MetabaseSource, "client", lambda self: _client(_reply(401, text="Unauthenticated")))
    monkeypatch.setattr(MetabaseSource, "inventory", lambda self: called.append(1))
    run = CliRunner().invoke(main, ["inventory", "metabase", "--repo", str(tmp_path)])
    assert run.exit_code != 0 and "rejected the credentials (401)" in run.output
    assert not called and not (tmp_path / "migration").exists()


def test_check_command(monkeypatch):
    monkeypatch.setattr(MetabaseSource, "client", lambda self: _client(_reply(200, {"email": "bot@example.com"})))
    run = CliRunner().invoke(main, ["check", "metabase"])
    assert run.exit_code == 0 and run.output == "ok: https://bi.example.com as bot@example.com\n"


def test_check_prompt_writes_env_without_echoing_secrets(tmp_path: Path, monkeypatch):
    for v in ("METABASE_URL", "METABASE_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(MetabaseSource, "check", lambda self: "fine")
    run = CliRunner().invoke(main, ["check", "metabase", "--prompt", "--repo", str(tmp_path)],
                             input="https://metabase.example.com\nmb_not_a_secret\n")
    assert run.exit_code == 0, run.output
    assert "mb_not_a_secret" not in run.output
    assert envfile.parse((tmp_path / ".env").read_text()) == {"METABASE_URL": "https://metabase.example.com",
                                                              "METABASE_API_KEY": "mb_not_a_secret"}
    assert oct((tmp_path / ".env").stat().st_mode & 0o777) == "0o600"


def test_tools_tool_says_where_to_find_each_variable():
    out = CliRunner().invoke(main, ["tools", "tableau"]).output
    assert "#/site/" in out and "pod address" in out and "unshackle check tableau" in out


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_every_variable_is_in_the_access_section(tool):
    text = (data_dir("skills") / f"source-{tool}" / "SKILL.md").read_text()
    access = text.split("\n## Access\n", 1)[1].split("\n## ", 1)[0]
    missing = [v for v in source_class(tool).env_vars if v not in access]
    assert not missing, f"skills/source-{tool} Access section doesn't say where to find {missing}"


# ------------------------------------------------------------------ .env


def test_env_file_parsing():
    text = """# credentials
export TABLEAU_SITE=acme
TABLEAU_PAT_NAME = migration   # the token's name
TABLEAU_PAT_SECRET='a#b c'
EMPTY=
QUOTED="two\\nlines"
not a line
"""
    assert envfile.parse(text) == {"TABLEAU_SITE": "acme", "TABLEAU_PAT_NAME": "migration", "TABLEAU_PAT_SECRET": "a#b c",
                                   "EMPTY": "", "QUOTED": "two\nlines"}


def test_env_file_is_read_from_the_repo_root_and_real_variables_win(tmp_path: Path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".env").write_text("UNSHACKLE_TEST_A=from-file\nUNSHACKLE_TEST_B=from-file\n")
    (tmp_path / "models").mkdir()
    monkeypatch.setenv("UNSHACKLE_TEST_A", "from-shell")
    monkeypatch.delenv("UNSHACKLE_TEST_B", raising=False)
    monkeypatch.chdir(tmp_path / "models")
    CliRunner().invoke(main, ["tools"])
    assert os.environ["UNSHACKLE_TEST_A"] == "from-shell" and os.environ["UNSHACKLE_TEST_B"] == "from-file"


def test_env_file_lookup_stops_at_the_git_root(tmp_path: Path):
    (tmp_path / ".env").write_text("X=1\n")
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    assert envfile.find(tmp_path / "repo") is None
