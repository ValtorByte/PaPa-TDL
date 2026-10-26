"""Long-running Telegram group watcher with local crash recovery."""

import asyncio
import getpass
import json
import os
import re
import shutil
import socket
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from playwright.async_api import Error as BrowserError

from .config import Settings
from .config import private_json
from .health import record, verified_recently
from .common import StorageError
from .extract import (InvalidArchivePassword, PasswordRequired, PART_RE, SERIES_RE, ARCHIVE_RE,
                      EXTRACT_ENGINE_VERSION, batch_folder_name, extract_archives)
from .storage import make_storage
from .telegram import TelegramBrowser, TelegramError, matches_displayed_size, safe_filename


PENDING_RE = re.compile(r"^([1-9][0-9]{0,16})_([1-9][0-9]{0,19})$")
CONTROL_FILE = "extract-control.json"
PASSWORD_FILE = "extract-password.json"
PASSWORD_SOCKET = "archive-secret.sock"
AUTO_QUIET_SECONDS = 30
_password_receiver = None


def _archive_group(event):
    """Identify multipart sets and Telegram albums containing archives."""
    label = str(event.get("label", "")).strip()
    match = PART_RE.search(label)
    if match:
        return "parts:" + label[:match.start()].casefold()
    series = SERIES_RE.fullmatch(label)
    if series:
        return "series:" + series.group("base").casefold()
    if event.get("album") and ARCHIVE_RE.search(label):
        return "album:" + str(event["album"])
    archive = ARCHIVE_RE.search(label)
    if archive:
        return "parts:" + label[:archive.start()].casefold()
    return None


def _same_staged_media(batch, event):
    """Return the original message id when a re-forward is already staged."""
    doc_id = str(event.get("doc_id") or "")
    metadata = batch.setdefault("media_meta", {})
    if doc_id:
        for mid, saved in metadata.items():
            if str(saved.get("doc_id") or "") == doc_id:
                return mid
    # Upgrade old saved batches that predate document IDs.  Telegram's title
    # attribute now supplies the full name, so equal safe names are reliable.
    candidate = safe_filename(str(event.get("label") or ""), f"telegram_{event['mid']}.bin")
    for mid, filename in batch.get("downloaded", {}).items():
        if filename == candidate:
            return mid
    return None


def _missing_parts(files):
    numbers = set()
    for path in files:
        match = PART_RE.search(path.name)
        if match and match.group(1):
            numbers.add(int(match.group(1)))
        elif match and match.group(5):
            numbers.add(int(match.group(5)))
        else:
            series = SERIES_RE.fullmatch(path.name)
            if series:
                numbers.add(int(series.group("number")))
    if not numbers:
        return []
    return [number for number in range(1, max(numbers) + 1) if number not in numbers]


def _scan_floor(control, scan_since):
    """Keep older archive volumes visible when a later part started a batch."""
    batch = control.get("batch")
    deferred = control.get("deferred_archive_mids", [])
    if not batch:
        return min([scan_since, *deferred], key=int)
    known = [batch["start_mid"], *batch.get("media", [])]
    metadata = batch.get("media_meta", {})
    labels = [Path(str(metadata.get(mid, {}).get("label")
                       or batch.get("downloaded", {}).get(mid, "")))
              for mid in batch.get("media", [])]
    # A batch first discovered from part7 must search earlier history for
    # part1..part6. Stopping at part7 can never recover those messages.
    if batch.get("auto") and _missing_parts(labels):
        return "0"
    return min([*known, *deferred], key=int)


def _control_path(settings):
    return settings.data_dir / CONTROL_FILE


def _load_control(settings):
    try:
        value = json.loads(_control_path(settings).read_text())
        if not isinstance(value, dict) or not isinstance(value.get("last_command_mid", "0"), str):
            raise ValueError
        return value
    except FileNotFoundError:
        return {"last_command_mid": "0", "batch": None, "ignored_media": [],
                "deferred_archive_mids": []}
    except (OSError, ValueError, AttributeError):
        raise StorageError("The extraction control state is damaged; inspect extract-control.json.") from None


def _save_control(settings, control):
    private_json(_control_path(settings), json.dumps(control, separators=(",", ":")))


