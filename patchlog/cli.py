#!/usr/bin/env python3
"""
NAME
    patchlog — track system changes so you can cleanly undo them later

SYNOPSIS
    patchlog <command> [args]
    sudo patchlog <command> [args]    # write commands need root

DESCRIPTION
    patchlog snapshots system state before and after a session, diffs the two
    snapshots, and builds an ordered teardown plan. Run 'undo' at any time to
    reverse everything in the right order: stop services, restore files, remove
    packages, reload daemons.

    Works with any AI assistant: run 'patchlog sysprompt' and paste the output
    as the first message. The AI wraps all its suggested commands automatically.

COMMANDS — Session management

    init
        Create /var/lib/patchlog and initialise the store. Run once with sudo.

    start <label> [--note=<text>] [--script=<url>] [--branch=<label>]
        Begin a session. Snapshots system state now.
        --note=<text>         One-line description of what you are doing.
        --script=<url|path>   Download and snapshot a remote/local script.
        --branch=<label>      Fork from an existing session's baseline — useful
                              for trial-and-error where each attempt diffs against
                              the same clean state.

    stop
        End the active session. Diffs system state, builds and saves the teardown
        plan. Nothing is undone yet.

    abandon
        Discard the active session without saving any teardown plan.

COMMANDS — Mid-session file tracking

    track <path>
        Snapshot a file's current content BEFORE you edit it. Call this
        immediately before the command that modifies the file — never after.
        If the path does not exist yet, it is registered as a new file instead
        (will be deleted on undo).

    new-file <path>
        Register a file you are about to create, so it is deleted on undo.
        Also records any parent directories that do not yet exist — they are
        removed on undo if empty after the file is deleted.
        Not needed for auto-tracked paths (see WHAT IS TRACKED below).

    note <text>
        Append a free-text note to the active session.

COMMANDS — Review and diagnosis

    status
        Show the active session label and elapsed time.

    list
        List all recorded sessions with date, status, and note.

    show <label>
        Print full detail: diff, tracked files, new files, teardown plan.

    check <label>
        Check whether each artifact from the session still exists on disk.
        Useful after 'apt upgrade' to see if upstream has incorporated a fix.

    export <label>
        Print the full session as JSON to stdout. Pipe to an AI assistant for
        diagnosis when something did not undo cleanly.
        Example:  patchlog export my-fix | xclip -selection clipboard

COMMANDS — Undo

    undo <label> [--dry-run]
        Reverse the session in teardown order:
            stop services → restore files → delete new files →
            remove new dirs (if empty) → apt remove → daemon-reload → update-grub
        --dry-run   Print what would happen without making any changes.

COMMANDS — Maintenance

    delete <label>
        Permanently remove a session record and its file snapshots from disk.

    sysprompt
        Print the AI assistant system prompt (includes current system info).
        Paste into any AI chat before asking for Linux help.

WHAT IS TRACKED AUTOMATICALLY
    These are diffed before/after the session — no 'new-file' needed:

      apt packages          net-new installs only
      systemd units         enabled/disabled state
      DKMS modules          any
      modprobe/modules-load /etc/modprobe.d/, /etc/modules-load.d/
      udev rules            /etc/udev/rules.d/
      sysctl configs        /etc/sysctl.d/
      binaries              /usr/local/bin/, /usr/local/sbin/, /usr/local/lib/
      opt (top-level)       /opt/ direct children only
      systemd unit files    /etc/systemd/system/
      boot                  GRUB cmdline, initramfs mtimes
      firewall / cron       UFW rules, crontab

    Requires 'track' or 'new-file' for everything else:
      /etc/fstab, /etc/default/grub, /etc/hosts, /etc/NetworkManager/...
      ~/ paths, /opt/<subdir>/file

EXAMPLES
    Basic workflow:
        sudo patchlog start speaker-fix --note="HDMI audio dropout fix"
        sudo patchlog track /etc/pulse/default.pa
        sudo nano /etc/pulse/default.pa
        sudo patchlog stop
        sudo patchlog undo speaker-fix

    Trial and error:
        sudo patchlog start attempt-1 --note="try approach A"
        sudo patchlog stop
        sudo patchlog undo attempt-1
        sudo patchlog start attempt-2 --branch=attempt-1
        sudo patchlog stop

    AI-assisted workflow:
        patchlog sysprompt        # paste output into ChatGPT / Claude first

    Dry-run before committing:
        sudo patchlog undo my-fix --dry-run

    Export for AI diagnosis:
        patchlog export broken-session | xclip -selection clipboard

STORE
    Default:  /var/lib/patchlog
    Override: set PATCHLOG_DIR environment variable

PERMISSIONS
    Write commands (need sudo):  init, start, stop, track, new-file, note,
                                 undo, abandon, delete
    Read commands (no sudo):     list, show, export, check, status, sysprompt
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


from patchlog.core import (
    STORE, DB_FILE, ACTIVE_FILE,
    db_lock,
    load_db, save_db, get_session,
    active_label, set_active,
    capture_state, diff_states, has_changes,
    snapshot_file_original, snapshot_script,
    collect_session_new_dirs,
    build_teardown, execute_teardown,
    check_artifacts, gather_sysinfo,
)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init():
    STORE.mkdir(parents=True, exist_ok=True)
    (STORE / "snapshots").mkdir(exist_ok=True)
    if not DB_FILE.exists():
        save_db({"sessions": []})
    print(f"patchlog initialised at {STORE}")


def cmd_start(args: list):
    label = None
    note = ""
    scripts = []
    branch_from = None

    for arg in args:
        if arg.startswith("--note="):
            note = arg[len("--note="):]
        elif arg.startswith("--script="):
            scripts.append(arg[len("--script="):])
        elif arg.startswith("--branch="):
            branch_from = arg[len("--branch="):]
        elif not label:
            label = arg

    if not label:
        _die("usage: patchlog start <label> [--note=...] [--script=...] [--branch=...]")

    current = active_label()
    if current:
        _die(f"session '{current}' is already active. Run 'patchlog stop' first.")

    with db_lock():
        db = load_db()
        if get_session(db, label):
            _die(f"session '{label}' already exists. Choose a different label.")

        # --branch: inherit the BEFORE state of the parent session so that each
        # trial-and-error attempt is diffed against the same clean baseline,
        # regardless of what was undone between attempts.
        parent_state = None
        if branch_from:
            parent = get_session(db, branch_from)
            if not parent:
                _die(f"branch source '{branch_from}' not found")
            parent_state = parent.get("state_before")
            if not parent_state:
                _die(f"branch source '{branch_from}' has no state_before (was it stopped?)")
            print(f"Branching from '{branch_from}' (using its pre-session baseline)")

        print(f"Capturing system state... ", end="", flush=True)
        state_before = parent_state if parent_state else capture_state()
        print("done.")

        # Snapshot scripts
        snapshotted_scripts = []
        for s in scripts:
            print(f"Snapshotting script: {s}")
            try:
                snap = snapshot_script(label, s)
                snapshotted_scripts.append({"source": s, "snapshot": snap})
                print(f"  -> {snap}")
            except Exception as e:
                print(f"  WARNING: could not snapshot {s}: {e}")

        session = {
            "label": label,
            "note": note,
            "status": "active",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "stopped_at": None,
            "branch_from": branch_from,
            "state_before": state_before,
            "state_after": None,
            "diff": None,
            "modified_files": [],   # files snapshotted via 'patchlog track'
            "new_files": [],        # files explicitly registered via 'patchlog new-file'
            "new_dirs": [],         # parent dirs that didn't exist at new-file/track time
            "scripts_snapshotted": snapshotted_scripts,
            "notes": [],   # populated by 'patchlog note' mid-session only
            "teardown_sequence": [],
            "undone": False,
        }

        db["sessions"].append(session)
        save_db(db)
        set_active(label)

    print(f"\nSession '{label}' started.")
    print(f"Do your work. When done: sudo patchlog stop")
    if scripts:
        print(f"Scripts snapshotted: {len(scripts)}")


def cmd_stop(args: list):
    label = active_label()
    if not label:
        _die("No active session. Start one with: patchlog start <label>")

    print(f"Stopping session '{label}'...")
    print(f"Capturing system state... ", end="", flush=True)
    state_after = capture_state()
    print("done.")

    with db_lock():
        db = load_db()
        session = get_session(db, label)
        if not session:
            _die(f"Active session '{label}' not found in DB. This is a bug.")

        diff = diff_states(session["state_before"], state_after)

        teardown = build_teardown(
            diff,
            session.get("modified_files", []),
            session.get("new_files", []),
            new_dirs=session.get("new_dirs", []),
            session_started_at=session.get("started_at"),
        )

        session["status"] = "complete"
        session["stopped_at"] = datetime.now(timezone.utc).isoformat()
        session["state_after"] = state_after
        session["diff"] = diff
        session["teardown_sequence"] = teardown

        save_db(db)
        set_active(None)

    print(f"\nSession '{label}' recorded.")
    _print_diff(diff)

    if teardown:
        print(f"\nTeardown plan: {len(teardown)} steps")
        for i, step in enumerate(teardown, 1):
            print(f"  {i:2}. {_describe_step(step)}")
    else:
        print("\nNo system-level changes detected.")
        if session.get("modified_files") or session.get("new_files"):
            print("(file-level changes tracked via 'patchlog track' / 'patchlog new-file')")


def cmd_track(args: list):
    """Snapshot a file's current content — call BEFORE editing it."""
    label = active_label()
    if not label:
        _die("No active session. Start one first.")

    if not args:
        _die("usage: patchlog track <path>")

    path = args[0]

    with db_lock():
        db = load_db()
        session = get_session(db, label)

        already = [mf for mf in session.get("modified_files", []) if mf["path"] == path]
        if already:
            print(f"'{path}' is already tracked in this session.")
            return

        try:
            snap = snapshot_file_original(label, path)
            session.setdefault("modified_files", []).append({
                "path": path,
                "snapshot": snap,
            })
            save_db(db)
        except FileNotFoundError:
            # File doesn't exist yet — register for deletion on undo instead of failing.
            # Common case: 'patchlog track /etc/motd' before 'tee /etc/motd' on a system
            # where the file is absent. Using _die/exit here causes silent data loss when
            # bash scripts don't have set -e.
            if path not in session.get("new_files", []):
                session.setdefault("new_files", []).append(path)
            new_dirs = collect_session_new_dirs(path, session.get("new_dirs", []))
            for d in new_dirs:
                session.setdefault("new_dirs", []).append(d)
            save_db(db)
            print(f"Note: '{path}' does not exist yet.")
            print(f"  Registered as new file — it will be deleted on undo.")
            return

    print(f"Tracked: {path}")
    print(f"  Original saved to: {snap}")
    print(f"  Now edit the file. It will be restored on undo.")


