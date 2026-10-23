import os
import unittest
from unittest.mock import Mock, patch
from tdl_watcher.ui import color


class TerminalTests(unittest.TestCase):
    def test_purple_in_terminal_and_plain_in_pipe(self):
        terminal = Mock()
        terminal.isatty.return_value = True
        with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True):
            self.assertEqual(color('PaPa-TDL', stream=terminal), '\033[95mPaPa-TDL\033[0m')
            terminal.isatty.return_value = False
            self.assertEqual(color('PaPa-TDL', stream=terminal), 'PaPa-TDL')

    def test_no_color_is_respected(self):
        terminal = Mock()
        terminal.isatty.return_value = True
        with patch.dict(os.environ, {'NO_COLOR': '1'}):
            self.assertEqual(color('PaPa-TDL', stream=terminal), 'PaPa-TDL')
