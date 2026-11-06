"""Hash guards for the one-file runtime installer. Uses temporary directories only."""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "apply_windows_inbox_replace_fix.py"
spec = importlib.util.spec_from_file_location("apply_windows_inbox_replace_fix", SCRIPT)
apply_fix_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply_fix_tool)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ApplyWindowsInboxReplaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name) / "runtime"
        bridge = self.runtime / "bridge"
        bridge.mkdir(parents=True)
        original = (ROOT / "tools" / "original" / "lifecycle.py").read_bytes()
        (bridge / "lifecycle.py").write_bytes(original)
        (bridge / "operations.py").write_text(
            '"""fixture"""\nAGENT_VERSION = "1.14.1"\nPROTOCOL_VERSION = 2\n',
            encoding="utf-8",
        )
        (self.runtime / "token.txt").write_text("rab_fixture_do_not_touch\n", encoding="utf-8")
        (self.runtime / "lua-inbox.json").write_text("{}\n", encoding="utf-8")
        cache = bridge / "__pycache__"
        cache.mkdir()
        (cache / "lifecycle.cpython-313.pyc").write_bytes(b"stale-lifecycle")
        (cache / "operations.cpython-313.pyc").write_bytes(b"keep-operations")

    def test_constants_match_v1_14_1_and_the_patched_tree(self):
        original = subprocess.check_output(
            ["git", "show", "5a8bba25cefbe8917dec10aca69828f9c02394ee:bridge/lifecycle.py"],
            cwd=ROOT,
        )
        self.assertEqual(hashlib.sha256(original).hexdigest(), apply_fix_tool.ORIGINAL_SHA256)
        self.assertEqual((ROOT / "tools" / "original" / "lifecycle.py").read_bytes(), original)
        patched = (ROOT / "bridge" / "lifecycle.py").read_bytes()
        self.assertEqual(hashlib.sha256(patched).hexdigest(), apply_fix_tool.PATCHED_SHA256)
        self.assertNotEqual(original, patched)

    def test_apply_replaces_only_lifecycle_and_keeps_a_backup(self):
        result = apply_fix_tool.apply_fix(self.runtime)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(_sha(self.runtime / "bridge" / "lifecycle.py"), apply_fix_tool.PATCHED_SHA256)
        backup = Path(result["backup"])
        self.assertEqual(_sha(backup), apply_fix_tool.ORIGINAL_SHA256)
        manifest = json.loads(backup.with_name("manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["relative_path"], "bridge/lifecycle.py")
        self.assertEqual(manifest["sha256"], apply_fix_tool.ORIGINAL_SHA256)
        self.assertEqual((self.runtime / "token.txt").read_text(encoding="utf-8"), "rab_fixture_do_not_touch\n")
        self.assertEqual((self.runtime / "lua-inbox.json").read_text(encoding="utf-8"), "{}\n")
        self.assertIn('AGENT_VERSION = "1.14.1"', (self.runtime / "bridge" / "operations.py").read_text(encoding="utf-8"))
        self.assertFalse((self.runtime / "bridge" / "__pycache__" / "lifecycle.cpython-313.pyc").exists())
        self.assertTrue((self.runtime / "bridge" / "__pycache__" / "operations.cpython-313.pyc").exists())
        again = apply_fix_tool.apply_fix(self.runtime)
        self.assertEqual(again["status"], "already-applied")
        self.assertEqual(len(list((self.runtime / "backup").rglob("lifecycle.py"))), 1)

    def test_dry_run_and_hash_mismatches_do_not_write(self):
        before = (self.runtime / "bridge" / "lifecycle.py").read_bytes()
        dry = apply_fix_tool.apply_fix(self.runtime, dry_run=True)
        self.assertEqual(dry["status"], "dry-run")
        self.assertEqual((self.runtime / "bridge" / "lifecycle.py").read_bytes(), before)
        self.assertFalse((self.runtime / "backup").exists())

        (self.runtime / "bridge" / "lifecycle.py").write_bytes(before + b"\n")
        with self.assertRaises(apply_fix_tool.ApplyError) as caught:
            apply_fix_tool.apply_fix(self.runtime)
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse((self.runtime / "backup").exists())
        (self.runtime / "bridge" / "lifecycle.py").write_bytes(before)

        (self.runtime / "bridge" / "operations.py").write_text('AGENT_VERSION = "1.14.0"\n', encoding="utf-8")
        with self.assertRaises(apply_fix_tool.ApplyError) as caught:
            apply_fix_tool.apply_fix(self.runtime)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual((self.runtime / "bridge" / "lifecycle.py").read_bytes(), before)

    def test_symlink_and_bad_payload_are_refused(self):
        link = self.runtime / "bridge" / "linked-lifecycle.py"
        link.symlink_to(self.runtime / "bridge" / "lifecycle.py")
        os.replace(link, self.runtime / "bridge" / "lifecycle.py")
        with self.assertRaises(apply_fix_tool.ApplyError) as caught:
            apply_fix_tool.apply_fix(self.runtime)
        self.assertEqual(caught.exception.code, 2)
        self.assertTrue((self.runtime / "bridge" / "lifecycle.py").is_symlink())

        # Restore a regular original file, then point the payload at unexpected bytes.
        (self.runtime / "bridge" / "lifecycle.py").unlink()
        (self.runtime / "bridge" / "lifecycle.py").write_bytes((ROOT / "tools" / "original" / "lifecycle.py").read_bytes())
        bogus = Path(self.tmp.name) / "bogus.py"
        bogus.write_bytes(b"print('no')\n")
        with patch.object(apply_fix_tool, "payload_path", return_value=bogus):
            with self.assertRaises(apply_fix_tool.ApplyError) as caught:
                apply_fix_tool.apply_fix(self.runtime)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(_sha(self.runtime / "bridge" / "lifecycle.py"), apply_fix_tool.ORIGINAL_SHA256)

    def test_failed_replace_leaves_the_original_installed(self):
        real = os.replace

        def fail_target(source, destination):
            if Path(destination).parent == self.runtime / "bridge":
                raise OSError(13, "busy")
            real(source, destination)

        with patch.object(apply_fix_tool.os, "replace", side_effect=fail_target):
            with self.assertRaises(apply_fix_tool.ApplyError) as caught:
                apply_fix_tool.apply_fix(self.runtime)
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(_sha(self.runtime / "bridge" / "lifecycle.py"), apply_fix_tool.ORIGINAL_SHA256)
        self.assertTrue((self.runtime / "bridge" / "__pycache__" / "lifecycle.cpython-313.pyc").exists())

    def test_rollback_uses_backup_then_embedded_original(self):
        apply_fix_tool.apply_fix(self.runtime)
        dry = apply_fix_tool.rollback(self.runtime, dry_run=True)
        self.assertEqual(dry["status"], "dry-run-rollback")
        self.assertEqual(_sha(self.runtime / "bridge" / "lifecycle.py"), apply_fix_tool.PATCHED_SHA256)
        restored = apply_fix_tool.rollback(self.runtime)
        self.assertEqual(restored["status"], "rolled-back")
        self.assertEqual(_sha(self.runtime / "bridge" / "lifecycle.py"), apply_fix_tool.ORIGINAL_SHA256)
        self.assertEqual(_sha(restored["patched_backup"]), apply_fix_tool.PATCHED_SHA256)
        self.assertEqual(apply_fix_tool.rollback(self.runtime)["status"], "already-original")

        apply_fix_tool.apply_fix(self.runtime)
        for path in (self.runtime / "backup").rglob("lifecycle.py"):
            path.unlink()
        restored = apply_fix_tool.rollback(self.runtime)
        self.assertEqual(restored["status"], "rolled-back")
        self.assertEqual(_sha(self.runtime / "bridge" / "lifecycle.py"), apply_fix_tool.ORIGINAL_SHA256)
        self.assertIn("original", restored["source"].replace("\\", "/"))

    def test_rollback_refuses_an_unexpected_installed_file(self):
        (self.runtime / "bridge" / "lifecycle.py").write_bytes(b"not-the-bridge\n")
        with self.assertRaises(apply_fix_tool.ApplyError) as caught:
            apply_fix_tool.rollback(self.runtime)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual((self.runtime / "bridge" / "lifecycle.py").read_bytes(), b"not-the-bridge\n")
        self.assertFalse((self.runtime / "backup").exists())

    def test_cli_refuses_without_writing(self):
        (self.runtime / "bridge" / "operations.py").write_text("AGENT_VERSION = \"9.9.9\"\n", encoding="utf-8")
        finished = subprocess.run(
            [sys.executable, str(SCRIPT), "--runtime", str(self.runtime), "--dry-run"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(finished.returncode, 2)
        self.assertIn("refusing", finished.stderr)
        self.assertEqual(_sha(self.runtime / "bridge" / "lifecycle.py"), apply_fix_tool.ORIGINAL_SHA256)
        self.assertFalse((self.runtime / "backup").exists())


if __name__ == "__main__":
    unittest.main()
