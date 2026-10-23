"""Install Desktop OAuth configuration privately on the runtime host."""
import argparse
import json
import os
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from tdl_watcher.common import StorageError
from tdl_watcher.oauth_config import desktop_config

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_file", type=Path)
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("PAPA_TDL_DATA_DIR", str(Path.home() / ".local/share/papa-tdl"))))
    args = parser.parse_args()
    target = args.data_dir.expanduser().resolve() / "google-client.json"
    if target.is_relative_to(ROOT):
        parser.error("Choose a private runtime directory outside the repository.")
    temporary = target.with_suffix(".tmp")
    try:
        config = desktop_config(json.loads(args.client_file.expanduser().read_text()))
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(config, output)
        temporary.replace(target)
    except (OSError, ValueError, StorageError):
        print("Could not save Desktop OAuth configuration. Check the file and private directory permissions.", file=sys.stderr)
        return 1
    finally:
        temporary.unlink(missing_ok=True)
    print("Desktop OAuth configuration saved privately. No credentials were added to the source code.")
    print("Run this as the app runtime user. VPS browser callbacks require SSH forwarding.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
