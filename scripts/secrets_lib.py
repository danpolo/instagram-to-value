#!/usr/bin/env python3
"""Shared minimal .env reader for API keys. Keys live outside the repo at
~/.config/instagram-to-value/secrets.env (mode 600, same pattern as the IG
cookies at ~/.config/instagram/cookies.txt) -- not as SDK-managed env vars.
See PLAN.md sec 7's Phase 2 design note."""
from pathlib import Path

DEFAULT_SECRETS = Path.home() / ".config" / "instagram-to-value" / "secrets.env"


def load_secret(name, secrets_path=DEFAULT_SECRETS):
    secrets_path = Path(secrets_path)
    if not secrets_path.exists():
        return None
    for line in secrets_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip() or None
    return None