def submit_extract_password(settings, password: str) -> None:
    """Send a one-time password through a private local socket, never a file."""
    if not isinstance(password, str) or not password or len(password) > 4096:
        raise ValueError("Enter a nonempty archive password of at most 4096 characters.")
    control = _load_control(settings)
    batch = control.get("batch")
    if not batch or not batch.get("awaiting_password"):
        raise ValueError("The watcher is not waiting for an archive password. Check papa-tdl logs first.")
    payload = json.dumps({"password": password, "peer": batch["peer"],
                          "start_mid": batch["start_mid"]}, separators=(",", ":")).encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.sendto(payload, str(settings.data_dir / PASSWORD_SOCKET))
    except (AttributeError, OSError):
        raise ValueError("The live watcher password channel is unavailable. Open the live service and retry.") from None
    print("Password delivered to the watcher without saving it to disk.")


@contextmanager
def _password_channel(settings):
    """Accept local password datagrams for the lifetime of one watcher."""
    global _password_receiver
    path = settings.data_dir / PASSWORD_SOCKET
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise StorageError("The archive password channel path is occupied by an unexpected file.")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as probe:
                probe.sendto(b"{}", str(path))
        except (ConnectionRefusedError, FileNotFoundError):
            pass  # Left by a stopped watcher.
        else:
            raise StorageError("Another watcher is already using the archive password channel.")
        path.unlink()
    if len(os.fsencode(path)) > 100:
        raise StorageError("The private data directory path is too long for the archive password channel.")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
            server.bind(str(path))
            path.chmod(0o600)
            server.setblocking(False)
            _password_receiver = server
            try:
                yield
            finally:
                _password_receiver = None
    except (AttributeError, OSError):
        raise StorageError("This system cannot open the private archive password channel.") from None
    finally:
        path.unlink(missing_ok=True)


