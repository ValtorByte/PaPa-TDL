"""Load private Google app configuration, with a first-login terminal setup."""

import json
import getpass
import sys
import re

from . import _google_client
from .common import StorageError
from .config import private_json


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
    if not sys.stdin.isatty():
        raise StorageError(
            "Google setup is needed once. Run papa-tdl drive-login in an interactive "
            "terminal to enter your Desktop OAuth client details, or use --client-file."
        )
    print("Google Drive setup — one time for this server.")
    print("Open https://console.cloud.google.com/auth/clients and select your Desktop app client.")
    print("Copy its Client ID and Client secret below. No JSON transfer is needed.")
    print("These are app settings, not your Google password. They stay private on this server.")
    try:
        client_id = input("Desktop Client ID (Enter to cancel): ").strip()
        if not client_id:
            raise StorageError("Google setup cancelled. Run papa-tdl drive-login when ready, or select local storage.")
        client_secret = getpass.getpass("Desktop Client secret (hidden): ").strip()
        config = desktop_config({"installed": {"client_id": client_id, "client_secret": client_secret}})
        settings.prepare_storage()
        private_json(settings.data_dir / "google-client.json", json.dumps(config))
    except (EOFError, KeyboardInterrupt):
        raise StorageError("Google setup cancelled. No sign-in was completed.") from None
    except OSError:
        raise StorageError("Could not save Google settings in the private data directory. Check its permissions.") from None
    print("Google app settings saved. Starting browser sign-in...", flush=True)
    return config
