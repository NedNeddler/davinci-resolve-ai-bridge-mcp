"""Windows sharing retries for atomic JSON publication.

These tests run on Linux. Windows is simulated by stubbing the host check and
by raising OSError values that carry the Win32 codes Python reports for a
failed MoveFileEx. os.name is left unchanged: setting it to "nt" makes pathlib
build WindowsPath and abort on Linux. Nothing here opens a live Resolve
project or a real Windows file lock.
"""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge import lifecycle


def windows_error(winerror, exc_type=None, errno=None):
    """Build the OSError a Windows os.replace would raise.

    CPython on Linux drops the winerror constructor slot, so the code is
    attached afterwards. Production code only reads it with getattr.
    """
    if exc_type is None:
        exc_type = PermissionError if winerror in (5, 32, 33, 65) else OSError
    if errno is None:
        errno = 13 if issubclass(exc_type, PermissionError) else 22
    exc = exc_type(errno, "windows replace failed", "temp.json", None, "target.json")
    exc.winerror = winerror
    return exc


class WindowsReplaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "lua-inbox.json"
        self.previous = '{"previous": 1}'
        self.path.write_text(self.previous, encoding="utf-8")
        self.real_replace = os.replace

    def _temps(self):
        return list(self.path.parent.glob(".lua-inbox.json.*.tmp"))

    def _scripted(self, failures, succeed=True):
        """Return an os.replace stand-in that fails with the given exceptions first."""
        pending = list(failures)
        sources = []
        snapshots = []
        temp_hashes = []

        def replace(source, destination):
            sources.append(source)
            snapshots.append(Path(destination).read_bytes())
            temp_hashes.append(Path(source).read_bytes())
            if pending:
                raise pending.pop(0)
            if not succeed:
                raise AssertionError("replace called after the scripted failures")
            self.real_replace(source, destination)

        replace.sources = sources
        replace.snapshots = snapshots
        replace.temp_hashes = temp_hashes
        return replace

    def _publish(self, replace, windows=True):
        with patch.object(lifecycle, "_windows_host", return_value=windows), \
                patch.object(lifecycle.os, "replace", side_effect=replace), \
                patch.object(lifecycle.time, "sleep") as sleep, \
                patch.object(lifecycle.time, "monotonic", side_effect=AssertionError("monotonic used")):
            lifecycle.atomic_json(self.path, {"request": "signed", "n": 1})
        return sleep

    def test_pause_budget_is_bounded_and_not_a_spin(self):
        pauses = lifecycle._WINDOWS_REPLACE_ATTEMPTS - 1
        self.assertGreaterEqual(lifecycle._WINDOWS_REPLACE_PAUSE_SECONDS, 0.001)
        self.assertLessEqual(
            pauses * lifecycle._WINDOWS_REPLACE_PAUSE_SECONDS,
            0.3,
        )
        self.assertIn(5, lifecycle._WINDOWS_RETRYABLE_REPLACE_WINERRORS)
        self.assertNotIn(65, lifecycle._WINDOWS_RETRYABLE_REPLACE_WINERRORS)

    def test_first_replace_publishes_without_sleeping(self):
        replace = self._scripted([])
        sleep = self._publish(replace, windows=False)
        self.assertEqual(sleep.call_count, 0)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"request": "signed", "n": 1})
        self.assertEqual(self._temps(), [])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_windows_access_denied_retries_same_temp_then_publishes(self):
        replace = self._scripted([windows_error(5), windows_error(5), windows_error(5)])
        sleep = self._publish(replace)
        self.assertEqual(len(replace.sources), 4)
        self.assertEqual(len(set(replace.sources)), 1)
        self.assertEqual(len(set(replace.temp_hashes)), 1)
        self.assertTrue(all(snapshot == self.previous.encode() for snapshot in replace.snapshots[:3]))
        self.assertEqual(sleep.call_count, 3)
        self.assertTrue(all(call.args == (lifecycle._WINDOWS_REPLACE_PAUSE_SECONDS,) for call in sleep.call_args_list))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"request": "signed", "n": 1})
        self.assertNotEqual(self.path.read_text(encoding="utf-8"), self.previous)
        self.assertEqual(self._temps(), [])

    def test_windows_sharing_lock_and_mapped_section_are_retryable(self):
        for winerror, exc_type in ((32, PermissionError), (33, PermissionError), (1224, OSError)):
            with self.subTest(winerror=winerror):
                self.path.write_text(self.previous, encoding="utf-8")
                replace = self._scripted([windows_error(winerror, exc_type=exc_type)])
                sleep = self._publish(replace)
                self.assertEqual(sleep.call_count, 1)
                self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["n"], 1)
                self.assertEqual(self._temps(), [])

    def test_posix_permission_error_is_not_retried(self):
        replace = self._scripted([windows_error(5)])
        with patch.object(lifecycle, "_windows_host", return_value=False), \
                patch.object(lifecycle.os, "replace", side_effect=replace), \
                patch.object(lifecycle.time, "sleep") as sleep:
            with self.assertRaises(PermissionError) as caught:
                lifecycle.atomic_json(self.path, {"request": "signed"})
        self.assertEqual(caught.exception.winerror, 5)
        self.assertEqual(len(replace.sources), 1)
        self.assertEqual(sleep.call_count, 0)
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.previous)
        self.assertEqual(self._temps(), [])

    def test_nonretryable_windows_errors_fail_immediately(self):
        cases = (
            ("sharing-unrelated-acl", windows_error(65)),
            ("disk-full", windows_error(112, exc_type=OSError, errno=28)),
            ("missing-temp", windows_error(2, exc_type=FileNotFoundError, errno=2)),
            ("no-winerror", PermissionError(13, "Permission denied")),
            ("plain-io", OSError(5, "Input/output error")),
        )
        for name, exc in cases:
            with self.subTest(name=name):
                self.path.write_text(self.previous, encoding="utf-8")
                replace = self._scripted([exc])
                with patch.object(lifecycle, "_windows_host", return_value=True), \
                        patch.object(lifecycle.os, "replace", side_effect=replace), \
                        patch.object(lifecycle.time, "sleep") as sleep:
                    with self.assertRaises(OSError):
                        lifecycle.atomic_json(self.path, {"request": "signed"})
                self.assertEqual(len(replace.sources), 1)
                self.assertEqual(sleep.call_count, 0)
                self.assertEqual(self.path.read_text(encoding="utf-8"), self.previous)
                self.assertEqual(self._temps(), [])

    def test_persistent_windows_access_denied_stops_at_bound(self):
        calls = []

        def always_denied(source, destination):
            calls.append(source)
            self.assertEqual(Path(destination).read_text(encoding="utf-8"), self.previous)
            raise windows_error(5)

        with patch.object(lifecycle, "_windows_host", return_value=True), \
                patch.object(lifecycle.os, "replace", side_effect=always_denied), \
                patch.object(lifecycle.time, "sleep") as sleep:
            with self.assertRaises(PermissionError) as caught:
                lifecycle.atomic_json(self.path, {"request": "signed"})
        self.assertEqual(caught.exception.winerror, 5)
        self.assertEqual(len(calls), lifecycle._WINDOWS_REPLACE_ATTEMPTS)
        self.assertEqual(len(set(calls)), 1)
        self.assertEqual(sleep.call_count, lifecycle._WINDOWS_REPLACE_ATTEMPTS - 1)
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.previous)
        self.assertEqual(self._temps(), [])

    def test_interrupt_during_pause_removes_temp_and_keeps_destination(self):
        def fail_once(source, destination):
            raise windows_error(5)

        with patch.object(lifecycle, "_windows_host", return_value=True), \
                patch.object(lifecycle.os, "replace", side_effect=fail_once), \
                patch.object(lifecycle.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                lifecycle.atomic_json(self.path, {"request": "signed"})
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.previous)
        self.assertEqual(self._temps(), [])


if __name__ == "__main__":
    unittest.main()
