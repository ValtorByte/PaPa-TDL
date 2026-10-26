"""Exercise redraws in a real terminal while the menu is awaiting input."""

import json
import os
import re
import select
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

try:
    import fcntl
    import pty
    import termios
except ImportError:
    fcntl = pty = termios = None


@unittest.skipUnless(pty and fcntl and termios, "PTY resizing requires POSIX")
class ResizeTests(unittest.TestCase):
    def test_menu_redraws_without_broken_borders_after_resize(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            (data / "config.json").write_text(json.dumps({"TDL_DESTINATION": "local"}))
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            environment = dict(os.environ, TERM="xterm-256color", TDL_DATA_DIR=temporary)
            process = subprocess.Popen([sys.executable, "-m", "tdl_watcher"],
                                       stdin=slave, stdout=slave, stderr=slave,
                                       env=environment, close_fds=True)
            os.close(slave)
            output = bytearray()

            def read_until(marker, start=0):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if marker in output[start:]:
                        return
                    ready, _, _ = select.select([master], [], [], 0.1)
                    if ready:
                        output.extend(os.read(master, 65536))
                self.fail("The menu did not respond to terminal resize.")

            try:
                read_until(b"Choose [0-12]:")
                first_clear = output.rfind(b"\x1b[2J\x1b[H")
                self.assertGreaterEqual(first_clear, 0)
                for width in (80, 60, 40, 160, 120):
                    start = len(output)
                    fcntl.ioctl(master, termios.TIOCSWINSZ,
                                struct.pack("HHHH", 40, width, 0, 0))
                    read_until(b"\x1b[2J\x1b[H", start)
                    last_clear = output.rfind(b"\x1b[2J\x1b[H")
                    read_until(b"Choose [0-12]:", last_clear)
                    latest = output[last_clear:].decode(errors="replace")
                    plain = re.sub(r"\x1b\[[0-9;]*m", "", latest)
                    self.assertIn("CONTROL CENTER", plain)
                    self.assertIn("12  Stop background service", plain)
                    if width < 100:
                        self.assertNotIn("FILES & LIVE SERVICE", plain)
                    if width < 55:
                        self.assertIn("PaPa-TDL", plain)
                    self.assertTrue(all(len(line.rstrip("\r")) <= width
                                        for line in plain.splitlines()
                                        if not line.startswith("\x1b")), width)
                os.write(master, b"0\n")
                self.assertEqual(process.wait(timeout=5), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)
