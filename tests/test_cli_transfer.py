import asyncio
from dataclasses import replace
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch, AsyncMock

from tdl_watcher.config import Settings
from tdl_watcher.direct import download_url, url_identity
from tdl_watcher.drive import DriveClient, SCOPES, login_drive, login_drive_device, WSGITimeoutError
from tdl_watcher.common import StorageError, remote_name
from tdl_watcher.service import retry_pending, _save_upload_id, _read_upload_id, watch, connect_browser
from playwright.async_api import Error as BrowserError
from tdl_watcher.storage import LocalClient

PAYLOAD = b'test-file-' * 50000


class Handler(BaseHTTPRequestHandler):
    ranges = []
    flaky = True
    etag = '"v1"'
    disposition = 'attachment; filename="../../sample.bin"'

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.split('?')[0] == '/error':
            self.send_error(403)
            return
        offset = 0
        body = PAYLOAD if not self.path.startswith('/flaky') else b'x' * (2 * 1024 * 1024 + 100)
        requested = self.headers.get('Range')
        self.ranges.append(requested)
        if requested and self.headers.get('If-Range') == self.etag:
            offset = int(requested.split('=')[1].split('-')[0])
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {offset}-{len(body)-1}/{len(body)}')
        else:
            self.send_response(200)
        self.send_header('Content-Length', str(len(body) - offset))
        self.send_header('Content-Disposition', self.disposition)
        self.send_header('ETag', self.etag)
        self.end_headers()
        if self.path.startswith('/flaky') and self.flaky:
            type(self).flaky = False
            self.wfile.write(body[:1024 * 1024])
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(body[offset:])


class TransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/file?private=secret'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings('TDL', Path(self.temp.name), 3, destination='local')
        self.settings.prepare_storage()
        Handler.ranges = []
        Handler.flaky = True
        Handler.disposition = 'attachment; filename="../../sample.bin"'

    def partial(self, validator='"v1"'):
        folder = self.settings.pending_dir / ('url_' + url_identity(self.url))
        folder.mkdir()
        (folder / 'payload.part').write_bytes(PAYLOAD[:17])
        (folder / 'source.json').write_text(json.dumps({'validator': validator, 'complete': False}))
        return folder

    def test_full_download_and_atomic_local_transfer(self):
        path = download_url(self.settings, self.url)
        self.assertEqual(path.name, 'sample.bin')
        self.assertEqual(path.read_bytes(), PAYLOAD)
        self.assertNotIn('secret', (path.parent / 'source.json').read_text())
        local = LocalClient(self.settings)
        folder = local.choose_folder()
        asyncio.run(retry_pending(self.settings, local, folder))
        result = local.find_message(folder, 'url', url_identity(self.url))
        self.assertEqual(Path(result['id']).read_bytes(), PAYLOAD)
        self.assertFalse(path.parent.exists())

    def test_resume_with_validator(self):
        self.partial()
        path = download_url(self.settings, self.url)
        self.assertEqual(Handler.ranges, ['bytes=17-'])
        self.assertEqual(path.read_bytes(), PAYLOAD)

    def test_actual_network_disconnect_resumes_without_duplicate_bytes(self):
        with patch('tdl_watcher.direct.time.sleep'):
            path = download_url(self.settings, self.url.replace('/file?', '/flaky?'))
        self.assertEqual(path.read_bytes(), b'x' * (2 * 1024 * 1024 + 100))
        self.assertEqual(Handler.ranges, [None, 'bytes=1048576-'])

    def test_changed_remote_representation_restarts(self):
        self.partial('"old"')
        path = download_url(self.settings, self.url)
        self.assertEqual(path.read_bytes(), PAYLOAD)

    def test_no_validator_restarts(self):
        self.partial(None)
        path = download_url(self.settings, self.url)
        self.assertEqual(Handler.ranges, [None])
        self.assertEqual(path.read_bytes(), PAYLOAD)

    def test_completed_pending_is_reused(self):
        path = download_url(self.settings, self.url)
        self.assertEqual(download_url(self.settings, self.url), path)
        self.assertEqual(len(Handler.ranges), 1)

    def test_reserved_filename_is_renamed(self):
        Handler.disposition = 'attachment; filename="upload.json"'
        self.assertEqual(download_url(self.settings, self.url).name, 'file_upload.json')

    def test_size_limit_retains_queue_without_upload(self):
        settings = replace(self.settings, max_download_bytes=10)
        with self.assertRaisesRegex(StorageError, 'exceeds'):
            download_url(settings, self.url)
        self.assertEqual(list((settings.data_dir / 'downloads').iterdir()), [])

    def test_low_disk_space_stops_before_downloading(self):
        with patch('tdl_watcher.direct.shutil.disk_usage', return_value=MagicMock(free=1)):
            with self.assertRaisesRegex(StorageError, 'disk space'):
                download_url(self.settings, self.url)

    def test_http_error_has_no_url_secret(self):
        with self.assertRaises(StorageError) as raised:
            download_url(self.settings, self.url.replace('/file?', '/error?'))
        self.assertNotIn('secret', str(raised.exception))

    def test_incomplete_download_is_never_uploaded(self):
        self.partial()
        storage = MagicMock()
        asyncio.run(retry_pending(self.settings, storage, 'folder'))
        storage.upload.assert_not_called()

    def test_pending_folder_binding(self):
        folder = self.settings.pending_dir / '123_45'
        folder.mkdir()
        _save_upload_id(folder, 'upload1', 'original')
        with self.assertRaisesRegex(StorageError, 'another destination'):
            _read_upload_id(folder, 'different')

    def test_target_queues_are_separate(self):
        self.assertNotEqual(self.settings.pending_dir, replace(self.settings, destination='drive').pending_dir)

    def test_invalid_urls(self):
        for value in ('file:///etc/passwd', 'ftp://example.com/a', 'https://u:p@example.com/a', 'not-a-url'):
            with self.assertRaises(ValueError):
                url_identity(value)

    def test_real_cli_round_trip_and_duplicate_skip(self):
        env = dict(os.environ, TDL_DATA_DIR=self.temp.name, TDL_DESTINATION='local')
        result = subprocess.run([sys.executable, '-m', 'tdl_watcher', 'download', self.url],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([sys.executable, '-m', 'tdl_watcher', 'download', self.url],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Already saved', result.stdout)
        files = list((self.settings.data_dir / 'downloads').iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), PAYLOAD)
        self.assertNotIn('secret', result.stdout + result.stderr)

    def test_selected_missing_telegram_id_fails_once(self):
        browser = MagicMock()
        browser.start = AsyncMock()
        browser.stop = AsyncMock()
        browser.find_group = AsyncMock(return_value='-123')
        browser.logged_in = AsyncMock(return_value=True)
        browser.visible_media = AsyncMock(return_value=[{'mid': '77', 'peer': '-123', 'kind': 'bubble'}])
        with patch('tdl_watcher.service.TelegramBrowser', return_value=browser):
            with self.assertRaisesRegex(Exception, '88'):
                asyncio.run(watch(self.settings, selected_ids={'88'}, once=True))
        browser.download.assert_not_called()

    def test_local_size_conflict_retains_source(self):
        local = LocalClient(self.settings)
        folder = local.choose_folder()
        path = Path(self.temp.name) / 'test.txt'
        path.write_bytes(b'one')
        local.upload(path, folder, '-123', '45')
        path.unlink()  # A new source copy; the first was atomically hard-linked.
        path.write_bytes(b'different')
        with self.assertRaises(StorageError):
            local.upload(path, folder, '-123', '45')
        self.assertTrue(path.exists())

    def test_browser_reconnect_reuses_same_group(self):
        browser = MagicMock()
        browser.start = AsyncMock(side_effect=[BrowserError('network'), None])
        browser.stop = AsyncMock()
        browser.find_group = AsyncMock(return_value='-123')
        with patch('tdl_watcher.service.asyncio.sleep', new=AsyncMock()):
            peer = asyncio.run(connect_browser(browser, '-123'))
        self.assertEqual(peer, '-123')
        self.assertEqual(browser.start.await_count, 2)

    def test_once_transfer_failure_is_reported_and_pending_retained(self):
        queue = self.settings.pending_dir / '123_77'
        queue.mkdir()
        path = queue / 'file.bin'
        path.write_bytes(b'important')
        browser = MagicMock()
        browser.start = AsyncMock()
        browser.stop = AsyncMock()
        browser.find_group = AsyncMock(return_value='-123')
        browser.logged_in = AsyncMock(return_value=True)
        storage = MagicMock()
        storage.choose_folder.return_value = 'folder'
        storage.upload.side_effect = StorageError('storage unavailable')
        with patch('tdl_watcher.service.TelegramBrowser', return_value=browser), patch('tdl_watcher.service.make_storage', return_value=storage):
            with self.assertRaisesRegex(StorageError, 'unavailable'):
                asyncio.run(watch(self.settings, once=True))
        self.assertEqual(path.read_bytes(), b'important')

    def test_real_setup_saves_chosen_local_path(self):
        output = Path(self.temp.name) / 'selected output'
        env = dict(os.environ, TDL_DATA_DIR=self.temp.name, TDL_DESTINATION='local')
        result = subprocess.run([sys.executable, '-m', 'tdl_watcher', 'setup'], input=f'local\nTDL\n{output}\n',
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads((self.settings.data_dir / 'config.json').read_text())
        self.assertEqual(config['TDL_OUTPUT_DIR'], str(output))
        self.assertEqual(config['TDL_DESTINATION'], 'local')
        self.assertEqual((self.settings.data_dir / 'config.json').stat().st_mode & 0o777, 0o600)


class GoogleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings('TDL', Path(self.temp.name), 3, destination='drive')
        self.settings.prepare_storage()

    def test_missing_login_is_actionable(self):
        with self.assertRaisesRegex(StorageError, 'drive-login'):
            DriveClient(self.settings)

    def test_google_login_prints_link_flow_with_pkce_and_saves_private_token(self):
        config = self.settings.data_dir / 'google-client.json'
        config.write_text('{"installed": {"client_id":"test.apps.googleusercontent.com", "client_secret":"secret", "auth_uri":"https://accounts.google.com/o/oauth2/auth", "token_uri":"https://oauth2.googleapis.com/token"}}')
        credentials = MagicMock(refresh_token='test-refresh')
        credentials.to_json.return_value = '{"refresh_token":"test-refresh"}'
        flow = MagicMock()
        flow.run_local_server.return_value = credentials
        with patch('tdl_watcher.drive.InstalledAppFlow.from_client_config', return_value=flow) as factory, \
             patch('tdl_watcher.drive.DriveClient') as storage:
            storage.return_value.choose_folder.return_value = 'folder-id'
            login_drive(self.settings, config)
        self.assertTrue(factory.call_args.kwargs['autogenerate_code_verifier'])
        self.assertEqual(factory.call_args.args[1], SCOPES)
        self.assertFalse(flow.run_local_server.call_args.kwargs['open_browser'])
        self.assertEqual((self.settings.data_dir / 'google-token.json').stat().st_mode & 0o777, 0o600)

    def test_missing_desktop_json_explains_why_no_link_appeared(self):
        missing = self.settings.data_dir / 'google-client.json'
        with self.assertRaisesRegex(StorageError, 'missing, unreadable or invalid at') as raised:
            login_drive(self.settings, missing)
        self.assertIn(str(missing), str(raised.exception))

    def test_desktop_callback_timeout_explains_ssh_forward(self):
        config = self.settings.data_dir / 'google-client.json'
        config.write_text('{"installed": {"client_id":"test.apps.googleusercontent.com", "client_secret":"secret", "auth_uri":"https://accounts.google.com/o/oauth2/auth", "token_uri":"https://oauth2.googleapis.com/token"}}')
        flow = MagicMock()
        flow.run_local_server.side_effect = WSGITimeoutError('callback missing')
        with patch('tdl_watcher.drive.InstalledAppFlow.from_client_config', return_value=flow):
            with self.assertRaisesRegex(StorageError, 'SSH port forward'):
                login_drive(self.settings, config)
        self.assertFalse((self.settings.data_dir / 'google-token.json').exists())

    def test_device_link_polls_then_saves_private_token(self):
        def reply(status, payload):
            result = MagicMock(status_code=status)
            result.json.return_value = payload
            return result
        responses = [
            reply(200, {'verification_url': 'https://www.google.com/device',
                        'user_code': 'ABCD-EFGH', 'device_code': 'private-device-code',
                        'expires_in': 600, 'interval': 5}),
            reply(428, {'error': 'authorization_pending'}),
            reply(200, {'access_token': 'private-access-token',
                        'refresh_token': 'private-refresh-token',
                        'token_type': 'Bearer', 'scope': SCOPES[0],
                        'expires_in': 3600}),
        ]
        with patch('tdl_watcher.drive._device_client', return_value=('client-id', 'client-secret')), \
             patch('tdl_watcher.drive.requests.post', side_effect=responses) as request, \
             patch('tdl_watcher.drive.time.sleep'), \
             patch('tdl_watcher.drive.DriveClient') as storage, \
             patch('builtins.print') as output:
            storage.return_value.choose_folder.return_value = 'folder-id'
            login_drive_device(self.settings)
        shown = str(output.call_args_list)
        self.assertIn('https://www.google.com/device', shown)
        self.assertIn('ABCD-EFGH', shown)
        self.assertIn('Google Drive connected', shown)
        self.assertNotIn('client-secret', shown)
        self.assertNotIn('private-device-code', shown)
        self.assertNotIn('private-access-token', shown)
        token = self.settings.data_dir / 'google-token.json'
        self.assertEqual(token.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(token.read_text())['refresh_token'], 'private-refresh-token')
        self.assertEqual(request.call_count, 3)

    def test_device_rejects_desktop_client_type(self):
        failed = MagicMock(status_code=401)
        failed.json.return_value = {'error': 'invalid_client'}
        with patch('tdl_watcher.drive._device_client', return_value=('desktop-id', 'secret')), \
             patch('tdl_watcher.drive.requests.post', return_value=failed):
            with self.assertRaisesRegex(StorageError, 'TVs and Limited Input devices'):
                login_drive_device(self.settings)

    def test_google_uses_stable_file_id_and_verifies_remote_size(self):
        api = MagicMock()
        files = api.files.return_value
        files.list.return_value.execute.return_value = {'files': []}
        files.generateIds.return_value.execute.return_value = {'ids': ['stable-id']}
        path = self.settings.data_dir / 'data.txt'
        path.write_bytes(b'hello')
        files.create.return_value.next_chunk.return_value = (None, {'id': 'stable-id'})
        files.get.return_value.execute.return_value = {'id': 'stable-id', 'name': 'data.txt', 'size': '5', 'md5Checksum': hashlib.md5(b'hello').hexdigest()}
        storage = DriveClient(self.settings, api=api)
        saved = []
        item = storage.upload(path, 'folder', '-123', '45', save_upload_id=saved.append)
        self.assertEqual(saved, ['stable-id'])
        self.assertEqual(files.create.call_args.kwargs['body']['id'], 'stable-id')
        self.assertEqual(item['size'], '5')
        files.get.return_value.execute.return_value['size'] = '99'
        with self.assertRaisesRegex(StorageError, 'size verification'):
            storage.upload(path, 'folder', '-123', '46', existing_upload_id='stable-id')

    def test_drive_same_size_corrupt_upload_is_not_success(self):
        api = MagicMock()
        files = api.files.return_value
        files.list.return_value.execute.return_value = {'files': []}
        files.generateIds.return_value.execute.return_value = {'ids': ['stable-id']}
        files.create.return_value.next_chunk.return_value = (None, {'id': 'stable-id'})
        files.get.return_value.execute.return_value = {'id': 'stable-id', 'size': '5', 'md5Checksum': 'incorrect'}
        path = self.settings.data_dir / 'file.txt'
        path.write_bytes(b'hello')
        with self.assertRaisesRegex(StorageError, 'checksum verification'):
            DriveClient(self.settings, api=api).upload(path, 'folder', '-123', '45')
        self.assertTrue(path.exists())

    def test_drive_creates_default_folder_and_reuses_it(self):
        api = MagicMock()
        files = api.files.return_value
        files.list.return_value.execute.return_value = {'files': []}
        files.create.return_value.execute.return_value = {'id': 'folder'}
        storage = DriveClient(self.settings, api=api)
        self.assertEqual(storage.choose_folder(), 'folder')
        files.list.return_value.execute.return_value = {'files': [{'id': 'folder'}]}
        self.assertEqual(storage.choose_folder(), 'folder')
        self.assertEqual(files.create.call_count, 1)

    def test_config_defaults_to_drive_and_environment_overrides_saved_config(self):
        (self.settings.data_dir / 'config.json').write_text('{"TDL_DESTINATION":"drive","TDL_POLL_SECONDS":15}')
        with patch.dict(os.environ, {'TDL_DATA_DIR': self.temp.name, 'TDL_DESTINATION': 'local'}, clear=True):
            loaded = Settings.load()
        self.assertEqual(loaded.destination, 'local')
        self.assertEqual(loaded.poll_seconds, 15)
        with patch.dict(os.environ, {'TDL_DATA_DIR': self.temp.name + '/fresh'}, clear=True):
            self.assertEqual(Settings.load().destination, 'drive')


if __name__ == '__main__':
    unittest.main()
