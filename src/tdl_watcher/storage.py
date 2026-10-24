"""Storage selection and atomic local saves without overwriting files."""

import errno
import os
import shutil
import tempfile
import json
from pathlib import Path

from .common import StorageError, remote_name, source_prefix
from .telegram import safe_filename


class LocalClient:
    def __init__(self, settings):
        self.root = settings.local_root.resolve()

    def close(self):
        pass

    def choose_folder(self, requested_id=None):
        if self.root == Path(self.root.anchor):
            raise StorageError("Choose a dedicated output folder, not the filesystem root.")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Do not change existing directory permissions chosen by the owner.
        with tempfile.TemporaryFile(dir=self.root):
            pass
        return str(self.root)

    def find_message(self, folder_id, peer, mid):
        if Path(folder_id).resolve() != self.root:
            raise StorageError("The selected local destination changed.")
        prefix = source_prefix(peer, mid)
        matches = [p for p in self.root.iterdir() if p.name.startswith(prefix)]
        if len(matches) > 1:
            raise StorageError("Multiple local files match this source.")
        if not matches:
            return None
        path = matches[0]
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            raise StorageError("Existing local file is empty, unsafe or unavailable.")
        return {"id": str(path), "name": path.name, "size": path.stat().st_size}

    def upload(self, path, folder_id, peer, mid, **kwargs):
        existing = self.find_message(folder_id, peer, mid)
        if existing:
            if int(existing["size"]) != path.stat().st_size:
                raise StorageError("Existing local file has a different size.")
            return existing
        destination = self.root / remote_name(peer, mid, path.name)
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            raise StorageError("The pending local file is unsafe or empty.")
        path.chmod(0o600)
        try:
            # Same filesystem: atomic publication with no second large copy.
            os.link(path, destination)
        except OSError as exc:
            if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP}:
                raise
            if shutil.disk_usage(self.root).free < path.stat().st_size + 64 * 1024**2:
                raise StorageError("Not enough space in the output filesystem; pending file retained.") from None
            descriptor, temporary = tempfile.mkstemp(prefix=".papa-tdl-", dir=self.root)
            try:
                with os.fdopen(descriptor, "wb") as output, path.open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                if Path(temporary).stat().st_size != path.stat().st_size:
                    raise StorageError("Local copy size verification failed; pending file retained.")
                # link publishes without overwriting an existing destination.
                os.link(temporary, destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
        item = self.find_message(folder_id, peer, mid)
        if item is None or int(item["size"]) != path.stat().st_size:
            raise StorageError("Local save verification failed; pending file retained.")
        return item

    def find_batch(self, folder_id, batch_id):
        if Path(folder_id).resolve() != self.root:
            raise StorageError("The selected local destination changed.")
        matches = []
        for path in self.root.iterdir() if self.root.exists() else ():
            marker = path / ".papa-tdl-batch.json"
            if not path.is_symlink() and path.is_dir() and marker.is_file():
                try:
                    if json.loads(marker.read_text()).get("batch_id") == batch_id:
                        matches.append(path)
                except (OSError, ValueError, AttributeError):
                    continue
        if len(matches) > 1:
            raise StorageError("Multiple local folders match this extraction batch.")
        if not matches:
            return None
        total = sum(item.stat().st_size for item in matches[0].rglob("*") if item.is_file())
        return {"id": str(matches[0]), "name": matches[0].name, "size": total,
                "webViewLink": None}

    def upload_tree(self, source, folder_id, batch_id, folder_name):
        existing = self.find_batch(folder_id, batch_id)
        if existing:
            return existing
        source = Path(source)
        files = [item for item in source.rglob("*") if item.is_file() and not item.is_symlink()]
        if not files or any(item.is_symlink() for item in source.rglob("*")):
            raise StorageError("Extracted folder is empty or contains an unsafe link.")
        name = safe_filename(folder_name, "extracted")
        destination = self.root / name
        if destination.exists():
            destination = self.root / f"{name} [{batch_id[-8:]}]"
        marker_value = json.dumps({"batch_id": batch_id}, separators=(",", ":"))
        if source.stat().st_dev == self.root.stat().st_dev:
            marker = source / ".papa-tdl-batch.json"
            marker.write_text(marker_value)
            marker.chmod(0o600)
            os.replace(source, destination)
            item = self.find_batch(folder_id, batch_id)
            if item is None:
                raise StorageError("Local extracted-folder verification failed; source retained.")
            return item
        temporary = Path(tempfile.mkdtemp(prefix=".papa-tdl-batch-", dir=self.root))
        try:
            payload = temporary / "payload"
            shutil.copytree(source, payload)
            marker = payload / ".papa-tdl-batch.json"
            marker.write_text(marker_value)
            marker.chmod(0o600)
            os.replace(payload, destination)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        item = self.find_batch(folder_id, batch_id)
        if item is None:
            raise StorageError("Local extracted-folder verification failed; source retained.")
        return item


def make_storage(settings):
    if settings.destination == "drive":
        from .drive import DriveClient
        return DriveClient(settings)
    return LocalClient(settings)
