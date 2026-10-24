import os
import unittest
from unittest.mock import Mock, patch
from tdl_watcher.ui import ProgressBar, color, status
from tdl_watcher.telegram import size_matches_estimate


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

    def test_progress_rewrites_one_terminal_line(self):
        stream = Mock()
        stream.isatty.return_value = True
        with patch.dict(os.environ, {'TERM': 'xterm', 'NO_COLOR': '1'}, clear=True):
            progress = ProgressBar('Download', 100, stream=stream, width=10)
            progress.update(25)
            progress.update(50)
            progress.finish('ready')
        rendered = ''.join(call.args[0] for call in stream.write.call_args_list)
        self.assertIn('\rDownload', rendered)
        self.assertIn('50.0%', rendered)
        self.assertEqual(rendered.count('\n'), 1)  # Only the final completion line advances.

    def test_status_has_text_as_well_as_color(self):
        with patch.dict(os.environ, {'NO_COLOR': '1'}):
            self.assertIn('connected', status(True))
            self.assertIn('not connected', status(False))

    def test_telegram_size_rounding_is_allowed_but_truncation_is_rejected(self):
        estimate = 1000 * 1024 * 1024
        self.assertTrue(size_matches_estimate(estimate - 64 * 1024, estimate))
        self.assertFalse(size_matches_estimate(estimate - 128 * 1024, estimate))
