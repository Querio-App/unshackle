"""Connection check: one cheap authenticated call per source, with errors a person can act on.

`unshackle check <tool>` runs it, and `inventory` (so `migrate`) runs it before anything else, so a wrong
server, site or token fails in seconds with the variable to fix instead of halfway through an extract.
"""

from __future__ import annotations

import re

import httpx

from unshackle.http import SourceError
from unshackle.sources.base import Source

_CREDENTIAL = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|CLIENT_ID|USERNAME|PAT_NAME")


def check(src: Source) -> str:
    """Run src.check(); return what it reached, or raise SourceError with a message that names what to fix."""
    try:
        return src.check()
    except (SourceError, httpx.HTTPError, ValueError) as exc:
        raise SourceError(explain(src, exc)) from exc


def explain(src: Source, exc: Exception) -> str:
    url_var = next((v for v in src.env_vars if v.endswith("_URL")), "the server address")
    creds = " / ".join(v for v in src.env_vars if _CREDENTIAL.search(v)) or "the credentials"
    where = f"where to find each variable: unshackle tools {src.name}"
    if isinstance(exc, httpx.HTTPError):
        host = ""
        try:
            host = f" {exc.request.url.host}"
        except (AttributeError, RuntimeError):
            pass
        return f"cannot reach{host}: check {url_var} ({exc})\n{where}"
    if isinstance(exc, ValueError):  # a JSON parse error: a web page answered, not the API
        return (f"the server answered, but not like the {src.label} API (a web page, not JSON): check {url_var} points at "
                f"the {src.label} address itself, without a path\n{where}")
    status = getattr(exc, "status", None)
    body = getattr(exc, "body", "").lower()
    if status in (401, 403) and "expired" in body:
        return f"{src.label} says the token has expired ({status}): create a new one and update {creds}\n{where}"
    if status == 401:
        return (f"{src.label} rejected the credentials (401): check {creds}; a token may be mistyped, revoked or "
                f"expired\n{where}")
    if status == 403:
        return (f"{src.label} accepted the credentials but refused access (403): the user behind {creds} lacks a "
                f"permission (see the Access section of skills/source-{src.name}), or a proxy blocked the request")
    if status in (404, 405):
        return f"{src.label} answered {status} for its API: check {url_var} is the {src.label} address, without a path\n{where}"
    message = str(exc)
    if message.startswith("missing environment variable"):
        message += f"\n{where}"
    return message
