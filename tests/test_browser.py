"""Opt-in real Chromium fixture test; never contacts Telegram or Google."""

import asyncio
import os
from pathlib import Path
import tempfile
import unittest

from playwright.async_api import async_playwright

from tdl_watcher.config import Settings
from tdl_watcher.telegram import TelegramBrowser, matches_displayed_size


class DisplayedSizeTests(unittest.TestCase):
    def test_short_completed_browser_volume_is_rejected(self):
        self.assertTrue(matches_displayed_size("1000.0 MB / 1000.0 MB", 1048576000))
        self.assertFalse(matches_displayed_size("1000.0 MB / 1000.0 MB", 1048444928))
        self.assertTrue(matches_displayed_size("418.9 MB / 418.9 MB", 439269342))
        self.assertTrue(matches_displayed_size("", 123456))


@unittest.skipUnless(os.environ.get('PAPA_TDL_BROWSER_TEST') == '1', 'Set PAPA_TDL_BROWSER_TEST=1 for Chromium fixtures')
class BrowserTests(unittest.TestCase):
    def test_real_browser_media_download_and_safe_name(self):
        async def check():
            with tempfile.TemporaryDirectory() as directory:
                settings = Settings('TDL', Path(directory))
                settings.prepare_storage()
                browser = TelegramBrowser(settings)
                async with async_playwright() as runner:
                    browser.context = await runner.chromium.launch_persistent_context(
                        str(settings.data_dir / 'browser-profile'), headless=True,
                        accept_downloads=True, downloads_path=str(settings.data_dir / 'browser-downloads'))
                    browser.page = browser.context.pages[0]
                    await browser.page.set_content('''
                    <div id="page-chats" style="height:20px">Chats</div><div id="column-center"><div class="bubbles-scrollable">
                      <div class="bubble" data-peer-id="-123" data-mid="55">
                        <div class="document" data-doc-id="991"><div class="document-name"><span title="sample-full.txt">sample….txt</span></div></div>
                        <div class="document-description">5 B</div>
                      </div></div></div>
                    <div class="btn-menu active" style="display:none">
                      <a class="btn-menu-item" download="sample.txt" href="data:application/octet-stream;base64,aGVsbG8=">
                        <span class="btn-menu-item-text">Download</span>
                      </a>
                    </div>
                    <script>
                      document.querySelector('.document').addEventListener('contextmenu', e => {
                        e.preventDefault(); document.querySelector('.btn-menu').style.display='block';
                      });
                    </script>
                    ''')
                    try:
                        self.assertTrue(await browser.logged_in())
                        specs = await browser.visible_media('-123')
                        self.assertEqual([s['mid'] for s in specs], ['55'])
                        self.assertEqual(specs[0]['label'], 'sample-full.txt')
                        self.assertEqual(specs[0]['doc_id'], '991')
                        saved = await asyncio.wait_for(browser.download(specs[0]), timeout=30)
                        self.assertEqual(saved.read_bytes(), b'hello')
                        self.assertEqual(saved.parent, settings.pending_dir / '123_55')
                    finally:
                        await browser.stop()
        asyncio.run(check())
