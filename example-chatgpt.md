# Example with ChatGPT (non-pro, basic model)

Tested with ChatGPT-4o mini (free tier) after pasting the output of `patchlog sysprompt` as the first message.

---

**You:**

````text
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

### Notes

- patchlog is already installed. Never suggest installing it.
- User says "undo" → `sudo patchlog undo <label>`
- Something broke after `apt upgrade` → `patchlog check <label>`
- Diagnose with AI → `patchlog export <label>`

## Current system

- **OS:** <redacted>
- **Kernel:** <redacted>
- **Hardware:** <redacted>
- **Desktop:** <redacted>
- **DKMS modules:** <redacted>
````

I want to set up a test environment on this Ubuntu 26.04 machine. Please give me the commands to do all of the following in one shot:

- Install cowsay, rustc, and cargo via apt
- Create a small test binary at /usr/local/bin/pl-bin1 that just echoes "binary 1"
- Create a small test binary at ~/pl-test/pl-bin2 that just echoes "binary 2"
- Create a small test binary at /opt/pl-test/pl-bin3 that just echoes "binary 3"
- Append a test line to /etc/motd
- Create and enable a systemd service called pl-test.service that runs /usr/local/bin/pl-bin1 on boot

I need to be able to completely undo all of this later.

---

**ChatGPT said:**

````text
Because every change must be undoable later, wrap the entire workflow in a single patchlog session and track /etc/motd immediately before modifying it.

```bash
sudo patchlog start pl-test-env --note="test environment with packages binaries motd and systemd service"

sudo apt update
sudo apt install -y cowsay rustc cargo

sudo tee /usr/local/bin/pl-bin1 > /dev/null <<'EOF'
#!/bin/sh
echo "binary 1"
EOF
sudo chmod +x /usr/local/bin/pl-bin1

mkdir -p ~/pl-test
sudo patchlog new-file "$HOME/pl-test/pl-bin2"
cat > ~/pl-test/pl-bin2 <<'EOF'
#!/bin/sh
echo "binary 2"
EOF
chmod +x ~/pl-test/pl-bin2

sudo mkdir -p /opt/pl-test
sudo patchlog new-file /opt/pl-test/pl-bin3
sudo tee /opt/pl-test/pl-bin3 > /dev/null <<'EOF'
#!/bin/sh
echo "binary 3"
EOF
sudo chmod +x /opt/pl-test/pl-bin3

sudo patchlog track /etc/motd
echo "patchlog test environment" | sudo tee -a /etc/motd

sudo tee /etc/systemd/system/pl-test.service > /dev/null <<'EOF'
[Unit]
Description=Patchlog Test Service
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/pl-bin1
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable pl-test.service

sudo patchlog stop
```

To undo everything later:

```bash
sudo patchlog undo pl-test-env
```

That will revert the tracked configuration changes, remove the service enablement, remove the created files that were tracked, and roll back the package installs recorded in that session.
````

---

## Honest review

**What it got right (everything that matters):**

- Did **not** call `patchlog new-file` for `/usr/local/bin/pl-bin1` — correctly understood that `/usr/local/bin/` is auto-tracked.
- Did **not** call `patchlog new-file` for `/etc/systemd/system/pl-test.service` — correctly understood that `/etc/systemd/system/` is auto-tracked.
- Correctly called `patchlog new-file "$HOME/pl-test/pl-bin2"` for the homedir path — not auto-tracked.
- Correctly called `patchlog new-file /opt/pl-test/pl-bin3` for the `/opt` subdir path — not auto-tracked.
- Placed `patchlog track /etc/motd` **before** the edit, not after.
- Wrapped the entire workflow in a single session — no double sessions.
- Added a helpful undo reminder at the end.

**Minor issues (harmless):**

- `apt update` is inside the session. It only refreshes package lists — nothing installs — so it won't appear in the apt diff. Ideally it goes before `patchlog start`, but it doesn't affect undo correctness.
- `systemctl enable` without `--now`: the service is registered but never actually starts. Undo will still work correctly (the `stop` step fails silently and is skipped; `disable` and file deletion proceed normally).

**Verdict:** The undo will work completely. The sysprompt's auto-tracking table successfully prevented the AI from over-calling `patchlog new-file` — the most common failure mode seen in earlier versions of the prompt.
