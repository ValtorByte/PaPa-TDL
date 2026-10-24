"""Google device/desktop OAuth and resumable Drive uploads."""

import json
import getpass
import mimetypes
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .common import CHUNK_SIZE, StorageError, remote_name, source_prefix, md5_file
from .config import private_json
from .oauth_config import resolve_desktop_config
from .telegram import safe_filename
from .ui import ProgressBar

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def _read_client_file(client_file: Path, label: str) -> dict:
    try:
        config = json.loads(client_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise StorageError(f"{label} OAuth JSON was not found at {client_file}. Create the matching Google OAuth client and provide its JSON with --client-file.") from None
    except PermissionError:
        raise StorageError(f"{label} OAuth JSON at {client_file} is not readable by this runtime user.") from None
    except (OSError, ValueError):
        raise StorageError(f"{label} OAuth JSON at {client_file} could not be read or is invalid JSON.") from None
    if not isinstance(config, dict):
        raise StorageError(f"{label} OAuth JSON must be a JSON object.")
    return config


def _save_and_check_drive(settings, credentials) -> None:
    private_json(settings.data_dir / "google-token.json", credentials.to_json())
    try:
        client = DriveClient(settings)
        try:
            folder = client.choose_folder(settings.drive_folder_id)
        finally:
            client.close()
    except (StorageError, OSError) as exc:
        raise StorageError(f"Google approved access and the token was saved, but Drive is not ready: {exc}") from None
    print(f"Google Drive connected. Destination folder ready: {folder}", flush=True)


def _google_response(url: str, data: dict) -> tuple[int, dict]:
    try:
        response = requests.post(url, data=data, timeout=20)
        payload = response.json()
    except requests.RequestException:
        raise StorageError("Could not reach Google's authorization server. Check the VPS network and retry.") from None
    except ValueError:
        raise StorageError("Google's authorization server returned an invalid response.") from None
    if not isinstance(payload, dict):
        raise StorageError("Google's authorization server returned an invalid response.")
    return response.status_code, payload


def _device_client(client_file: Path | None) -> tuple[str, str]:
    if client_file is not None:
        config = _read_client_file(client_file, "TV/device")
        values = config.get("installed") or config.get("device")
        if not isinstance(values, dict):
            raise StorageError("Use OAuth credentials of type TVs and Limited Input devices for link-and-code login.")
        client_id, client_secret = values.get("client_id"), values.get("client_secret")
    else:
        if not sys.stdin.isatty():
            raise StorageError("Create a Google OAuth client of type TVs and Limited Input devices, then run papa-tdl drive-login in an interactive terminal or pass --client-file.")
        print("Create an OAuth client of type 'TVs and Limited Input devices' in Google Cloud Console.")
        print("Enable the Google Drive API and add your account as a test user if needed.")
        print("The client ID is visible as you type; the client secret is hidden.")
        client_id = input("TV OAuth client ID: ").strip()
        client_secret = getpass.getpass("TV OAuth client secret (hidden): ").strip()
    if (not isinstance(client_id, str) or not isinstance(client_secret, str)
            or not 1 <= len(client_id) <= 512 or not 1 <= len(client_secret) <= 1024
            or any(char.isspace() for char in client_id + client_secret)):
        raise StorageError("A valid TV OAuth client ID and client secret are required; Google account passwords are never entered here.")
    return client_id, client_secret


def login_drive_device(settings, client_file: Path | None = None) -> None:
    """Authorize on any browser, then poll Google until the user approves."""
    settings.prepare_storage()
    client_id, client_secret = _device_client(client_file)
    status, data = _google_response(DEVICE_CODE_URL,
        {"client_id": client_id, "scope": " ".join(SCOPES)})
    if status != 200:
        if data.get("error") == "invalid_client":
            raise StorageError("Google rejected this client. Create one of type TVs and Limited Input devices; a Desktop client cannot use link-and-code login.")
        raise StorageError(f"Google could not start device login (HTTP {status}). Check the OAuth client and test-user settings.")
    url, code, device_code = (data.get(key) for key in
                              ("verification_url", "user_code", "device_code"))
    try:
        parsed_url = urlsplit(url) if isinstance(url, str) else None
        allowed_url = (parsed_url and parsed_url.scheme == "https"
                       and parsed_url.hostname in {"google.com", "www.google.com", "accounts.google.com"}
                       and not parsed_url.username and not parsed_url.password)
    except ValueError:
        allowed_url = False
    if (not allowed_url or not isinstance(code, str)
            or not re.fullmatch(r"[A-Za-z0-9-]{4,64}", code)
            or not isinstance(device_code, str) or not 1 <= len(device_code) <= 4096):
        raise StorageError("Google returned an incomplete device login response.")
    try:
        interval = max(5, min(int(data.get("interval", 5)), 60))
        lifetime = max(1, min(int(data["expires_in"]), 3600))
    except (KeyError, TypeError, ValueError):
        raise StorageError("Google returned an invalid device login lifetime.") from None
    print(f"Open this Google link in any browser: {url}", flush=True)
    print(f"Enter this code there: {code}", flush=True)
    print("Waiting for approval. This terminal will continue automatically when you allow access.", flush=True)
    deadline = time.monotonic() + lifetime
    while time.monotonic() < deadline:
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
        status, token = _google_response(TOKEN_URL, {
            "client_id": client_id, "client_secret": client_secret,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })
        if status == 200:
            if (str(token.get("token_type", "")).lower() != "bearer"
                    or not isinstance(token.get("access_token"), str)
                    or not isinstance(token.get("refresh_token"), str)
                    or not token["access_token"] or not token["refresh_token"]
                    or SCOPES[0] not in str(token.get("scope", "")).split()):
                raise StorageError("Google did not grant the Drive file scope and an offline refresh token.")
            try:
                expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
                    seconds=max(1, int(token["expires_in"])))
            except (KeyError, TypeError, ValueError):
                raise StorageError("Google returned an invalid token lifetime.") from None
            credentials = Credentials(
                token=token["access_token"], refresh_token=token["refresh_token"],
                token_uri=TOKEN_URL, client_id=client_id,
                client_secret=client_secret, scopes=SCOPES, expiry=expiry)
            _save_and_check_drive(settings, credentials)
            return
        error = token.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = min(interval + 5, 60)
            continue
        if error == "access_denied":
            raise StorageError("Google access was denied. Run drive-login again if you want to retry.")
        if error == "expired_token":
            raise StorageError("The Google sign-in code expired. Run drive-login again for a new code.")
        if error == "invalid_client":
            raise StorageError("Google rejected this client. Use a TVs and Limited Input devices OAuth client.")
        raise StorageError(f"Google device login failed (HTTP {status}). Check the OAuth client and consent settings.")
    raise StorageError("The Google sign-in code expired before approval. Run drive-login again.")


