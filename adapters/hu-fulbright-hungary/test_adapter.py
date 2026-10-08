"""One live collection test; no publication or output files."""

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
            all(record["status"] in ("open", "unknown") for record in records)
        )
        print(json.dumps(records, ensure_ascii=False, indent=2))
        print(
            f"Validated {len(records)} Fulbright Hungary opportunities",
            file=sys.stderr,
        )


if __name__ == "__main__":
    unittest.main()
