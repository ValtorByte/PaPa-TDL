"""Long-running Telegram group watcher with local crash recovery."""

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path

from playwright.async_api import Error as BrowserError

from .config import Settings
from .config import private_json
from .common import StorageError
from .extract import (InvalidArchivePassword, PasswordRequired, batch_folder_name,
                      extract_archives)
from .storage import make_storage
from .telegram import TelegramBrowser, TelegramError, safe_filename


PENDING_RE = re.compile(r"^([1-9][0-9]{0,16})_([1-9][0-9]{0,19})$")
CONTROL_FILE = "extract-control.json"


def _control_path(settings):
    return settings.data_dir / CONTROL_FILE


def _load_control(settings):
    try:
        value = json.loads(_control_path(settings).read_text())
        if not isinstance(value, dict) or not isinstance(value.get("last_command_mid", "0"), str):
            raise ValueError
        return value
    except FileNotFoundError:
        return {"last_command_mid": "0", "batch": None, "ignored_media": []}
    except (OSError, ValueError, AttributeError):
        raise StorageError("The extraction control state is damaged; inspect extract-control.json.") from None


def _save_control(settings, control):
    private_json(_control_path(settings), json.dumps(control, separators=(",", ":")))


def _batch_root(settings, batch):
    return settings.data_dir / "extract-batches" / f"{batch['peer'][1:]}_{batch['start_mid']}"


async def _process_extract_commands(settings, browser, storage, folder_id, peer, events):
    """Advance one explicit /extract batch. Returns True while batch mode owns the loop."""
    control = _load_control(settings)
    batch = control.get("batch")
    commands = [event for event in events if event["type"] == "command"]
    media = [event for event in events if event["type"] == "media"]

    if not batch:
        starts = [event for event in commands if int(event["mid"]) > int(control["last_command_mid"])
                  and re.fullmatch(r"/extract(?:@\w+)?", event["text"], re.I)]
        if not starts:
            return False
        start = starts[-1]
        batch = {"peer": peer, "start_mid": start["mid"], "done_mid": None,
                 "media": [], "downloaded": {}, "awaiting_password": False,
                 "destination": settings.destination}
        control["batch"] = batch
        control["last_command_mid"] = start["mid"]
        _save_control(settings, control)
        await browser.send_text("Extraction batch opened. Send all ZIP/RAR/7z parts, then send /done.")

    if batch.get("peer") != peer:
        raise StorageError("An extraction batch belongs to another Telegram group; finish it before changing groups.")
    if batch.get("destination") != settings.destination:
        raise StorageError("The extraction destination changed mid-batch; restore it or finish the saved batch first.")

    done_candidates = [event for event in commands
                       if int(event["mid"]) > int(batch["start_mid"])
                       and re.fullmatch(r"/done(?:@\w+)?", event["text"], re.I)]
    if not batch.get("done_mid") and done_candidates:
        batch["done_mid"] = done_candidates[0]["mid"]
        control["last_command_mid"] = batch["done_mid"]

    upper = int(batch["done_mid"]) if batch.get("done_mid") else None
    known = set(batch["media"])
    for event in media:
        mid = int(event["mid"])
        if mid > int(batch["start_mid"]) and (upper is None or mid < upper) and event["mid"] not in known:
            batch["media"].append(event["mid"])
            known.add(event["mid"])
    batch["media"].sort(key=int)
    _save_control(settings, control)

    root = _batch_root(settings, batch)
    incoming = root / "archives"
    incoming.mkdir(parents=True, exist_ok=True, mode=0o700)
    specs = {event["mid"]: event for event in media}
    for mid in batch["media"]:
        if mid in batch["downloaded"]:
            continue
        spec = specs.get(mid)
        if not spec:
            if batch.get("done_mid"):
                raise TelegramError(f"Batch file message {mid} is no longer visible; scroll to it and retry.")
            return True
        print(f"Batch download: Telegram message {mid}", flush=True)
        path = await browser.download(spec, destination=incoming)
        batch["downloaded"][mid] = path.name
        _save_control(settings, control)
        return True  # Telegram's rendered message list may have changed.

    if not batch.get("done_mid"):
        return True
    if not batch["media"]:
        await browser.send_text("No archive files were found between /extract and /done. Send /extract to start again.")
        control["batch"] = None
        _save_control(settings, control)
        shutil.rmtree(root, ignore_errors=True)
        return True

    password = None
    password_commands = [event for event in commands
                         if int(event["mid"]) > int(batch["done_mid"])
                         and int(event["mid"]) > int(control["last_command_mid"])
                         and re.match(r"^/password(?:@\w+)?\s+", event["text"], re.I)]
    if batch.get("awaiting_password"):
        if not password_commands:
            return True
        command = password_commands[-1]
        password = re.sub(r"^/password(?:@\w+)?\s+", "", command["text"], flags=re.I)
        control["last_command_mid"] = command["mid"]

    extracted = root / "extracted"
    files = [incoming / batch["downloaded"][mid] for mid in batch["media"]]
    name = batch_folder_name(files, f"Telegram batch {batch['start_mid']}")
    try:
        extract_archives(incoming, extracted, password, max_bytes=settings.max_download_bytes)
    except PasswordRequired:
        batch["awaiting_password"] = True
        _save_control(settings, control)
        await browser.send_text("This archive is password protected. Send /password YOUR_PASSWORD in this private group.")
        return True
    except InvalidArchivePassword:
        batch["awaiting_password"] = True
        _save_control(settings, control)
        await browser.send_text("That password was rejected. Send /password YOUR_PASSWORD again.")
        return True

    batch_id = f"tg-{peer[1:]}-{batch['start_mid']}"
    item = await asyncio.to_thread(storage.upload_tree, extracted, folder_id, batch_id, name)
    destination = item.get("webViewLink") or item["id"]
    await browser.send_text(f"Extraction complete: {name} ({len(list(extracted.rglob('*')))} items). Saved to {destination}")
    print(f"Batch saved: {name} | {destination}", flush=True)
    ignored = list(dict.fromkeys([*control.get("ignored_media", []), *batch["media"]]))
    control["ignored_media"] = ignored[-10000:]
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
                        *, only_peer: str | None = None) -> None:
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
        path = _pending_file(folder)
        upload_id = _read_upload_id(folder, folder_id)
        print(f"Transferring pending {('URL' if peer == 'url' else 'Telegram')} file: {safe_filename(path.name, 'file')}", flush=True)
        try:
            item = await asyncio.to_thread(storage.upload, path, folder_id, peer, mid,
                existing_upload_id=upload_id,
                save_upload_id=lambda value: _save_upload_id(folder, value, folder_id))
        except StorageError as exc:
            if upload_id and ("no longer usable" in str(exc) or "HTTP 404" in str(exc)):
                # Start a fresh session only when the old reservation is gone.
                _manifest_path(folder).unlink(missing_ok=True)
            raise
        if int(item["size"]) != path.stat().st_size:
            raise StorageError("Stored size differs from the pending local file; the local copy was retained.")
        print(f"Saved: {safe_filename(str(item['name']), 'file')} | {item['size']} bytes | ID {item['id']}", flush=True)
        if settings.destination == "drive":
            print("Google Drive URL: https://drive.google.com/file/d/" + item["id"] + "/view", flush=True)
        else:
            print("Local path: " + item["id"], flush=True)
        shutil.rmtree(folder)