def login_drive(settings, client_file: Path | None = None, port: int = 53682) -> None:
    settings.prepare_storage()
    if not 1024 <= port <= 65535:
        raise ValueError("OAuth callback port must be between 1024 and 65535.")
    config = resolve_desktop_config(settings, client_file)
    try:
        flow = InstalledAppFlow.from_client_config(config, SCOPES, autogenerate_code_verifier=True)
        print("Google browser sign-in is ready. If this runs on a remote VPS, first open", flush=True)
        print("this SSH forward on the computer with your browser and keep it open:", flush=True)
        print(f"  ssh -N -L {port}:127.0.0.1:{port} YOUR_SSH_USER@YOUR_VPS", flush=True)
        print("Then open the Google URL below in that computer's browser.", flush=True)
        credentials = flow.run_local_server(
            host="127.0.0.1", bind_addr="0.0.0.0" if os.environ.get("TDL_CONTAINER") else "127.0.0.1",
            port=port, open_browser=False,
            timeout_seconds=600, prompt="consent", access_type="offline",
            authorization_prompt_message="Google login URL:\n{url}",
            success_message="Google approval received. Return to PaPa-TDL while it checks Drive access.",
        )
        if not credentials or not credentials.refresh_token:
            raise StorageError("Google did not return offline access; repeat drive-login and grant access.")
        _save_and_check_drive(settings, credentials)
    except StorageError:
        raise
    except ValueError:
        raise StorageError("The Desktop OAuth JSON is invalid. Download a fresh Desktop app JSON from Google Cloud and retry.") from None
    except WSGITimeoutError:
        raise StorageError("No Google browser callback reached this machine in 10 minutes. On a VPS, open the SSH port forward before visiting the login URL.") from None
    except OSError:
        raise StorageError(f"Could not listen for Google login on port {port}. Check whether the port is already in use, or choose another with --port.") from None
    except Exception:
        raise StorageError("Google rejected the Desktop OAuth login. Check the client type, test-user access, and browser consent; use --method device on a VPS without an SSH tunnel.") from None


