"""Shared identities, safe storage errors and streaming helpers."""

import hashlib
import re
from pathlib import Path

CHUNK_SIZE = 8 * 1024 * 1024


class StorageError(RuntimeError):
    """A user-facing storage error without credentials or raw responses."""


def source_prefix(peer: str, mid: str) -> str:
    if peer == "url" and re.fullmatch(r"[0-9a-f]{64}", mid):
        return f"tdlurl-v1-{mid}__"
    if not re.fullmatch(r"-[1-9][0-9]{0,16}", peer):
        raise ValueError("Invalid Telegram chat identity.")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", mid):
        raise ValueError("Invalid Telegram message identity.")
    return f"tdlmsg-v1-{peer[1:]}_{mid}__"


def remote_name(peer: str, mid: str, original_name: str) -> str:
    # Keep the old identity format so existing saved files are still skipped.
    from .telegram import safe_filename
    prefix = source_prefix(peer, mid)
    original_name = safe_filename(original_name, "file.bin")
    suffix = Path(original_name).suffix
    suffix = suffix if len(suffix.encode("utf-8")) <= 20 else ""
    stem = original_name[:-len(suffix)] if suffix else original_name
    while len((prefix + stem + suffix).encode("utf-8")) > 220:
        stem = stem[:-1]
    return prefix + (stem or "file") + suffix


def md5_file(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()
