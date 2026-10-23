import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from urllib.parse import parse_qs, urlsplit

from google_auth_oauthlib.flow import InstalledAppFlow
from tdl_watcher.common import StorageError
from tdl_watcher.config import Settings
from tdl_watcher.drive import login_drive, SCOPES
from tdl_watcher.oauth_config import desktop_config, resolve_desktop_config


class AppOAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings('TDL', Path(self.temp.name))
        self.identity = {'installed': {'client_id': 'test.apps.googleusercontent.com', 'client_secret': 'test-secret'}}

    def test_missing_release_identity_fails_without_prompt_or_network(self):
        with patch('tdl_watcher.oauth_config._google_client.CLIENT_ID', ''), \
             patch('sys.stdin.isatty', return_value=False), \
             patch('builtins.input') as prompt, patch('requests.sessions.Session.request') as request:
            with self.assertRaisesRegex(StorageError, 'interactive'):
                login_drive(self.settings)
        prompt.assert_not_called()
        request.assert_not_called()

    def test_interactive_setup_saves_privately_and_is_reused(self):
        with patch('tdl_watcher.oauth_config._google_client.CLIENT_ID', ''), \
             patch('sys.stdin.isatty', return_value=True), \
             patch('builtins.input', return_value='test.apps.googleusercontent.com') as prompt, \
             patch('getpass.getpass', return_value='test-secret'), patch('builtins.print') as output:
            resolve_desktop_config(self.settings)
            resolve_desktop_config(self.settings)
        prompt.assert_called_once()
        path = self.settings.data_dir / 'google-client.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('test-secret', str(output.call_args_list))
        self.assertEqual(json.loads(path.read_text())['installed']['client_secret'], 'test-secret')

    def test_interactive_invalid_details_do_not_save(self):
        with patch('tdl_watcher.oauth_config._google_client.CLIENT_ID', ''), \
             patch('sys.stdin.isatty', return_value=True), \
             patch('builtins.input', return_value='invalid'), \
             patch('getpass.getpass', return_value='test-secret'), patch('builtins.print'):
            with self.assertRaises(StorageError):
                resolve_desktop_config(self.settings)
        self.assertFalse((self.settings.data_dir / 'google-client.json').exists())

    def test_bundled_identity_generates_real_google_url_with_pkce(self):
        with patch('tdl_watcher.oauth_config._google_client.CLIENT_ID', 'test.apps.googleusercontent.com'), \
             patch('tdl_watcher.oauth_config._google_client.CLIENT_SECRET', 'test-secret'):
            config = resolve_desktop_config(self.settings)
        flow = InstalledAppFlow.from_client_config(config, SCOPES, autogenerate_code_verifier=True)
        flow.redirect_uri = 'http://127.0.0.1:53682/'
        url, state = flow.authorization_url(access_type='offline', prompt='consent')
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.hostname, 'accounts.google.com')
        self.assertEqual(query['scope'], SCOPES)
        self.assertEqual(query['state'], [state])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertNotIn('test-secret', url)

    def test_saved_client_overrides_bundle_and_invalid_override_is_not_ignored(self):
        path = self.settings.data_dir / 'google-client.json'
        path.write_text(json.dumps(self.identity))
        with patch('tdl_watcher.oauth_config._google_client.CLIENT_ID', 'other.apps.googleusercontent.com'):
            self.assertEqual(resolve_desktop_config(self.settings)['installed']['client_id'], 'test.apps.googleusercontent.com')
            path.write_text('{invalid')
            with self.assertRaises(StorageError):
                resolve_desktop_config(self.settings)

    def test_only_app_identity_is_used_and_endpoints_cannot_be_redirected(self):
        self.identity['installed'].update(auth_uri='https://example.invalid', token_uri='https://example.invalid', refresh_token='never-copy')
        config = desktop_config(self.identity)
        self.assertEqual(config['installed']['token_uri'], 'https://oauth2.googleapis.com/token')
        self.assertNotIn('never-copy', json.dumps(config))
        for supplied in ({'type': 'service_account'}, {'refresh_token': 'private'}, {'web': self.identity['installed']}):
            with self.assertRaises(StorageError):
                desktop_config(supplied)

    def test_success_requires_drive_verification(self):
        credentials = MagicMock(refresh_token='private')
        credentials.to_json.return_value = '{"refresh_token":"private"}'
        flow = MagicMock()
        flow.run_local_server.return_value = credentials
        with patch('tdl_watcher.drive.resolve_desktop_config', return_value=desktop_config(self.identity)), \
             patch('tdl_watcher.drive.InstalledAppFlow.from_client_config', return_value=flow), \
             patch('tdl_watcher.drive.DriveClient', side_effect=StorageError('Drive unavailable')), \
             patch('builtins.print') as output:
            with self.assertRaisesRegex(StorageError, 'token was saved'):
                login_drive(self.settings)
        self.assertNotIn('Google Drive connected', str(output.call_args_list))
