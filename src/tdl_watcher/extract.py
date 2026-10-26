"""Safe, recoverable extraction of user-defined Telegram archive batches."""

from __future__ import annotations

import errno
import os
import re
import select
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath

from .common import StorageError
from .telegram import safe_filename
from .ui import ProgressBar

EXTRACT_ENGINE_VERSION = 7


class PasswordRequired(StorageError):
    """The batch contains an encrypted archive and needs a password."""


class InvalidArchivePassword(StorageError):
    """The supplied archive password was rejected."""


PART_RE = re.compile(r"(?i)(?:\.part0*([0-9]+)\.rar|\.([rz])([0-9]{2})|\.(zip|7z)\.0*([0-9]+))$")
SERIES_RE = re.compile(
    r"(?i)^(?P<base>.+?)[._ -]+part0*(?P<number>[1-9][0-9]*)"
    r"(?:\s*\([^)]*\))?\.(?:rar|zip|7z)$"
)
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
    series = SERIES_RE.fullmatch(name)
    if series:
        return safe_filename(series.group("base"), "extracted")
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
                          env={**os.environ, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})


def _seven_zip_error(result: subprocess.CompletedProcess, password: str | None) -> None:
    output = result.stdout.lower()
    if result.returncode == 0:
        return
    if password and any(value in output for value in ("wrong password", "password is incorrect")):
        raise InvalidArchivePassword("The archive password was rejected.")
    if any(value in output for value in ("missing volume", "unexpected end", "cannot find", "no such file")):
        raise StorageError("An archive volume is incomplete, missing, or truncated; it must be downloaded again.")
    if not password and any(value in output for value in ("enter password", "encrypted")):
        raise PasswordRequired("This extraction batch requires a password.")
    if "unsupported method" in output:
        raise StorageError("The installed 7-Zip cannot decode this archive; install a current RAR-capable 7-Zip or unrar.")
    raise StorageError(f"7-Zip failed (exit {result.returncode}); check archive integrity and installed codec support.")


