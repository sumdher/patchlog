"""
patchlog integration and unit tests.

All tests use a temporary PATCHLOG_DIR so they don't touch /var/lib/patchlog
and don't require root.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Allow importing patchlog.core directly for pure-function unit tests.
sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pl(*args, patchlog_dir, expect_success=True):
    env = {**os.environ, "PATCHLOG_DIR": str(patchlog_dir)}
    r = subprocess.run(["patchlog"] + list(args), env=env, capture_output=True, text=True)
    if expect_success and r.returncode != 0:
        raise AssertionError(
            f"patchlog {' '.join(args)} exited {r.returncode}\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
    return r


class PatchlogBase(unittest.TestCase):
    """Sets up a fresh temp store per test."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = Path(self._td.name) / "store"
        self.pl("init")

    def tearDown(self):
        if (self.store / ".active").exists():
            self.pl("abandon", expect_success=False)
        self._td.cleanup()

    def pl(self, *args, expect_success=True):
        return _pl(*args, patchlog_dir=self.store, expect_success=expect_success)

    def _db(self):
        return json.loads((self.store / "sessions.json").read_text())

    def _session(self, label="s"):
        return next(s for s in self._db()["sessions"] if s["label"] == label)

    def _tmpfile(self, name="target.conf", content="original\n"):
        p = Path(self._td.name) / name
        p.write_text(content)
        return p


# ---------------------------------------------------------------------------
# cmd_track: existing file
# ---------------------------------------------------------------------------

class TestTrackExistingFile(PatchlogBase):

    def test_adds_to_modified_files(self):
        target = self._tmpfile()
        self.pl("start", "s", "--note=x")
        self.pl("track", str(target))

        session = self._session()
        self.assertEqual(len(session["modified_files"]), 1)
        self.assertEqual(session["modified_files"][0]["path"], str(target))
        self.assertEqual(session["new_files"], [])

    def test_snapshot_file_exists(self):
        target = self._tmpfile()
        self.pl("start", "s", "--note=x")
        self.pl("track", str(target))

        snap_rel = self._session()["modified_files"][0]["snapshot"]
        snap_full = self.store / snap_rel
        self.assertTrue(snap_full.exists(), f"snapshot not on disk: {snap_full}")
        self.assertEqual(snap_full.read_text(), "original\n")

    def test_double_track_no_duplicate(self):
        target = self._tmpfile()
        self.pl("start", "s", "--note=x")
        self.pl("track", str(target))
        self.pl("track", str(target))

        self.assertEqual(len(self._session()["modified_files"]), 1)


# ---------------------------------------------------------------------------
# cmd_track: non-existent file (the /etc/motd bug fix)
# ---------------------------------------------------------------------------

class TestTrackNonExistentFile(PatchlogBase):

    def test_registers_as_new_file_not_modified(self):
        ghost = Path(self._td.name) / "ghost.conf"
        self.assertFalse(ghost.exists())

        self.pl("start", "s", "--note=x")
        result = self.pl("track", str(ghost))

        session = self._session()
        self.assertIn(str(ghost), session["new_files"])
        self.assertEqual(session["modified_files"], [])

    def test_prints_helpful_note(self):
        ghost = Path(self._td.name) / "ghost.conf"
        self.pl("start", "s", "--note=x")
        result = self.pl("track", str(ghost))

        self.assertIn("Registered as new file", result.stdout)

    def test_exits_zero(self):
        ghost = Path(self._td.name) / "ghost.conf"
        self.pl("start", "s", "--note=x")
        result = _pl("track", str(ghost), patchlog_dir=self.store)
        self.assertEqual(result.returncode, 0)

    def test_double_track_nonexistent_no_duplicate(self):
        ghost = Path(self._td.name) / "ghost.conf"
        self.pl("start", "s", "--note=x")
        self.pl("track", str(ghost))
        self.pl("track", str(ghost))

        self.assertEqual(self._session()["new_files"].count(str(ghost)), 1)


# ---------------------------------------------------------------------------
# Round-trip: track existing → modify → undo restores
# ---------------------------------------------------------------------------

