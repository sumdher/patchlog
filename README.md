# patchlog

Track system patches and customisations so you can cleanly undo them later.

Works seamlessly with AI assistants. See the [AI section](#using-with-ai-assistants)

**The model:** tell patchlog when you start doing something and when you stop.
It snapshots system state at both ends, diffs them, and builds a teardown plan.
When something breaks — or upstream ships a native fix — run `patchlog undo` and it
reverses everything in the right order.

Zero external dependencies. Pure Python 3.8+ stdlib.

---

## Install

```bash
git clone https://github.com/srsudhir31/patchlog
cd patchlog
pip install --break-system-packages -e .

# Initialise the store (once, needs root):
sudo patchlog init
```

---

## Workflow

```bash
sudo patchlog start <label> --note="what you're doing"
    # ... do your work freely ...
sudo patchlog stop
```
(*Alternatively, see the [AI section](#using-with-ai-assistants) instead of manually wrapping them*)

Between start and stop patchlog doesn't intercept anything — you work normally.
On stop it diffs system state and writes a teardown plan.

### Before editing an existing system file

```bash
sudo patchlog track /etc/systemd/sleep.conf   # snapshot before editing
sudo nano /etc/systemd/sleep.conf
```

### Before creating a file outside auto-tracked paths

```bash
sudo patchlog new-file ~/tools/my-script      # homedir paths need this
sudo patchlog new-file /opt/myapp/binary      # /opt subdirs need this
```

> Files in auto-tracked paths (see below) are detected automatically — no `new-file` needed.

### Trial and error

```bash
sudo patchlog start attempt-1 --note="try approach A"
sudo patchlog stop
sudo patchlog undo attempt-1

sudo patchlog start attempt-2 --branch=attempt-1   # same clean baseline
sudo patchlog stop
```

---

## What's tracked automatically

| Category | Paths |
|---|---|
| apt packages | net-new installs only |
| systemd units | enabled/disabled |
| DKMS modules | any |
| modprobe / modules-load | `/etc/modprobe.d/`, `/etc/modules-load.d/` |
| udev rules | `/etc/udev/rules.d/` |
| sysctl configs | `/etc/sysctl.d/` |
| binaries | `/usr/local/bin/`, `/usr/local/sbin/`, `/usr/local/lib/` |
| opt (top-level) | `/opt/` direct children — subdirs like `/opt/myapp/` need `new-file` |
| systemd unit files | `/etc/systemd/system/` (even if not enabled) |
| boot | GRUB cmdline, initramfs mtimes |
| firewall / cron | UFW rules, crontab |

**Requires `patchlog track` or `patchlog new-file`:** anything else —
`/etc/fstab`, `/etc/default/grub`, homedir paths, `/opt/<subdir>/` files.

---

## Undo

```bash
sudo patchlog undo <label> --dry-run   # preview first
sudo patchlog undo <label>
```

Teardown runs in the correct order: stop services → remove DKMS → remove kernel
configs → restore modified files → delete new files → apt remove → daemon-reload →
update-grub / update-initramfs. Idempotent — missing artifacts are skipped cleanly.

---

## Using with AI assistants

Run `patchlog sysprompt` and paste the output as the first message in any AI chat
(Claude, ChatGPT, etc.). The AI will automatically wrap all its suggested commands
in patchlog sessions — you don't need to think about it.

The prompt is also available as [`sys_prompt.md`](sys_prompt.md).  
Regenerate it anytime: `patchlog sysprompt > sys_prompt.md`

---

## Commands

| Command | What it does |
|---|---|
| `patchlog init` | Create `/var/lib/patchlog` (once, needs sudo) |
| `patchlog start <label>` | Begin a session, snapshot state now |
| `patchlog stop` | End session, diff, build teardown plan |
| `patchlog track <path>` | Snapshot a file before you edit it |
| `patchlog new-file <path>` | Register a file you're about to create |
| `patchlog note <text>` | Add a note to the active session |
| `patchlog status` | Show active session + elapsed time |
| `patchlog list` | List all sessions |
| `patchlog show <label>` | Full detail of a session |
| `patchlog undo <label>` | Reverse a session |
| `patchlog check <label>` | Check if session artifacts still exist |
| `patchlog export <label>` | Print session JSON (pipe to AI for diagnosis) |
| `patchlog delete <label>` | Remove a completed session and its snapshots |
| `patchlog abandon` | Discard the active session without saving |
| `patchlog sysprompt` | Print the AI assistant system prompt |

Write commands need `sudo`. Read commands (`list`, `show`, `export`, `check`) do not.