def _run_unrar(arguments: list[str], password: str | None) -> subprocess.CompletedProcess:
    command = ["unrar", *arguments[:1], *([] if password else ["-p-"]), *arguments[1:]]
    # RAR archives may contain non-ASCII names. The C locale replaces those
    # characters with '?' and can make distinct files collide on extraction.
    env = {**os.environ, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    if not password:
        return subprocess.run(command, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=24 * 60 * 60, env=env)
    if os.name != "posix":
        raise StorageError("Secure RAR password entry requires a POSIX terminal or 7-Zip.")
    return _run_secret_pty(command, password, env=env, timeout=24 * 60 * 60)


def _run_secret_pty(command: list[str], password: str, *, env: dict[str, str],
                    timeout: float) -> subprocess.CompletedProcess:
    """Give the extractor a controlling terminal and answer its password prompt."""
    import pty
    import termios
    import sys

    master, slave = pty.openpty()
    attributes = termios.tcgetattr(slave)
    attributes[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, attributes)
    # unrar may open /dev/tty instead of reading stdin. A PTY alone is not a
    # controlling terminal. The helper creates a new session and attaches its
    # stdin before replacing itself with the extractor. The password is never
    # passed in arguments or the environment.
    helper = (
        "import fcntl,os,sys,termios;"
        "os.setsid();"
        "fcntl.ioctl(0,termios.TIOCSCTTY,0);"
        "os.execvpe(sys.argv[1],sys.argv[1:],os.environ)"
    )
    try:
        process = subprocess.Popen([sys.executable, "-c", helper, *command],
                                   stdin=slave, stdout=slave, stderr=slave,
                                   env=env, close_fds=True)
    except BaseException:
        os.close(master)
        os.close(slave)
        raise
    os.close(slave)
    output = bytearray()
    prompt_buffer = bytearray()
    sent = 0
    last_answer_at = 0
    rejected = False
    deadline = time.monotonic() + timeout
    # Native unrar includes the full archive path in this prompt, which may be
    # much longer than a normal terminal line.
    # Wait for the *end* of the prompt. unrar flushes pending terminal input
    # before reading it, so responding to an early partial write loses the
    # password and leaves the extractor waiting indefinitely.
    prompt = re.compile(
        rb"(?i)(?:enter\s+password|password\s+(?:required|needed))[^\r\n]*:\s*$"
    )
    try:
        while True:
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                raise StorageError("Archive extraction timed out; downloaded parts were retained.")
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b""
                if chunk:
                    output.extend(chunk)
                    prompt_buffer.extend(chunk)
                    if len(prompt_buffer) > 4096:
                        del prompt_buffer[:-4096]
                    if prompt.search(bytes(prompt_buffer)):
                        recent = bytes(output[last_answer_at:]).lower()
                        if sent and any(word in recent for word in
                                        (b"incorrect", b"wrong password", b"bad password")):
                            rejected = True
                            process.kill()
                        elif sent >= 64:
                            process.kill()
                        else:
                            # unrar can flush terminal input immediately after
                            # rendering the prompt. Give it time to enter its
                            # read call so our answer cannot be discarded.
                            time.sleep(0.2)
                            os.write(master, password.encode("utf-8") + b"\n")
                            sent += 1
                            last_answer_at = len(output)
                        prompt_buffer.clear()
            if process.poll() is not None:
                while True:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            break
                        raise
                    if not chunk:
                        break
                    output.extend(chunk)
                break
        result = output.decode("utf-8", errors="replace")
        # Do not trust a third-party extractor to suppress terminal echo.
        result = result.replace(password, "[redacted]")
        if rejected:
            result += "\nWrong password."
        return subprocess.CompletedProcess(command, process.returncode, result)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


def _unrar_error(result: subprocess.CompletedProcess, password: str | None) -> None:
    if result.returncode == 0:
        return
    output = result.stdout.lower()
    if "unexpected end" in output or "missing volume" in output or "cannot find volume" in output:
        raise StorageError("An archive volume is incomplete, missing, or truncated; it must be downloaded again.")
    if result.returncode == 11:
        if password:
            raise InvalidArchivePassword("The archive password was rejected.")
        raise PasswordRequired("This extraction batch requires a password.")
    if password and ("wrong password" in output or "password is incorrect" in output):
        raise InvalidArchivePassword("The archive password was rejected.")
    if not password and ("password" in output or "encrypted" in output):
        raise PasswordRequired("This extraction batch requires a password.")
    reasons = {3: "Archive checksum failed; a part is damaged or truncated.",
               5: "Archive output could not be written; check free disk space and permissions.",
               6: "An archive volume could not be opened; check all parts and their filenames.",
               7: "The installed unrar does not support the requested options.",
               8: "Not enough memory to extract the archive.",
               9: "An extracted file could not be created; check disk space and permissions.",
               10: "No archive files could be extracted; check archive metadata and tool compatibility."}
    raise StorageError(f"unrar failed (exit {result.returncode}): " + reasons.get(
        result.returncode, "Verify that every archive volume is present and undamaged."))


def _extract_rar(archive: Path, output: Path, password: str | None, limit: int) -> None:
    listing = _run_unrar(["lt", "-c-", str(archive)], password)
    _unrar_error(listing, password)
    if not password and re.search(r"(?im)^\s*Flags:\s*[^\r\n]*\bencrypted\b", listing.stdout):
        raise PasswordRequired("This RAR archive requires a password.")
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
    if not password and re.search(r"(?im)^Encrypted\s*=\s*\+", listing.stdout):
        raise PasswordRequired("This archive requires a password.")
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
    # Numbered "Part01 (01-22).rar" files are independent archives, unlike
    # RAR's ".part01.rar" volumes. Each may contain the same cover/photo
    # filename, so retain a separate PartNN directory within the batch.
    numbered_parts = (len(archives) > 1 and all(
        SERIES_RE.fullmatch(path.name) and not PART_RE.search(path.name)
        for path in archives
    ))
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
                target = destination
                if numbered_parts:
                    number = int(SERIES_RE.fullmatch(archive.name).group("number"))
                    target = destination / f"Part{number:02d}"
                    if target.exists():
                        raise StorageError(f"Two archives use the same part number: {number}.")
                _merge_tree(output, target)
            progress.update(index, detail=archive.name)
        result = _validate_tree(destination)
        progress.finish(f"{len(result)} files ready")
        return result
    except BaseException as exc:
        reason = str(exc) if isinstance(exc, StorageError) else type(exc).__name__
        progress.finish(f"failed: {reason}; source files retained", success=False)
        shutil.rmtree(destination, ignore_errors=True)
        raise
