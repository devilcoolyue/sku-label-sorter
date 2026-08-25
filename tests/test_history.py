import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from fastapi import HTTPException


class HistoryPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_is_returned_in_ten_item_pages(self):
        items = [{"id": f"{i:012x}"} for i in range(25)]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(app_module, "WORK_DIR", Path(tmp)), \
                mock.patch.object(app_module, "load_history", return_value=items):
            first = await app_module.history()
            second = await app_module.history(offset=10, limit=10)
            last = await app_module.history(offset=20, limit=10)

        self.assertEqual(len(first["items"]), 10)
        self.assertEqual(first["next_offset"], 10)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["total"], 25)

        self.assertEqual(len(second["items"]), 10)
        self.assertEqual(second["next_offset"], 20)
        self.assertTrue(second["has_more"])

        self.assertEqual(len(last["items"]), 5)
        self.assertEqual(last["next_offset"], 25)
        self.assertFalse(last["has_more"])

    async def test_history_rejects_invalid_pagination(self):
        for offset, limit in ((-1, 10), (0, 0), (0, app_module.MAX_HISTORY + 1)):
            with self.subTest(offset=offset, limit=limit):
                with self.assertRaises(HTTPException) as raised:
                    await app_module.history(offset=offset, limit=limit)
                self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
