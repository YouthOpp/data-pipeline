"""Run one complete nonpublishing live collection; stdout stays in memory."""

import json
import unittest

import adapter


class TestFMFI(unittest.TestCase):
    def test_complete_public_collection(self):
        records = adapter.collect()
        adapter.validate_records(records)
        self.assertEqual(len(records), 15)
        self.assertEqual(len({record["id"] for record in records}), 15)
        self.assertTrue(
            all(record["source"] == adapter.SOURCE_ID for record in records)
        )
        print(json.dumps(records, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
