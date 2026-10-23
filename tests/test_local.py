"""Local destination invariants and private configuration."""

import asyncio
from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tdl_watcher.common import StorageError, source_prefix
from tdl_watcher.config import Settings, private_json
from tdl_watcher.service import retry_pending
from tdl_watcher.storage import LocalClient


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings('TDL', self.root / 'private', destination='local', output_dir=self.root / 'chosen output')
        self.settings.prepare_storage()
        self.client = LocalClient(self.settings)
        self.folder = self.client.choose_folder()

    def test_custom_output_atomic_no_second_copy(self):
        path = self.root / 'movie.mp4'
        path.write_bytes(b'x' * 2000)
        with patch('tdl_watcher.storage.shutil.copyfileobj', side_effect=AssertionError('unnecessary copy')):
            item = self.client.upload(path, self.folder, '-123', '55')
        saved = Path(item['id'])
        self.assertEqual(saved.parent, self.root / 'chosen output')
        self.assertEqual(saved.stat().st_ino, path.stat().st_ino)
        if os.name != 'nt':
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertTrue(path.exists())

    def test_other_filesystem_copy_is_published_and_verified(self):
        path = self.root / 'document.bin'
        path.write_bytes(b'content' * 100)
        real_link = os.link
        def link(source, target):
            if Path(source) == path:
                raise OSError(errno.EXDEV, 'different device')
            real_link(source, target)
        with patch('tdl_watcher.storage.os.link', side_effect=link):
            item = self.client.upload(path, self.folder, '-123', '56')
        self.assertEqual(Path(item['id']).read_bytes(), path.read_bytes())
        self.assertFalse(list(self.settings.local_root.glob('.papa-tdl-*')))

    def test_failed_copy_keeps_pending_and_does_not_publish(self):
        path = self.root / 'document.bin'
        path.write_bytes(b'content')
        with patch('tdl_watcher.storage.os.link', side_effect=OSError(errno.EXDEV, 'device')):
            with patch('tdl_watcher.storage.shutil.copyfileobj', side_effect=OSError('full disk')):
                with self.assertRaises(OSError):
                    self.client.upload(path, self.folder, '-123', '56')
        self.assertEqual(list(self.settings.local_root.iterdir()), [])
        self.assertTrue(path.exists())

    def test_unsafe_existing_symlink_is_never_followed(self):
        original = self.root / 'private.bin'
        original.write_bytes(b'secret')
        try:
            (self.settings.local_root / (source_prefix('-123', '5') + 'file.bin')).symlink_to(original)
        except (OSError, NotImplementedError):
            self.skipTest('Creating symlinks requires privilege on this host')
        with self.assertRaises(StorageError):
            self.client.find_message(self.folder, '-123', '5')

    def test_crash_left_empty_queue_is_removed(self):
        empty = self.settings.pending_dir / '123_7'
        empty.mkdir()
        asyncio.run(retry_pending(self.settings, self.client, self.folder))
        self.assertFalse(empty.exists())

    def test_private_atomic_json(self):
        target = self.root / 'settings.json'
        private_json(target, '{"safe": true}')
        self.assertEqual(json.loads(target.read_text()), {'safe': True})
        if os.name != 'nt':
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_root_output_is_rejected(self):
        client = LocalClient(replace(self.settings, output_dir=Path('/')))
        with self.assertRaises(StorageError):
            client.choose_folder()
