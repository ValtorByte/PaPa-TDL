"""Phone entry stays visible while login secrets remain separate."""

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tdl_watcher.config import Settings
from tdl_watcher.telegram import PHONE_INPUT, TelegramBrowser, normalize_phone


class TelegramLoginTests(unittest.TestCase):
    def test_phone_formats(self):
        self.assertEqual(normalize_phone('0912 345 6789'), '+989123456789')
        self.assertEqual(normalize_phone('989123456789'), '+989123456789')
        self.assertEqual(normalize_phone('+9809123456789'), '+989123456789')
        self.assertEqual(normalize_phone('0098-912-345-6789'), '+989123456789')
        self.assertEqual(normalize_phone('+1 (202) 555-0123'), '+12025550123')
        self.assertIsNone(normalize_phone('1234'))
        self.assertIsNone(normalize_phone('+0 123456789'))

    def test_phone_uses_visible_input(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = TelegramBrowser(Settings('TDL', Path(directory)))
            phone_box = Mock(count=AsyncMock(return_value=1),
                             wait_for=AsyncMock(), fill=AsyncMock())
            next_button = Mock(count=AsyncMock(return_value=1), click=AsyncMock())
            page = Mock()
            page.locator.side_effect = lambda selector: (phone_box if selector == PHONE_INPUT else next_button)
            browser.page = page
            browser.logged_in = AsyncMock(side_effect=[False, True])
            browser._wait_step = AsyncMock(return_value='success')
            with patch('builtins.input', return_value='09123456789') as visible, \
                 patch('tdl_watcher.telegram.getpass.getpass', side_effect=AssertionError('phone was hidden')), \
                 patch('builtins.print'):
                asyncio.run(browser.login())
            self.assertIn('Phone', visible.call_args.args[0])
            phone_box.fill.assert_awaited_once_with('+989123456789')
            next_button.click.assert_awaited_once()
