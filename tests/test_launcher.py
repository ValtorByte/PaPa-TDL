"""Run the real native installer with isolated package/browser installation stubs."""

import getpass
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest

if sys.platform == 'linux':
    import fcntl

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'linux', 'The native shell installer targets Linux')
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.prefix = self.root / 'program with spaces'
        self.data = self.root / 'private'
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'],
            PAPA_TDL_PREFIX=str(self.prefix), PAPA_TDL_BIN_DIR=str(self.bin),
            PAPA_TDL_DATA_DIR=str(self.data), PAPA_TDL_PYTHON=str(self.bin / 'python-test'),
            PAPA_TDL_USER=getpass.getuser(), PYTHONPATH=str(ROOT / 'src'))
        real = shlex.quote(sys.executable)
        self.script('python-test', f'''#!/usr/bin/env bash
set -eu
if [[ "${{1:-}}" == -m && "${{2:-}}" == venv ]]; then
  if [[ "${{3:-}}" != --without-pip ]]; then
    echo 'simulated ensurepip failure' >&2
    exit 31
  fi
  mkdir -p "$4/bin"
  cp "{self.bin}/python-test" "$4/bin/python"
  exit 0
fi
if [[ "${{1:-}}" == -m && "${{2:-}}" == pip ]]; then exit 0; fi
if [[ "${{1:-}}" == -m && "${{2:-}}" == playwright ]]; then
  mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"
  exit 0
fi
exec {real} "$@"
''')
        self.script('systemctl', '#!/bin/sh\necho "systemctl must not be called" >&2\nexit 99\n')
        self.script('docker', '#!/bin/sh\necho "docker must not be called" >&2\nexit 99\n')

    def script(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def install(self, remote=False):
        command = ['bash', str(ROOT / 'install.sh'), 'install', '--skip-system-deps']
        if remote:
            command = ['bash', '-c', (ROOT / 'install.sh').read_text(), '@', 'install', '--skip-system-deps']
        result = subprocess.run(command, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_native_install_and_update_preserve_private_data(self):
        self.data.mkdir()
        token = self.data / 'google-token.json'
        token.write_text('private test token')
        for _ in range(2):
            self.install()
        self.assertEqual(token.read_text(), 'private test token')
        self.assertEqual(self.data.stat().st_mode & 0o777, 0o700)
        self.assertTrue((self.prefix / 'current').is_symlink())
        result = subprocess.run([str(self.bin / 'papa-tdl'), 'check', '--output', str(self.root / 'files')],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Ready: local', result.stdout)

    def test_bash_c_bootstrap_downloads_repository(self):
        archive = self.root / 'source.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for item in ('pyproject.toml', 'requirements.lock', 'README.md', 'LICENSE', 'src'):
                tar.add(ROOT / item, arcname='repo-main/' + item)
        self.script('curl', f'''#!/usr/bin/env bash
set -eu
while [[ $# -gt 0 ]]; do
  if [[ "$1" == -o ]]; then cp {shlex.quote(str(archive))} "$2"; exit 0; fi
  shift
done
exit 1
''')
        result = self.install(remote=True)
        self.assertIn('Downloading', result.stdout)
        self.assertTrue((self.bin / 'papa-tdl').exists())

    def test_active_profile_blocks_update(self):
        self.data.mkdir()
        with (self.data / 'app.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = subprocess.run(['bash', str(ROOT / 'install.sh'), 'update', '--skip-system-deps'],
                                    env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Stop the watcher', result.stderr)
        self.assertFalse((self.prefix / 'current').exists())

    def test_bad_repo_is_rejected_before_installation(self):
        env = dict(self.env, PAPA_TDL_REPO='../bad')
        result = subprocess.run(['bash', str(ROOT / 'install.sh'), 'install', '--skip-system-deps'],
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.prefix.exists())
