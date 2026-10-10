"""Run one complete live collection without publishing or saving its records."""

import json
import unittest

import adapter


class TestFRSE(unittest.TestCase):
    def test_complete_public_collection(self):
        records = adapter.collect()
        adapter.validate_records(records)
        self.assertEqual(len(records), 53)
        self.assertEqual(len({record["id"] for record in records}), 53)
        self.assertTrue(
            all(record["source"] == adapter.SOURCE_ID for record in records)
        )
        print(json.dumps(records, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