class TestUndoRestoresFile(PatchlogBase):

    def test_file_restored_to_original(self):
        target = self._tmpfile(content="before\n")
        self.pl("start", "s", "--note=x")
        self.pl("track", str(target))

        target.write_text("after\n")
        self.assertEqual(target.read_text(), "after\n")

        self.pl("stop")
        self.pl("undo", "s")

        self.assertEqual(target.read_text(), "before\n")

    def test_export_shows_modified_files_populated(self):
        target = self._tmpfile(content="before\n")
        self.pl("start", "s", "--note=x")
        self.pl("track", str(target))
        target.write_text("after\n")
        self.pl("stop")

        result = self.pl("export", "s")
        data = json.loads(result.stdout)
        self.assertEqual(len(data["modified_files"]), 1)
        self.assertEqual(data["modified_files"][0]["path"], str(target))


# ---------------------------------------------------------------------------
# Round-trip: new-file → create → undo deletes
# ---------------------------------------------------------------------------

class TestUndoDeletesNewFile(PatchlogBase):

    def test_explicit_new_file_deleted_on_undo(self):
        target = Path(self._td.name) / "created.sh"
        self.pl("start", "s", "--note=x")
        self.pl("new-file", str(target))
        target.write_text("#!/bin/sh\necho hi\n")
        self.assertTrue(target.exists())

        self.pl("stop")
        self.pl("undo", "s")

        self.assertFalse(target.exists())


# ---------------------------------------------------------------------------
# Round-trip: track non-existent file → create → undo deletes (the motd scenario)
# ---------------------------------------------------------------------------

class TestUndoDeletesTrackedNonExistentFile(PatchlogBase):

    def test_motd_scenario_file_deleted_on_undo(self):
        """
        Mirrors the /etc/motd bug:
        - patchlog track <path>   (path doesn't exist yet)
        - <create the file>
        - patchlog stop
        - patchlog undo           → file should be deleted
        """
        target = Path(self._td.name) / "motd_sim.txt"
        self.assertFalse(target.exists())

        self.pl("start", "s", "--note=x")
        result = self.pl("track", str(target))
        self.assertIn("Registered as new file", result.stdout)

        target.write_text("patchlog test environment\n")

        self.pl("stop")
        self.pl("undo", "s")

        self.assertFalse(target.exists(), "undo should delete the file that was created after track")

    def test_export_shows_new_files_populated(self):
        target = Path(self._td.name) / "motd_sim.txt"
        self.pl("start", "s", "--note=x")
        self.pl("track", str(target))
        target.write_text("content\n")
        self.pl("stop")

        data = json.loads(self.pl("export", "s").stdout)
        self.assertIn(str(target), data["new_files"])
        self.assertEqual(data["modified_files"], [])

    def test_undo_of_nonexistent_file_is_harmless(self):
        """File was never created — undo file_delete step should not error."""
        target = Path(self._td.name) / "never_created.txt"
        self.pl("start", "s", "--note=x")
        self.pl("track", str(target))
        # Don't create the file
        self.pl("stop")
        self.pl("undo", "s")  # should not crash


# ---------------------------------------------------------------------------
# build_teardown ordering (pure unit tests — no subprocess)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Directory lifecycle: session-created dirs removed on undo
# ---------------------------------------------------------------------------

