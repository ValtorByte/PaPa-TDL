"""Real background process start/status/stop without an init system."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from tdl_watcher.background import _start_ticks
from tdl_watcher.locks import locked

ROOT = Path(__file__).resolve().parents[1]


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'private'
        self.fixture = self.root / 'fixtures'
        self.fixture.mkdir()
        # Only replace Telegram. Use the real CLI, storage, locks and process lifecycle.
        (self.fixture / 'sitecustomize.py').write_text('''
import os
from pathlib import Path
import tdl_watcher.service as service
class Browser:
    def __init__(self, settings): pass
    async def start(self): pass
    async def stop(self): Path(os.environ['TEST_STOP_MARKER']).write_text('closed')
    async def logged_in(self): return True
    async def find_group(self): return '-123'
    async def visible_media(self, peer): return []
service.TelegramBrowser = Browser
''')
        self.env = dict(os.environ, TDL_DATA_DIR=str(self.data), TDL_DESTINATION='local',
            TEST_STOP_MARKER=str(self.root / 'closed'), TDL_POLL_SECONDS='3',
            PYTHONPATH=str(self.fixture) + os.pathsep + str(ROOT / 'src'))
        self.addCleanup(self.cli, 'stop')

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'tdl_watcher', *args], env=self.env,
                              capture_output=True, text=True, timeout=20)

    def test_actual_background_lifecycle(self):
        started = self.cli('start', '--output', str(self.root / 'files'))
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertIn('background', started.stdout)
        state = json.loads((self.data / 'watcher.pid').read_text())
        self.assertIsNotNone(_start_ticks(state['pid']))
        self.assertIn('already running', self.cli('start').stdout)
        self.assertIn('running (PID', self.cli('status').stdout)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if 'Watching Telegram group' in (self.data / 'watcher.log').read_text():
                break
            time.sleep(0.1)
        self.assertIn('Watching Telegram group', self.cli('logs').stdout)
        stopped = self.cli('stop')
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertIn('stopped', self.cli('status').stdout)
        self.assertTrue((self.root / 'closed').exists())
        self.assertTrue((self.root / 'files').is_dir())

    def test_unrelated_pid_is_not_signaled(self):
        self.data.mkdir()
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        (self.data / 'watcher.pid').write_text(json.dumps({'pid': process.pid,
                                                        'start_ticks': _start_ticks(process.pid)}))
        result = self.cli('stop')
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(process.poll())

    def test_foreground_lock_prevents_background_start(self):
        self.data.mkdir()
        with locked(self.data / 'app.lock'):
            result = self.cli('start')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('using this profile', result.stderr)
