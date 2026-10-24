import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from tdl_watcher.common import StorageError
from tdl_watcher.config import Settings
from tdl_watcher.extract import (InvalidArchivePassword, PasswordRequired,
                                 _safe_member, batch_folder_name, extract_archives)
from tdl_watcher.storage import LocalClient
from tdl_watcher.service import (_archive_group, _load_control, _process_extract_commands,
                                 _same_staged_media, _save_control, _batch_root)


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archives = self.root / "archives"
        self.archives.mkdir()

    def test_zip_batch_extracts_into_one_folder(self):
        with zipfile.ZipFile(self.archives / "course.zip", "w") as archive:
            archive.writestr("chapter/lesson.txt", "ready")
        output = self.root / "output"
        files = extract_archives(self.archives, output)
        self.assertEqual([path.relative_to(output) for path in files], [Path("chapter/lesson.txt")])
        self.assertEqual(files[0].read_text(), "ready")

    def test_zip_path_traversal_is_rejected_and_source_retained(self):
        archive_path = self.archives / "unsafe.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("../../outside.txt", "unsafe")
        with self.assertRaisesRegex(StorageError, "unsafe path"):
            extract_archives(self.archives, self.root / "output")
        self.assertTrue(archive_path.exists())
        self.assertFalse((self.root / "outside.txt").exists())

    def test_archive_paths_reject_windows_and_unix_traversal(self):
        self.assertTrue(_safe_member("folder/file.txt"))
        for name in ("../../outside.txt", "..\\outside.txt", "/etc/passwd", "C:\\outside.txt"):
            self.assertFalse(_safe_member(name), name)

    def test_multipart_name_uses_first_volume(self):
        paths = [self.archives / "SOC TIER 1.part2.rar", self.archives / "SOC TIER 1.part1.rar"]
        for path in paths:
            path.write_bytes(b"part")
        self.assertEqual(batch_folder_name(paths, "fallback"), "SOC TIER 1")

    def test_password_errors_are_specific_without_exposing_password(self):
        archive = self.archives / "private.rar"
        archive.write_bytes(b"part")
        failed = __import__("subprocess").CompletedProcess([], 2, "ERROR: Wrong password")
        with patch("tdl_watcher.extract._run_7z", return_value=failed), \
             patch("tdl_watcher.extract._run_unrar", return_value=failed):
            with self.assertRaises(PasswordRequired):
                extract_archives(self.archives, self.root / "one")
            with self.assertRaises(InvalidArchivePassword) as raised:
                extract_archives(self.archives, self.root / "two", "top-secret")
        self.assertNotIn("top-secret", str(raised.exception))

    def test_local_tree_save_is_idempotent(self):
        output = self.root / "saved"
        settings = Settings("TDL", self.root / "private", destination="local", output_dir=output)
        settings.prepare_storage()
        source = self.root / "extracted"
        (source / "nested").mkdir(parents=True)
        (source / "nested/file.txt").write_text("payload")
        client = LocalClient(settings)
        folder = client.choose_folder()
        first = client.upload_tree(source, folder, "tg-123-10", "Course")
        second = client.upload_tree(source, folder, "tg-123-10", "Course")
        self.assertEqual(first["id"], second["id"])
        marker = Path(first["id"]) / ".papa-tdl-batch.json"
        self.assertEqual(json.loads(marker.read_text())["batch_id"], "tg-123-10")
        self.assertEqual((Path(first["id"]) / "nested/file.txt").read_text(), "payload")

    def test_telegram_commands_form_one_recoverable_batch(self):
        settings = Settings("TDL", self.root / "private", poll_seconds=3,
                            destination="local", output_dir=self.root / "saved")
        settings.prepare_storage()
        storage = LocalClient(settings)
        folder = storage.choose_folder()
        browser = type("Browser", (), {})()
        browser.send_text = AsyncMock()

        async def download(spec, destination):
            path = destination / "lessons.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("week/one.txt", "lesson")
            return path

        browser.download = AsyncMock(side_effect=download)
        events = [
            {"type": "command", "peer": "-123", "mid": "10", "text": "/extract"},
            {"type": "media", "peer": "-123", "mid": "11", "kind": "bubble", "label": "lessons.zip"},
            {"type": "command", "peer": "-123", "mid": "12", "text": "/done"},
        ]
        asyncio.run(_process_extract_commands(settings, browser, storage, folder, "-123", events))
        asyncio.run(_process_extract_commands(settings, browser, storage, folder, "-123", events))
        saved = settings.local_root / "lessons"
        self.assertEqual((saved / "week/one.txt").read_text(), "lesson")
        self.assertIsNone(_load_control(settings)["batch"])
        self.assertIn("11", _load_control(settings)["ignored_media"])
        self.assertTrue(any("Extraction complete" in call.args[0]
                            for call in browser.send_text.await_args_list))

    def test_multipart_rar_is_recognized_without_commands(self):
        first = {"type": "media", "peer": "-123", "mid": "21", "kind": "bubble",
                 "label": "Course.part1.rar"}
        second = {**first, "mid": "22", "label": "Course.part2.rar"}
        self.assertEqual(_archive_group(first), _archive_group(second))
        self.assertEqual(_archive_group({**first, "label": "Course.rar"}), _archive_group(first))

    def test_reforwarded_archive_part_is_deduplicated(self):
        batch = {"downloaded": {"21": "Course.part1.rar"},
                 "media_meta": {"21": {"doc_id": "991", "label": "Course.part1.rar"}}}
        self.assertEqual(_same_staged_media(batch, {"mid": "45", "doc_id": "991",
                                                    "label": "Course.part1.rar"}), "21")
        self.assertEqual(_same_staged_media(batch, {"mid": "46", "doc_id": "992",
                                                    "label": "Course.part1.rar"}), "21")
        self.assertIsNone(_same_staged_media(batch, {"mid": "47", "doc_id": "993",
                                                     "label": "Course.part2.rar"}))

    def test_saved_password_batch_drops_unstaged_reforwards(self):
        settings = Settings("TDL", self.root / "private", destination="local",
                            output_dir=self.root / "saved")
        settings.prepare_storage()
        storage = LocalClient(settings)
        folder = storage.choose_folder()
        batch = {"peer": "-123", "start_mid": "21", "done_mid": "27",
                 "media": ["21", "22", "45"],
                 "downloaded": {"21": "Course.part1.rar", "22": "Course.part2.rar"},
                 "awaiting_password": True, "destination": "local",
                 "extract_engine_version": 2}
        incoming = _batch_root(settings, batch) / "archives"
        incoming.mkdir(parents=True)
        (incoming / "Course.part1.rar").write_bytes(b"one")
        (incoming / "Course.part2.rar").write_bytes(b"two")
        _save_control(settings, {"last_command_mid": "0", "batch": batch, "ignored_media": []})
        browser = type("Browser", (), {"send_text": AsyncMock()})()
        with patch.dict("os.environ", {"TDL_BACKGROUND": "1"}):
            asyncio.run(_process_extract_commands(settings, browser, storage, folder, "-123", []))
        saved = _load_control(settings)
        self.assertEqual(saved["batch"]["media"], ["21", "22"])
        self.assertIn("45", saved["ignored_media"])

    def test_auto_archive_batch_downloads_and_extracts_without_commands(self):
        settings = Settings("TDL", self.root / "private", destination="local",
                            output_dir=self.root / "saved")
        settings.prepare_storage()
        storage = LocalClient(settings)
        folder = storage.choose_folder()
        browser = type("Browser", (), {})()
        browser.send_text = AsyncMock()

        async def download(spec, destination):
            path = destination / spec["label"]
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(f"lesson-{spec['mid']}.txt", "ready")
            return path

        browser.download = AsyncMock(side_effect=download)
        events = [
            {"type": "media", "peer": "-123", "mid": "31", "kind": "grouped-item",
             "album": "31", "label": "first.zip"},
            {"type": "media", "peer": "-123", "mid": "32", "kind": "grouped-item",
             "album": "31", "label": "second.zip"},
        ]
        with patch("tdl_watcher.service.AUTO_QUIET_SECONDS", 0):
            for _ in range(3):
                asyncio.run(_process_extract_commands(settings, browser, storage, folder, "-123", events))
        output = settings.local_root / "first"
        self.assertEqual((output / "lesson-31.txt").read_text(), "ready")
        self.assertEqual((output / "lesson-32.txt").read_text(), "ready")
        self.assertEqual(set(_load_control(settings)["ignored_media"]), {"31", "32"})

    def test_missing_part_waits_and_recovers_when_next_part_arrives(self):
        settings = Settings("TDL", self.root / "private", destination="local",
                            output_dir=self.root / "saved")
        settings.prepare_storage()
        storage = LocalClient(settings)
        folder = storage.choose_folder()
        browser = type("Browser", (), {})()
        browser.send_text = AsyncMock()

        async def download(spec, destination):
            path = destination / spec["label"]
            path.write_bytes(b"part")
            return path

        browser.download = AsyncMock(side_effect=download)
        first = {"type": "media", "peer": "-123", "mid": "41", "kind": "bubble",
                 "label": "Course.part1.rar"}
        third = {**first, "mid": "43", "label": "Course.part3.rar"}
        second = {**first, "mid": "42", "label": "Course.part2.rar"}
        with patch("tdl_watcher.service.AUTO_QUIET_SECONDS", 0):
            for _ in range(3):
                asyncio.run(_process_extract_commands(settings, browser, storage, folder,
                                                      "-123", [first, third]))
            batch = _load_control(settings)["batch"]
            self.assertTrue(batch["awaiting_more"])
            self.assertTrue(any("missing: 2" in call.args[0]
                                for call in browser.send_text.await_args_list))
            asyncio.run(_process_extract_commands(settings, browser, storage, folder,
                                                  "-123", [first, second, third]))
            self.assertIn("42", _load_control(settings)["batch"]["downloaded"])


