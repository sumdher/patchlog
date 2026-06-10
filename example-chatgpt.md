# Example with ChatGPT (non-pro, basic model)

Tested with ChatGPT-4o mini (free tier) after pasting the output of `patchlog sysprompt` as the first message.

---

**You:**

````text
```
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

## Current system

- **OS:** Ubuntu 26.04 LTS
- **Kernel:** 7.0.0-22-generic  (x86_64)
- **Hardware:** ASUSTeK COMPUTER INC. ASUS TUF Dash F15 FX516PM_FX516PM
- **Desktop:** ubuntu:GNOME  (Wayland)
- **DKMS modules:** none

```
I want to set up a test environment on this Ubuntu 26.04 machine. Please give me the commands to do all of the following in one shot:

Install cowsay, rustc, and cargo via apt
Create a small test binary at /usr/local/bin/pl-bin1 that just echoes "binary 1"
Create a small test binary at ~/pl-test/pl-bin2 that just echoes "binary 2"
Create a small test binary at /opt/pl-test/pl-bin3 that just echoes "binary 3"
Create a small test binary at ~/.local/bin/pl-bin4 that just echoes "binary 4"
Append a test line to /etc/hosts
Append a test line to /etc/motd
Create and enable a systemd service called pl-test.service that runs /usr/local/bin/pl-bin1 on boot
I need to be able to completely undo all of this later.
---
````

**ChatGPT said:**

````text
Because this workflow performs package installs, creates files in tracked and non-tracked locations, edits system files, and enables a systemd service, it should be wrapped in a single patchlog session.

```bash
sudo patchlog start pl-test-env --note="test environment with binaries, service, and config changes"

sudo apt update
sudo apt install -y cowsay rustc cargo

sudo tee /usr/local/bin/pl-bin1 > /dev/null <<'EOF'
#!/bin/sh
echo "binary 1"
EOF
sudo chmod +x /usr/local/bin/pl-bin1

sudo patchlog new-file "$HOME/pl-test/pl-bin2"
mkdir -p "$HOME/pl-test"
cat > "$HOME/pl-test/pl-bin2" <<'EOF'
#!/bin/sh
echo "binary 2"
EOF
chmod +x "$HOME/pl-test/pl-bin2"

sudo patchlog new-file /opt/pl-test/pl-bin3
sudo mkdir -p /opt/pl-test
sudo tee /opt/pl-test/pl-bin3 > /dev/null <<'EOF'
#!/bin/sh
echo "binary 3"
EOF
sudo chmod +x /opt/pl-test/pl-bin3

sudo patchlog new-file "$HOME/.local/bin/pl-bin4"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/pl-bin4" <<'EOF'
#!/bin/sh
echo "binary 4"
EOF
chmod +x "$HOME/.local/bin/pl-bin4"

sudo patchlog track /etc/hosts
echo '127.0.0.1 pl-test.local' | sudo tee -a /etc/hosts

sudo patchlog track /etc/motd
echo 'patchlog test line' | sudo tee -a /etc/motd

sudo tee /etc/systemd/system/pl-test.service > /dev/null <<'EOF'
[Unit]
Description=Patchlog test service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/pl-bin1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable pl-test.service

sudo patchlog stop
```

To completely undo everything later:

```bash
sudo patchlog undo pl-test-env
```

Verification:

```bash
sudo patchlog check pl-test-env

dpkg -l cowsay rustc cargo 2>/dev/null

ls /usr/local/bin/pl-bin1 2>/dev/null && echo "WARNING: still present" || echo "gone ✓"
ls "$HOME/pl-test/pl-bin2" 2>/dev/null && echo "WARNING: still present" || echo "gone ✓"
ls /opt/pl-test/pl-bin3 2>/dev/null && echo "WARNING: still present" || echo "gone ✓"
ls "$HOME/.local/bin/pl-bin4" 2>/dev/null && echo "WARNING: still present" || echo "gone ✓"

systemctl is-enabled pl-test.service 2>/dev/null | grep -qv enabled && echo "disabled ✓" || echo "WARNING: still enabled"

grep -q 'pl-test.local' /etc/hosts && echo "WARNING: hosts entry remains" || echo "hosts reverted ✓"
grep -q 'patchlog test line' /etc/motd && echo "WARNING: motd line remains" || echo "motd reverted ✓"
```

````

---

## Worked correctly

After executing,

```bash
[ 14:43, 10 Jun ]
(sud@tufdash) [~]
➜ [+]$ sudo patchlog undo pl-test-env

[sudo: authenticate] Password:          

Undoing session: pl-test-env
  (test environment with binaries, service, and config changes)

  systemctl stop + disable pl-test.service
  restore /etc/hosts from snapshot
  restore /etc/motd from snapshot
  delete /home/sud/pl-test/pl-bin2
  delete /opt/pl-test/pl-bin3
  delete /home/sud/.local/bin/pl-bin4
  delete /usr/local/bin/pl-bin1
  delete /etc/systemd/system/pl-test.service
  apt remove --autoremove cargo cargo-1.93 cowsay libstd-rust-1.93 libstd-rust-1.93-dev rustc-1.93
Reading package lists... Done
Building dependency tree... Done
Reading state information... Done
Solving dependencies... Done
The following packages will be REMOVED:
  cargo cargo-1.93 cowsay libstd-rust-1.93 libstd-rust-1.93-dev rustc rustc-1.93
0 upgraded, 0 newly installed, 7 to remove and 8 not upgraded.
After this operation, 332 MB disk space will be freed.
(Reading database… 219038 files and directories currently installed.)
Removing cargo (1.93.1ubuntu1)…
Removing cargo-1.93 (1.93.1+dfsg-0ubuntu6)…
Removing cowsay (3.03+dfsg2-8build1)…
Removing rustc (1.93.1ubuntu1)…
Removing rustc-1.93 (1.93.1+dfsg-0ubuntu6)…
Removing libstd-rust-1.93-dev:amd64 (1.93.1+dfsg-0ubuntu6)…
Removing libstd-rust-1.93:amd64 (1.93.1+dfsg-0ubuntu6)…
Processing triggers for man-db (2.13.1-1build1)…
Processing triggers for libc-bin (2.43-2ubuntu2)…
  systemctl daemon-reload  # systemd unit files changed

All 10 steps completed successfully.
Session 'pl-test-env' marked as undone.


```

