"""Browser sign-in for remote machines, without a loopback listener."""

import json
import secrets
import time

import requests
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from .common import StorageError
from .ui import message

BROKER = "https://valtorbyte.ir/papa-tdl-auth"
SCOPE = "https://www.googleapis.com/auth/drive.file"


def _post(path, payload):
    try:
        response = requests.post(BROKER + path, json=payload, timeout=25,
                                 allow_redirects=False)
        if path == "/poll" and response.status_code == 410:
            return {"status": "expired"}
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (requests.RequestException, ValueError):
        raise StorageError("The PaPa-TDL sign-in service is unavailable. Retry later; no login was completed.") from None


class HostedCredentials(Credentials):
    """Refresh through the app owner; never distribute the Web client secret."""

    def __init__(self, token, refresh_token):
        super().__init__(token=token, refresh_token=refresh_token,
                         token_uri="https://oauth2.googleapis.com/token",
                         client_id="papa-tdl-hosted", scopes=[SCOPE])

    def refresh(self, request):
        from datetime import datetime, timedelta, timezone
        try:
            data = _post("/refresh", {"refresh_token": self.refresh_token})
            self.token = data["access_token"]
            self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=int(data["expires_in"]))
        except (StorageError, KeyError, TypeError, ValueError):
            raise RefreshError("Google access could not be renewed. Retry or run papa-tdl drive-login.") from None

    def to_json(self, *args, **kwargs):
        return json.dumps({"papa_tdl_hosted": 1, "token": self.token,
                           "refresh_token": self.refresh_token})


def login_hosted(settings, save_and_check):
    settings.prepare_storage()
    # Only this process holds the polling secret; it never appears in the URL.
    poll_secret = secrets.token_urlsafe(32)
    data = _post("/start", {"poll_secret": poll_secret})
    ticket = data.get("ticket", "")
    if not isinstance(ticket, str) or len(ticket) != 43 or not all(c.isalnum() or c in "_-" for c in ticket):
        raise StorageError("The sign-in service returned an invalid request.")
    message("Open this Google sign-in link on your computer or phone:", "info")
    print(f"{BROKER}/login/{ticket}", flush=True)
    message("Waiting for Google approval; this terminal continues automatically.", "warning")
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        time.sleep(3)
        result = _post("/poll", {"ticket": ticket, "poll_secret": poll_secret})
        if result.get("status") == "pending":
            continue
        if result.get("status") != "complete":
            raise StorageError("Google sign-in was cancelled or expired. Run papa-tdl drive-login again.")
        if not all(isinstance(result.get(k), str) and result[k] for k in ("access_token", "refresh_token")):
            raise StorageError("Google did not grant offline access. Sign in again.")
        save_and_check(settings, HostedCredentials(result["access_token"], result["refresh_token"]))
        return
    raise StorageError("Google sign-in expired after 10 minutes. Run papa-tdl drive-login again.")
