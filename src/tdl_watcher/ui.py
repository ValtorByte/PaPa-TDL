"""A readable terminal interface with a compact ANSI Shadow wordmark."""

import os
import shutil
import sys
import textwrap

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
    content = [title.upper(), ""] + list(lines)
    for line in content:
        wrapped = textwrap.wrap(str(line), width=width - 6,
                                break_long_words=True, break_on_hyphens=False) or [""]
        for item in wrapped:
            print(color(side, "35") + "  " + item.ljust(width - 6) + "  " + color(side, "35"))
    print(color(bottom + horizontal * (width - 2) + right_bottom, "35"))


def dashboard(settings):
    drive = "authorization saved" if (settings.data_dir / "google-token.json").is_file() else "sign-in needed"
    profile = settings.data_dir / "browser-profile"
    telegram = "session saved" if profile.exists() and any(profile.iterdir()) else "sign-in needed"
    destination = "Google Drive / " + (settings.drive_folder_id or "TDL") if settings.destination == "drive" else "Local / " + str(settings.local_root)
    panel("Current setup", ["Save to    " + destination,
          "Telegram   " + telegram,
          "Google     " + drive,
          "Group      " + settings.group_name,
          "Local path " + str(settings.local_root)])
