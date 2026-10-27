"""Checkpointed downloads through the signed-in Telegram Web stream worker.

This is an optional integration with Telegram Web K internals. If metadata or
the range endpoint is unavailable, the caller may use the ordinary browser
download. No Telegram API credentials or browser cookies are exported.
"""

import asyncio
import base64
import hashlib
import json
import os
import shutil
from pathlib import Path

BLOCK_SIZE = 1024 * 1024


class RangeUnavailable(RuntimeError):
    """The web client does not expose the range interface."""


class RangeFailure(RuntimeError):
    """A resumable download failed; its checkpoints are retained."""


async def document_metadata(page, doc_id):
    if not doc_id or not str(doc_id).isdigit():
        raise RangeUnavailable("Document identity is unavailable.")
    data = await page.evaluate("""async id => {
        const manager = window.rootScope?.managers?.appDocsManager;
        if (!manager || !navigator.serviceWorker?.controller) return null;
        const doc = await manager.getDoc(id);
        if (!doc || String(doc.id) !== id || !Number.isSafeInteger(doc.size) ||
            doc.size <= 0 || !Number.isInteger(doc.dc_id)) return null;
        return {id: String(doc.id), size: doc.size, dc: doc.dc_id,
                name: doc.file_name || ''};
    }""", str(doc_id))
    if not isinstance(data, dict):
        raise RangeUnavailable("Telegram Web does not expose document metadata.")
    if (data.get("id") != str(doc_id) or type(data.get("size")) is not int
            or data["size"] <= 0 or type(data.get("dc")) is not int):
        raise RangeUnavailable("Telegram Web returned invalid document metadata.")
    return data


async def fetch_block(page, meta, offset, length):
    response = await asyncio.wait_for(page.evaluate("""async ({id, size, dc, offset, length}) => {
        const info = {location: {id}, size, dcId: dc, mimeType: 'application/octet-stream'};
        const url = new URL('stream/' + encodeURIComponent(JSON.stringify(info)),
                            location.origin + '/k/');
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 90000);
        try {
            const end = offset + length - 1;
            const response = await fetch(url.href, {
                headers: {'Range': `bytes=${offset}-${end}`},
                credentials: 'same-origin', signal: controller.signal
            });
            const range = response.headers.get('content-range');
            if (response.status !== 206 || range !== `bytes ${offset}-${end}/${size}`)
                return {status: response.status, range};
            const bytes = new Uint8Array(await response.arrayBuffer());
            let text = '';
            for (let i = 0; i < bytes.length; i += 0x8000)
                text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
            return {status: response.status, range, bytes: btoa(text)};
        } finally { clearTimeout(timer); }
    }""", {"id": meta["id"], "size": meta["size"], "dc": meta["dc"],
           "offset": offset, "length": length}), timeout=100)
    expected = f"bytes {offset}-{offset + length - 1}/{meta['size']}"
    if not isinstance(response, dict) or response.get("status") != 206 or response.get("range") != expected:
        raise RangeFailure("Telegram Web did not return the requested byte range.")
    try:
        data = base64.b64decode(response["bytes"], validate=True)
    except (KeyError, ValueError, TypeError) as exc:
        raise RangeFailure("Telegram Web returned invalid range data.") from exc
    if len(data) != length:
        raise RangeFailure("Telegram Web returned an incomplete byte range.")
    return data


def _write_checkpoint(path, state):
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(state, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


async def download_ranges(page, meta, destination, checkpoint_dir, peer, mid, filename, progress, max_bytes=0):
    """Download validated blocks, resuming only checkpoints with matching hashes."""
    total = meta["size"]
    if max_bytes and total > max_bytes:
        raise RangeFailure("Telegram media exceeds TDL_MAX_DOWNLOAD_BYTES.")
    prefix = f".tdl-{peer[1:]}-{mid}"
    checkpoint_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    part = checkpoint_dir / (prefix + ".part")
    checkpoint = checkpoint_dir / (prefix + ".json")
    target = destination / filename
    identity = {"peer": peer, "mid": mid, "doc_id": meta["id"], "size": total,
                "filename": filename, "block_size": BLOCK_SIZE}
    state = {**identity, "hashes": []}
    # Recover if the process stopped between publishing the file and removing
    # its checkpoint. Verify every committed block before reusing the file.
    if checkpoint.is_file() and not part.exists() and target.is_file() and not target.is_symlink():
        try:
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            hashes = saved["hashes"]
            if (any(saved.get(key) != value for key, value in identity.items())
                    or not isinstance(hashes, list)
                    or len(hashes) != (total + BLOCK_SIZE - 1) // BLOCK_SIZE
                    or target.stat().st_size != total):
                raise ValueError("identity mismatch")
            with target.open("rb") as stream:
                for i, expected in enumerate(hashes):
                    size = min(BLOCK_SIZE, total - i * BLOCK_SIZE)
                    if hashlib.sha256(stream.read(size)).hexdigest() != expected:
                        raise ValueError("hash mismatch")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RangeFailure("The completed file could not be verified; it was retained for inspection.") from exc
        checkpoint.unlink()
        return target
    if checkpoint.exists() or part.exists():
        if not checkpoint.is_file() or not part.is_file() or checkpoint.is_symlink() or part.is_symlink():
            raise RangeFailure("Partial download state is incomplete or unsafe; inspect it before retrying.")
        try:
            state = json.loads(checkpoint.read_text(encoding="utf-8"))
            if any(state.get(key) != value for key, value in identity.items()):
                raise ValueError("identity changed")
            hashes = state["hashes"]
            if not isinstance(hashes, list) or len(hashes) > (total + BLOCK_SIZE - 1) // BLOCK_SIZE:
                raise ValueError("invalid checkpoint")
            with part.open("rb") as stream:
                for i, expected in enumerate(hashes):
                    size = min(BLOCK_SIZE, total - i * BLOCK_SIZE)
                    data = stream.read(size)
                    if len(data) != size or hashlib.sha256(data).hexdigest() != expected:
                        raise ValueError("checkpoint mismatch")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RangeFailure("Partial download checkpoint is damaged or refers to a different file; retained for inspection.") from exc
        offset = min(len(hashes) * BLOCK_SIZE, total)
        with part.open("r+b") as stream:
            stream.truncate(offset)  # Discard only bytes not recorded in the checkpoint.
    else:
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        _write_checkpoint(checkpoint, state)
        offset = 0

    try:
        if shutil.disk_usage(checkpoint_dir).free < total - offset + 512 * 1024**2:
            raise RangeFailure("Insufficient disk space for the remaining file; progress retained.")
        if offset:
            progress.update(offset, total=total, detail="resuming")
        with part.open("ab") as stream:
            while offset < total:
                length = min(BLOCK_SIZE, total - offset)
                try:
                    data = await fetch_block(page, meta, offset, length)
                except Exception as exc:
                    if offset == 0:
                        raise RangeUnavailable("Telegram Web range download is unavailable.") from exc
                    raise RangeFailure(f"Telegram Web range failed at {offset}/{total} bytes; progress retained.") from exc
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                state["hashes"].append(hashlib.sha256(data).hexdigest())
                _write_checkpoint(checkpoint, state)
                offset += length
                progress.update(offset, total=total)
    except RangeUnavailable:
        # Only an empty partial may be removed when using the ordinary fallback.
        part.unlink(missing_ok=True)
        checkpoint.unlink(missing_ok=True)
        raise

    if target.exists():
        raise RangeFailure("A file with this name already exists; the downloaded partial was retained.")
    os.replace(part, target)
    checkpoint.unlink(missing_ok=True)
    return target