if __name__ == "__main__":
    unittest.main()


class NativeArchiveMetadataTests(unittest.TestCase):
    def test_rar_encryption_flags_request_password_before_quiet_extraction(self):
        import subprocess
        from tdl_watcher.extract import _extract_rar
        listing = subprocess.CompletedProcess([], 0, '''
Archive: course.part1.rar
Details: RAR 5, volume 1
        Name: lesson.bin
        Type: File
        Size: 500000
       Flags: encrypted
''')
        with patch('tdl_watcher.extract._run_unrar', return_value=listing) as tool:
            with self.assertRaises(PasswordRequired):
                _extract_rar(Path('course.part1.rar'), Path('output'), None, 1000000)
        self.assertEqual(tool.call_count, 1)

    def test_seven_zip_encryption_metadata_requests_password(self):
        import subprocess
        from tdl_watcher.extract import _extract_7z
        listing = subprocess.CompletedProcess([], 0, '''----------
Path = lesson.bin
Size = 500000
Encrypted = +
''')
        with patch('tdl_watcher.extract._run_7z', return_value=listing) as tool:
            with self.assertRaises(PasswordRequired):
                _extract_7z(Path('course.part1.rar'), Path('output'), None, 1000000)
        self.assertEqual(tool.call_count, 1)

    def test_unrar_password_exit_code_is_detected_without_output(self):
        import subprocess
        from tdl_watcher.extract import _unrar_error
        for password, error in [(None, PasswordRequired), ('not-logged', InvalidArchivePassword)]:
            with self.assertRaises(error):
                _unrar_error(subprocess.CompletedProcess([], 11, ''), password)

    def test_other_failure_reports_tool_exit_code(self):
        import subprocess
        from tdl_watcher.extract import _unrar_error
        with self.assertRaisesRegex(StorageError, 'exit 3.*checksum'):
            _unrar_error(subprocess.CompletedProcess([], 3, ''), None)
