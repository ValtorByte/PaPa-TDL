"""Storage selection and atomic local saves without overwriting files."""

import errno
import os
import shutil
import tempfile
from pathlib import Path

from .common import StorageError, remote_name, source_prefix


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


def make_storage(settings):
    if settings.destination == "drive":
        from .drive import DriveClient
        return DriveClient(settings)
    return LocalClient(settings)