def cmd_new_file(args: list):
    """Register a file you are about to create, so it gets deleted on undo."""
    label = active_label()
    if not label:
        _die("No active session. Start one first.")
    if not args:
        _die("usage: patchlog new-file <path>")
    path = args[0]

    with db_lock():
        db = load_db()
        session = get_session(db, label)
        if path not in session.get("new_files", []):
            session.setdefault("new_files", []).append(path)
        new_dirs = collect_session_new_dirs(path, session.get("new_dirs", []))
        for d in new_dirs:
            session.setdefault("new_dirs", []).append(d)
        save_db(db)

    print(f"Registered new file: {path}  (will be deleted on undo)")


def cmd_note(args: list):
    """Append a free-text note to the active session."""
    label = active_label()
    if not label:
        _die("No active session.")
    if not args:
        _die("usage: patchlog note <text>")
    text = " ".join(args)

    with db_lock():
        db = load_db()
        session = get_session(db, label)
        session.setdefault("notes", []).append(text)
        save_db(db)

    print(f"Note added to '{label}'.")


def cmd_status():
    label = active_label()
    if not label:
        print("No active session.")
        return

    db = load_db()
    session = get_session(db, label)
    if not session:
        print(f"Active: {label}  (not found in DB — run 'patchlog abandon')")
        return

    started = session.get("started_at", "")
    elapsed = ""
    if started:
        try:
            dt = datetime.fromisoformat(started)
            secs = int((datetime.now(timezone.utc) - dt).total_seconds())
            elapsed = f"  ({secs // 60}m {secs % 60}s elapsed)"
        except Exception:
            pass

    print(f"Active session: {label}{elapsed}")
    if session.get("note"):
        print(f"Note: {session['note']}")
    tracked = session.get("modified_files", [])
    if tracked:
        print(f"Files tracked: {len(tracked)}")
        for mf in tracked:
            print(f"  {mf['path']}")
    new_files = session.get("new_files", [])
    if new_files:
        print(f"New files registered: {len(new_files)}")


