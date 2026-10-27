"""Opt-in browser test for the Web K range transport using a local service worker."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest

from playwright.async_api import async_playwright
from tdl_watcher.web_range import BLOCK_SIZE, document_metadata, download_ranges


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/k/sw.js':
            body = b'''self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
 if (!new URL(e.request.url).pathname.includes('/stream/')) return;
 const range = e.request.headers.get('range');
 const m = /^bytes=(\\d+)-(\\d+)$/.exec(range || '');
 if (!m) return;
 const start = Number(m[1]), end = Number(m[2]), size = 1048581;
 const bytes = new Uint8Array(end - start + 1);
 for (let i = 0; i < bytes.length; i++) bytes[i] = (start + i) % 251;
 e.respondWith(new Response(bytes, {status:206, headers:{
   'Content-Range':`bytes ${start}-${end}/${size}`,
   'Content-Length':String(bytes.length)}}));
});'''
            kind = 'application/javascript'
        else:
            body = b'''<html><script>
window.rootScope={managers:{appDocsManager:{getDoc: async id =>
 ({id, size:1048581, dc_id:2, file_name:'test.rar'})}}};
navigator.serviceWorker.register('/k/sw.js');
</script></html>'''
            kind = 'text/html'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(os.environ.get('PAPA_TDL_BROWSER_TEST') == '1', 'opt-in browser test')
class BrowserRangeTests(unittest.TestCase):
    def test_local_service_worker_ranges(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        async def run():
            async with async_playwright() as p:
                options = {'executable_path': os.environ['TDL_CHROMIUM_EXECUTABLE']} if os.environ.get('TDL_CHROMIUM_EXECUTABLE') else {}
                browser = await p.chromium.launch(headless=True, args=['--no-sandbox'], **options)
                page = await browser.new_page()
                try:
                    await page.goto(f'http://127.0.0.1:{server.server_port}/k/')
                    await page.evaluate('navigator.serviceWorker.ready')
                    await page.reload()
                    await page.wait_for_function('navigator.serviceWorker.controller !== null')
                    meta = await document_metadata(page, '991')
                    class Progress:
                        def update(self, *args, **kwargs):
                            pass
                    with tempfile.TemporaryDirectory() as folder:
                        root = Path(folder)
                        output = root / 'out'
                        output.mkdir()
                        path = await download_ranges(page, meta, output, root / 'checkpoints',
                                                     '-123', '55', 'test.rar', Progress())
                        content = path.read_bytes()
                        self.assertEqual(len(content), BLOCK_SIZE + 5)
                        self.assertEqual(content[-5:], bytes(i % 251 for i in range(BLOCK_SIZE, BLOCK_SIZE + 5)))
                finally:
                    await browser.close()

        try:
            asyncio.run(run())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
