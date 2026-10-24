"""A readable terminal interface with a compact ANSI Shadow wordmark."""

import os
import shutil
import sys
import textwrap
import time
import re

# ANSI Shadow from the FIGlet collection available on patorjk.com/software/taag/.
# Keep the rendered letters in source so the menu has no font package dependency.
WORDMARK = (
    "██████╗  █████╗ ██████╗  █████╗    ████████╗██████╗ ██╗",
    "██╔══██╗██╔══██╗██╔══██╗██╔══██╗   ╚══██╔══╝██╔══██╗██║",
    "██████╔╝███████║██████╔╝███████║█████╗██║   ██║  ██║██║",
    "██╔═══╝ ██╔══██║██╔═══╝ ██╔══██║╚════╝██║   ██║  ██║██║",
    "██║     ██║  ██║██║     ██║  ██║      ██║   ██████╔╝███████╗",
    "╚═╝     ╚═╝  ╚═╝╚═╝     ╚═╝  ╚═╝      ╚═╝   ╚═════╝ ╚══════╝",
)
COMPACT_WORDMARK = (
    "   ___       ___          _________  __",
    "  / _ \\___ _/ _ \\___ ____/_  __/ _ \\/ /",
    " / ___/ _ `/ ___/ _ `/___// / / // / /__",
    "/_/   \\_,_/_/   \\_,_/    /_/ /____/____/",
)


def color(value, code="95", *, stream=None):
    stream = stream or sys.stdout
    if stream.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb":
        return f"\033[{code}m{value}\033[0m"
    return value


def supports_color(stream=None):
    stream = stream or sys.stdout
    return stream.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


def status(ready, ready_text="connected", missing_text="not connected"):
    """A status label that remains understandable when color is unavailable."""
    mark = "✓" if ready and unicode_terminal() else ("+" if ready else "!")
    label = ready_text if ready else missing_text
    return color(f"{mark} {label}", "1;92" if ready else "1;93")


class ProgressBar:
    """One-line terminal progress; log-friendly when output is redirected."""

    def __init__(self, label, total=None, *, width=24, stream=None):
        self.label = label
        self.total = int(total or 0)
        self.width = width
        self.stream = stream or sys.stdout
        self.tty = self.stream.isatty()
        self.last_text = ""
        self.last_log = 0.0
        self.closed = False

    @staticmethod
    def _size(value):
        value = float(value)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if value < 1024 or unit == "TiB":
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024

    def update(self, current, *, total=None, detail=""):
        if self.closed:
            return
        if total is not None:
            self.total = int(total or 0)
        current = max(0, int(current or 0))
        if self.total:
            ratio = min(1.0, current / self.total)
            filled = round(self.width * ratio)
            bar = "█" * filled + "░" * (self.width - filled) if unicode_terminal() else "#" * filled + "-" * (self.width - filled)
            text = f"{self.label:<18} [{bar}] {ratio:>6.1%}  {self._size(current)} / {self._size(self.total)}"
        else:
            pulse = "∙●∙" if unicode_terminal() else "..."
            text = f"{self.label:<18} [{pulse}]  {self._size(current)}"
        if detail:
            text += "  " + detail
        if text == self.last_text:
            return
        now = time.monotonic()
        if self.tty:
            padding = max(0, len(self.last_text) - len(text))
            self.stream.write("\r" + color(text, "96", stream=self.stream) + " " * padding)
            self.stream.flush()
        elif now - self.last_log >= 30 or not self.last_text:
            print(text, file=self.stream, flush=True)
            self.last_log = now
        self.last_text = text

    def finish(self, text="complete", *, success=True):
        if self.closed:
            return
        if self.tty and self.last_text:
            self.stream.write("\r" + " " * len(self.last_text) + "\r")
        mark = "✓" if success and unicode_terminal() else ("+" if success else "!")
        print(color(f"{mark} {self.label}: {text}", "1;92" if success else "1;91", stream=self.stream),
              file=self.stream, flush=True)
        self.closed = True


def unicode_terminal():
    try:
        "█╭│╰".encode(sys.stdout.encoding or "utf-8")
        return os.environ.get("TERM") != "dumb"
    except UnicodeEncodeError:
        return False


def logo_lines():
    columns = shutil.get_terminal_size((80, 24)).columns
    if unicode_terminal() and columns >= 65:
        return [color(line.rstrip(), "1;95" if index < 3 else "35")
                for index, line in enumerate(WORDMARK)]
    if columns >= 42:
        return [color(line.rstrip(), "95") for line in COMPACT_WORDMARK]
    return [color("PaPa-TDL", "1;95")]


def banner():
    print()
    print("\n".join(logo_lines()))
    print(color("  TELEGRAM  /  DRIVE + LOCAL", "1;95"))
    print()


def panel(title, lines, width=72):
    width = max(28, min(width, shutil.get_terminal_size((80, 24)).columns - 2))
    utf = unicode_terminal()
    top, bottom, side, horizontal = ("╭", "╰", "│", "─") if utf else ("+", "+", "|", "-")
    right_top, right_bottom = ("╮", "╯") if utf else ("+", "+")
    print(color(top + horizontal * (width - 2) + right_top, "35"))
    content = [(title.upper(), "1;96"), ("", None)] + [
        (str(line), "1;96" if str(line) in {"FILES", "ACCOUNTS & STORAGE", "ACTIVITY", "EXTRACTION"} else None)
        for line in lines]
    for line, style in content:
        wrapped = textwrap.wrap(line, width=width - 6,
                                break_long_words=True, break_on_hyphens=False) or [""]
        for item in wrapped:
            visible_length = len(re.sub(r"\x1b\[[0-9;]*m", "", item))
            content_text = item + " " * max(0, width - 6 - visible_length)
            if style:
                content_text = color(content_text, style)
            print(color(side, "35") + "  " + content_text + "  " + color(side, "35"))
    print(color(bottom + horizontal * (width - 2) + right_bottom, "35"))


def dashboard(settings):
    drive_ready = (settings.data_dir / "google-token.json").is_file()
    profile = settings.data_dir / "browser-profile"
    telegram_ready = profile.exists() and any(profile.iterdir())
    destination = "Google Drive / " + (settings.drive_folder_id or "TDL") if settings.destination == "drive" else "Local / " + str(settings.local_root)
    panel("Current setup", ["Save to    " + destination,
          "Telegram   " + status(telegram_ready, "connected", "sign-in required"),
          "Google     " + status(drive_ready, "connected", "sign-in required"),
          "Group      " + settings.group_name,
          "Local path " + str(settings.local_root)])