def cmd_abandon():
    label = active_label()
    if not label:
        _die("No active session.")

    with db_lock():
        db = load_db()
        session = get_session(db, label)
        if session:
            db["sessions"] = [s for s in db["sessions"] if s["label"] != label]
            save_db(db)

    set_active(None)
    print(f"Session '{label}' abandoned and removed.")


def cmd_delete(args: list):
    """Permanently remove a completed session record and its snapshots from disk."""
    if not args:
        _die("usage: patchlog delete <label>")
    label = args[0]

    if active_label() == label:
        _die(f"'{label}' is the active session. Run 'patchlog abandon' instead.")

    with db_lock():
        db = load_db()
        session = get_session(db, label)
        if not session:
            _die(f"session '{label}' not found")

        if session.get("status") == "active":
            _die(f"'{label}' is still active. Stop or abandon it first.")

        db["sessions"] = [s for s in db["sessions"] if s["label"] != label]
        save_db(db)

    # Remove snapshot directory for this label if it exists
    snap_dir = STORE / "snapshots" / label
    if snap_dir.exists():
        shutil.rmtree(snap_dir)
        print(f"Deleted snapshots: {snap_dir}")

    print(f"Session '{label}' deleted.")


def cmd_list():
    db = load_db()
    sessions = db.get("sessions", [])
    if not sessions:
        print("No sessions recorded.")
        return

    current = active_label()
    print(f"{'':2} {'LABEL':<30} {'DATE':<12} {'STATUS':<10}  NOTE")
    print("-" * 75)
    for s in sessions:
        marker = "▶ " if s["label"] == current else "  "
        date = s.get("started_at", "")[:10]
        status = s.get("status", "")
        if s.get("undone"):
            status = "undone"
        note = (s.get("note") or "")[:35]
        print(f"{marker}{s['label']:<30} {date:<12} {status:<10}  {note}")


