"""First-launch defaults and a wizard that does not hide missing Google consent."""

import json
import os
try:
    import pty
except ImportError:  # Windows has no pseudo-terminal module.
    pty = None
import select
import subprocess
import sys
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tdl_watcher.__main__ import first_run, parser, run, setup
from tdl_watcher.config import Settings
from tdl_watcher.common import StorageError


class FirstRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ, {'TDL_DATA_DIR': self.temp.name}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.settings = Settings.load()
        self.settings.prepare_storage()

    def test_accepting_defaults_saves_drive_and_creates_local_default(self):
        with patch('builtins.input', side_effect=['', '', '']), patch('builtins.print'):
            setup(self.settings)
        config = json.loads((self.settings.data_dir / 'config.json').read_text())
        self.assertEqual(config['TDL_DESTINATION'], 'drive')
        self.assertEqual(config['GOOGLE_DRIVE_FOLDER_ID'], '')
        self.assertTrue(Path(config['TDL_OUTPUT_DIR']).is_dir())
        self.assertEqual(config['TDL_GROUP_NAME'], 'TDL')

    def test_first_launch_can_defer_both_logins_without_changing_destination(self):
        with patch('builtins.input', side_effect=['', '', '', 'n', 'n']), patch('builtins.print'):
            first_run(self.settings)
        self.assertEqual(Settings.load().destination, 'drive')
        self.assertFalse((self.settings.data_dir / 'google-token.json').exists())

    def test_missing_oauth_client_keeps_setup_and_explains_the_next_step(self):
        # Accept defaults, try browser login without its JSON, defer Telegram.
        with patch('tdl_watcher.hosted_login.login_hosted', side_effect=StorageError('Sign-in service unavailable')), \
             patch('builtins.input', side_effect=['', '', '', '', 'n']), patch('builtins.print') as output:
            first_run(self.settings)
        displayed = str(output.call_args_list)
        self.assertIn('papa-tdl drive-login', displayed)
        self.assertIn('unavailable', displayed)
        self.assertEqual(Settings.load().destination, 'drive')

    def test_first_launch_uses_saved_desktop_json_for_browser_login(self):
        client = self.settings.data_dir / 'google-client.json'
        client.write_text('{}')
        with patch('builtins.input', side_effect=['', '', '', '', 'n']), \
             patch('tdl_watcher.hosted_login.login_hosted') as login, patch('builtins.print'):
            first_run(self.settings)
        self.assertEqual(login.call_args.args[0], Settings.load())

    def test_drive_login_command_defaults_to_browser(self):
        arguments = parser().parse_args(['drive-login'])
        self.assertEqual(arguments.method, 'hosted')
        with patch('tdl_watcher.hosted_login.login_hosted') as login:
            run(arguments, self.settings)
        self.assertEqual(login.call_args.args[0], self.settings)

    def test_explicit_local_choice_uses_default_folder_without_a_custom_path(self):
        with patch('builtins.input', side_effect=['local', '', '']), patch('builtins.print'):
            setup(self.settings)
        loaded = Settings.load()
        self.assertEqual(loaded.destination, 'local')
        self.assertTrue(loaded.local_root.is_dir())

    @unittest.skipUnless(pty, 'PTY terminal fixture needs POSIX')
    def test_real_first_launch_in_a_terminal(self):
        master, slave = pty.openpty()
        environment = dict(os.environ, TERM='xterm-256color')
        environment.pop('NO_COLOR', None)
        process = subprocess.Popen([sys.executable, '-m', 'tdl_watcher'], env=environment,
                                   stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        output = bytearray()
        prompts = [
            ('Save to [drive/local]', b'\n'),
            ('Telegram group [TDL]', b'\n'),
            ('Google Drive folder ID', b'\n'),
            ('Open Google sign-in in your browser now?', b'n\n'),
            ('Connect Telegram now (phone + code)?', b'n\n'),
            ('Choose [0-11]', b'0\n'),
        ]
        next_prompt = 0
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                readable, _, _ = select.select([master], [], [], 0.1)
                if readable:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        if process.poll() is not None:
                            break
                        continue
                    if not data:
                        break
                    output.extend(data)
                    if next_prompt < len(prompts) and prompts[next_prompt][0] in output.decode(errors='replace'):
                        os.write(master, prompts[next_prompt][1])
                        next_prompt += 1
                if process.poll() is not None and not readable:
                    break
            if process.poll() is None:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    self.fail('First-launch terminal flow did not exit: ' + output.decode(errors='replace')[-1800:])
            self.assertEqual(process.wait(), 0, output.decode(errors='replace'))
            self.assertEqual(next_prompt, len(prompts), output.decode(errors='replace'))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
        text = output.decode(errors='replace')
        self.assertIn('CONTROL CENTER', text)
        self.assertIn('TELEGRAM  /  DRIVE + LOCAL', text)
        self.assertIn('\033[1;95m', text)
        self.assertEqual(Settings.load().destination, 'drive')
        self.assertTrue(self.settings.local_root.is_dir())
