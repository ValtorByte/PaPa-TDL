"""PaPa-TDL terminal menu and scriptable commands."""

import argparse
import asyncio
from dataclasses import replace
import getpass
import json
import os
from pathlib import Path
import re
import signal
import sys

from playwright.async_api import Error as BrowserError

from .config import Settings, absolute_path, private_json
from . import __version__
from .common import StorageError
from .direct import download_url, url_identity
from .locks import locked
from .service import retry_pending, watch
from .storage import make_storage, LocalClient
from .telegram import TelegramBrowser, TelegramError, safe_filename
from .ui import banner, color, panel, dashboard


def setup(settings):
    panel("Choose where files go", ["Press Enter to keep the value in brackets.",
          "Drive creates a private TDL folder after Google sign-in.",
          "Local saves files to a folder on this machine."])
    destination = input(f"Save to [drive/local] [{settings.destination}]: ").strip().lower() or settings.destination
    if destination not in {"drive", "local"}:
        raise ValueError("Choose local or drive.")
    group = input(f"Telegram group [{settings.group_name}]: ").strip() or settings.group_name
    config = {"TDL_DESTINATION": destination, "TDL_GROUP_NAME": group,
              "TDL_POLL_SECONDS": settings.poll_seconds,
              "TDL_MAX_DOWNLOAD_BYTES": settings.max_download_bytes,
              "TDL_OUTPUT_DIR": str(settings.local_root),
              "GOOGLE_DRIVE_FOLDER_ID": settings.drive_folder_id or ""}
    if destination == "local":
        output = input(f"Local folder [{settings.local_root}]: ").strip() or str(settings.local_root)
        output = absolute_path(output, "Output directory")
        LocalClient(replace(settings, output_dir=output)).choose_folder()
        config["TDL_OUTPUT_DIR"] = str(output)
    else:
        folder = input("Google Drive folder ID [automatic TDL folder]: ").strip()
        if folder and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", folder):
            raise ValueError("Invalid Google Drive folder ID.")
        config["GOOGLE_DRIVE_FOLDER_ID"] = folder
    if not group or len(group) > 100 or any(ord(c) < 32 for c in group):
        raise ValueError("Enter a printable group name of 1-100 characters.")
    private_json(settings.data_dir / "config.json", json.dumps(config, indent=2))
    (settings.data_dir / "downloads").mkdir(exist_ok=True, mode=0o700)
    print(color("Configuration saved.", "92"))
    print("Local default: " + config["TDL_OUTPUT_DIR"])
    print("Connect Telegram: papa-tdl login")
    if destination == "drive":
        print("Connect Google Drive: papa-tdl drive-login (Google browser sign-in)")
    print("Then run: papa-tdl check && papa-tdl start")


async def telegram_login(settings):
    browser = TelegramBrowser(settings)
    try:
        await browser.start()
        await browser.login()
    finally:
        await browser.stop()


async def list_media(settings):
    browser = TelegramBrowser(settings)
    try:
        await browser.start()
        peer = await browser.find_group()
        specs = await browser.visible_media(peer)
        print(f"Visible media in {settings.group_name} ({peer}):")
        for spec in specs:
            print(f"  {spec['mid']:>10}  {safe_filename(spec.get('label', 'media'), 'media')}")
        print("Select IDs: papa-tdl telegram --ids ID1,ID2 --once")
        print("Only files currently rendered in Telegram Web are listed.")
    finally:
        await browser.stop()


def ask_yes(prompt):
    return input(prompt + " [Y/n]: ").strip().lower() not in {"n", "no"}


def first_run(settings):
    banner()
    panel("First run", ["Choose a save location and the Telegram group to watch.",
          "Then sign in. Your sessions stay in this machine's private data folder."])
    setup(settings)
    settings = Settings.load()
    if settings.destination == "drive" and ask_yes("Open Google sign-in in your browser now?"):
        try:
            from .hosted_login import login_hosted
            from .drive import _save_and_check_drive
            login_hosted(settings, _save_and_check_drive)
        except (StorageError, ValueError, OSError) as exc:
            print("Google Drive sign-in did not finish: " + str(exc))
            print("You can retry with menu option 6 or papa-tdl drive-login.")
    if ask_yes("Connect Telegram now (phone + code)?"):
        try:
            run_async(telegram_login(settings))
        except (TelegramError, OSError) as exc:
            print("Telegram sign-in did not finish. Retry later with papa-tdl login.")
    panel("Ready for the next step", ["1. Check the destination: papa-tdl check",
          "2. Start watching: papa-tdl start",
          "3. See transfers: papa-tdl logs -f"])


