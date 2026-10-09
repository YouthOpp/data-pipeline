"""One real non-publishing Study in Denmark funding collection test."""

import json
import sys
import unittest

import adapter


class LiveAdapterTest(unittest.TestCase):
    def test_live_collection(self):
        records = adapter.collect()
        adapter.validate_records(records)
        self.assertTrue(records)
        self.assertEqual(
            len(records), len({record["id"] for record in records})
        )
        print(json.dumps(records, ensure_ascii=False, indent=2))
        print(
            f"Validated {len(records)} Study in Denmark " "opportunities",
            file=sys.stderr,
        )


if __name__ == "__main__":
    unittest.main()
