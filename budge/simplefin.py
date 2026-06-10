"""SimpleFIN Bridge client — speaks only the public SimpleFIN protocol.

https://www.simplefin.org/protocol.html
  claim:  POST the claim URL (base64-decoded setup token) -> access URL
  fetch:  GET {access_url}/accounts?start-date=...  (HTTP basic auth in URL)
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request


class SimpleFINError(Exception):
    pass


def _fake_server() -> str:
    """Test hook: BUDGE_FAKE_SIMPLEFIN points at a local stand-in server."""
    return os.environ.get("BUDGE_FAKE_SIMPLEFIN", "")


def claim(setup_token: str) -> str:
    """Exchange a one-time setup token for the permanent access URL.

    The setup token itself is never persisted (PRD section 6).
    """
    setup_token = setup_token.strip()
    try:
        claim_url = base64.b64decode(setup_token).decode("utf-8").strip()
    except Exception:
        raise SimpleFINError(
            "that does not look like a SimpleFIN setup token "
            "(expected base64-encoded claim URL)"
        )
    if not claim_url.startswith(("http://", "https://")):
        raise SimpleFINError(f"decoded claim URL looks wrong: {claim_url!r}")
    req = urllib.request.Request(
        claim_url, data=b"", method="POST",
        headers={"Content-Length": "0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8").strip()
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            raise SimpleFINError(
                "SimpleFIN refused the claim — this setup token has most "
                "likely ALREADY BEEN CLAIMED (tokens are one-time use). "
                "Generate a new setup token at your SimpleFIN Bridge and re-run."
            )
        raise SimpleFINError(f"claim failed: HTTP {e.code}")


def get_accounts(access_url: str, start_date: int = None) -> dict:
    """GET /accounts. Returns the parsed JSON (accounts with transactions)."""
    base = _fake_server() or access_url
    parsed = urllib.parse.urlparse(base)
    auth = None
    if parsed.username:
        auth = (parsed.username, parsed.password or "")
        netloc = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
        parsed = parsed._replace(netloc=netloc)
    url = urllib.parse.urlunparse(parsed).rstrip("/") + "/accounts"
    params = {}
    if start_date is not None:
        params["start-date"] = str(int(start_date))
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SimpleFINError(f"SimpleFIN fetch failed: HTTP {e.code}")
    except urllib.error.URLError as e:
        raise SimpleFINError(f"SimpleFIN unreachable: {e.reason}")
    for err in data.get("errors", []):
        raise SimpleFINError(f"SimpleFIN reported: {err}")
    return data
