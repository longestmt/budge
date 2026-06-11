"""Configuration and secrets.

Two files, both OUTSIDE the data repo (PRD section 5/6):

  ~/.config/budge/budge.conf   INI; machine-local, non-secret settings
  ~/.config/budge/secrets.env  KEY=VALUE lines, chmod 600; secrets only

Environment overrides (used by tests and one-off runs):
  BUDGE_CONFIG_DIR  alternate config directory
  BUDGE_REPO        alternate data repo path
  BUDGE_HLEDGER     alternate hledger binary
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path


def config_dir() -> Path:
    return Path(
        os.environ.get("BUDGE_CONFIG_DIR", Path.home() / ".config" / "budge")
    )


def conf_path() -> Path:
    return config_dir() / "budge.conf"


def secrets_path() -> Path:
    return config_dir() / "secrets.env"


def load_secrets() -> dict:
    """Parse secrets.env (KEY=VALUE lines, # comments)."""
    secrets = {}
    path = secrets_path()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            secrets[key.strip()] = value.strip().strip('"').strip("'")
    return secrets


class Config:
    """Read-mostly view over budge.conf + secrets.env."""

    def __init__(self):
        self.ini = configparser.ConfigParser()
        if conf_path().exists():
            self.ini.read(conf_path(), encoding="utf-8")
        self.secrets = load_secrets()

    # --- repo ---
    @property
    def repo(self) -> Path:
        env = os.environ.get("BUDGE_REPO")
        if env:
            return Path(env)
        path = self.ini.get("repo", "path", fallback="")
        if not path:
            raise SystemExit(
                "error: data repo not configured. Run `budge setup` first "
                "(or set BUDGE_REPO)."
            )
        return Path(os.path.expanduser(path))

    # --- ai (PRD section 6) ---
    @property
    def ai_provider(self) -> str:
        # openai-compatible | anthropic | fake (tests only)
        return self.ini.get("ai", "provider", fallback="openai-compatible")

    @property
    def ai_base_url(self) -> str:
        return self.ini.get("ai", "base_url", fallback="https://ollama.com/v1")

    @property
    def ai_model(self) -> str:
        return self.ini.get("ai", "model", fallback="")

    @property
    def ai_api_key(self) -> str:
        return self.secrets.get("AI_API_KEY", "")

    @property
    def ai_timeout(self) -> int:
        """Seconds to wait on an AI request. Cloud models analyzing a big
        merchant list can be slow; default generously."""
        return int(self.ini.get("ai", "timeout", fallback="300"))

    # --- simplefin ---
    @property
    def simplefin_access_url(self) -> str:
        return self.secrets.get("SIMPLEFIN_ACCESS_URL", "")

    # --- schedules (systemd OnCalendar expressions) ---
    def schedule(self, which: str) -> str:
        defaults = {
            "fetch": "*-*-* 06:00:00",
            "categorize": "*-*-* 06:20:00",
            "push": "*-*-* 06:40:00",
        }
        return self.ini.get("schedule", which, fallback=defaults[which])

    # --- interactive UIs ---
    @property
    def ui_enabled(self) -> set:
        """Which UIs the operator chose: paisa, hledger-web, hledger-ui,
        hledger-textual (any combination, or none)."""
        raw = self.ini.get("ui", "enabled", fallback="paisa")
        return {u.strip() for u in raw.split(",") if u.strip()}

    # --- notifications ---
    @property
    def openclaw_url(self) -> str:
        return self.ini.get("notify", "openclaw_url", fallback="")

    @property
    def review_day(self) -> str:
        return self.ini.get("notify", "review_day", fallback="Sat")

    def hledger_bin(self) -> str:
        return os.environ.get(
            "BUDGE_HLEDGER", self.ini.get("tools", "hledger", fallback="hledger")
        )
