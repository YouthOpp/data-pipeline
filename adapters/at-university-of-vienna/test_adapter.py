"""Collect and validate the complete real inventory without publication."""

import json
import sys
import unittest

import adapter


class LiveAdapterTest(unittest.TestCase):
    def test_live_collection(self):
        records = adapter.collect()
        adapter.validate_records(records)
        self.assertTrue(records)
        self.assertTrue(
            all(
                record["attribution"] == adapter.ATTRIBUTION
                for record in records
            )
        )
        print(json.dumps(records, ensure_ascii=False))
        print(
            f"{len(records)} genuine exchange allocations validated",
            file=sys.stderr,
        )


if __name__ == "__main__":
    unittest.main()
