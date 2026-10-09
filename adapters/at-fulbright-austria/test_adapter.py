"""One complete real non-publishing Fulbright Austria collection test."""

import json
import sys
import unittest

import adapter


class LiveAdapterTest(unittest.TestCase):
    def test_live_collection(self):
        records = adapter.collect()
        adapter.validate_records(records)
        self.assertEqual(len(records), 45)
        self.assertEqual(len({record["id"] for record in records}), 45)
        print(json.dumps(records, ensure_ascii=False, indent=2))
        print(
            "Validated 45 Fulbright Austria awards and teaching positions",
            file=sys.stderr,
        )


if __name__ == "__main__":
    unittest.main()
