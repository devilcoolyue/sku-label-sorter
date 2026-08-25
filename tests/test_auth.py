import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from label_sorter import auth


class AuthTests(unittest.TestCase):
    def test_default_token_lifetime_is_thirty_days(self):
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as tmp:
            authenticator = auth.Auth(Path(tmp))
            with mock.patch.object(auth.time, "time", return_value=now):
                token = authenticator.make_token("admin")

        payload, _ = token.rsplit(".", 1)
        data = json.loads(auth._b64d(payload))
        self.assertEqual(auth.SESSION_HOURS, 30 * 24)
        self.assertEqual(data["exp"] - now, 30 * 24 * 3600)


if __name__ == "__main__":
    unittest.main()