def _consume_extract_password(settings) -> str | None:
    if _password_receiver is not None:
        while True:
            try:
                raw = _password_receiver.recv(8192)
            except BlockingIOError:
                break
            try:
                value = json.loads(raw)
                batch = _load_control(settings).get("batch")
                if (batch and value.get("peer") == batch.get("peer")
                        and value.get("start_mid") == batch.get("start_mid")
                        and isinstance(value.get("password"), str)
                        and 0 < len(value["password"]) <= 4096):
                    return value["password"]
            except (ValueError, TypeError, AttributeError):
                continue
    # Read a one-time handoff left by older releases; new versions never write it.
    path = settings.data_dir / PASSWORD_FILE
    claim = settings.data_dir / ".extract-password.consume"
    try:
        claim.unlink(missing_ok=True)  # Clear a handoff left behind by a crashed prior watcher.
        path.replace(claim)
    except FileNotFoundError:
        return None
    except OSError:
        raise StorageError("The one-time archive password handoff could not be read; enter it again.") from None
    try:
        value = json.loads(claim.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise StorageError("The one-time archive password handoff is damaged; enter it again.") from None
    finally:
        # Claim atomically so a new password can't be deleted in a read/write race.
        claim.unlink(missing_ok=True)
    password = value.get("password") if isinstance(value, dict) else None
    if not isinstance(password, str) or not password or len(password) > 4096:
        raise StorageError("The one-time archive password handoff is invalid; enter it again.")
    return password


def _batch_root(settings, batch):
    return settings.data_dir / "extract-batches" / f"{batch['peer'][1:]}_{batch['start_mid']}"


async def _process_extract_commands(settings, browser, storage, folder_id, peer, events):
    """Advance one archive batch while retaining unrelated work for later."""
    control = _load_control(settings)
    batch = control.get("batch")
    commands = [event for event in events if event["type"] == "command"]
    media = [event for event in events if event["type"] == "media"]

    if not batch:
        starts = [event for event in commands if int(event["mid"]) > int(control["last_command_mid"])
                  and re.fullmatch(r"/extract(?:@\w+)?", event["text"], re.I)]
        if starts:
            start = starts[-1]
            batch = {"peer": peer, "start_mid": start["mid"], "done_mid": None,
                     "media": [], "downloaded": {}, "awaiting_password": False,
                     "destination": settings.destination}
            control["last_command_mid"] = start["mid"]
        else:
            ignored = set(control.get("ignored_media", []))
            groups = {}
            for event in media:
                key = _archive_group(event)
                if key and event["mid"] not in ignored:
                    groups.setdefault(key, []).append(event)
            if not groups:
                return False
            key, found = min(groups.items(), key=lambda pair: min(int(e["mid"]) for e in pair[1]))
            batch = {"peer": peer, "start_mid": min((e["mid"] for e in found), key=int),
                     "done_mid": None, "media": [], "downloaded": {},
                     "awaiting_password": False, "destination": settings.destination,
                     "auto": True, "group_key": key, "quiet_since": time.time()}
        control["batch"] = batch
        _save_control(settings, control)
        if not batch.get("auto"):
            await browser.send_text("Extraction batch opened. Send all ZIP/RAR/7z parts, then send /done.")

    if batch.get("peer") != peer:
        raise StorageError("An extraction batch belongs to another Telegram group; finish it before changing groups.")
    if batch.get("destination") != settings.destination:
        raise StorageError("The extraction destination changed mid-batch; restore it or finish the saved batch first.")
    if batch.get("auto") and not str(batch.get("group_key", "")).startswith("series:"):
        # Releases before 1.6.8 treated "Title_Part01 (01-22).rar" as a
        # standalone archive. Upgrade a retained batch so later parts join it.
        for mid in batch.get("media", []):
            label = (batch.get("media_meta", {}).get(mid, {}).get("label")
                     or batch.get("downloaded", {}).get(mid, ""))
            group = _archive_group({"label": label})
            if group and group.startswith("series:"):
                batch["group_key"] = group
                batch.pop("last_attempt_signature", None)
                batch["awaiting_more"] = False
                _save_control(settings, control)
                break
    if batch.get("extract_engine_version") != EXTRACT_ENGINE_VERSION:
        # An earlier decoder failure must not leave an upgraded watcher stuck
        # behind its saved attempt signature. Reuse downloaded parts once.
        if not batch.get("extraction_complete") and batch.get("last_attempt_signature"):
            batch.pop("last_attempt_signature", None)
            batch["awaiting_more"] = False
            print("Retrying the retained archive with the updated extraction engine.", flush=True)
        batch["extract_engine_version"] = EXTRACT_ENGINE_VERSION
        _save_control(settings, control)

    done_candidates = [event for event in commands
                       if int(event["mid"]) > int(batch["start_mid"])
                       and int(event["mid"]) > int(batch.get("done_mid") or 0)
                       and int(event["mid"]) > int(control["last_command_mid"])
                       and re.fullmatch(r"/done(?:@\w+)?", event["text"], re.I)]
    if done_candidates and (not batch.get("auto") or batch.get("awaiting_more")):
        batch["done_mid"] = done_candidates[0]["mid"]
        batch["awaiting_more"] = False
        batch.pop("last_attempt_signature", None)
        control["last_command_mid"] = batch["done_mid"]

    upper = int(batch["done_mid"]) if batch.get("done_mid") and not batch.get("awaiting_more") else None
    known = set(batch["media"])
    deferred = set(control.get("deferred_archive_mids", []))
    for event in media:
        mid = int(event["mid"])
        belongs = (_archive_group(event) == batch.get("group_key") if batch.get("auto")
                   else mid > int(batch["start_mid"]) and (upper is None or mid < upper))
        if not belongs and _archive_group(event) and event["mid"] not in control.get("ignored_media", []):
            deferred.add(event["mid"])
        if belongs and event["mid"] not in known:
            duplicate_of = _same_staged_media(batch, event)
            if duplicate_of:
                ignored = list(dict.fromkeys([*control.get("ignored_media", []), event["mid"]]))
                control["ignored_media"] = ignored[-10000:]
                continue
            batch["media"].append(event["mid"])
            batch.setdefault("media_meta", {})[event["mid"]] = {
                "doc_id": event.get("doc_id"), "label": event.get("label", "")
            }
            known.add(event["mid"])
            if batch.get("auto"):
                batch["quiet_since"] = time.time()
                batch["done_mid"] = None
                batch["awaiting_more"] = False
                batch.pop("last_attempt_signature", None)
        if event["mid"] in known:
            batch.setdefault("media_meta", {}).setdefault(event["mid"], {}).update({
                "doc_id": event.get("doc_id"), "label": event.get("label", ""),
                "size": event.get("size", ""),
            })
    batch["media"].sort(key=int)
    control["deferred_archive_mids"] = sorted(deferred - set(batch["media"]), key=int)[:10000]
    _save_control(settings, control)

    root = _batch_root(settings, batch)
    incoming = root / "archives"
    incoming.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Recover saved v1.6.3 batches that collected re-forwarded message IDs
    # after a complete multipart set had already reached the password prompt.
    # Their original seven files are intact, so never block the prompt on
    # downloading later copies that have no staged filename.
    staged = [incoming / name for name in batch.get("downloaded", {}).values()]
    if (batch.get("awaiting_password") and staged and not _missing_parts(staged)
            and all(path.is_file() and path.stat().st_size > 0 for path in staged)):
        retained = set(batch["downloaded"])
        duplicates = [mid for mid in batch["media"] if mid not in retained]
        if duplicates:
            batch["media"] = [mid for mid in batch["media"] if mid in retained]
            ignored = list(dict.fromkeys([*control.get("ignored_media", []), *duplicates]))
            control["ignored_media"] = ignored[-10000:]
            for key in ("download_failures", "download_retry_at"):
                for mid in duplicates:
                    batch.get(key, {}).pop(mid, None)
            _save_control(settings, control)

    specs = {event["mid"]: event for event in media}
    for mid in batch["media"]:
        if mid in batch["downloaded"]:
            staged_path = incoming / batch["downloaded"][mid]
            exact_size = batch.get("media_meta", {}).get(mid, {}).get("downloaded_size")
            displayed_size = batch.get("media_meta", {}).get(mid, {}).get("size", "")
            if (staged_path.is_file() and staged_path.stat().st_size > 0
                    and (exact_size is None or staged_path.stat().st_size == int(exact_size))
                    and matches_displayed_size(displayed_size, staged_path.stat().st_size)):
                continue
            staged_path.unlink(missing_ok=True)
            batch["downloaded"].pop(mid, None)
            batch.pop("last_attempt_signature", None)
            batch["awaiting_more"] = False
            batch["awaiting_password"] = False
            print(f"Archive part {mid} is incomplete; downloading it again.", flush=True)
            _save_control(settings, control)
        spec = specs.get(mid)
        if not spec:
            if batch.get("done_mid"):
                print(f"Archive part {mid} is temporarily outside Telegram Web's loaded history; retrying scan.", flush=True)
            return False
        retry_at = float(batch.get("download_retry_at", {}).get(mid, 0))
        if time.time() < retry_at:
            return False
        print(f"Batch download: Telegram message {mid}", flush=True)
        try:
            path = await browser.download(spec, destination=incoming)
        except (TelegramError, BrowserError, OSError) as exc:
            failures = batch.setdefault("download_failures", {})
            count = int(failures.get(mid, 0)) + 1
            failures[mid] = count
            retry_at = batch.setdefault("download_retry_at", {})
            retry_at[mid] = time.time() + min(300, 15 * (2 ** min(count - 1, 5)))
            _save_control(settings, control)
            print(f"Archive part {mid} download failed ({count} attempts): {exc}", flush=True)
            if isinstance(exc, BrowserError):
                await browser.stop()
            # Keep the watcher available for unrelated files while this archive
            # part waits for its retry window.
            return False
        batch.setdefault("download_failures", {}).pop(mid, None)
        batch.setdefault("download_retry_at", {}).pop(mid, None)
        batch["downloaded"][mid] = path.name
        metadata = batch.setdefault("media_meta", {}).setdefault(mid, {})
        metadata.update({"doc_id": spec.get("doc_id"), "label": spec.get("label", ""),
                         "size": spec.get("size", ""), "downloaded_size": path.stat().st_size})
        _save_control(settings, control)
        return False  # Continue unrelated files while the browser list settles.

    if batch.get("auto") and batch["media"] and not batch.get("done_mid"):
        if time.time() - batch["quiet_since"] >= AUTO_QUIET_SECONDS:
            batch["done_mid"] = max(batch["media"], key=int)
            _save_control(settings, control)
    if not batch.get("done_mid") or batch.get("awaiting_more"):
        # An archive waiting for more parts must not stall unrelated media.
        return False
    if not batch["media"]:
        print("No archive files were found in this batch. Send /extract to start again.", flush=True)
        control["batch"] = None
        _save_control(settings, control)
        shutil.rmtree(root, ignore_errors=True)
        return True

    password = None
    if batch.get("awaiting_password"):
        password = _consume_extract_password(settings)
        if password is None and os.environ.get("TDL_BACKGROUND") != "1" and sys.stdin.isatty():
            print("\nArchive password required. Type it below; input is hidden.", flush=True)
            password = getpass.getpass("Archive password (hidden): ")
            if not password:
                print("Password cannot be empty. It will be requested again.", flush=True)
                return False
        if password is None:
            return False
        batch["password_prompt_sent"] = False
        _save_control(settings, control)

    extracted = root / "extracted"
    files = [incoming / batch["downloaded"][mid] for mid in batch["media"]]
    name = batch_folder_name(files, f"Telegram batch {batch['start_mid']}")
    signature = ",".join(batch["media"])
    if batch.get("last_attempt_signature") == signature and not password and not batch.get("extraction_complete"):
        return False
    missing = _missing_parts(files)
    if missing:
        print("Archive parts missing: " + ", ".join(map(str, missing[:20])), flush=True)
        batch["last_attempt_signature"] = signature
        batch["awaiting_more"] = True
        _save_control(settings, control)
        return False
    while not batch.get("extraction_complete"):
        try:
            extract_archives(incoming, extracted, password, max_bytes=settings.max_download_bytes)
        except PasswordRequired:
            print("Archive password required. Waiting for hidden input in the live service.", flush=True)
            batch["awaiting_password"] = True
            batch["password_attempt"] = int(batch.get("password_attempt", 0)) + 1
            batch["password_prompt_sent"] = True
            _save_control(settings, control)
            if os.environ.get("TDL_BACKGROUND") != "1" and sys.stdin.isatty():
                password = getpass.getpass("Archive password (hidden): ") or None
                if password:
                    continue
                return False
            return False
        except InvalidArchivePassword:
            print("Archive password incorrect. Please try again in the live service.", flush=True)
            batch["awaiting_password"] = True
            batch["password_attempt"] = int(batch.get("password_attempt", 0)) + 1
            batch["password_prompt_sent"] = True
            _save_control(settings, control)
            if os.environ.get("TDL_BACKGROUND") != "1" and sys.stdin.isatty():
                password = getpass.getpass("Archive password (hidden): ") or None
                if password:
                    continue
                return False
            return False
        except StorageError as exc:
            print(f"Archive extraction failed: {exc}", flush=True)
            batch["last_attempt_signature"] = signature
            batch["awaiting_more"] = True
            _save_control(settings, control)
            return False
        batch["extraction_complete"] = True
        batch["awaiting_password"] = False
        batch["password_prompt_sent"] = False
        _save_control(settings, control)

    batch_id = f"tg-{peer[1:]}-{batch['start_mid']}"
    if time.time() < float(batch.get("upload_retry_at", 0)):
        return False
    extracted_count = sum(1 for _ in extracted.rglob("*"))
    try:
        item = await asyncio.to_thread(storage.upload_tree, extracted, folder_id, batch_id, name)
    except (StorageError, OSError) as exc:
        failures = int(batch.get("upload_failures", 0)) + 1
        batch["upload_failures"] = failures
        batch["upload_retry_at"] = time.time() + min(300, 15 * (2 ** min(failures - 1, 5)))
        _save_control(settings, control)
        print(f"Archive extracted but saving failed ({failures} attempts): {exc}", flush=True)
        return False
    destination = item.get("webViewLink") or item["id"]
    await browser.send_text(f"Extraction complete: {name} ({extracted_count} items). Saved to {destination}")
    print(f"Batch saved: {name} | {destination}", flush=True)
    ignored = list(dict.fromkeys([*control.get("ignored_media", []), *batch["media"]]))
    control["ignored_media"] = ignored[-10000:]
    control["deferred_archive_mids"] = [mid for mid in control.get("deferred_archive_mids", [])
                                         if mid not in batch["media"]]
    control["batch"] = None
    _save_control(settings, control)
    shutil.rmtree(root, ignore_errors=True)
    return True


def _stop_requested(settings: Settings) -> bool:
    if os.environ.get("TDL_BACKGROUND") != "1":
        return False
    try:
        return json.loads((settings.data_dir / "watcher.stop").read_text()).get("pid") == os.getpid()
    except (FileNotFoundError, OSError, ValueError, AttributeError):
        return False


async def _pause_or_stop(settings: Settings) -> bool:
    for _ in range(settings.poll_seconds):
        if _stop_requested(settings):
            return True
        await asyncio.sleep(1)
    return _stop_requested(settings)


def _manifest_path(folder: Path) -> Path:
    return folder / "upload.json"


def _read_upload_id(folder: Path, folder_id: str | None = None) -> str | None:
    path = _manifest_path(folder)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        value = data.get("upload_id")
    except (OSError, ValueError, AttributeError):
        raise StorageError("A pending upload manifest is damaged; inspect it before retrying.") from None
    if folder_id and data.get("folder_id") and data["folder_id"] != folder_id:
        raise StorageError("Pending upload belongs to another destination folder. Restore the original folder setting.")
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise StorageError("A pending upload ID is invalid; inspect it before retrying.")
    return value


def _save_upload_id(folder: Path, value: str, folder_id: str | None = None) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise StorageError("The storage API returned an invalid upload ID.")
    temporary = folder / "upload.json.tmp"
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps({"upload_id": value, "folder_id": folder_id}))
    temporary.chmod(0o600)
    temporary.replace(_manifest_path(folder))


