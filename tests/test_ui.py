import os
import io
import unittest
from unittest.mock import Mock, patch
from tdl_watcher.ui import ProgressBar, color, status, panel_row


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

    def test_menu_cards_fit_common_terminal_widths(self):
        cards = [('Accounts', ['Telegram connected', 'Google needs sign-in'], '95'),
                 ('Destination', ['Google Drive / TDL', '/var/lib/papa-tdl/downloads'], '36'),
                 ('Service', ['Waiting for password', 'Retry available'], '35')]
        for width in (40, 60, 80, 120, 160):
            with self.subTest(width=width), patch('tdl_watcher.ui._terminal_columns', return_value=width), \
                 patch('tdl_watcher.ui.unicode_terminal', return_value=False), \
                 patch('sys.stdout', new_callable=io.StringIO) as output:
                panel_row(cards)
                self.assertTrue(output.getvalue())
                self.assertTrue(all(len(line) <= width for line in output.getvalue().splitlines()))

    def test_progress_reflows_after_terminal_resize(self):
        stream = Mock()
        stream.isatty.return_value = True
        with patch.dict(os.environ, {'TERM': 'xterm', 'NO_COLOR': '1'}, clear=True), \
             patch('tdl_watcher.ui._terminal_columns', side_effect=[120, 40, 40]), \
             patch('tdl_watcher.ui.unicode_terminal', return_value=False):
            progress = ProgressBar('Downloading', 100, stream=stream)
            progress.update(25)
            progress.update(75)
            progress.finish('ready')
        writes = [call.args[0] for call in stream.write.call_args_list]
        self.assertTrue(any('75.0%' in line for line in writes))
