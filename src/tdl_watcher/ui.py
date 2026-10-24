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


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _panel_lines(title, lines, width, accent="35"):
    utf = unicode_terminal()
    tl, tr, bl, br, side, bar = ("╭", "╮", "╰", "╯", "│", "─") if utf else ("+", "+", "+", "+", "|", "-")
    result = [color(tl + bar * (width - 2) + tr, accent)]
    for line in [color(title.upper(), "1;96"), "", *map(str, lines)]:
        plain = ANSI_RE.sub("", line)
        style = re.search(r"\x1b\[([0-9;]*)m", line)
        wrapped = textwrap.wrap(plain, width=max(1, width - 6),
                                break_long_words=True, break_on_hyphens=False) or [""]
        for item in wrapped:
            padded = item.ljust(width - 6)
            if style:
                padded = color(padded, style.group(1))
            result.append(color(side, accent) + "  " + padded + "  " + color(side, accent))
    result.append(color(bl + bar * (width - 2) + br, accent))
    return result


def panel(title, lines, width=72):
    width = max(16, min(width, shutil.get_terminal_size((80, 24)).columns - 2))
    print("\n".join(_panel_lines(title, lines, width)))


def panel_row(cards):
    """Adapt horizontal cards to terminal width without ANSI alignment errors."""
    columns = max(20, min(160, shutil.get_terminal_size((80, 24)).columns - 2))
    count = min(len(cards), 3 if columns >= 120 else 2 if columns >= 76 else 1)
    width = (columns - 2 * (count - 1)) // count
    for offset in range(0, len(cards), count):
        group = cards[offset:offset + count]
        rendered = [_panel_lines(title, lines, width, accent) for title, lines, accent in group]
        height = max(map(len, rendered))
        for box, (_, _, accent) in zip(rendered, group):
            empty = color("│" if unicode_terminal() else "|", accent) + " " * (width - 2) + color("│" if unicode_terminal() else "|", accent)
            box[-1:-1] = [empty] * (height - len(box))
        for row in zip(*rendered):
            print("  ".join(row))


def dashboard(settings):
    from .background import _running
    drive_ready = (settings.data_dir / "google-token.json").is_file()
    profile = settings.data_dir / "browser-profile"
    telegram_ready = profile.exists() and any(profile.iterdir())
    running = _running(settings)
    disk = shutil.disk_usage(settings.data_dir)
    panel_row([
        ("01 / Accounts", ["Telegram  " + status(telegram_ready, "session saved", "sign-in required"),
            "Google    " + status(drive_ready, "authorized", "sign-in required"),
            "Group     " + settings.group_name], "95"),
        ("02 / Destination", [color("GOOGLE DRIVE" if settings.destination == "drive" else "LOCAL SERVER", "1;96"),
            "Folder: " + (settings.drive_folder_id or "TDL") if settings.destination == "drive" else str(settings.local_root),
            "Private files by default"], "36"),
        ("03 / Service", [status(bool(running), "running", "stopped"),
            "Disk free: " + ProgressBar._size(disk.free),
            "Archive passwords: live console"], "35"),
    ])