def menu(settings):
    banner()
    dashboard(settings)
    panel("Control center", [
        "FILES",
        " 1  Start watching in background",
        " 2  Choose visible Telegram messages",
        " 3  Watch in this terminal",
        " 4  Save a file from an HTTP(S) URL",
        "",
        "EXTRACTION",
        "    In TDL: /extract → send archive parts → /done",
        "    If asked: /password YOUR_PASSWORD",
        "",
        "ACCOUNTS & STORAGE",
        " 5  Sign in to Telegram (phone + code)",
        " 6  Connect Google Drive (Google browser sign-in)",
        " 7  Change destination or folder",
        " 8  Check storage connection",
        "",
        "ACTIVITY",
        " 9  Watcher status",
        "10  View recent transfers",
        "11  Stop background watcher",
        " 0  Exit"])
    choice = input(color("  Choose [0-11]: ", "1;95")).strip()
    if choice == "0":
        return None
    commands = {"1": "start", "2": "select", "3": "watch", "4": "download", "5": "login", "6": "drive-login",
                "7": "setup", "8": "check", "9": "status", "10": "logs", "11": "stop"}
    if choice not in commands:
        raise ValueError("Choose a menu number from 0 to 11.")
    command, argv = commands[choice], [commands[choice]]
    return argv


def parser():
    result = argparse.ArgumentParser(prog="papa-tdl", description="Save Telegram files on your VPS or Google Drive.")
    result.add_argument("--version", action="version", version=f"PaPa-TDL {__version__}")
    sub = result.add_subparsers(dest="command")
    for command in ("setup", "login", "drive-login", "download", "telegram", "watch", "list", "select",
                    "check", "retry", "start", "stop", "status", "logs"):
        item = sub.add_parser(command)
        if command in {"download", "telegram", "watch", "list", "select", "check", "retry", "start"}:
            item.add_argument("--to", choices=["local", "drive"], help="Override the configured destination")
            item.add_argument("--output", type=Path, help="Absolute local output directory (implies --to local)")
        if command == "download":
            item.add_argument("url", nargs="?", help="HTTP(S) direct URL; omit for a hidden prompt")
        if command in {"telegram", "watch", "list", "select", "start"}:
            item.add_argument("--group", help="Telegram group name")
        if command in {"telegram", "watch"}:
            item.add_argument("--ids", help="Comma-separated Telegram message IDs")
            item.add_argument("--once", action="store_true", help="Process visible matching media and exit")
        if command == "drive-login":
            item.add_argument("--method", choices=["hosted", "browser", "device"], default="hosted",
                              help="hosted: sign in from any browser (default); browser: local callback; device: personal TV client")
            item.add_argument("--client-file", type=Path, help="Optional personal OAuth JSON override; browser otherwise uses saved or bundled app identity")
            item.add_argument("--port", type=int, default=53682)
        if command == "logs":
            item.add_argument("-f", "--follow", action="store_true")
    return result


def run_async(coroutine):
    async def runner():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        can_handle_signal = False
        try:
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
            can_handle_signal = True
        except (NotImplementedError, RuntimeError):
            pass  # Windows event loops do not implement add_signal_handler.
        try:
            return await coroutine
        finally:
            if can_handle_signal:
                loop.remove_signal_handler(signal.SIGTERM)
    return asyncio.run(runner())


