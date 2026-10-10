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


class TestFiniteNews195Diagnostic(unittest.TestCase):
    def test_target_headers_only_never_reads_or_follows_redirect(self):
        with isolated_state_adapter() as (local, leaf):
            local._DIAGNOSTIC_MODE = True
            response = unittest.mock.MagicMock()
            response.__enter__.return_value = response
            response.status = 302
            response.headers = {"Location": "https://user:private@unreviewed.example/path?secret=private"}
            response.read.side_effect = AssertionError("Diagnostic body read")
            with unittest.mock.patch.object(local, "request_timeout", return_value=1), unittest.mock.patch.object(local, "pace"), unittest.mock.patch.object(local, "check_deadline"), unittest.mock.patch.object(local, "record_retry_after"), unittest.mock.patch.object(local, "publisher_attempt", return_value=contextlib.nullcontext()), unittest.mock.patch.object(local._OPENER, "open", return_value=response) as opened:
                status, headers, body = local.request_bytes(local._DIAGNOSTIC_URL, publisher=True, headers_only=True)
                self.assertEqual((status, body), (302, b""))
                self.assertEqual(local.diagnostic_location(headers["Location"]), "https://unreviewed.example/path")
                opened.assert_called_once()
                response.read.assert_not_called()
                self.assertEqual(local._DIAGNOSTIC_STARTS, 1)

    def test_third_physical_start_and_normal_mode_header_request_refused(self):
        with isolated_state_adapter() as (local, leaf):
            with unittest.mock.patch.object(local._OPENER, "open") as opened:
                with self.assertRaises(local.AdapterError):
                    local.request_bytes(local._DIAGNOSTIC_URL, publisher=True, headers_only=True)
                local._DIAGNOSTIC_MODE = True
                local._DIAGNOSTIC_STARTS = 2
                with unittest.mock.patch.object(local, "check_deadline"):
                    with self.assertRaises(local.AdapterError):
                        local.request_bytes(local._DIAGNOSTIC_URL, publisher=True, headers_only=True)
                opened.assert_not_called()

    def test_policy_failure_exports_state_without_target_or_publication(self):
        with isolated_state_adapter() as (local, leaf):
            with unittest.mock.patch.object(local, "phase", return_value=contextlib.nullcontext()), unittest.mock.patch.object(local, "prepare_collection"), unittest.mock.patch.object(local, "check_robots", side_effect=local.AdapterError("Policy refusal", "access")), unittest.mock.patch.object(local, "request_bytes") as requested, unittest.mock.patch.object(local, "export_family_artifact") as exported, unittest.mock.patch.object(local, "publish_files") as published:
                self.assertEqual(local.run_news195_diagnostic(), 1)
                requested.assert_not_called()
                exported.assert_called_once()
                published.assert_not_called()
                self.assertFalse(local._DIAGNOSTIC_MODE)


    def test_robots_redirect_refused_without_follow(self):
        with isolated_state_adapter() as (local, leaf):
            local._DIAGNOSTIC_MODE = True
            response = unittest.mock.MagicMock()
            response.__enter__.return_value = response
            response.status = 302
            response.headers = {"Location": "/robots-replacement"}
            with unittest.mock.patch.object(local, "request_timeout", return_value=1), unittest.mock.patch.object(local, "pace"), unittest.mock.patch.object(local, "check_deadline"), unittest.mock.patch.object(local, "record_retry_after"), unittest.mock.patch.object(local, "publisher_attempt", return_value=contextlib.nullcontext()), unittest.mock.patch.object(local._OPENER, "open", return_value=response) as opened:
                with self.assertRaises(local.AdapterError):
                    local.check_robots(local._DIAGNOSTIC_URL)
                opened.assert_called_once()
                response.read.assert_not_called()

    def test_503_header_capture_does_not_retry_and_preserves_retry_after(self):
        with isolated_state_adapter() as (local, leaf):
            local._DIAGNOSTIC_MODE = True
            response = unittest.mock.MagicMock()
            response.__enter__.return_value = response
            response.status = 503
            response.headers = {"Retry-After": "120"}
            with unittest.mock.patch.object(local, "request_timeout", return_value=1), unittest.mock.patch.object(local, "pace"), unittest.mock.patch.object(local, "check_deadline"), unittest.mock.patch.object(local, "record_retry_after") as embargo, unittest.mock.patch.object(local, "publisher_attempt", return_value=contextlib.nullcontext()), unittest.mock.patch.object(local._OPENER, "open", return_value=response) as opened:
                self.assertEqual(local.request_bytes(local._DIAGNOSTIC_URL, publisher=True, headers_only=True)[0], 503)
                embargo.assert_called_once_with("120")
                opened.assert_called_once()
                response.read.assert_not_called()

    def test_export_failure_makes_diagnostic_unsuccessful(self):
        with isolated_state_adapter() as (local, leaf):
            with unittest.mock.patch.object(local, "phase", return_value=contextlib.nullcontext()), unittest.mock.patch.object(local, "prepare_collection"), unittest.mock.patch.object(local, "check_robots"), unittest.mock.patch.object(local, "request_bytes", return_value=(200, {}, b"")), unittest.mock.patch.object(local, "export_family_artifact", side_effect=OSError("Export finalization failure")):
                self.assertEqual(local.run_news195_diagnostic(), 1)
                self.assertFalse(local._DIAGNOSTIC_MODE)


if __name__ == "__main__":
    unittest.main()