def _pending_file(folder: Path) -> Path:
    files = [path for path in folder.iterdir()
             if path.is_file() and path.name not in {"upload.json", "upload.json.tmp", "source.json", "source.tmp", "payload.part"}]
    if len(files) != 1 or files[0].stat().st_size <= 0:
        raise StorageError("A pending Telegram file is missing or ambiguous; inspect the private pending directory.")
    return files[0]


async def retry_pending(settings: Settings, storage, folder_id: str,
                        *, only_peer: str | None = None,
                        continue_on_error: bool = False) -> None:
    root = settings.pending_dir
    for folder in sorted(root.iterdir()):
        match = PENDING_RE.fullmatch(folder.name) if folder.is_dir() else None
        direct = re.fullmatch(r"url_([0-9a-f]{64})", folder.name) if folder.is_dir() else None
        if not match and not direct:
            continue
        peer, mid = ("-" + match.group(1), match.group(2)) if match else ("url", direct.group(1))
        if not any(folder.iterdir()):
            folder.rmdir()
            continue
        if only_peer and peer != only_peer:
            continue
        if direct:
            state = json.loads((folder / "source.json").read_text())
            if not state.get("complete"):
                continue
        upload_id = None
        try:
            path = _pending_file(folder)
            upload_id = _read_upload_id(folder, folder_id)
            print(f"Transferring pending {('URL' if peer == 'url' else 'Telegram')} file: {safe_filename(path.name, 'file')}", flush=True)
            item = await asyncio.to_thread(storage.upload, path, folder_id, peer, mid,
                existing_upload_id=upload_id,
                save_upload_id=lambda value: _save_upload_id(folder, value, folder_id))
        except StorageError as exc:
            if upload_id and ("no longer usable" in str(exc) or "HTTP 404" in str(exc)):
                # Start a fresh session only when the old reservation is gone.
                _manifest_path(folder).unlink(missing_ok=True)
            if continue_on_error:
                print(f"Pending transfer {folder.name} failed: {exc}", flush=True)
                continue
            raise
        except OSError as exc:
            if continue_on_error:
                print(f"Pending transfer {folder.name} failed: {exc}", flush=True)
                continue
            raise
        if int(item["size"]) != path.stat().st_size:
            raise StorageError("Stored size differs from the pending local file; the local copy was retained.")
        print(f"Saved: {safe_filename(str(item['name']), 'file')} | {item['size']} bytes | ID {item['id']}", flush=True)
        if settings.destination == "drive":
            print("Google Drive URL: https://drive.google.com/file/d/" + item["id"] + "/view", flush=True)
        else:
            print("Local path: " + item["id"], flush=True)
        shutil.rmtree(folder)


