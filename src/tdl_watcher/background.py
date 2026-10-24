"""Portable background lifecycle, without requiring an init service."""

import json
import getpass
import os
import re
import signal
import subprocess
import sys
import time
import psutil

from .config import private_json
from .locks import locked
from .ui import color, panel


def _start_ticks(pid):
    try:
        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        return str(process.create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def _running(settings):
    try:
        state = json.loads((settings.data_dir / "watcher.pid").read_text())
        pid = int(state["pid"])
        if pid <= 1 or _start_ticks(pid) != state["start_ticks"]:
            return None
        command = psutil.Process(pid).cmdline()
        if "tdl_watcher" not in command or "watch" not in command:
            return None
        return pid
    except (OSError, ValueError, KeyError, TypeError, psutil.Error):
        return None


def start(settings):
    if settings.destination == "drive" and not (settings.data_dir / "google-token.json").is_file():
        raise ValueError("Google Drive is not connected. Run papa-tdl drive-login, or choose local in papa-tdl setup.")
    with locked(settings.data_dir / "control.lock", blocking=True):
        if pid := _running(settings):
            print(f"PaPa-TDL is already running (PID {pid}).")
            return
        try:
            with locked(settings.data_dir / "app.lock"):
                pass
        except BlockingIOError:
            raise ValueError("Another PaPa-TDL command is using this profile; stop it first.") from None
        log_path = settings.data_dir / "watcher.log"
        if log_path.exists() and log_path.stat().st_size > 10 * 1024**2:
            log_path.replace(log_path.with_suffix(".log.1"))
        environment = dict(os.environ, PYTHONUNBUFFERED="1", TDL_DATA_DIR=str(settings.data_dir),
            TDL_BACKGROUND="1",
            TDL_DESTINATION=settings.destination, TDL_GROUP_NAME=settings.group_name,
            TDL_POLL_SECONDS=str(settings.poll_seconds),
            TDL_OUTPUT_DIR=str(settings.local_root),
            TDL_MAX_DOWNLOAD_BYTES=str(settings.max_download_bytes),
            GOOGLE_DRIVE_FOLDER_ID=settings.drive_folder_id or "")
        (settings.data_dir / "watcher.stop").unlink(missing_ok=True)
        with log_path.open("ab", buffering=0) as log:
            log_path.chmod(0o600)
            process = subprocess.Popen([sys.executable, "-m", "tdl_watcher", "watch"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env=environment, start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                close_fds=True)
        ticks = _start_ticks(process.pid)
        if ticks is None:
            process.wait(timeout=5)
            raise ValueError("The watcher could not start. Run papa-tdl logs to see the error.")
        private_json(settings.data_dir / "watcher.pid",
                     json.dumps({"pid": process.pid, "start_ticks": ticks}))
        time.sleep(1)
        if process.poll() is not None:
            (settings.data_dir / "watcher.pid").unlink(missing_ok=True)
            raise ValueError("The watcher exited during startup. Run papa-tdl logs to see the error.")
        print(f"PaPa-TDL started in the background (PID {process.pid}).")
        print("Use papa-tdl logs to confirm Telegram and storage connected.")
        print("It survives SSH disconnects. Start it again after a VPS reboot.")


def stop(settings):
    with locked(settings.data_dir / "control.lock", blocking=True):
        pid = _running(settings)
        if not pid:
            print("PaPa-TDL background watcher is not running.")
            return
        private_json(settings.data_dir / "watcher.stop", json.dumps({"pid": pid}))
        for _ in range(150):
            if not _running(settings):
                break
            time.sleep(0.1)
        else:
            # A stalled transfer did not reach the stop request; signal only the
            # verified process/group and keep any pending local file for retry.
            if _running(settings) == pid:
                try:
                    if os.name == "nt":
                        psutil.Process(pid).terminate()
                    elif os.getpgid(pid) == pid:
                        os.killpg(pid, signal.SIGTERM)
                    else:
                        raise ValueError("Watcher process group changed; refusing to signal it.")
                except (ProcessLookupError, psutil.NoSuchProcess):
                    pass
                for _ in range(50):
                    if not _running(settings):
                        break
                    time.sleep(0.1)
                if _running(settings) == pid:
                    try:
                        if os.name == "nt":
                            psutil.Process(pid).kill()
                        elif os.getpgid(pid) == pid:
                            os.killpg(pid, signal.SIGKILL)
                    except (ProcessLookupError, psutil.NoSuchProcess):
                        pass
        for _ in range(20):
            if not _running(settings):
                break
            time.sleep(0.1)
        if _running(settings):
            raise ValueError("The watcher did not stop. Inspect papa-tdl status and logs; its state was kept.")
        (settings.data_dir / "watcher.pid").unlink(missing_ok=True)
        (settings.data_dir / "watcher.stop").unlink(missing_ok=True)
        print("PaPa-TDL stopped. Login, saved files and pending transfers are retained.")


def status(settings):
    pid = _running(settings)
    print(f"PaPa-TDL: running (PID {pid})" if pid else "PaPa-TDL: stopped")
    print(f"Private data directory: {settings.data_dir}")
    print(f"Log: {settings.data_dir / 'watcher.log'}")


def logs(settings, follow=False):
    path = settings.data_dir / "watcher.log"
    try:
        stream = path.open("r", encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print("No background log yet. Run papa-tdl start first.")
        return
    with stream:
        stream.seek(max(0, path.stat().st_size - 65536))
        print(stream.read(), end="", flush=True)
        while follow:
            line = stream.readline()
            if line:
                print(line, end="", flush=True)
            else:
                time.sleep(0.5)


def console(settings):
    """Attach the current terminal to logs and archive password requests."""
    from .service import _load_control, submit_extract_password, PASSWORD_FILE
    if not sys.stdin.isatty():
        raise ValueError("Open the live service in an interactive terminal.")
    if not _running(settings):
        start(settings)
    panel("Live service", ["Downloads, extraction and errors appear here.",
          "Archive passwords are requested here with hidden input.",
          "Ctrl+C returns to the menu. Background transfers keep running."])
    prompted = None
    progress_active = False
    path = settings.data_dir / "watcher.log"
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        stream.seek(max(0, path.stat().st_size - 8192))
        while _running(settings):
            for line in stream.readlines():
                if sys.stdout.isatty() and re.search(r"\[[#█░∙●. -]+\]", line):
                    print("\r\033[2K" + color(line.rstrip(), "96"), end="", flush=True)
                    progress_active = True
                    continue
                if progress_active:
                    print()
                    progress_active = False
                code = "91" if any(word in line.lower() for word in
                    ("failed", "error", "incorrect", "interrupted")) else "96"
                if any(word in line.lower() for word in ("saved:", "verified", "ready")):
                    code = "92"
                print(color(line.rstrip(), code), flush=True)
            batch = _load_control(settings).get("batch")
            if batch and batch.get("awaiting_password"):
                request = (batch["peer"], batch["start_mid"], batch.get("password_attempt", 0))
                if request != prompted and not (settings.data_dir / PASSWORD_FILE).exists():
                    if progress_active:
                        print()
                        progress_active = False
                    panel("Password required", ["The archive is downloaded and waiting to be extracted.",
                          "Type its password below. Input is hidden; it is not sent to Telegram."])
                    password = getpass.getpass("Archive password (hidden): ")
                    if password:
                        submit_extract_password(settings, password)
                        prompted = request
                    else:
                        print(color("Password cannot be empty. Try again, or press Ctrl+C to return.", "93"))
            time.sleep(0.5)
    print(color("The background watcher stopped. Check the errors above.", "93"))
