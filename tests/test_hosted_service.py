"""Offline broker tests: no Google credentials or network needed."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet


class HostedServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        client = root / "client.json"
        client.write_text(json.dumps({"web": {"client_id": "test", "client_secret": "test"}}))
        env = patch.dict(os.environ, {
            "PAPA_OAUTH_CLIENT_FILE": str(client),
            "PAPA_OAUTH_DATABASE": str(root / "sessions.sqlite"),
            "PAPA_OAUTH_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        })
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.temp.cleanup)
        spec = importlib.util.spec_from_file_location("broker_test", Path(__file__).parents[1] / "ops/oauth_service.py")
        self.service = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.service)
        self.client = self.service.app.test_client()
        self.secret = "a" * 43
        self.ticket = self.client.post("/start", json={"poll_secret": self.secret}).json["ticket"]

    def poll(self, secret=None):
        return self.client.post("/poll", json={"ticket": self.ticket, "poll_secret": secret or self.secret})

    def test_approval_is_private_encrypted_and_single_use(self):
        self.assertEqual(self.poll("b" * 43).status_code, 410)
        self.assertEqual(self.poll().json["status"], "pending")
        with self.service.db() as conn:
            state = conn.execute("SELECT state FROM sessions").fetchone()[0]
        with patch.object(self.service, "google", return_value={"scope": self.service.SCOPE,
                          "access_token": "access-test", "refresh_token": "refresh-test"}):
            response = self.client.get("/callback", query_string={"state": state, "code": "fake-code"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"access-test", response.data)
        with self.service.db() as conn:
            stored = conn.execute("SELECT result FROM sessions").fetchone()[0]
        self.assertNotIn("refresh-test", stored)
        self.assertEqual(self.poll().json["refresh_token"], "refresh-test")
        self.assertEqual(self.poll().status_code, 410)

    def test_invalid_state_and_denial(self):
        self.assertEqual(self.client.get("/callback?state=unknown&code=x").status_code, 410)
        with self.service.db() as conn:
            state = conn.execute("SELECT state FROM sessions").fetchone()[0]
        self.client.get("/callback", query_string={"state": state, "error": "access_denied"})
        self.assertEqual(self.poll().json["status"], "denied")

    def test_expiry_and_pkce(self):
        location = self.client.get("/login/" + self.ticket).headers["Location"]
        self.assertIn("code_challenge_method=S256", location)
        with self.service.db() as conn:
            conn.execute("UPDATE sessions SET expires=0")
        self.assertEqual(self.poll().status_code, 410)


if __name__ == "__main__":
    unittest.main()
