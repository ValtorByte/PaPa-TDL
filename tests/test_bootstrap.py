"""Use real pip to install into a real venv without pip or ensurepip."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile


@unittest.skipUnless(importlib.util.find_spec('pip'), 'The regression test needs pip in the test interpreter')
class BootstrapTests(unittest.TestCase):
    def test_pip_python_bootstraps_a_pipless_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            venv = root / 'private venv'
            subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True,
                           capture_output=True)
            python = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            probe = subprocess.run([str(python), '-I', '-c',
                "import importlib.util; assert importlib.util.find_spec('pip') is None"], capture_output=True)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            wheel = root / 'papa_tdl_bootstrap_probe-0.0.1-py3-none-any.whl'
            metadata = 'papa_tdl_bootstrap_probe-0.0.1.dist-info/'
            with ZipFile(wheel, 'w') as archive:
                archive.writestr('papa_tdl_bootstrap_probe.py', 'VALUE = "bootstrap worked"\n')
                archive.writestr(metadata + 'METADATA', 'Metadata-Version: 2.1\nName: papa-tdl-bootstrap-probe\nVersion: 0.0.1\n')
                archive.writestr(metadata + 'WHEEL', 'Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
                archive.writestr(metadata + 'RECORD', '')
            install = subprocess.run([sys.executable, '-m', 'pip', '--python', str(python), 'install',
                '--disable-pip-version-check', '--no-index', '--no-deps', '--no-compile', str(wheel)],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(install.returncode, 0, install.stderr)
            verified = subprocess.run([str(python), '-I', '-c',
                'from papa_tdl_bootstrap_probe import VALUE; print(VALUE)'], capture_output=True, text=True)
            self.assertEqual(verified.stdout.strip(), 'bootstrap worked')
