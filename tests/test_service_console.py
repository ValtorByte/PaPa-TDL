"""Interactive service passwords and navigation without a second terminal."""
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from tdl_watcher import background
from tdl_watcher import __main__ as cli
from tdl_watcher.config import Settings
from tdl_watcher.service import (_save_control, _load_control, _consume_extract_password,
                                  _process_extract_commands, _batch_root)
from tdl_watcher.extract import InvalidArchivePassword
from tdl_watcher.storage import LocalClient
from tdl_watcher.ui import ANSI_RE, panel_row


class ServiceConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings('TDL', Path(self.temp.name) / 'private', destination='local')
        self.settings.prepare_storage()
        self.batch = {'peer': '-123', 'start_mid': '10', 'done_mid': '12',
                      'media': ['11'], 'downloaded': {'11': 'private.rar'},
                      'awaiting_password': True, 'password_attempt': 1, 'destination': 'local'}
        _save_control(self.settings, {'batch': self.batch, 'last_command_mid': '12', 'ignored_media': []})

    def test_console_prompts_and_delivers_password_without_logging_it(self):
        (self.settings.data_dir / 'watcher.log').write_text('Archive password required.\n')
        output = io.StringIO()
        with patch.object(background.sys.stdin, 'isatty', return_value=True), \
             patch.object(background, '_running', side_effect=[123, 123, None]), \
             patch.object(background.getpass, 'getpass', return_value='private-secret') as prompt, \
             patch.object(background.time, 'sleep'), contextlib.redirect_stdout(output):
            background.console(self.settings)
        prompt.assert_called_once()
        self.assertNotIn('private-secret', output.getvalue())
        self.assertNotIn('private-secret', (self.settings.data_dir / 'watcher.log').read_text())
        handoff = self.settings.data_dir / 'extract-password.json'
        if os.name != 'nt':
            self.assertEqual(handoff.stat().st_mode & 0o777, 0o600)
        self.assertEqual(_consume_extract_password(self.settings), 'private-secret')
        self.assertFalse(handoff.exists())

    def test_foreground_retries_wrong_password_in_the_same_service(self):
        root = _batch_root(self.settings, self.batch)
        incoming = root / 'archives'
        incoming.mkdir(parents=True)
        (incoming / 'private.rar').write_bytes(b'archive fixture')
        browser = type('Browser', (), {'send_text': AsyncMock()})()
        storage = LocalClient(self.settings)
        folder = storage.choose_folder()
        passwords = []

        def extract(source, output, password, **kwargs):
            passwords.append(password)
            if password != 'correct':
                raise InvalidArchivePassword('Incorrect password')
            output.mkdir()
            (output / 'payload.txt').write_text('ready')

        with patch.dict(os.environ, {'TDL_BACKGROUND': '0'}), \
             patch('tdl_watcher.service.sys.stdin.isatty', return_value=True), \
             patch('tdl_watcher.service.getpass.getpass', side_effect=['wrong', 'correct']), \
             patch('tdl_watcher.service.extract_archives', side_effect=extract), \
             contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(_process_extract_commands(self.settings, browser, storage, folder, '-123', []))
        self.assertEqual(passwords, ['wrong', 'correct'])
        self.assertIsNone(_load_control(self.settings)['batch'])
        self.assertEqual((self.settings.local_root / 'private/payload.txt').read_text(), 'ready')

    def test_ctrl_c_goes_directly_back_to_menu(self):
        (self.settings.data_dir / 'config.json').write_text('{}')
        with patch.object(cli.sys, 'argv', ['papa-tdl']), \
             patch.object(cli.sys.stdin, 'isatty', return_value=True), \
             patch.object(cli.Settings, 'load', return_value=self.settings), \
             patch.object(cli, 'menu', side_effect=[['console'], None]) as menu, \
             patch.object(cli, 'execute', side_effect=KeyboardInterrupt), \
             patch('builtins.input') as pause, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(menu.call_count, 2)
        pause.assert_not_called()

    def test_cards_fit_narrow_and_wide_terminals(self):
        for width in (16, 24, 40, 80, 120, 160):
            output = io.StringIO()
            with patch('tdl_watcher.ui.shutil.get_terminal_size', return_value=os.terminal_size((width, 40))), \
                 contextlib.redirect_stdout(output):
                panel_row([('Accounts', ['Telegram: connected'], '95'),
                           ('Storage', ['Drive / TDL'], '36'), ('Service', ['Running'], '35')])
            lines = output.getvalue().splitlines()
            self.assertTrue(all(len(ANSI_RE.sub('', line)) <= width for line in lines))
            if width == 160:
                self.assertTrue(any('ACCOUNTS' in line and 'STORAGE' in line and 'SERVICE' in line for line in lines))

    @unittest.skipUnless(os.name == 'posix', 'Controlling terminal fixture requires POSIX')
    def test_real_terminal_password_and_ctrl_c_return_without_enter(self):
        import pty
        import select
        import signal
        import sys
        import time
        root = Path(__file__).resolve().parents[1]
        fixture = Path(self.temp.name) / 'fixture'
        fixture.mkdir()
        (fixture / 'sitecustomize.py').write_text(
            'import tdl_watcher.background as bg\nbg._running = lambda settings: 123\n')
        (self.settings.data_dir / 'config.json').write_text('{}')
        (self.settings.data_dir / 'watcher.log').write_text('Archive password required.\n')
        env = dict(os.environ, TERM='xterm', TDL_DATA_DIR=str(self.settings.data_dir),
                   TDL_DESTINATION='local', PYTHONPATH=str(fixture) + os.pathsep + str(root / 'src'))
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe(sys.executable, [sys.executable, '-m', 'tdl_watcher'], env)
        captured = bytearray()

        def read_until(target):
            start = len(captured)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if target in captured[start:]:
                    return bytes(captured[start:])
                ready, _, _ = select.select([fd], [], [], 0.1)
                if ready:
                    captured.extend(os.read(fd, 65536))
            self.fail('Terminal did not display: ' + repr(target))

        try:
            read_until(b'Choose [0-11]:')
            os.write(fd, b'1\n')
            read_until(b'Archive password (hidden):')
            os.write(fd, b'private-secret-42\n')
            read_until(b'Password sent securely')
            os.write(fd, b'\x03')
            returned = read_until(b'Choose [0-11]:')
            self.assertNotIn(b'Press Enter to return', returned)
            self.assertNotIn(b'private-secret-42', captured)
            os.write(fd, b'0\n')
            self.assertEqual(_consume_extract_password(self.settings), 'private-secret-42')
        finally:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
            os.close(fd)

    def test_updated_engine_retries_retained_failed_batch_without_downloading(self):
        import zipfile
        self.batch.update(awaiting_password=False, awaiting_more=True,
                          last_attempt_signature='11', downloaded={'11': 'retained.zip'})
        _save_control(self.settings, {'batch': self.batch, 'last_command_mid': '12', 'ignored_media': []})
        incoming = _batch_root(self.settings, self.batch) / 'archives'
        incoming.mkdir(parents=True)
        with zipfile.ZipFile(incoming / 'retained.zip', 'w') as archive:
            archive.writestr('payload.txt', 'retained-payload')
        browser = type('Browser', (), {'send_text': AsyncMock(), 'download': AsyncMock()})()
        storage = LocalClient(self.settings)
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(_process_extract_commands(self.settings, browser, storage,
                                                  storage.choose_folder(), '-123', []))
        browser.download.assert_not_called()
        self.assertIsNone(_load_control(self.settings)['batch'])
        self.assertEqual((self.settings.local_root / 'retained/payload.txt').read_text(), 'retained-payload')
