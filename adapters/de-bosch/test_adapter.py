"""The sole actual nonpublishing Bosch collection test."""

import contextlib
import importlib.util
import json
import os
import pathlib
import tempfile
import types
import unittest
import unittest.mock
import sys

import adapter


def main():
    records = adapter.collect()
    adapter.validate_records(records)
    print(
        f"Validated {len(records)} real Bosch programme overviews",
        file=sys.stderr,
    )
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
        local.tempfile = types.SimpleNamespace(gettempdir=lambda: directory)
        try:
            yield local, pathlib.Path(directory) / (local.FAMILY + "-" + str(os.getuid()))
        finally:
            if local._STATE_DIR_FD is not None:
                os.close(local._STATE_DIR_FD)
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
            self.assertFalse(local._STATE_INITIALIZING)
            self.assertTrue((leaf / "collection.lock").is_file())
            self.assertTrue((leaf / "state.lock").is_file())
            self.assertTrue((leaf / "state.json").is_file())
            state = local.load_budget()
            self.assertFalse(state["blocked"])
            self.assertGreaterEqual(state["not_before"], local._COLLECTION_STARTED + 60)

    def test_new_family_save_retires_missing_state_allowance(self):
        with isolated_state_adapter() as (local, leaf):
            local.budget_file()
            self.assertTrue(local._STATE_INITIALIZING)
            with local.locked_budget():
                state = local.load_budget()
                state.update(
                    blocked=True, interval=120, not_before=local.time.time() + 600
                )
                local.save_budget(state)
            self.assertFalse(local._STATE_INITIALIZING)
            self.assertEqual(local.load_budget(), state)
            (leaf / "state.json").unlink()
            with self.assertRaises(local.AdapterError):
                local.load_budget()

    def test_existing_family_missing_state_or_locks_fails_closed(self):
        with isolated_state_adapter() as (local, leaf):
            leaf.mkdir(mode=0o700)
            local.budget_file()
            self.assertFalse(local._STATE_INITIALIZING)
            with self.assertRaises(local.AdapterError):
                local.load_budget()
            with self.assertRaises(FileNotFoundError):
                local.locked_budget()
            with self.assertRaises((local.AdapterError, FileNotFoundError)):
                local.prepare_collection()
            self.assertFalse((leaf / "state.lock").exists())
            self.assertFalse((leaf / "collection.lock").exists())

    def test_restart_preserves_refusal_and_stronger_interval(self):
        with isolated_state_adapter() as (local, leaf):
            local.budget_file()
            with local.locked_budget():
                state = local.load_budget()
                state.update(
                    blocked=True, interval=120, not_before=local.time.time() + 600
                )
                local.save_budget(state)
            os.close(local._STATE_DIR_FD)
            local._STATE_DIR_FD = local._STATE_DIR_ID = None
            local._STATE_PATH = local._STATE_LOCK = None
            local.budget_file()
            self.assertFalse(local._STATE_INITIALIZING)
            self.assertEqual(local.load_budget(), state)
            (leaf / "state.json").unlink()
            with self.assertRaises(local.AdapterError):
                local.load_budget()

    def test_replaced_family_leaf_refuses_all_state_operations(self):
        with isolated_state_adapter() as (local, leaf):
            local.budget_file()
            leaf.rename(leaf.with_name(leaf.name + "-original"))
            leaf.mkdir(mode=0o700)
            for operation in (local.budget_file, local.load_budget, local.locked_budget):
                with self.assertRaises(local.AdapterError):
                    operation()
            with self.assertRaises(local.AdapterError):
                local.save_budget(local.empty_budget(local.time.time()))

    def test_directory_fsync_failure_does_not_restore_fresh_allowance(self):
        with isolated_state_adapter() as (local, leaf):
            local.budget_file()
            actual_fsync = os.fsync

            def fail_directory_fsync(descriptor):
                if descriptor == local._STATE_DIR_FD:
                    raise OSError("Isolated directory fsync failure")
                return actual_fsync(descriptor)

            with unittest.mock.patch.object(os, "fsync", fail_directory_fsync):
                with self.assertRaises(OSError):
                    local.save_budget(local.empty_budget(local.time.time()))
            self.assertFalse(local._STATE_INITIALIZING)
            (leaf / "state.json").unlink()
            with self.assertRaises(local.AdapterError):
                local.load_budget()


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestDurablePublisherState)
    if not unittest.TextTestRunner(stream=sys.stderr).run(suite).wasSuccessful():
        raise SystemExit(1)
    main()
