import os
from unittest import TestCase
from unittest.mock import patch
from lounge.config import Settings
from lounge.credentials import is_exposed
import hashlib


class ConfigTests(TestCase):
    def test_exposed_credential_is_rejected_by_hash(self):
        hashes = {hashlib.sha256(b"fake-exposed-token").hexdigest()}
        self.assertTrue(is_exposed("fake-exposed-token", hashes))
        self.assertFalse(is_exposed("rotated-token", hashes))

    def test_invalid_dsn_and_remote_cleartext_rejected(self):
        for url, ssl in [
            ("sqlite:///data.db", "false"),
            ("postgresql://user@db.example.com/data", "false"),
            ("postgresql://user@db.example.com/data?sslmode=disable", "true"),
        ]:
            with (
                patch.dict(
                    os.environ, {"DATABASE_URL": url, "DB_SSL": ssl}, clear=True
                ),
                self.assertRaises(ValueError),
            ):
                Settings.load()

    def test_pool_limits_and_no_secrets_in_repr(self):
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://user:REPLACE@localhost/data",
                "DB_SSL": "false",
            },
            clear=True,
        ):
            s = Settings.load()
            self.assertNotIn("REPLACE", repr(s))
            self.assertEqual(s.pool_max, 4)
        with patch.dict(
            os.environ,
            {"API_DATABASE_URL": "postgresql://user@localhost/data"},
            clear=True,
        ):
            s = Settings.load(api=True)
            self.assertEqual((s.pool_max, s.pool_mode), (1, "transaction"))

    def test_queue_routing_preserves_real_channel_scope(self):
        s = Settings("postgresql://user@localhost/data", guild_id=123)
        self.assertEqual(s.queue_key(123, 987, "join"), "987")
        self.assertIsNone(s.queue_key(456, 987, "join"))
        self.assertIsNone(s.queue_key(123, 987, "other"))