def cmd_show(args: list):
    if not args:
        _die("usage: patchlog show <label>")
    label = args[0]
    db = load_db()
    session = get_session(db, label)
    if not session:
        _die(f"session '{label}' not found")

    print(f"\n{'='*65}")
    print(f"Label:    {session['label']}")
    print(f"Status:   {session['status']}{' (undone)' if session.get('undone') else ''}")
    print(f"Note:     {session.get('note','')}")
    for n in (session.get("notes") or []):
        print(f"  + {n}")
    print(f"Started:  {session.get('started_at','')[:19].replace('T',' ')}")
    print(f"Stopped:  {(session.get('stopped_at') or '')[:19].replace('T',' ')}")
    if session.get("branch_from"):
        print(f"Branch:   forked from '{session['branch_from']}'")

    diff = session.get("diff") or {}
    if diff:
        print(f"\nSystem changes:")
        _print_diff(diff)

    mf = session.get("modified_files", [])
    if mf:
        print(f"\nTracked files (originals snapshotted):")
        for f in mf:
            print(f"  {f['path']}")
            print(f"    snapshot: {f['snapshot']}")

    nf = session.get("new_files", [])
    if nf:
        print(f"\nNew files (deleted on undo):")
        for f in nf:
            print(f"  {f}")

    nd = session.get("new_dirs", [])
    if nd:
        print(f"\nNew directories (rmdir on undo if empty and session-created):")
        for d in nd:
            print(f"  {d}")

    scripts = session.get("scripts_snapshotted", [])
    if scripts:
        print(f"\nScripts snapshotted:")
        for s in scripts:
            print(f"  {s['source']}")
            print(f"    -> {s['snapshot']}")

    td = session.get("teardown_sequence", [])
    if td:
        print(f"\nTeardown plan ({len(td)} steps):")
        for i, step in enumerate(td, 1):
            print(f"  {i:2}. {_describe_step(step)}")


