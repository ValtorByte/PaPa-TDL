import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from google.auth.exceptions import RefreshError
from tdl_watcher.common import StorageError
from tdl_watcher.config import Settings
from tdl_watcher.hosted_login import HostedCredentials, login_hosted


class HostedLoginTests(unittest.TestCase):
    def test_approval_is_saved_only_after_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings('TDL', Path(directory))
            save = Mock()
            with patch('tdl_watcher.hosted_login._post', side_effect=[
                {'ticket': 'a' * 43}, {'status': 'pending'},
                {'status': 'complete', 'access_token': 'access-test', 'refresh_token': 'refresh-test'},
            ]), patch('time.sleep'), patch('builtins.print') as output:
                login_hosted(settings, save)
            save.assert_called_once()
            self.assertIsInstance(save.call_args.args[1], HostedCredentials)
            self.assertNotIn('refresh-test', str(output.call_args_list))

    def test_denial_does_not_save_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            save = Mock()
            with patch('tdl_watcher.hosted_login._post', side_effect=[
                {'ticket': 'a' * 43}, {'status': 'denied'},
            ]), patch('time.sleep'), patch('builtins.print'):
                with self.assertRaises(StorageError):
                    login_hosted(Settings('TDL', Path(directory)), save)
            save.assert_not_called()

    def test_refresh_uses_broker_and_does_not_serialize_web_secret(self):
        credentials = HostedCredentials(None, 'refresh-test')
        with patch('tdl_watcher.hosted_login._post', return_value={
            'access_token': 'renewed', 'expires_in': 3600,
        }) as post:
            credentials.refresh(None)
        post.assert_called_once_with('/refresh', {'refresh_token': 'refresh-test'})
        self.assertTrue(credentials.valid)
        saved = json.loads(credentials.to_json())
        self.assertEqual(saved['papa_tdl_hosted'], 1)
        self.assertNotIn('client_secret', saved)
        with patch('tdl_watcher.hosted_login._post', side_effect=StorageError('unavailable')):
            with self.assertRaises(RefreshError):
                credentials.refresh(None)
