"""Run one complete nonpublishing live collection; stdout stays in memory."""

import json
import contextlib
import importlib.util
import os
import pathlib
import tempfile
import types
import unittest.mock
import unittest

import adapter


class TestIPDJ(unittest.TestCase):
    def test_complete_public_collection(self):
        records = adapter.collect()
        adapter.validate_records(records)
        self.assertEqual(len(records), 245)
        self.assertEqual(len({record["id"] for record in records}), 245)
        self.assertTrue(all(record["source"] == adapter.SOURCE_ID
                            for record in records))
        print(json.dumps(records, ensure_ascii=False))


@contextlib.contextmanager
def isolated_state_adapter():
    """Exercise the own runtime against real owned temporary state, never HTTP."""
    with tempfile.TemporaryDirectory(prefix="publisher-state-regression-") as directory:
        spec = importlib.util.spec_from_file_location(
            "isolated_own_adapter", adapter.__file__
        )
        local = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(local)
        local.tempfile = types.SimpleNamespace(gettempdir=lambda: directory, mkstemp=tempfile.mkstemp)
        try:
            yield local, pathlib.Path(directory) / (local.FAMILY + "-" + str(os.getuid()))
        finally:
            if local._COLLECTION_LOCK_FD is not None:
                os.close(local._COLLECTION_LOCK_FD)


class TestDurablePublisherState(unittest.TestCase):
    def test_fresh_collection_initializes_both_locks_before_retirement(self):
        with isolated_state_adapter() as (local, leaf):
            # This state regression does not inspect legacy/default-family paths
            # or contact GitHub; the own native initialization remains real.
            local.preserve_legacy_pacing = lambda: None
            with unittest.mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
                local.prepare_collection()
            self.assertFalse(local._STATE_NEW)
            self.assertTrue((leaf / "collection.lock").is_file())
            self.assertTrue((leaf / "state.lock").is_file())
            self.assertTrue((leaf / "state.json").is_file())
            state = local.load_budget()
            self.assertFalse(state["blocked"])
            self.assertGreaterEqual(state["not_before"], local._COLLECTION_STARTED + 60)

    def test_new_family_save_retires_missing_state_allowance(self):
        with isolated_state_adapter() as (local, leaf):
            local.budget_file()
            self.assertTrue(local._STATE_NEW)
            with local.locked_budget():
                state = local.load_budget()
                state.update(
                    blocked=True, not_before=local.time.time() + 600
                )
                local.save_budget(state)
            self.assertFalse(local._STATE_NEW)
            self.assertEqual(local.load_budget(), state)
            (leaf / "state.json").unlink()
            with self.assertRaises(local.AdapterError):
                local.load_budget()

    def test_existing_family_missing_state_or_locks_fails_closed(self):
        with isolated_state_adapter() as (local, leaf):
            leaf.mkdir(mode=0o700)
            local.budget_file()
            self.assertFalse(local._STATE_NEW)
            with self.assertRaises(local.AdapterError):
                local.load_budget()
            with self.assertRaises(FileNotFoundError):
                local.locked_budget()
            with self.assertRaises((local.AdapterError, FileNotFoundError)):
                local.prepare_collection()
            self.assertFalse((leaf / "state.lock").exists())
            self.assertFalse((leaf / "collection.lock").exists())

    def test_restart_preserves_refusal_and_future_embargo(self):
        with isolated_state_adapter() as (local, leaf):
            local.budget_file()
            with local.locked_budget():
                state = local.load_budget()
                state.update(
                    blocked=True, not_before=local.time.time() + 600
                )
                local.save_budget(state)
            local._STATE_PATH = local._STATE_LOCK = None
            local.budget_file()
            self.assertFalse(local._STATE_NEW)
            self.assertEqual(local.load_budget(), state)
            (leaf / "state.json").unlink()
            with self.assertRaises(local.AdapterError):
                local.load_budget()

    def test_symlink_and_hardlink_state_refuse_without_changing_original(self):
        with isolated_state_adapter() as (local, leaf):
            local.budget_file()
            with local.locked_budget():
                local.save_budget(local.load_budget())
            saved = leaf / "saved-state"
            (leaf / "state.json").rename(saved)
            original = saved.read_bytes()
            os.symlink(saved, leaf / "state.json")
            with self.assertRaises(OSError):
                local.load_budget()
            (leaf / "state.json").unlink()
            os.link(saved, leaf / "state.json")
            with self.assertRaises(local.AdapterError):
                local.load_budget()
            self.assertEqual(saved.read_bytes(), original)

    def test_failed_mature_save_preserves_state_and_retired_allowance(self):
        with isolated_state_adapter() as (local, leaf):
            local.budget_file()
            with local.locked_budget():
                local.save_budget(local.load_budget())
            original = (leaf / "state.json").read_bytes()
            with unittest.mock.patch.object(os, "fsync", side_effect=OSError("Isolated file fsync failure")):
                with self.assertRaises(OSError):
                    local.save_budget(local.empty_budget(local.time.time()))
            self.assertFalse(local._STATE_NEW)
            self.assertEqual((leaf / "state.json").read_bytes(), original)
            (leaf / "state.json").unlink()
            with self.assertRaises(local.AdapterError):
                local.load_budget()


if __name__ == "__main__":
    unittest.main()
