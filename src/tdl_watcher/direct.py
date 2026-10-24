"""Streaming HTTP downloads with validator-aware restart recovery."""

import hashlib
import json
import re
import shutil
import time
from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

from .common import StorageError
from .telegram import MIN_FREE_BYTES, safe_filename
from .ui import ProgressBar

RESERVED = {"upload.json", "upload.json.tmp", "source.json", "source.tmp", "payload.part"}


def url_identity(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use an HTTP(S) direct file URL without embedded username/password.")
    return hashlib.sha256(url.encode()).hexdigest()


def download_url(settings, url, *, session=None):
    identity = url_identity(url)
    folder = settings.pending_dir / ("url_" + identity)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata, partial = folder / "source.json", folder / "payload.part"
    state = {}
    if metadata.exists():
        try:
            state = json.loads(metadata.read_text())
        except (OSError, ValueError):
            raise StorageError("Damaged download metadata; inspect the pending directory.") from None
    if state.get("complete"):
        path = folder / safe_filename(state["name"], "download.bin")
        if not path.is_file() or path.stat().st_size != state["size"]:
            raise StorageError("The saved download does not match its metadata.")
        return path
    client = session or requests.Session()
    progress = None
    try:
        for attempt in range(4):
            offset = partial.stat().st_size if partial.exists() else 0
            validator = state.get("validator")
            if offset and not validator:
                offset = 0  # Never append bytes from an unvalidated representation.
            headers = {"Accept-Encoding": "identity"}
            if offset:
                headers.update({"Range": f"bytes={offset}-", "If-Range": validator})
            try:
                with client.get(url, headers=headers, stream=True, timeout=(20, 120),
                                allow_redirects=True) as response:
                    if response.status_code in {429, 500, 502, 503, 504}:
                        raise requests.ConnectionError("Temporary download failure")
                    if response.status_code == 416 and offset:
                        partial.unlink(missing_ok=True)
                        state = {}
                        continue
                    if response.status_code not in {200, 206}:
                        raise StorageError(f"Direct download returned HTTP {response.status_code}.")
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise StorageError("Server ignored identity encoding; download cannot be safely resumed.")
                    if response.status_code == 206:
                        match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)",
                                             response.headers.get("Content-Range", ""))
                        if not match or int(match[1]) != offset or int(match[2]) != int(match[3]) - 1:
                            raise StorageError("Invalid HTTP range response; no bytes appended.")
                        total = int(match[3])
                    else:
                        offset = 0
                        total = int(response.headers.get("Content-Length", "0"))
                    if settings.max_download_bytes and total > settings.max_download_bytes:
                        raise StorageError("Download exceeds TDL_MAX_DOWNLOAD_BYTES.")
                    if shutil.disk_usage(folder).free < MIN_FREE_BYTES + max(0, total - offset):
                        raise StorageError("Insufficient disk space for the direct download.")
                    disposition = Message()
                    disposition["content-disposition"] = response.headers.get("Content-Disposition", "")
                    name = safe_filename(disposition.get_filename() or
                        unquote(Path(urlsplit(response.url).path).name), "download.bin")
                    if name in RESERVED:
                        name = "file_" + name
                    etag = response.headers.get("ETag", "")
                    validator = etag if etag and not etag.startswith("W/") else response.headers.get("Last-Modified")
                    state = {"name": name, "validator": validator, "complete": False}
                    _save(metadata, state)
                    received = offset
                    if progress is None:
                        progress = ProgressBar("Direct download", total or None)
                    progress.update(received, total=total or None)
                    with partial.open("ab" if offset else "wb") as stream:
                        partial.chmod(0o600)
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            if settings.max_download_bytes and received + len(chunk) > settings.max_download_bytes:
                                raise StorageError("Download exceeds TDL_MAX_DOWNLOAD_BYTES.")
                            if shutil.disk_usage(folder).free < MIN_FREE_BYTES + len(chunk):
                                raise StorageError("Disk space ran low; partial download retained.")
                            stream.write(chunk)
                            received += len(chunk)
                            progress.update(received, total=total or None)
                    if received == 0 or (total and received != total):
                        raise requests.ConnectionError("Incomplete response")
                    # The queue is uploadable only once the final file and metadata exist.
                    state.update(complete=True, size=received)
                    partial.replace(folder / name)
                    _save(metadata, state)
                    progress.finish(f"{name} ready")
                    return folder / name
            except requests.RequestException:
                if attempt == 3:
                    if progress:
                        progress.finish("interrupted; partial file retained", success=False)
                    raise StorageError("Download interrupted after four attempts; partial file retained.") from None
                time.sleep(min(2 ** attempt, 8))
        raise StorageError("Could not recover the direct download; rerun the command.")
    finally:
        if session is None:
            client.close()


def _save(path, state):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