class TestUndoRemovesSessionCreatedDir(PatchlogBase):

    def test_new_dir_deleted_when_empty_after_undo(self):
        """new-file in a dir that didn't exist → dir removed on undo."""
        new_dir = Path(self._td.name) / "myapp"
        target = new_dir / "binary"
        self.assertFalse(new_dir.exists())

        self.pl("start", "s", "--note=x")
        self.pl("new-file", str(target))
        new_dir.mkdir()
        target.write_text("#!/bin/sh\n")
        self.pl("stop")
        self.pl("undo", "s")

        self.assertFalse(target.exists(), "file should be deleted")
        self.assertFalse(new_dir.exists(), "empty session-created dir should be removed")

    def test_preexisting_dir_preserved_after_undo(self):
        """new-file in an already-existing dir → dir left alone on undo."""
        existing_dir = Path(self._td.name) / "existing"
        existing_dir.mkdir()
        target = existing_dir / "binary"

        self.pl("start", "s", "--note=x")
        self.pl("new-file", str(target))
        target.write_text("#!/bin/sh\n")
        self.pl("stop")
        self.pl("undo", "s")

        self.assertFalse(target.exists(), "file should be deleted")
        self.assertTrue(existing_dir.exists(), "pre-existing dir must not be removed")

    def test_nonempty_dir_preserved_after_undo(self):
        """Session-created dir that still has other files is left alone."""
        new_dir = Path(self._td.name) / "shared"
        target = new_dir / "pl-file"
        other = new_dir / "other-file"

        self.pl("start", "s", "--note=x")
        self.pl("new-file", str(target))
        new_dir.mkdir()
        target.write_text("#!/bin/sh\n")
        other.write_text("I was here too\n")
        self.pl("stop")
        self.pl("undo", "s")

        self.assertFalse(target.exists(), "registered file should be deleted")
        self.assertTrue(new_dir.exists(), "non-empty dir must not be removed")
        self.assertTrue(other.exists(), "unrelated file must survive")

    def test_new_dirs_recorded_in_session(self):
        """new_dirs field is populated for non-existent parent dirs."""
        new_dir = Path(self._td.name) / "myapp"
        target = new_dir / "bin"

        self.pl("start", "s", "--note=x")
        self.pl("new-file", str(target))

        session = self._session()
        self.assertIn(str(new_dir), session["new_dirs"])


from patchlog.core import build_teardown  # noqa: E402


class TestBuildTeardownOrdering(unittest.TestCase):

    def _types(self, steps):
        return [s["type"] for s in steps]

    def test_systemd_disable_before_file_delete(self):
        diff = {
            "systemd_units_added": ["test.service"],
            "watched_files_added": ["/etc/systemd/system/test.service"],
        }
        steps = build_teardown(diff, [], [])
        types = self._types(steps)
        self.assertLess(types.index("systemd_disable"), types.index("file_delete"))

    def test_file_restore_before_file_delete(self):
        diff = {"watched_files_added": ["/tmp/newfile"]}
        modified = [{"path": "/tmp/existing", "snapshot": "snapshots/s/orig__tmp_existing__abc"}]
        steps = build_teardown(diff, modified_files=modified, new_files=[])
        types = self._types(steps)
        self.assertLess(types.index("file_restore"), types.index("file_delete"))

    def test_file_delete_before_apt_remove(self):
        diff = {
            "watched_files_added": ["/usr/local/bin/test-tool"],
            "apt_packages_added": ["test-pkg"],
        }
        steps = build_teardown(diff, [], [])
        types = self._types(steps)
        self.assertLess(types.index("file_delete"), types.index("apt_remove"))

    def test_daemon_reload_after_disable_and_delete(self):
        diff = {
            "systemd_units_added": ["foo.service"],
            "watched_files_added": ["/etc/systemd/system/foo.service"],
        }
        steps = build_teardown(diff, [], [])
        reload_idx = next(
            i for i, s in enumerate(steps)
            if s["type"] == "run_command" and "daemon-reload" in s.get("command", [])
        )
        types = self._types(steps)
        self.assertGreater(reload_idx, types.index("systemd_disable"))
        self.assertGreater(reload_idx, types.index("file_delete"))

    def test_explicit_new_files_not_duplicated_with_watched(self):
        """A path in both new_files and watched_files_added should produce one delete step."""
        path = "/usr/local/bin/my-tool"
        diff = {"watched_files_added": [path]}
        steps = build_teardown(diff, [], new_files=[path])
        delete_steps = [s for s in steps if s["type"] == "file_delete" and s["path"] == path]
        self.assertEqual(len(delete_steps), 1)

    def test_no_steps_when_nothing_changed(self):
        steps = build_teardown({}, [], [])
        self.assertEqual(steps, [])


if __name__ == "__main__":
    unittest.main()
