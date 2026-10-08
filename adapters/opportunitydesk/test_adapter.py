"""One non-publishing live collection check for this adapter."""
import unittest
import adapter


class LiveAdapterTest(unittest.TestCase):
    def test_live_collection(self):
        records = adapter.collect()
        adapter.validate_records(records)
        self.assertTrue(records)


if __name__ == "__main__":
    unittest.main()
