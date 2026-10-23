"""Private persistent configuration with explicit environment overrides."""

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


def default_data_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "PaPa-TDL"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/PaPa-TDL"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "papa-tdl"


def absolute_path(value, label):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path.")
    return path.resolve()


def private_json(path: Path, value: str) -> None:
    import tempfile
    descriptor, name = tempfile.mkstemp(prefix=".papa-tdl-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class Settings:
    group_name: str
    data_dir: Path
    poll_seconds: int = 10
    destination: str = "drive"
    drive_folder_id: str | None = None
    max_download_bytes: int = 0
    output_dir: Path | None = None

    @classmethod
    def load(cls) -> "Settings":
        default = default_data_dir()
        data_dir = absolute_path(os.environ.get("TDL_DATA_DIR", str(default)), "TDL_DATA_DIR")
        if data_dir == Path(data_dir.anchor) or data_dir == Path.home().resolve():
            raise ValueError("TDL_DATA_DIR must be a dedicated private directory.")
        try:
            saved = json.loads((data_dir / "config.json").read_text())
        except FileNotFoundError:
            saved = {}
        except (ValueError, OSError):
            raise ValueError("Cannot read config.json; fix it or restore its backup.") from None
        if not isinstance(saved, dict):
            raise ValueError("config.json must contain a JSON object.")
        def value(name, default=""):
            result = os.environ.get(name, saved.get(name, default))
            if not isinstance(result, (str, int)):
                raise ValueError(f"Invalid setting: {name}.")
            return str(result).strip()
        destination = value("TDL_DESTINATION", "drive").lower()
        if destination not in {"drive", "local"}:
            raise ValueError("TDL_DESTINATION must be local or drive. Run papa-tdl setup to migrate old settings.")
        group = value("TDL_GROUP_NAME", "TDL")
        if not group or len(group) > 100 or any(ord(c) < 32 for c in group):
            raise ValueError("TDL_GROUP_NAME must contain 1-100 printable characters.")
        poll = int(value("TDL_POLL_SECONDS", "10"))
        if not 3 <= poll <= 300:
            raise ValueError("TDL_POLL_SECONDS must be between 3 and 300.")
        limit = int(value("TDL_MAX_DOWNLOAD_BYTES", "0"))
        if limit < 0:
            raise ValueError("TDL_MAX_DOWNLOAD_BYTES must be nonnegative.")
        folder = value("GOOGLE_DRIVE_FOLDER_ID") or None
        if folder and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", folder):
            raise ValueError("Invalid GOOGLE_DRIVE_FOLDER_ID.")
        output = absolute_path(value("TDL_OUTPUT_DIR", str(data_dir / "downloads")), "TDL_OUTPUT_DIR")
        return cls(group, data_dir, poll, destination, folder, limit, output)

    @property
    def pending_dir(self) -> Path:
        return self.data_dir / "pending" / self.destination

    @property
    def local_root(self) -> Path:
        return self.output_dir or self.data_dir / "downloads"

    @property
    def target_folder(self) -> str | None:
        return self.drive_folder_id if self.destination == "drive" else str(self.local_root)

    def prepare_storage(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        for name in ("browser-profile", "browser-downloads", "pending"):
            path = self.data_dir / name
            path.mkdir(exist_ok=True, mode=0o700)
            path.chmod(0o700)
        self.pending_dir.mkdir(exist_ok=True, mode=0o700)
        # A usable local default exists even when Drive is the chosen destination.
        (self.data_dir / "downloads").mkdir(exist_ok=True, mode=0o700)
