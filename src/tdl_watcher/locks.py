"""Process locks shared by the menu, watcher, and native installer."""

from contextlib import contextmanager
from pathlib import Path

import portalocker


@contextmanager
def locked(path: Path, *, blocking: bool = False):
    with path.open("a+b") as stream:
        flags = portalocker.LOCK_EX | (0 if blocking else portalocker.LOCK_NB)
        try:
            portalocker.lock(stream, flags)
        except portalocker.LockException:
            raise BlockingIOError("PaPa-TDL is already using this profile.") from None
        try:
            yield
        finally:
            portalocker.unlock(stream)
