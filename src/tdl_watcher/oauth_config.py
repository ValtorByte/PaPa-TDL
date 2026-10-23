"""Resolve the application's Google identity without prompting end users."""

import json
import re

from . import _google_client
from .common import StorageError


def desktop_config(config):
    values = config.get("installed") if isinstance(config, dict) else None
    if not isinstance(values, dict):
        raise StorageError("Expected a Desktop app OAuth JSON, not a token, Web client, or service account key.")
    client_id, secret = values.get("client_id"), values.get("client_secret")
    if (not isinstance(client_id, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,480}\.apps\.googleusercontent\.com", client_id)
            or not isinstance(secret, str) or not 1 <= len(secret) <= 1024
            or any(ord(c) < 33 or ord(c) > 126 for c in secret)):
        raise StorageError("The Desktop OAuth client ID or client secret is missing or invalid.")
    # Only app identity is copied. Tokens and supplied endpoints are never used.
    return {"installed": {
        "client_id": client_id, "client_secret": secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }}


def resolve_desktop_config(settings, client_file=None):
    source = client_file or settings.data_dir / "google-client.json"
    if client_file is not None or source.exists():
        try:
            config = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise StorageError(f"Desktop OAuth JSON is missing, unreadable or invalid at {source}.") from None
        return desktop_config(config)
    if _google_client.CLIENT_ID:
        return desktop_config({"installed": {
            "client_id": _google_client.CLIENT_ID,
            "client_secret": _google_client.CLIENT_SECRET,
        }})
    raise StorageError(
        "Google sign-in is not configured in this release. The PaPa-TDL maintainer "
        "must configure the app once with tools/configure_google_oauth.py and publish "
        "the updated release. No sign-in URL can be generated yet. "
        "For a personal setup, use papa-tdl drive-login --client-file /path/to/desktop.json."
    )
