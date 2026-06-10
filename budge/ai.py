"""Provider-configurable AI client (PRD sections 6, 7.4).

Providers:
  openai-compatible  /chat/completions — Ollama (cloud or local), OpenAI, etc.
                     Switching to a local model is a base_url change only.
  anthropic          api.anthropic.com /v1/messages
  command            run a local executable: prompt on stdin, reply on stdout.
                     Used by the test suite; also handy for offline scripting.

DATA MINIMIZATION CONTRACT (PRD section 6, enforced here in code regardless of
provider): the request payload contains ONLY payee/description, amount, date,
and source account name per transaction — plus the category list and examples
from agent.md. Account numbers, balances, and full history are never sent.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request

ALLOWED_TXN_FIELDS = ("id", "date", "payee", "description", "amount",
                      "source_account")
CONFIDENCES = ("high", "medium", "low")


class AIError(Exception):
    pass


def complete(cfg, system: str, user: str) -> str:
    """Send one prompt to the configured provider; return raw text reply."""
    provider = cfg.ai_provider
    if provider == "command":
        proc = subprocess.run(
            [cfg.ai_model], input=f"{system}\n\n{user}",
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise AIError(f"command provider failed: {proc.stderr.strip()}")
        return proc.stdout
    if provider == "anthropic":
        url = (cfg.ai_base_url or "https://api.anthropic.com").rstrip("/") \
            + "/v1/messages"
        body = {
            "model": cfg.ai_model,
            "max_tokens": 4096,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "content-type": "application/json",
            "x-api-key": cfg.ai_api_key,
            "anthropic-version": "2023-06-01",
        }
        data = _post_json(url, body, headers)
        return "".join(
            b.get("text", "") for b in data.get("content", [])
        )
    # default: openai-compatible
    url = cfg.ai_base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": cfg.ai_model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {"content-type": "application/json"}
    if cfg.ai_api_key:
        headers["authorization"] = f"Bearer {cfg.ai_api_key}"
    data = _post_json(url, body, headers)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise AIError(f"unexpected response shape: {str(data)[:200]}")


def _post_json(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise AIError(f"AI request failed: {e}")


def minimized_payload(txns: list) -> list:
    """Strip every transaction down to the contract fields — nothing else."""
    out = []
    for t in txns:
        out.append({k: t[k] for k in ALLOWED_TXN_FIELDS if k in t})
    return out


def extract_json(text: str):
    """Pull the first JSON object out of a model reply (tolerates fencing)."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [m.group(1)] if m else []
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    return None


def validate_decision(d: dict, allowed_categories: set):
    """Validate one decision against the output contract (PRD section 7.4).

    Returns (decision, None) if valid, else (None, reason). Malformed output is
    rejected, never guessed at.
    """
    if not isinstance(d, dict):
        return None, "decision is not an object"
    transfer = bool(d.get("transfer", False))
    category = str(d.get("category", "")).strip()
    confidence = str(d.get("confidence", "")).strip().lower()
    rationale = str(d.get("rationale", "")).strip()
    if transfer:
        category = "assets:transfers"
    elif not category:
        return None, "missing category"
    elif allowed_categories and category not in allowed_categories:
        return None, f"category {category!r} not in chart of accounts"
    if confidence not in CONFIDENCES:
        return None, f"bad confidence {confidence!r}"
    if not rationale:
        return None, "missing rationale"
    return {
        "category": category,
        "confidence": confidence,
        "rationale": rationale[:200],
        "transfer": transfer,
    }, None
