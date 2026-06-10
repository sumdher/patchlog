"""
patchlog.core
~~~~~~~~~~~~~
System state snapshots, diffing, teardown building and execution.
No external dependencies — pure Python 3.8+ stdlib only.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Store layout
# ---------------------------------------------------------------------------

STORE = Path(os.environ.get("PATCHLOG_DIR", "/var/lib/patchlog"))
DB_FILE = STORE / "sessions.json"
SNAPSHOTS_DIR = STORE / "snapshots"
ACTIVE_FILE = STORE / ".active"   # contains label of in-progress session
_LOCK_FILE = STORE / ".lock"

# Directories whose file listings are snapshotted before/after.
# Net-new files found here are automatically added to the teardown as file_delete steps.
_WATCHED_DIRS = [
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/local/lib",
    "/etc/systemd/system",
    "/opt",             # flat only — files directly in /opt, not in subdirs like /opt/myapp/
]


# ---------------------------------------------------------------------------
# DB helpers + file locking
# ---------------------------------------------------------------------------

@contextmanager
def db_lock():
    """Exclusive lock on the patchlog store. Use around every load+modify+save sequence."""
    STORE.mkdir(parents=True, exist_ok=True)
    _LOCK_FILE.touch()
    with open(_LOCK_FILE) as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def load_db() -> dict:
    if DB_FILE.exists():
        with open(DB_FILE) as f:
            return json.load(f)
    return {"sessions": []}


def save_db(db: dict):
    STORE.mkdir(parents=True, exist_ok=True)
    # Use pid in tmp name to avoid collision if two processes write simultaneously.
    tmp = DB_FILE.with_name(f"sessions.tmp.{os.getpid()}")
    with open(tmp, "w") as f:
        json.dump(db, f, indent=2)
    tmp.replace(DB_FILE)


def get_session(db: dict, label: str) -> Optional[dict]:
    for s in db["sessions"]:
        if s["label"] == label:
            return s
    return None


def active_label() -> Optional[str]:
    if ACTIVE_FILE.exists():
        return ACTIVE_FILE.read_text().strip() or None
    return None


def set_active(label: Optional[str]):
    if label is None:
        ACTIVE_FILE.unlink(missing_ok=True)
    else:
        ACTIVE_FILE.write_text(label)


# ---------------------------------------------------------------------------
# System state snapshot
# ---------------------------------------------------------------------------

def capture_state() -> dict:
    """
    Capture a point-in-time snapshot of system state.
    All fields are lists/strings so they diff cleanly.
    """
    return {
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "apt_packages":      _apt_packages(),
        "systemd_units":     _systemd_units(),
        "dkms_modules":      _dkms_modules(),
        "modprobe_configs":  _list_dir_files("/etc/modprobe.d"),
        "modules_load_d":    _list_dir_files("/etc/modules-load.d"),
        "udev_rules":        _list_dir_files("/etc/udev/rules.d"),
        "sysctl_d":          _list_dir_files("/etc/sysctl.d"),
        "grub_cmdline":      _grub_cmdline(),
        "initramfs_mtimes":  _initramfs_mtimes(),
        "ufw_rules":         _ufw_rules(),
        "cron_entries":      _cron_entries(),
        "watched_dirs":      _watched_dir_files(),
    }


def _run(cmd: list, **kwargs) -> str:
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, **kwargs)
    except FileNotFoundError:
        return ""   # command not installed — not an error
    except Exception:
        return ""


def _apt_packages() -> list:
    out = _run(["dpkg-query", "-W", "-f=${Package}\n"])
    return sorted(out.strip().splitlines())


def _systemd_units() -> list:
    out = _run(["systemctl", "list-unit-files", "--state=enabled",
                "--no-legend", "--plain"])
    return sorted(line.split()[0] for line in out.strip().splitlines() if line.strip())


def _dkms_modules() -> list:
    out = _run(["dkms", "status"])
    seen: set = set()
    modules = []
    for line in out.strip().splitlines():
        m = re.match(r"^([^,\s]+/[^,\s]+)", line)
        if m:
            key = m.group(1).strip()
            if key not in seen:
                seen.add(key)
                modules.append(key)
    return sorted(modules)


def _list_dir_files(path: str) -> list:
    """List regular files in a directory, sorted. Returns [] if missing or unreadable."""
    d = Path(path)
    if not d.exists():
        return []
    try:
        return sorted(str(p) for p in d.iterdir() if p.is_file())
    except PermissionError:
        return []


def _watched_dir_files() -> dict:
    """Snapshot file listings of directories that install scripts commonly write to."""
    result: dict = {}
    for d in _WATCHED_DIRS:
        p = Path(d)
        if p.exists():
            try:
                result[d] = sorted(str(f) for f in p.iterdir() if f.is_file())
            except PermissionError:
                result[d] = []
    return result


def _grub_cmdline() -> str:
    grub = Path("/etc/default/grub")
    if not grub.exists():
        return ""
    for line in grub.read_text().splitlines():
        if line.startswith("GRUB_CMDLINE_LINUX_DEFAULT"):
            return line.strip()
    return ""


def _initramfs_mtimes() -> dict:
    boot = Path("/boot")
    if not boot.exists():
        return {}
    result: dict = {}
    for f in boot.glob("initrd.img-*"):
        try:
            result[str(f)] = f.stat().st_mtime
        except OSError:
            pass
    return result


def _ufw_rules() -> list:
    """
    Return UFW rules as content strings WITHOUT rule numbers.

    ufw status numbered shows:
        [ 1] 22/tcp   ALLOW IN   Anywhere

    We strip the '[ N]' prefix before storing, so that:
    - rule ordering changes don't produce false diffs
    - teardown can match by content (rule numbers shift as rules are deleted)
    """
    out = _run(["ufw", "status", "numbered"])
    rules = []
    for line in out.splitlines():
        stripped = line.strip()
        if re.match(r'^\[\s*\d+\]', stripped):
            content = re.sub(r'^\[\s*\d+\]\s*', '', stripped)
            if content:
                rules.append(content)
    return sorted(rules)


def _cron_entries() -> list:
    out = _run(["crontab", "-l"])
    return [l for l in out.splitlines() if l.strip() and not l.startswith("#")]


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def diff_states(before: dict, after: dict) -> dict:
    """Compute what changed between two state snapshots."""

    def added(key):
        return sorted(set(after.get(key, [])) - set(before.get(key, [])))

    def removed(key):
        return sorted(set(before.get(key, [])) - set(after.get(key, [])))

    grub_changed = before.get("grub_cmdline") != after.get("grub_cmdline")
    initramfs_changed = before.get("initramfs_mtimes") != after.get("initramfs_mtimes")

    # Watched dirs: collect net-new files across all watched directories
    before_dirs = before.get("watched_dirs", {})
    after_dirs = after.get("watched_dirs", {})
    watched_files_added: list = []
    for d in _WATCHED_DIRS:
        before_set = set(before_dirs.get(d, []))
        after_set = set(after_dirs.get(d, []))
        watched_files_added.extend(sorted(after_set - before_set))

    return {
        "apt_packages_added":        added("apt_packages"),
        "apt_packages_removed":      removed("apt_packages"),
        "systemd_units_added":       added("systemd_units"),
        "systemd_units_removed":     removed("systemd_units"),
        "dkms_modules_added":        added("dkms_modules"),
        "dkms_modules_removed":      removed("dkms_modules"),
        "modprobe_configs_added":    added("modprobe_configs"),
        "modprobe_configs_removed":  removed("modprobe_configs"),
        "modules_load_d_added":      added("modules_load_d"),
        "udev_rules_added":          added("udev_rules"),
        "sysctl_d_added":            added("sysctl_d"),
        "ufw_rules_added":           added("ufw_rules"),
        "ufw_rules_removed":         removed("ufw_rules"),
        "cron_entries_added":        added("cron_entries"),
        "watched_files_added":       watched_files_added,
        "grub_cmdline_changed":      grub_changed,
        "grub_cmdline_before":       before.get("grub_cmdline") if grub_changed else None,
        "grub_cmdline_after":        after.get("grub_cmdline") if grub_changed else None,
        "initramfs_updated":         initramfs_changed,
    }


def has_changes(diff: dict) -> bool:
    list_keys = [
        "apt_packages_added", "apt_packages_removed",
        "systemd_units_added", "dkms_modules_added",
        "modprobe_configs_added", "modules_load_d_added",
        "udev_rules_added", "sysctl_d_added",
        "ufw_rules_added", "cron_entries_added",
        "watched_files_added",
    ]
    return (
        any(diff.get(k) for k in list_keys)
        or diff.get("grub_cmdline_changed")
        or diff.get("initramfs_updated")
    )


# ---------------------------------------------------------------------------
# File snapshots
# ---------------------------------------------------------------------------

def snapshot_dir(label: str) -> Path:
    d = SNAPSHOTS_DIR / label
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshot_file_original(label: str, path: str) -> str:
    """
    Copy original content of a system file into the snapshot store.
    Returns the snapshot path relative to STORE (stored in DB).
    Call this BEFORE the session modifies the file.
    """
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(f"{path} does not exist")
    snap_dir = snapshot_dir(label)
    # Hash the full path to prevent name collisions between paths that flatten
    # identically (e.g. /etc/foo_bar and /etc/foo/bar both → etc_foo_bar).
    path_hash = hashlib.sha256(path.encode()).hexdigest()[:8]
    flat = path.replace("/", "_").lstrip("_")[:80]
    dest = snap_dir / f"orig__{flat}__{path_hash}"
    shutil.copy2(src, dest)
    return str(dest.relative_to(STORE))


def snapshot_script(label: str, url_or_path: str) -> str:
    """
    Snapshot a remote URL or local script for reproducibility.
    Returns path relative to STORE.
    """
    import urllib.request
    snap_dir = snapshot_dir(label)
    if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
        fname = url_or_path.rstrip("/").split("/")[-1] or "script"
        dest = snap_dir / f"remote__{fname}"
        urllib.request.urlretrieve(url_or_path, dest)
    else:
        src = Path(url_or_path)
        dest = snap_dir / f"local__{src.name}"
        shutil.copy2(src, dest)
    return str(dest.relative_to(STORE))


# ---------------------------------------------------------------------------
# Teardown sequence
# ---------------------------------------------------------------------------

def build_teardown(diff: dict, modified_files: list, new_files: list) -> list:
    """
    Build an ordered teardown sequence.

    Order:
      1.  Stop + disable systemd units      (before removing their files)
      2.  DKMS remove                       (before removing source)
      3.  modprobe config removal + rmmod
      4.  modules-load.d removal + rmmod
      5.  udev rules removal + udevadm reload
      6.  sysctl.d removal + sysctl --system
      7.  Restore modified system files     (original content from snapshot)
      8.  Delete new files                  (explicit + auto-detected from watched dirs)
      9.  apt remove net-new packages
      10. Remove added cron entries
      11. Remove added UFW rules
      12. systemctl daemon-reload           (whenever any systemd paths were touched)
      13. update-grub                       (if GRUB cmdline changed)
      14. update-initramfs                  (if initramfs was updated)
    """
    steps = []

    # 1. Stop + disable systemd units
    for unit in diff.get("systemd_units_added", []):
        steps.append({"type": "systemd_disable", "unit": unit})

    # 2. DKMS remove
    for mod in diff.get("dkms_modules_added", []):
        parts = mod.split("/", 1)
        steps.append({
            "type": "dkms_remove",
            "module": parts[0],
            "version": parts[1] if len(parts) == 2 else None,
        })

    # 3. modprobe config removal + rmmod
    for cfg in diff.get("modprobe_configs_added", []):
        steps.append({"type": "modprobe_config_remove", "file": cfg})

    # 4. modules-load.d removal + rmmod of listed modules
    for cfg in diff.get("modules_load_d_added", []):
        steps.append({"type": "modules_load_d_remove", "file": cfg})

    # 5. udev rules removal + udevadm reload
    udev_added = diff.get("udev_rules_added", [])
    if udev_added:
        steps.append({"type": "udev_rules_remove", "files": udev_added})

    # 6. sysctl.d removal + re-apply remaining settings
    for cfg in diff.get("sysctl_d_added", []):
        steps.append({"type": "sysctl_d_remove", "file": cfg})

    # 7. Restore modified system files (via 'patchlog track')
    for mf in modified_files:
        steps.append({
            "type": "file_restore",
            "path": mf["path"],
            "snapshot": mf["snapshot"],
        })

    # 8. Delete new files — both explicitly registered and auto-detected from watched dirs.
    # Deduplicate: explicit new_files take precedence; skip if already in that list.
    explicit_set = set(new_files)
    all_new_files = list(new_files)
    for f in diff.get("watched_files_added", []):
        if f not in explicit_set:
            all_new_files.append(f)
    for nf in all_new_files:
        steps.append({"type": "file_delete", "path": nf})

    # 9. apt remove net-new packages
    pkgs = diff.get("apt_packages_added", [])
    if pkgs:
        steps.append({"type": "apt_remove", "packages": pkgs})

    # 10. Remove added cron entries
    cron_added = diff.get("cron_entries_added", [])
    if cron_added:
        steps.append({"type": "cron_remove", "entries": cron_added})

    # 11. Remove added UFW rules
    ufw_added = diff.get("ufw_rules_added", [])
    if ufw_added:
        steps.append({"type": "ufw_delete", "rules": ufw_added})

    # 12. daemon-reload whenever any systemd-related path was touched
    systemd_touched = (
        bool(diff.get("systemd_units_added"))
        or any(f.startswith("/etc/systemd/") for f in diff.get("watched_files_added", []))
        or any(mf["path"].startswith("/etc/systemd/") for mf in modified_files)
    )
    if systemd_touched:
        steps.append({
            "type": "run_command",
            "command": ["systemctl", "daemon-reload"],
            "reason": "systemd unit files changed",
        })

    # 13. update-grub
    if diff.get("grub_cmdline_changed"):
        steps.append({
            "type": "run_command",
            "command": ["update-grub"],
            "reason": "GRUB cmdline was changed during session",
        })

    # 14. update-initramfs
    if diff.get("initramfs_updated"):
        steps.append({
            "type": "run_command",
            "command": ["update-initramfs", "-u", "-k", "all"],
            "reason": "initramfs was updated during session",
        })

    return steps


# ---------------------------------------------------------------------------
# Execute teardown
# ---------------------------------------------------------------------------

def execute_teardown(steps: list, dry_run: bool = False) -> list:
    results = []
    prefix = "[DRY-RUN] " if dry_run else ""

    for step in steps:
        t = step["type"]
        res = {"step": step, "success": None, "output": ""}

        try:
            if t == "systemd_disable":
                unit = step["unit"]
                print(f"  {prefix}systemctl stop + disable {unit}")
                if not dry_run:
                    subprocess.run(["systemctl", "stop", unit], capture_output=True)
                    r = subprocess.run(
                        ["systemctl", "disable", unit],
                        capture_output=True, text=True
                    )
                    # stop may fail if unit wasn't running — that's OK
                    res["success"] = r.returncode == 0
                    if not res["success"]:
                        res["output"] = (r.stdout + r.stderr).strip()
                else:
                    res["success"] = True

            elif t == "dkms_remove":
                mod, ver = step["module"], step.get("version")
                mod_label = f"{mod}/{ver}" if ver else mod
                print(f"  {prefix}dkms remove {mod_label} --all")
                if not dry_run:
                    # Stream so user sees dkms output (it can take a while)
                    r = subprocess.run(["dkms", "remove", mod_label, "--all"])
                    res["success"] = r.returncode == 0
                else:
                    res["success"] = True

            elif t == "modprobe_config_remove":
                cfg = step["file"]
                print(f"  {prefix}remove modprobe config {cfg}")
                if not dry_run:
                    try:
                        content = Path(cfg).read_text()
                        for line in content.splitlines():
                            m = re.match(
                                r"^\s*(?:install|softdep|alias|blacklist)\s+(\S+)", line
                            )
                            if m:
                                subprocess.run(
                                    ["modprobe", "-r", m.group(1)], capture_output=True
                                )
                    except Exception:
                        pass
                    Path(cfg).unlink(missing_ok=True)
                res["success"] = True

            elif t == "modules_load_d_remove":
                cfg = step["file"]
                print(f"  {prefix}remove modules-load.d entry {cfg}")
                if not dry_run:
                    try:
                        content = Path(cfg).read_text()
                        for line in content.splitlines():
                            mod_name = line.strip()
                            if mod_name and not mod_name.startswith("#"):
                                subprocess.run(
                                    ["modprobe", "-r", mod_name], capture_output=True
                                )
                    except Exception:
                        pass
                    Path(cfg).unlink(missing_ok=True)
                res["success"] = True

            elif t == "udev_rules_remove":
                files = step["files"]
                for f in files:
                    print(f"  {prefix}remove udev rule {f}")
                    if not dry_run:
                        Path(f).unlink(missing_ok=True)
                if not dry_run:
                    print(f"  reloading udev rules...")
                    subprocess.run(
                        ["udevadm", "control", "--reload-rules"], capture_output=True
                    )
                    subprocess.run(["udevadm", "trigger"], capture_output=True)
                res["success"] = True

            elif t == "sysctl_d_remove":
                cfg = step["file"]
                print(f"  {prefix}remove sysctl config {cfg}")
                if not dry_run:
                    Path(cfg).unlink(missing_ok=True)
                    # Re-apply all remaining sysctl configs so removed settings revert
                    subprocess.run(["sysctl", "--system"], capture_output=True)
                res["success"] = True

            elif t == "file_restore":
                path, snap = step["path"], step["snapshot"]
                snap_full = STORE / snap
                print(f"  {prefix}restore {path} from snapshot")
                if not dry_run:
                    if snap_full.exists():
                        shutil.copy2(snap_full, path)
                        res["success"] = True
                    else:
                        res["success"] = False
                        res["output"] = f"snapshot not found: {snap_full}"
                else:
                    res["success"] = True

            elif t == "file_delete":
                path = step["path"]
                print(f"  {prefix}delete {path}")
                if not dry_run:
                    Path(path).unlink(missing_ok=True)
                res["success"] = True

            elif t == "apt_remove":
                pkgs = step["packages"]
                print(f"  {prefix}apt remove --autoremove {' '.join(pkgs)}")
                if not dry_run:
                    # Stream so user sees apt progress
                    r = subprocess.run(
                        ["apt-get", "remove", "-y", "--autoremove"] + pkgs
                    )
                    res["success"] = r.returncode == 0
                else:
                    res["success"] = True

            elif t == "cron_remove":
                entries = step["entries"]
                print(f"  {prefix}remove {len(entries)} cron entry/entries")
                if not dry_run:
                    current = _run(["crontab", "-l"])
                    current_lines = current.splitlines()
                    entries_set = set(entries)
                    new_lines = [l for l in current_lines if l not in entries_set]
                    if new_lines != current_lines:
                        with tempfile.NamedTemporaryFile(
                            mode="w", suffix=".crontab", delete=False
                        ) as f:
                            f.write("\n".join(new_lines))
                            if new_lines:
                                f.write("\n")
                            fname = f.name
                        try:
                            r = subprocess.run(
                                ["crontab", fname], capture_output=True, text=True
                            )
                            res["success"] = r.returncode == 0
                            res["output"] = (r.stdout + r.stderr).strip()
                        finally:
                            os.unlink(fname)
                    else:
                        res["success"] = True  # already gone
                else:
                    for e in entries:
                        print(f"    would remove: {e}")
                    res["success"] = True

            elif t == "ufw_delete":
                rules = step["rules"]
                print(f"  {prefix}remove {len(rules)} UFW rule(s)")
                if not dry_run:
                    # Re-fetch numbered rules; delete from highest index to lowest
                    # so that earlier deletions don't shift the numbers we still need.
                    out = _run(["ufw", "status", "numbered"])
                    numbered = []
                    for line in out.splitlines():
                        stripped = line.strip()
                        m = re.match(r'^\[\s*(\d+)\]\s*(.*)', stripped)
                        if m:
                            numbered.append((int(m.group(1)), m.group(2).strip()))
                    rules_set = set(rules)
                    to_delete = sorted(
                        [num for num, content in numbered if content in rules_set],
                        reverse=True,
                    )
                    for num in to_delete:
                        print(f"    deleting UFW rule #{num}")
                        subprocess.run(
                            ["ufw", "--force", "delete", str(num)],
                            capture_output=True,
                        )
                    res["success"] = True
                else:
                    for rule in rules:
                        print(f"    would delete: {rule}")
                    res["success"] = True

            elif t == "run_command":
                cmd = step["command"]
                reason = step.get("reason", "")
                print(f"  {prefix}{' '.join(cmd)}  # {reason}")
                if not dry_run:
                    # Stream output (update-grub and update-initramfs take time)
                    r = subprocess.run(cmd)
                    res["success"] = r.returncode == 0
                else:
                    res["success"] = True

        except Exception as e:
            res["success"] = False
            res["output"] = str(e)
            print(f"  ERROR in step {t}: {e}", file=sys.stderr)

        results.append(res)

    return results


# ---------------------------------------------------------------------------
# Artifact existence check
# ---------------------------------------------------------------------------

def check_artifacts(session: dict) -> list:
    """
    Check whether each artifact from a session still exists.
    Returns list of {type, name, present, note} dicts.
    """
    diff = session.get("diff", {})
    results = []
    seen_paths: set = set()   # avoid double-reporting the same path

    installed_pkgs = set(_apt_packages())
    for pkg in diff.get("apt_packages_added", []):
        present = pkg in installed_pkgs
        results.append({
            "type": "apt_package", "name": pkg, "present": present,
            "note": "" if present else "not installed — may have been removed by upstream",
        })

    dkms_out = _run(["dkms", "status"])
    for mod in diff.get("dkms_modules_added", []):
        present = mod.split("/")[0] in dkms_out
        results.append({"type": "dkms_module", "name": mod, "present": present, "note": ""})

    for unit in diff.get("systemd_units_added", []):
        paths = [
            Path(f"/etc/systemd/system/{unit}"),
            Path(f"/usr/lib/systemd/system/{unit}"),
        ]
        present = any(p.exists() for p in paths)
        results.append({"type": "systemd_unit", "name": unit, "present": present, "note": ""})

    for cfg in diff.get("modprobe_configs_added", []):
        present = Path(cfg).exists()
        seen_paths.add(cfg)
        results.append({"type": "modprobe_config", "name": cfg, "present": present, "note": ""})

    for cfg in diff.get("udev_rules_added", []):
        present = Path(cfg).exists()
        seen_paths.add(cfg)
        results.append({"type": "udev_rule", "name": cfg, "present": present, "note": ""})

    for cfg in diff.get("sysctl_d_added", []):
        present = Path(cfg).exists()
        seen_paths.add(cfg)
        results.append({"type": "sysctl_config", "name": cfg, "present": present, "note": ""})

    for nf in session.get("new_files", []):
        present = Path(nf).exists()
        seen_paths.add(nf)
        results.append({"type": "new_file", "name": nf, "present": present, "note": ""})

    for nf in diff.get("watched_files_added", []):
        if nf not in seen_paths:
            present = Path(nf).exists()
            results.append({
                "type": "new_file", "name": nf, "present": present,
                "note": "(auto-detected)",
            })

    return results


# ---------------------------------------------------------------------------
# System info for AI assistant context
# ---------------------------------------------------------------------------

def gather_sysinfo() -> str:
    """
    Return a Markdown snippet describing the current system.
    Appended to the sysprompt so the AI has hardware/OS context.
    """
    lines = ["## Current system\n"]

    # OS
    os_name = _run(["lsb_release", "-ds"]).strip()
    if not os_name:
        try:
            for line in Path("/etc/os-release").read_text().splitlines():
                if line.startswith("PRETTY_NAME="):
                    os_name = line.split("=", 1)[1].strip('"')
                    break
        except Exception:
            os_name = "unknown"
    lines.append(f"- **OS:** {os_name}")

    # Kernel + architecture
    kernel = _run(["uname", "-r"]).strip()
    arch   = _run(["uname", "-m"]).strip()
    lines.append(f"- **Kernel:** {kernel}  ({arch})")

    # Hardware model (readable without root via sysfs)
    try:
        vendor  = Path("/sys/class/dmi/id/sys_vendor").read_text().strip()
        product = Path("/sys/class/dmi/id/product_name").read_text().strip()
        lines.append(f"- **Hardware:** {vendor} {product}")
    except Exception:
        pass

    # Desktop environment + display server
    desktop = os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION", "").strip()
    if desktop:
        display = "Wayland" if os.environ.get("WAYLAND_DISPLAY") else "X11" if os.environ.get("DISPLAY") else ""
        lines.append(f"- **Desktop:** {desktop}" + (f"  ({display})" if display else ""))

    # Active DKMS modules — directly relevant for driver fix context
    dkms = _run(["dkms", "status"]).strip()
    lines.append(f"- **DKMS modules:** {dkms if dkms else 'none'}")

    return "\n".join(lines)
