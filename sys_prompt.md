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
