"""Safe, recoverable extraction of user-defined Telegram archive batches."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath

from .common import StorageError
from .telegram import safe_filename
from .ui import ProgressBar


class PasswordRequired(StorageError):
    """The batch contains an encrypted archive and needs a password."""


class InvalidArchivePassword(StorageError):
    """The supplied archive password was rejected."""


PART_RE = re.compile(r"(?i)(?:\.part0*([0-9]+)\.rar|\.([rz])([0-9]{2})|\.(zip|7z)\.0*([0-9]+))$")
ARCHIVE_RE = re.compile(r"(?i)(?:\.zip|\.rar|\.7z|\.tar|\.tar\.(?:gz|bz2|xz)|\.tgz|\.tbz2|\.txz)$")


def _safe_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(name)
    return (bool(name) and not posix.is_absolute() and not windows.is_absolute()
            and ".." not in posix.parts and not windows.drive
            and "\x00" not in name)


def _is_first_volume(path: Path) -> bool:
    name = path.name
    match = PART_RE.search(name)
    if match:
        if match.group(1):
            return int(match.group(1)) == 1
        if match.group(2):
            return False
        return int(match.group(5)) == 1
    return bool(ARCHIVE_RE.search(name))


def _archive_stem(name: str) -> str:
    value = re.sub(r"(?i)\.part0*1\.rar$|\.(?:zip|7z)\.0*1$", "", name)
    value = re.sub(r"(?i)\.(?:zip|rar|7z|tar|tgz|tbz2|txz)(?:\.(?:gz|bz2|xz))?$", "", value)
    return safe_filename(value, "extracted")


def batch_folder_name(files: list[Path], fallback: str) -> str:
    first = next((path for path in files if _is_first_volume(path)), files[0] if files else None)
    return _archive_stem(first.name) if first else safe_filename(fallback, "extracted")


def _validate_tree(root: Path) -> list[Path]:
    files = []
    resolved_root = root.resolve()
    for item in root.rglob("*"):
        if item.is_symlink():
            raise StorageError("Archive contains a symbolic link; extraction was stopped.")
        try:
            item.resolve().relative_to(resolved_root)
        except ValueError:
            raise StorageError("Archive contains an unsafe path; extraction was stopped.") from None
        if item.is_file():
            files.append(item)
    if not files:
        raise StorageError("The archive produced no files.")
    return files


def _merge_tree(source: Path, destination: Path) -> None:
    for directory in sorted((p for p in source.rglob("*") if p.is_dir()), key=lambda p: len(p.parts)):
        (destination / directory.relative_to(source)).mkdir(parents=True, exist_ok=True, mode=0o700)
    for item in (p for p in source.rglob("*") if p.is_file()):
        target = destination / item.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            raise StorageError(f"Two archives contain the same output path: {target.relative_to(destination)}")
        os.replace(item, target)


def _extract_zip(archive: Path, output: Path, password: str | None, limit: int) -> None:
    try:
        with zipfile.ZipFile(archive) as handle:
            members = handle.infolist()
            supported = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED,
                         zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}
            if any(member.compress_type not in supported for member in members):
                _extract_7z(archive, output, password, limit)
                return
            expanded = sum(member.file_size for member in members)
            if expanded > limit:
                raise StorageError("Extracted data would exceed the available storage limit.")
            for member in members:
                member_path = Path(member.filename)
                if not _safe_member(member.filename):
                    raise StorageError("Archive contains an unsafe path; extraction was stopped.")
                if member.is_dir():
                    (output / member_path).mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                target = output / member_path
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    with handle.open(member, pwd=password.encode() if password else None) as source, target.open("xb") as dest:
                        shutil.copyfileobj(source, dest, length=1024 * 1024)
                except RuntimeError as exc:
                    message = str(exc).lower()
                    if "password" in message or "encrypted" in message:
                        if password:
                            raise InvalidArchivePassword("The archive password was rejected.") from None
                        raise PasswordRequired("This extraction batch requires a password.") from None
                    raise StorageError("ZIP extraction failed; downloaded parts were retained.") from None
    except zipfile.BadZipFile:
        raise StorageError("The ZIP archive is incomplete or damaged; downloaded parts were retained.") from None


def _seven_zip() -> str:
    executable = next((shutil.which(name) for name in ("7zz", "7z", "7za") if shutil.which(name)), None)
    if not executable:
        raise StorageError("RAR/7z extraction requires 7-Zip. Update PaPa-TDL or install p7zip-full.")
    return executable


def _run_7z(arguments: list[str], password: str | None) -> subprocess.CompletedProcess:
    command = [_seven_zip(), *arguments]
    # Encrypted archives make 7-Zip prompt on stdin. The secret therefore never
    # appears in argv, process listings, state files, or captured output.
    return subprocess.run(command, input=(password or "") + "\n", text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=24 * 60 * 60,
                          env={**os.environ, "LANG": "C", "LC_ALL": "C"})


def _seven_zip_error(result: subprocess.CompletedProcess, password: str | None) -> None:
    output = result.stdout.lower()
    if result.returncode == 0:
        return
    if any(value in output for value in ("wrong password", "password is incorrect", "enter password", "encrypted")):
        if password:
            raise InvalidArchivePassword("The archive password was rejected.")
        raise PasswordRequired("This extraction batch requires a password.")
    raise StorageError("Archive extraction failed; verify that every volume is present and undamaged.")


def _run_unrar(arguments: list[str], password: str | None) -> subprocess.CompletedProcess:
    return subprocess.run(["unrar", *arguments], input=(password or "") + "\n", text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=24 * 60 * 60,
                          env={**os.environ, "LANG": "C", "LC_ALL": "C"})


def _unrar_error(result: subprocess.CompletedProcess, password: str | None) -> None:
    if result.returncode == 0:
        return
    output = result.stdout.lower()
    if "password" in output or "encrypted" in output:
        if password:
            raise InvalidArchivePassword("The archive password was rejected.")
        raise PasswordRequired("This extraction batch requires a password.")
    raise StorageError("RAR extraction failed; verify that every volume is present and undamaged.")


def _extract_rar(archive: Path, output: Path, password: str | None, limit: int) -> None:
    listing = _run_unrar(["lt", "-c-", str(archive)], password)
    _unrar_error(listing, password)
    members = re.findall(r"(?m)^\s*Name:\s*(.+)$", listing.stdout)
    sizes = [int(value) for value in re.findall(r"(?m)^\s*Size:\s*([0-9]+)$", listing.stdout)]
    if not members or any(not _safe_member(name) for name in members):
        raise StorageError("RAR contains an unsafe or empty path; extraction was stopped.")
    if sum(sizes) > limit:
        raise StorageError("Extracted data would exceed the available storage limit.")
    result = _run_unrar(["x", "-y", "-idq", "-c-", str(archive), str(output) + os.sep], password)
    _unrar_error(result, password)


def _extract_7z(archive: Path, output: Path, password: str | None, limit: int) -> None:
    listing = _run_7z(["l", "-slt", "--", str(archive)], password)
    _seven_zip_error(listing, password)
    sizes = [int(value) for value in re.findall(r"(?m)^Size = ([0-9]+)$", listing.stdout)]
    members = re.findall(r"(?m)^Path = (.+)$", listing.stdout.split("----------", 1)[-1])
    if not members or any(not _safe_member(name) for name in members):
        raise StorageError("Archive contains an unsafe or empty path; extraction was stopped.")
    if sum(sizes) > limit:
        raise StorageError("Extracted data would exceed the available storage limit.")
    result = _run_7z(["x", "-y", f"-o{output}", "--", str(archive)], password)
    _seven_zip_error(result, password)


def extract_archives(source: Path, destination: Path, password: str | None = None,
                     *, max_bytes: int = 0) -> list[Path]:
    """Extract all first archive volumes into one destination, retaining source on failure."""
    files = sorted(path for path in source.iterdir() if path.is_file())
    archives = [path for path in files if _is_first_volume(path)]
    if not archives:
        raise StorageError("No supported ZIP, RAR, 7z or TAR archive was found in the batch.")
    free_limit = max(0, shutil.disk_usage(source).free - 256 * 1024**2)
    configured_limit = max_bytes * 10 if max_bytes else free_limit
    limit = min(free_limit, configured_limit)
    if limit <= 0:
        raise StorageError("Not enough free disk space to extract this batch.")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    progress = ProgressBar("Extracting batch", len(archives))
    try:
        for index, archive in enumerate(archives, 1):
            with tempfile.TemporaryDirectory(prefix=".papa-extract-", dir=source.parent) as temporary:
                output = Path(temporary)
                ordinary_zip = archive.suffix.lower() == ".zip" and not re.search(r"(?i)\.zip\.0*1$", archive.name)
                if ordinary_zip:
                    _extract_zip(archive, output, password, limit)
                elif archive.name.lower().endswith(".rar") and shutil.which("unrar"):
                    _extract_rar(archive, output, password, limit)
                else:
                    _extract_7z(archive, output, password, limit)
                _validate_tree(output)
                _merge_tree(output, destination)
            progress.update(index, detail=archive.name)
        result = _validate_tree(destination)
        progress.finish(f"{len(result)} files ready")
        return result
    except BaseException:
        progress.finish("failed; source files retained", success=False)
        shutil.rmtree(destination, ignore_errors=True)
        raise