Verification:

```bash
[ 15:52, 10 Jun ]
(sud@tufdash) [~]
➜ [-]$ sudo patchlog check pl-test-env


Checking artifacts for: pl-test-env

  ✗ [apt_package] cargo  ← not installed — may have been removed by upstream
  ✗ [apt_package] cargo-1.93  ← not installed — may have been removed by upstream
  ✗ [apt_package] cowsay  ← not installed — may have been removed by upstream
  ✗ [apt_package] libstd-rust-1.93  ← not installed — may have been removed by upstream
  ✗ [apt_package] libstd-rust-1.93-dev  ← not installed — may have been removed by upstream
  ✗ [apt_package] rustc-1.93  ← not installed — may have been removed by upstream
  ✗ [systemd_unit] pl-test.service
  ✗ [new_file] /home/sud/pl-test/pl-bin2
  ✗ [new_file] /opt/pl-test/pl-bin3
  ✗ [new_file] /home/sud/.local/bin/pl-bin4
  ✗ [new_file] /usr/local/bin/pl-bin1  ← (auto-detected)
  ✗ [new_file] /etc/systemd/system/pl-test.service  ← (auto-detected)

12 artifact(s) missing.
Upstream may have incorporated this fix natively.
If so, run: sudo patchlog undo pl-test-env

[ 15:52, 10 Jun ]
(sud@tufdash) [~]
➜ [+]$ ls /usr/local/bin/pl-bin1 2>/dev/null && echo "WARNING: still present" || echo "gone ✓"

gone ✓

[ 15:52, 10 Jun ]
(sud@tufdash) [~]
➜ [+]$ ls "$HOME/pl-test/pl-bin2" 2>/dev/null && echo "WARNING: still present" || echo "gone ✓"

gone ✓

[ 15:52, 10 Jun ]
(sud@tufdash) [~]
➜ [+]$ ls /opt/pl-test/pl-bin3 2>/dev/null && echo "WARNING: still present" || echo "gone ✓"

gone ✓

[ 15:52, 10 Jun ]
(sud@tufdash) [~]
➜ [+]$ ls "$HOME/.local/bin/pl-bin4" 2>/dev/null && echo "WARNING: still present" || echo "gone ✓"

gone ✓

[ 15:52, 10 Jun ]
(sud@tufdash) [~]
➜ [+]$ systemctl is-enabled pl-test.service 2>/dev/null | grep -qv enabled && echo "disabled ✓" || echo "WARNING: still enabled"

disabled ✓

[ 15:53, 10 Jun ]
(sud@tufdash) [~]
➜ [+]$ grep -q 'pl-test.local' /etc/hosts && echo "WARNING: hosts entry remains" || echo "hosts reverted ✓"

hosts reverted ✓

[ 15:53, 10 Jun ]
(sud@tufdash) [~]
➜ [+]$ grep -q 'patchlog test line' /etc/motd && echo "WARNING: motd line remains" || echo "motd reverted ✓"
motd reverted ✓

[ 15:53, 10 Jun ]
(sud@tufdash) [~]
➜ [+]$ dpkg -l cowsay cargo rustc 2>/dev/null | grep '^ii'

```

## How it performed

**What it got right — all five file-creation decisions correct:**

- Did **not** call `patchlog new-file` for `/usr/local/bin/pl-bin1` — correctly recognised `/usr/local/bin/` as auto-tracked.
- Did **not** call `patchlog new-file` for `/etc/systemd/system/pl-test.service` — correctly recognised `/etc/systemd/system/` as auto-tracked.
- Correctly called `patchlog new-file "$HOME/pl-test/pl-bin2"` — homedir path, not auto-tracked.
- Correctly called `patchlog new-file /opt/pl-test/pl-bin3` — `/opt` subdir, not auto-tracked.
- Correctly called `patchlog new-file "$HOME/.local/bin/pl-bin4"` — homedir path, not auto-tracked.
- Placed `patchlog track /etc/hosts` **before** the edit — file content restored correctly on undo.
- Placed `patchlog track /etc/motd` **before** the edit — file content restored correctly on undo.
- Wrapped the entire workflow in a single session.
- Added a complete verification block after the undo command.

**Issues (harmless):**

- `apt update` is inside the session. It only refreshes package lists — nothing installs — so it produces no diff entry and doesn't affect undo correctness. Ideally it runs before `patchlog start`.
- `systemctl enable` without `--now`: the service is registered but never started. Undo works correctly — `systemctl stop` is a no-op on an already-stopped unit.

**Verdict:** 10 of 10 changes undone cleanly. All files deleted, both config files restored to their original content, service unit gone (`not-found`), all packages removed. The sysprompt's auto-tracking table correctly guided the AI's `new-file` vs auto-tracked decisions across all five file-creation cases without a single mistake.
