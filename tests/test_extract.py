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
                                 batch_folder_name, extract_archives)
from tdl_watcher.storage import LocalClient
from tdl_watcher.service import _load_control, _process_extract_commands


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

    def test_multipart_name_uses_first_volume(self):
        paths = [self.archives / "SOC TIER 1.part2.rar", self.archives / "SOC TIER 1.part1.rar"]
        for path in paths:
            path.write_bytes(b"part")
        self.assertEqual(batch_folder_name(paths, "fallback"), "SOC TIER 1")

    def test_password_errors_are_specific_without_exposing_password(self):
        archive = self.archives / "private.rar"
        archive.write_bytes(b"part")
        failed = __import__("subprocess").CompletedProcess([], 2, "ERROR: Wrong password")
        with patch("tdl_watcher.extract._run_7z", return_value=failed):
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


if __name__ == "__main__":
    unittest.main()