def run(args, settings):
    if args.command == "setup":
        setup(settings)
        return
    if args.command == "login":
        if not sys.stdin.isatty():
            raise ValueError("Run papa-tdl login in an interactive terminal. The code and password prompts need a TTY.")
        run_async(telegram_login(settings))
        return
    if args.command == "drive-login":
        from .drive import login_drive, login_drive_device
        if args.method == "hosted":
            from .hosted_login import login_hosted
            from .drive import _save_and_check_drive
            login_hosted(settings, _save_and_check_drive)
        elif args.method == "browser":
            client_file = args.client_file.expanduser() if args.client_file else None
            login_drive(settings, client_file, args.port)
        else:
            client_file = args.client_file or settings.data_dir / "google-device.json"
            login_drive_device(settings, client_file.expanduser() if client_file.is_file() or args.client_file else None)
        return
    if args.command == "list":
        run_async(list_media(settings))
        return
    if args.command in {"watch", "telegram", "select"}:
        if args.command == "select":
            run_async(list_media(settings))
            ids = input("Message IDs, comma-separated (blank cancels): ").strip()
            if not ids:
                return
            once = True
        else:
            ids, once = args.ids, args.once
        selected = {mid.strip() for mid in ids.split(",")} if ids else None
        if selected and not all(re.fullmatch(r"[1-9][0-9]{0,19}", mid) for mid in selected):
            raise ValueError("Message IDs must be comma-separated positive integers.")
        run_async(watch(settings, selected_ids=selected, once=once))
        return
    storage = make_storage(settings)
    try:
        folder_id = storage.choose_folder(settings.target_folder)
        if args.command == "check":
            print(color(f"Ready: {settings.destination} | folder {folder_id}", "92"))
        elif args.command == "retry":
            run_async(retry_pending(settings, storage, folder_id))
        elif args.command == "download":
            if not args.url and not sys.stdin.isatty():
                raise ValueError("The hidden URL prompt needs an interactive terminal.")
            url = args.url or getpass.getpass("Direct URL (hidden): ").strip()
            identity = url_identity(url)
            existing = storage.find_message(folder_id, "url", identity)
            if existing:
                print(f"Already saved: {safe_filename(existing['name'], 'file')}")
                return
            download_url(settings, url)
            run_async(retry_pending(settings, storage, folder_id, only_peer="url"))
    finally:
        storage.close()


def command_settings(args):
    if args.command == "setup":
        try:
            settings = Settings.load()
        except ValueError:
            # Repair an obsolete provider choice without retaining its key.
            previous = os.environ.get("TDL_DESTINATION")
            os.environ["TDL_DESTINATION"] = "drive"
            try:
                settings = Settings.load()
            finally:
                if previous is None:
                    os.environ.pop("TDL_DESTINATION", None)
                else:
                    os.environ["TDL_DESTINATION"] = previous
    else:
        settings = Settings.load()
    if getattr(args, "to", None):
        settings = replace(settings, destination=args.to)
    if getattr(args, "output", None):
        if args.to == "drive":
            raise ValueError("--output is for local storage; omit --to drive.")
        settings = replace(settings, output_dir=absolute_path(args.output, "--output"), destination="local")
    if group := getattr(args, "group", None):
        if len(group) > 100 or not group.strip() or any(ord(c) < 32 for c in group):
            raise ValueError("Enter a printable group name of 1-100 characters.")
        settings = replace(settings, group_name=group.strip())
    settings.prepare_storage()
    return settings


def execute(args):
    settings = command_settings(args)
    if args.command in {"start", "stop", "status", "logs"}:
        from . import background
        if args.command == "logs":
            background.logs(settings, args.follow)
        else:
            getattr(background, args.command)(settings)
        return
    try:
        with locked(settings.data_dir / "app.lock"):
            run(args, settings)
    except BlockingIOError:
        raise ValueError("PaPa-TDL is using this profile. Run papa-tdl stop before login or setup.") from None


def show_error(exc):
    if isinstance(exc, (ValueError, TelegramError, StorageError)):
        message = str(exc)
    elif isinstance(exc, BrowserError):
        message = "Telegram Web or Chromium is unavailable. Check the network and run: python -m playwright install chromium"
    elif isinstance(exc, OSError):
        message = "Local path, disk space or port unavailable. Check directory permissions and free space."
    else:
        message = type(exc).__name__ + "; check connectivity and configuration."
    print(color("ERROR: " + message, "91", stream=sys.stderr), file=sys.stderr)


def main():
    os.umask(0o077)
    argument_parser = parser()
    argv = sys.argv[1:]
    if argv:
        try:
            execute(argument_parser.parse_args(argv))
            return 0
        except asyncio.CancelledError:
            return 0
        except (KeyboardInterrupt, EOFError):
            return 130
        except Exception as exc:
            show_error(exc)
            return 1
    if not sys.stdin.isatty():
        argument_parser.print_help()
        return 0
    try:
        while True:
            try:
                settings = Settings.load()
                settings.prepare_storage()
                if not (settings.data_dir / "config.json").exists():
                    try:
                        with locked(settings.data_dir / "app.lock"):
                            first_run(settings)
                    except BlockingIOError:
                        raise ValueError("PaPa-TDL is using this profile; stop it before setup.") from None
                choice = menu(Settings.load())
                if choice is None:
                    return 0
                try:
                    execute(argument_parser.parse_args(choice))
                except (KeyboardInterrupt, asyncio.CancelledError):
                    print("\nCommand interrupted. Your saved settings are retained.")
            except Exception as exc:
                show_error(exc)
            input("\nPress Enter to return to the control center...")
    except (KeyboardInterrupt, EOFError):
        print("\nGoodbye.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