def cmd_undo(args: list):
    dry_run = "--dry-run" in args
    label_args = [a for a in args if not a.startswith("--")]
    if not label_args:
        _die("usage: patchlog undo <label> [--dry-run]")
    label = label_args[0]

    db = load_db()
    session = get_session(db, label)
    if not session:
        _die(f"session '{label}' not found")
    if session.get("undone"):
        _die(f"session '{label}' is already marked as undone")
    if session.get("status") == "active":
        _die(f"session '{label}' is still active. Stop it first.")

    td = session.get("teardown_sequence", [])
    mf = session.get("modified_files", [])
    nf = session.get("new_files", [])
    if not td and not mf and not nf:
        print(f"Nothing to undo for '{label}'.")
        return

    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"\n{prefix}Undoing session: {label}")
    if session.get("note"):
        print(f"  ({session['note']})")
    print()

    results = execute_teardown(td, dry_run=dry_run)

    failed = [r for r in results if r["success"] is False]
    print()
    if failed:
        print(f"WARNING: {len(failed)} step(s) failed:")
        for r in failed:
            print(f"  ✗ {_describe_step(r['step'])}")
            if r["output"]:
                print(f"    {r['output'][:200]}")
    else:
        print(f"All {len(results)} steps completed {'(dry-run)' if dry_run else 'successfully'}.")

    if not dry_run:
        with db_lock():
            db = load_db()
            session = get_session(db, label)
            if session:
                session["undone"] = True
                session["undo_timestamp"] = datetime.now(timezone.utc).isoformat()
                save_db(db)
        print(f"Session '{label}' marked as undone.")


def cmd_check(args: list):
    if not args:
        _die("usage: patchlog check <label>")
    label = args[0]
    db = load_db()
    session = get_session(db, label)
    if not session:
        _die(f"session '{label}' not found")

    print(f"\nChecking artifacts for: {label}\n")
    artifacts = check_artifacts(session)

    if not artifacts:
        print("No trackable artifacts found for this session.")
        return

    all_present = all(a["present"] for a in artifacts)
    for a in artifacts:
        icon = "✓" if a["present"] else "✗"
        note = f"  ← {a['note']}" if a["note"] else ""
        print(f"  {icon} [{a['type']}] {a['name']}{note}")

    print()
    if all_present:
        print("All artifacts present — session is still active.")
    else:
        missing = sum(1 for a in artifacts if not a["present"])
        print(f"{missing} artifact(s) missing.")
        print("Upstream may have incorporated this fix natively.")
        print(f"If so, run: sudo patchlog undo {label}")


def cmd_export(args: list):
    if not args:
        _die("usage: patchlog export <label>")
    label = args[0]
    db = load_db()
    session = get_session(db, label)
    if not session:
        _die(f"session '{label}' not found")
    # Omit raw state blobs — too large and not useful for AI analysis
    out = {k: v for k, v in session.items()
           if k not in ("state_before", "state_after")}
    print(json.dumps(out, indent=2))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _die(msg: str):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _print_diff(diff: dict):
    def show(key, label):
        items = diff.get(key)
        if items:
            print(f"  {label}: {', '.join(items)}")

    show("apt_packages_added",       "apt added")
    show("apt_packages_removed",     "apt removed")
    show("dkms_modules_added",       "dkms added")
    show("systemd_units_added",      "systemd enabled")
    show("modprobe_configs_added",   "modprobe configs added")
    show("modules_load_d_added",     "modules-load.d added")
    show("udev_rules_added",         "udev rules added")
    show("sysctl_d_added",           "sysctl.d configs added")
    show("ufw_rules_added",          "ufw rules added")
    show("cron_entries_added",       "cron entries added")
    show("watched_files_added",      "new files (auto-detected)")

    if diff.get("grub_cmdline_changed"):
        print(f"  grub cmdline changed")
        print(f"    before: {diff['grub_cmdline_before']}")
        print(f"    after:  {diff['grub_cmdline_after']}")
    if diff.get("initramfs_updated"):
        print(f"  initramfs was updated")