class DriveClient:
    def __init__(self, settings, *, api=None):
        self.settings = settings
        if api is not None:
            self.api = api
            return
        token = settings.data_dir / "google-token.json"
        if not token.exists():
            raise StorageError("Connect Google Drive first: papa-tdl drive-login")
        try:
            saved = json.loads(token.read_text(encoding="utf-8"))
            if saved.get("papa_tdl_hosted") == 1:
                from .hosted_login import HostedCredentials
                credentials = HostedCredentials(None, saved["refresh_token"])
            else:
                credentials = Credentials.from_authorized_user_info(saved, SCOPES)
            if not credentials.valid:
                credentials.refresh(Request())
                private_json(token, credentials.to_json())
            self.api = build("drive", "v3", credentials=credentials, cache_discovery=False)
        except Exception:
            raise StorageError("Google access could not be refreshed. Check connectivity or run papa-tdl drive-login again.") from None

    def close(self):
        self.api.close()

    def _execute(self, request):
        try:
            return request.execute(num_retries=4)
        except HttpError as exc:
            raise StorageError(f"Google Drive returned HTTP {exc.resp.status}; local file retained.") from None
        except Exception:
            raise StorageError("Google Drive request failed; local file retained.") from None

    def _list(self, query):
        files, token = [], None
        for _ in range(1000):
            result = self._execute(self.api.files().list(
                q=query, spaces="drive", pageSize=100, pageToken=token,
                fields="nextPageToken,files(id,name,size,mimeType,trashed,md5Checksum,appProperties,webViewLink)",
                supportsAllDrives=True, includeItemsFromAllDrives=True))
            files.extend(result.get("files", []))
            token = result.get("nextPageToken")
            if not token:
                return files
        raise StorageError("Google Drive listing exceeded 1000 pages.")

    def choose_folder(self, requested_id=None):
        if requested_id:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", requested_id):
                raise StorageError("Invalid Google Drive folder ID.")
            item = self._execute(self.api.files().get(fileId=requested_id,
                fields="id,mimeType,trashed", supportsAllDrives=True))
            if item.get("trashed") or item.get("mimeType") != "application/vnd.google-apps.folder":
                raise StorageError("Google destination must be an accessible folder.")
            return requested_id
        matches = self._list("name = 'TDL' and 'root' in parents and trashed = false "
                             "and mimeType = 'application/vnd.google-apps.folder'")
        if len(matches) > 1:
            raise StorageError("Multiple accessible TDL folders; set GOOGLE_DRIVE_FOLDER_ID.")
        if matches:
            return matches[0]["id"]
        return self._execute(self.api.files().create(body={"name": "TDL",
            "mimeType": "application/vnd.google-apps.folder"}, fields="id"))["id"]

    def find_message(self, folder_id, peer, mid):
        prefix = source_prefix(peer, mid)
        matches = [item for item in self._list(f"'{folder_id}' in parents and trashed = false "
                   f"and name contains '{prefix}'") if item.get("name", "").startswith(prefix)]
        if len(matches) > 1:
            raise StorageError("Multiple Drive files match this source; inspect the destination folder.")
        if not matches:
            return None
        item = matches[0]
        if int(item.get("size", 0)) <= 0:
            raise StorageError("A matching Drive file is empty or unavailable.")
        return item

    def upload(self, path, folder_id, peer, mid, *, existing_upload_id=None, save_upload_id=None):
        size = path.stat().st_size
        local_md5 = md5_file(path)
        if size <= 0:
            raise StorageError("The local file is empty.")
        current = self.find_message(folder_id, peer, mid)
        if current:
            if int(current["size"]) != size:
                raise StorageError("A matching Drive file has a different size; local copy retained.")
            if current.get("md5Checksum") != local_md5:
                raise StorageError("Existing Drive file checksum differs; local copy retained.")
            return current
        # A preallocated ID makes a retry after a lost completion response idempotent.
        file_id = existing_upload_id or self._execute(self.api.files().generateIds(count=1, space="drive"))["ids"][0]
        if not existing_upload_id and save_upload_id:
            save_upload_id(file_id)
        media = MediaFileUpload(str(path), mimetype=mimetypes.guess_type(path.name)[0] or
                                "application/octet-stream", chunksize=CHUNK_SIZE, resumable=True)
        request = self.api.files().create(body={"id": file_id, "name": remote_name(peer, mid, path.name),
            "parents": [folder_id]}, media_body=media, fields="id,name,size,md5Checksum", supportsAllDrives=True)
        bar = ProgressBar("Drive upload", size)
        try:
            result = None
            while result is None:
                progress, result = request.next_chunk(num_retries=4)
                if progress:
                    bar.update(round(progress.progress() * size))
        except Exception:
            bar.finish("interrupted; local copy retained", success=False)
            raise StorageError("Google upload interrupted; local copy retained. Retry the command.") from None
        verified = self._execute(self.api.files().get(fileId=result["id"], fields="id,name,size,md5Checksum",
                                                    supportsAllDrives=True))
        if int(verified.get("size", -1)) != size:
            raise StorageError("Google Drive size verification failed; local copy retained.")
        if verified.get("md5Checksum") != local_md5:
            raise StorageError("Google Drive checksum verification failed; local copy retained.")
        bar.update(size)
        bar.finish("size and MD5 verified")
        return verified

    @staticmethod
    def _q(value):
        return str(value).replace("\\", "\\\\").replace("'", "\\'")

    def find_batch(self, folder_id, batch_id):
        key = self._q(batch_id)
        matches = self._list(f"'{folder_id}' in parents and trashed = false and "
                             "mimeType = 'application/vnd.google-apps.folder' and "
                             f"appProperties has {{ key='papaTdlBatch' and value='{key}' }}")
        if len(matches) > 1:
            raise StorageError("Multiple Drive folders match this extraction batch.")
        return matches[0] if matches else None

    def _child_folder(self, parent_id, name):
        escaped = self._q(name)
        matches = self._list(f"'{parent_id}' in parents and trashed = false and "
                             "mimeType = 'application/vnd.google-apps.folder' and "
                             f"name = '{escaped}'")
        if len(matches) > 1:
            raise StorageError("Multiple Drive folders have the same extracted path.")
        if matches:
            return matches[0]["id"]
        return self._execute(self.api.files().create(
            body={"name": name, "parents": [parent_id],
                  "mimeType": "application/vnd.google-apps.folder"},
            fields="id", supportsAllDrives=True))["id"]

    def _upload_named(self, path, parent_id, name, bar, completed, total):
        escaped = self._q(name)
        matches = self._list(f"'{parent_id}' in parents and trashed = false and "
                             "mimeType != 'application/vnd.google-apps.folder' and "
                             f"name = '{escaped}'")
        checksum = md5_file(path)
        if len(matches) > 1:
            raise StorageError("Multiple Drive files have the same extracted path.")
        if matches:
            item = matches[0]
            if int(item.get("size", -1)) != path.stat().st_size or item.get("md5Checksum") != checksum:
                raise StorageError("An existing Drive batch file differs from the extracted source.")
            bar.update(completed + path.stat().st_size, total=total, detail=name)
            return item
        media = MediaFileUpload(str(path), mimetype=mimetypes.guess_type(name)[0] or
                                "application/octet-stream", chunksize=CHUNK_SIZE, resumable=True)
        request = self.api.files().create(body={"name": name, "parents": [parent_id]},
            media_body=media, fields="id,name,size,md5Checksum", supportsAllDrives=True)
        try:
            result = None
            while result is None:
                progress, result = request.next_chunk(num_retries=4)
                if progress:
                    bar.update(completed + round(progress.progress() * path.stat().st_size),
                               total=total, detail=name)
        except Exception:
            raise StorageError("Google batch upload was interrupted; extracted files were retained.") from None
        verified = self._execute(self.api.files().get(
            fileId=result["id"], fields="id,name,size,md5Checksum", supportsAllDrives=True))
        if int(verified.get("size", -1)) != path.stat().st_size or verified.get("md5Checksum") != checksum:
            raise StorageError("Google batch file verification failed; extracted files were retained.")
        return verified

    def upload_tree(self, source, folder_id, batch_id, folder_name):
        source = Path(source)
        files = sorted(item for item in source.rglob("*") if item.is_file() and not item.is_symlink())
        if not files or any(item.is_symlink() for item in source.rglob("*")):
            raise StorageError("Extracted folder is empty or contains an unsafe link.")
        batch = self.find_batch(folder_id, batch_id)
        if batch and batch.get("appProperties", {}).get("papaTdlComplete") == "1":
            return batch
        if not batch:
            batch = self._execute(self.api.files().create(body={
                "name": safe_filename(folder_name, "extracted"), "parents": [folder_id],
                "mimeType": "application/vnd.google-apps.folder",
                "appProperties": {"papaTdlBatch": batch_id, "papaTdlComplete": "0"}},
                fields="id,name,mimeType,appProperties,webViewLink", supportsAllDrives=True))
        directories = {Path("."): batch["id"]}
        total = sum(path.stat().st_size for path in files)
        completed = 0
        bar = ProgressBar("Drive batch upload", total)
        try:
            for path in files:
                relative = path.relative_to(source)
                parent_relative = Path(".")
                parent_id = batch["id"]
                for part in relative.parent.parts:
                    if part == ".":
                        continue
                    parent_relative = parent_relative / part
                    if parent_relative not in directories:
                        directories[parent_relative] = self._child_folder(parent_id, part)
                    parent_id = directories[parent_relative]
                self._upload_named(path, parent_id, relative.name, bar, completed, total)
                completed += path.stat().st_size
                bar.update(completed, detail=str(relative))
            batch = self._execute(self.api.files().update(fileId=batch["id"],
                body={"appProperties": {"papaTdlBatch": batch_id, "papaTdlComplete": "1"}},
                fields="id,name,mimeType,appProperties,webViewLink", supportsAllDrives=True))
            batch["size"] = total
            bar.finish(f"{len(files)} files verified")
            return batch
        except BaseException:
            bar.finish("interrupted; extracted files retained", success=False)
            raise
