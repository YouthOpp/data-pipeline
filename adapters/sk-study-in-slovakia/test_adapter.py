"""The single real, nonpublishing collection test for this adapter."""
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("saia_adapter", Path(__file__).with_name("adapter.py"))
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class LiveCollectionTest(unittest.TestCase):
    def test_complete_public_collection(self):
        with adapter.execution():
            try:
                with adapter.phase("prepublication", adapter.COLLECTION_TIMEOUT):
                    started = adapter.utc_now()
                    records = adapter.collect()
                    completed = adapter.utc_now()
                    adapter.fresh_records(records, [], started, completed)
                    adapter.validate_records(records)
                    self.assertEqual(len(records), 348)
                    outcome = adapter.metadata(started, {}, records, checked_at=completed)
                    adapter.validate_prior_pair(records, outcome)
                    print(json.dumps({"records": records, "metadata": outcome}, ensure_ascii=False))
            finally:
                with adapter.phase("export", adapter.EXPORT_TIMEOUT):
                    adapter.export_family_artifact()


if __name__ == "__main__":
    unittest.main()