def _describe_step(step: dict) -> str:
    t = step["type"]
    if t == "systemd_disable":
        return f"systemctl stop + disable {step['unit']}"
    elif t == "dkms_remove":
        return f"dkms remove {step['module']}/{step.get('version','?')} --all"
    elif t == "modprobe_config_remove":
        return f"rm modprobe config {step['file']} + modprobe -r"
    elif t == "modules_load_d_remove":
        return f"rm modules-load.d {step['file']} + modprobe -r modules"
    elif t == "udev_rules_remove":
        return f"rm udev rules {', '.join(step['files'])} + udevadm reload"
    elif t == "sysctl_d_remove":
        return f"rm sysctl config {step['file']} + sysctl --system"
    elif t == "file_restore":
        return f"restore {step['path']} from snapshot"
    elif t == "file_delete":
        return f"delete {step['path']}"
    elif t == "dir_delete":
        return f"rmdir {step['path']}  (if empty and session-created)"
    elif t == "apt_remove":
        return f"apt remove --autoremove {' '.join(step['packages'])}"
    elif t == "cron_remove":
        return f"remove {len(step['entries'])} cron entry/entries"
    elif t == "ufw_delete":
        return f"ufw delete {len(step['rules'])} rule(s)"
    elif t == "run_command":
        return f"{' '.join(step['command'])}  # {step.get('reason','')}"
    return str(step)


# ---------------------------------------------------------------------------
# AI assistant system prompt
# ---------------------------------------------------------------------------

