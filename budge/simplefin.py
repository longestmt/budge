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


# beta-bridge.simplefin.org sits behind Cloudflare, which bans Python's
# default urllib User-Agent signature outright (Cloudflare "error code: 1010").
# Identify honestly as a real client and the bridge accepts the request.
USER_AGENT = "budge/1.0 (SimpleFIN client; hledger; +https://hledger.org)"


def _fake_server() -> str:
    """Test hook: BUDGE_FAKE_SIMPLEFIN points at a local stand-in server."""
    return os.environ.get("BUDGE_FAKE_SIMPLEFIN", "")


def decode_setup_token(setup_token: str) -> str:
    """Decode a setup token into its claim URL — strictly and robustly.

    Tokens are base64-encoded claim URLs, but in the wild they arrive with
    line wraps, missing padding, or URL-safe alphabet (-/_). Python's default
    decoder silently DISCARDS unknown characters, which can corrupt the URL
    into a valid-looking but wrong one — so decode with validate=True and try
    both alphabets. A pasted claim URL itself is also accepted.
    """
    token = "".join(setup_token.split())  # strip internal line wraps too
    if token.startswith(("http://", "https://")):
        return token
    padded = token + "=" * (-len(token) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            url = decoder(padded.encode("ascii"), validate=True) \
                .decode("utf-8").strip()
        except Exception:
            continue
        if url.startswith(("http://", "https://")):
            return url
    raise SimpleFINError(
        "that does not look like a SimpleFIN setup token (it should be a "
        "base64-encoded claim URL). Re-copy the whole token from your "
        "SimpleFIN Bridge page and try again."
    )


def claim(setup_token: str) -> str:
    """Exchange a one-time setup token for the permanent access URL.

    The setup token itself is never persisted (PRD section 6).
    """
    claim_url = decode_setup_token(setup_token)
    req = urllib.request.Request(
        claim_url, data=b"", method="POST",
        headers={"Content-Length": "0", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8").strip()
    except urllib.error.HTTPError as e:
        host = urllib.parse.urlparse(claim_url).netloc
        try:
            body = e.read().decode("utf-8", "replace").strip()[:300]
        except Exception:
            body = ""
        detail = f"\n  claim host: {host}\n  HTTP {e.code}" \
                 + (f"\n  server said: {body}" if body else "")
        if "1010" in body or "cloudflare" in body.lower():
            raise SimpleFINError(
                "the request was blocked by Cloudflare in front of the "
                "bridge (its bot filter), NOT by SimpleFIN — your token was "
                "never seen and is still unclaimed. This usually means the "
                "client signature was rejected; if it persists, your "
                "network/IP may be flagged — try from another network."
                + detail
            )
        if e.code == 403:
            raise SimpleFINError(
                "SimpleFIN refused the claim (HTTP 403). Tokens are one-time "
                "use, so the usual cause is that this token was already "
                "claimed — including by an earlier failed run. Generate a "
                "fresh setup token and re-run." + detail
            )
        if e.code in (404, 405):
            raise SimpleFINError(
                "the claim URL was not recognized by the server — the token "
                "may have been truncated or corrupted in copy/paste. "
                "Re-copy the WHOLE token and try again." + detail
            )
        raise SimpleFINError(f"claim failed.{detail}")
    except urllib.error.URLError as e:
        raise SimpleFINError(
            f"could not reach the SimpleFIN Bridge to claim: {e.reason}"
        )


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
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
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

    # SimpleFIN's `errors` array carries informational messages too (e.g.
    # "Requested date range exceeds limit of 90 days and was capped." on
    # backfill). Only fail when the response is actually unusable; otherwise
    # surface the messages and continue — the per-account balance assertions
    # are the real integrity check.
    messages = data.get("errors", [])
    if messages and not data.get("accounts"):
        raise SimpleFINError(
            "SimpleFIN returned no accounts and reported: "
            + "; ".join(str(m) for m in messages)
        )
    for msg in messages:
        from .util import warn
        warn(f"SimpleFIN says: {msg}")
    return data