async def connect_browser(browser, expected_peer=None):
    for attempt in range(4):
        try:
            await browser.start()
            peer = await browser.find_group()
            if expected_peer and peer != expected_peer:
                raise TelegramError("The selected Telegram group changed; monitoring stopped.")
            return peer
        except BrowserError:
            await browser.stop()
            if attempt == 3:
                raise TelegramError("Telegram Web could not reconnect after four attempts; check network/browser support.") from None
            print("Telegram Web connection interrupted; retrying with the saved session...", flush=True)
            await asyncio.sleep(5 * (attempt + 1))

async def watch(settings: Settings, *, selected_ids=None, once=False) -> None:
    settings.prepare_storage()
    storage = make_storage(settings)
    browser = TelegramBrowser(settings)
    try:
        folder_id = await asyncio.to_thread(storage.choose_folder, settings.target_folder)
        print(f"Destination: {settings.destination} | folder: {folder_id}", flush=True)
        print("Files are saved privately. Press Ctrl+C to stop foreground watching.", flush=True)
        peer = await connect_browser(browser)
        print(f"Watching Telegram group {settings.group_name} (chat ID {peer}).", flush=True)
        completed = set()
        retry_after = {}
        while True:
            if _stop_requested(settings):
                print("Watcher stopped by request.", flush=True)
                return
            if not await browser.logged_in():
                await connect_browser(browser, expected_peer=peer)
            try:
                await retry_pending(settings, storage, folder_id, only_peer=peer)
            except (StorageError, OSError) as exc:
                if once:
                    raise
                print(f"Pending transfer failed: {exc}", flush=True)
                await asyncio.sleep(30)
                continue
            try:
                events = await browser.visible_events(peer)
            except BrowserError:
                if once:
                    raise TelegramError("Telegram browser disconnected; retry the command.") from None
                await browser.stop()
                await asyncio.sleep(5)
                continue
            batch_mode = await _process_extract_commands(
                settings, browser, storage, folder_id, peer, events)
            extract_control = _load_control(settings)
            ignored = set(extract_control.get("ignored_media", []))
            if extract_control.get("batch"):
                ignored.update(extract_control["batch"].get("media", []))
            completed.update((peer, mid) for mid in ignored)
            if batch_mode:
                if await _pause_or_stop(settings):
                    print("Watcher stopped by request.", flush=True)
                    return
                continue
            specs = [event for event in events if event["type"] == "media"]
            for spec in specs:
                mid = spec["mid"]
                if selected_ids and mid not in selected_ids:
                    continue
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
                    await retry_pending(settings, storage, folder_id, only_peer=peer)
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