# Paste this into any AI chat session (Claude, ChatGPT, etc.) before asking
# for help with Linux tasks. The AI will automatically wrap all system-changing
# commands it suggests inside patchlog session wrappers.
_SYSPROMPT = """\
## patchlog — system change tracking

patchlog tracks every system change so it can be cleanly undone later.
**You** are responsible for inserting patchlog wrappers in every response that involves
system changes. The user does not manage patchlog manually — you do it for them.

---

### Session lifecycle

Open a session **before** the first command in the workflow (including downloads):

```bash
sudo patchlog start <label> --note="<one-line task description>"
```

Use a short kebab-case label derived from the task, e.g.:
`hibernate-pre-hook`, `speaker-fix`, `custom-dns`, `ufw-homelab`, `grub-acpi-fix`

Close the session **after** the last command:

```bash
sudo patchlog stop
```

One session per logical task. Never open multiple sessions in one response.

---

### File tracking — order is critical

**Before editing** an existing system file:

```bash
sudo patchlog track <path>
```

**Before creating** a new file that is **not** in an auto-tracked path (see below):

```bash
sudo patchlog new-file <path>
```

> These must appear **immediately before** the command that modifies/creates the file —
> never after, never batched at the top.

---

### What patchlog tracks automatically

These are diffed before/after the session — **no `patchlog new-file` needed** for them:

| Category | Paths / scope |
|---|---|
| apt packages | net-new installs only |
| systemd units | enabled/disabled |
| DKMS modules | any |
| modprobe / modules-load | `/etc/modprobe.d/`, `/etc/modules-load.d/` |
| udev rules | `/etc/udev/rules.d/` |
| sysctl configs | `/etc/sysctl.d/` |
| binaries | `/usr/local/bin/`, `/usr/local/sbin/`, `/usr/local/lib/` |
| opt (top-level only) | `/opt/` direct children — `/opt/myapp/bin/x` needs `new-file` |
| systemd unit files | `/etc/systemd/system/` (even if not enabled) |
| boot / initramfs | GRUB cmdline, initramfs mtimes |
| firewall / cron | UFW rules, crontab |

---

### What requires manual tracking

Use `patchlog track` (for edits) or `patchlog new-file` (for new files) for anything
outside the table above:

- `/etc/default/grub`, `/etc/fstab`, `/etc/hosts`, `/etc/NetworkManager/…`
- `~/.local/bin/<binary>`, `~/.config/systemd/user/<unit>` — homedir paths
- `/opt/<subdir>/file` — anything nested inside an `/opt/` subdirectory
- Any bespoke path an install script writes to

---

### When to open a session (wrap these)

- `apt install` / `apt remove` / `dpkg -i`
- `systemctl enable` / `disable`
- Editing config files under `/etc/`, `/boot/`, `~/.config/`
- Creating files under `/usr/local/`, `/opt/`, `~/.local/bin/`, `/etc/systemd/system/`
- DKMS installs, ufw rule changes, `crontab -e`, `/etc/cron.d/` additions
- `update-grub`, `update-initramfs`
- Adding udev rules or sysctl configs
- Running any install script from the internet (`bash install.sh`, `curl | sh`, etc.)
- `wget` / `curl` / `git clone` that starts a fix workflow

### When NOT to open a session (skip these)

- `apt search`, `apt show`, `apt list`
- `systemctl status`, `journalctl`, `dmesg`, `lsblk`, `lspci`, `lsusb`, `lsmod`
- Read-only commands: `cat`, `less`, `grep`, `find`, `ls`

---

### Remote install script — snapshot it

```bash
sudo patchlog start <label> --note="..." --script="<url>"
```

---

### Full example

```bash
sudo patchlog start hibernate-pre-hook --note="custom script before hibernate"

sudo patchlog track /etc/systemd/sleep.conf
sudo nano /etc/systemd/sleep.conf

sudo tee /etc/systemd/system/pre-sleep.service > /dev/null << 'EOF'
[Unit]
Description=Pre-sleep hook
Before=sleep.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/pre-sleep.sh

[Install]
WantedBy=sleep.target
EOF

sudo tee /usr/local/bin/pre-sleep.sh > /dev/null << 'EOF'
#!/bin/bash
sync
EOF
sudo chmod +x /usr/local/bin/pre-sleep.sh

sudo systemctl daemon-reload
sudo systemctl enable pre-sleep.service

sudo patchlog stop
```

> `/etc/systemd/system/pre-sleep.service` and `/usr/local/bin/pre-sleep.sh` are
> auto-tracked — no `patchlog new-file` needed for them.

---

### After suggesting undo

After every `sudo patchlog undo <label>`, **always append** a verification block
so the user can confirm the teardown was complete. Tailor it to what the session
actually contained — only include checks relevant to what was undone:

```bash
# packages removed
dpkg -l <pkg> 2>/dev/null | grep -q ^ii && echo "WARNING: still installed" || echo "removed ✓"

# files gone
ls <path> 2>/dev/null && echo "WARNING: still present" || echo "gone ✓"

# service disabled
systemctl is-enabled <service> 2>/dev/null | grep -qv enabled && echo "disabled ✓" || echo "WARNING: still enabled"

# directories gone (if a new dir was created)
ls -ld <dir> 2>/dev/null && echo "still exists (may have other contents)" || echo "gone ✓"
```

Use `patchlog check <label>` as a quick one-liner when the session had many artifacts.

---

### Notes

- patchlog is already installed. Never suggest installing it.
- User says "undo" → `sudo patchlog undo <label>`
- Something broke after `apt upgrade` → `patchlog check <label>`
- Diagnose with AI → `patchlog export <label>`
"""


def cmd_sysprompt():
    print(_SYSPROMPT)
    print(gather_sysinfo())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]
    rest = args[1:]

    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        sys.exit(0)

    dispatch = {
        "init":       lambda: cmd_init(),
        "start":      lambda: cmd_start(rest),
        "stop":       lambda: cmd_stop(rest),
        "track":      lambda: cmd_track(rest),
        "new-file":   lambda: cmd_new_file(rest),
        "note":       lambda: cmd_note(rest),
        "status":     lambda: cmd_status(),
        "abandon":    lambda: cmd_abandon(),
        "delete":     lambda: cmd_delete(rest),
        "list":       lambda: cmd_list(),
        "show":       lambda: cmd_show(rest),
        "undo":       lambda: cmd_undo(rest),
        "check":      lambda: cmd_check(rest),
        "export":     lambda: cmd_export(rest),
        "sysprompt":  lambda: cmd_sysprompt(),
    }

    if cmd not in dispatch:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(__doc__)
        sys.exit(1)

    dispatch[cmd]()


if __name__ == "__main__":
    main()