async def connect_browser(browser, expected_peer=None, *, settings=None):
    for attempt in range(4):
        try:
            await browser.start()
            peer = await browser.find_group()
            if expected_peer and peer != expected_peer:
                raise TelegramError("The selected Telegram group changed; monitoring stopped.")
            if settings is not None:
                record(settings, "telegram")
            return peer
        except BrowserError:
            await browser.stop()
            if attempt == 3:
                raise TelegramError("Telegram Web could not reconnect after four attempts; check network/browser support.") from None
            print("Telegram Web connection interrupted; retrying with the saved session...", flush=True)
            await asyncio.sleep(5 * (attempt + 1))

async def watch(settings: Settings, *, selected_ids=None, once=False) -> None:
    settings.prepare_storage()
    with _password_channel(settings):
        await _watch_impl(settings, selected_ids=selected_ids, once=once)


async def _watch_impl(settings: Settings, *, selected_ids=None, once=False) -> None:
    settings.prepare_storage()
    storage = make_storage(settings)
    browser = TelegramBrowser(settings)
    try:
        folder_id = await asyncio.to_thread(storage.choose_folder, settings.target_folder)
        if settings.destination == "drive":
            record(settings, "drive")
        print(f"Destination: {settings.destination} | folder: {folder_id}", flush=True)
        print("Files are saved privately. Press Ctrl+C to stop foreground watching.", flush=True)
        peer = await connect_browser(browser, settings=settings)
        print(f"Watching Telegram group {settings.group_name} (chat ID {peer}).", flush=True)
        completed = set()
        retry_after = {}
        scan_since = "0"
        while True:
            if _stop_requested(settings):
                print("Watcher stopped by request.", flush=True)
                return
            if not await browser.logged_in():
                await connect_browser(browser, expected_peer=peer, settings=settings)
            elif not verified_recently(settings, "telegram", max_age=60):
                record(settings, "telegram")
            try:
                await retry_pending(settings, storage, folder_id, only_peer=peer,
                                    continue_on_error=not once)
            except (StorageError, OSError) as exc:
                if once:
                    raise
                print(f"Pending transfer failed: {exc}", flush=True)
                await asyncio.sleep(30)
                continue
            try:
                control = _load_control(settings)
                floor = _scan_floor(control, scan_since)
                events = await browser.visible_events(peer, since_mid=floor)
                if events:
                    scan_since = max((item["mid"] for item in events), key=int)
            except BrowserError:
                if once:
                    raise TelegramError("Telegram browser disconnected; retry the command.") from None
                await browser.stop()
                await asyncio.sleep(5)
                continue
            try:
                await _process_extract_commands(
                    settings, browser, storage, folder_id, peer, events)
            except (BrowserError, TelegramError, StorageError, OSError) as exc:
                if once:
                    raise
                print(f"Archive watcher step failed; saved batch state will be retried: {exc}", flush=True)
                if isinstance(exc, BrowserError):
                    await browser.stop()
                if await _pause_or_stop(settings):
                    print("Watcher stopped by request.", flush=True)
                    return
                continue
            extract_control = _load_control(settings)
            ignored = set(extract_control.get("ignored_media", []))
            if extract_control.get("batch"):
                ignored.update(extract_control["batch"].get("media", []))
            completed.update((peer, mid) for mid in ignored)
            specs = [event for event in events if event["type"] == "media"]
            for spec in specs:
                mid = spec["mid"]
                if selected_ids and mid not in selected_ids:
                    continue
                if _archive_group(spec) and not selected_ids:
                    continue  # Archives are saved only after their batch extracts.
                key = (peer, mid)
                if key in completed or time.monotonic() < retry_after.get(key, 0):
                    continue
                try:
                    existing = await asyncio.to_thread(storage.find_message, folder_id, peer, mid)
                    if existing:
                        print(f"Already saved: {safe_filename(str(existing['name']), 'file')} | ID {existing['id']}", flush=True)
                        completed.add(key)
                        continue
                    print(f"Downloading Telegram message {mid}...", flush=True)
                    path = await browser.download(spec)
                    await retry_pending(settings, storage, folder_id, only_peer=peer,
                                        continue_on_error=not once)
                    completed.add(key)
                except (StorageError, TelegramError, OSError, BrowserError) as exc:
                    if once:
                        if isinstance(exc, BrowserError):
                            raise TelegramError("Telegram browser transfer interrupted; retry the command.") from None
                        raise
                    reason = "Browser/network transfer interrupted; saved session will be reused." if isinstance(exc, BrowserError) else str(exc)
                    print(f"Message {mid} failed: {reason}", flush=True)
                    if isinstance(exc, (TelegramError, BrowserError)):
                        await browser.stop()  # Stop any background browser transfer.
                    retry_after[key] = time.monotonic() + 60
                    break  # Refresh Telegram's rendered messages after a transfer.
                break  # A long transfer can change the rendered message list.
            else:
                if once:
                    missing = set(selected_ids or ()) - {mid for _, mid in completed}
                    if missing:
                        raise TelegramError("Some selected message IDs are not visible or did not complete: " + ", ".join(sorted(missing)))
                    return
            if await _pause_or_stop(settings):
                print("Watcher stopped by request.", flush=True)
                return
    finally:
        await browser.stop()
        storage.close()
