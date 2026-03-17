# Archero 🏹

> CachyOS & Arch Linux system management TUI. Snapshot your full system state (hardware, kernel, packages, dotfiles, services, config) to JSON, diff snapshots against each other or live, and restore to a fresh install.

**Author:** Kinn Coelho Juliao \<kinncj@protonmail.com\>  
**License:** [GPLv3](https://www.gnu.org/licenses/gpl-3.0.html)

---

## What it does

Archero solves the "I just nuked my Arch install" problem. It snapshots everything about your system into a structured JSON file — hardware, kernel parameters, every installed package, dotfiles, systemd units, udev rules, power config, and more — then gives you a TUI to browse, diff, and replay that state on a fresh machine.

Three modes, one script:

| Mode | What it does |
|---|---|
| **TUI** | Full interactive interface (default, no args) |
| **snapshot** | Dump current system state to JSON |
| **apply** | Restore from a snapshot (dry-run by default) |
| **diff** | Compare two snapshots, or a snapshot vs live |

---

## Installation

### Dependencies

- Python 3.10+ (pre-installed on Arch since 2022)
- `textual` — auto-installed on first run via `pacman` or `pip`
- No other runtime dependencies

### Setup

```bash
# Clone into your dotfiles
git clone https://github.com/kinncj/archero ~/.dotfiles/archero

# Make executable
chmod +x ~/.dotfiles/archero/main.py

# Optional: add to PATH
echo 'export PATH="$HOME/.dotfiles/archero:$PATH"' >> ~/.bashrc
# or for fish
fish_add_path ~/.dotfiles/archero
```

### First run

```bash
~/.dotfiles/archero/main.py
```

If `textual` isn't installed, Archero will install it automatically — trying `pacman`, then `paru`/`yay`, then `pip`, in that order. No manual steps needed.

---

## TUI

Launch with no arguments:

```bash
~/.dotfiles/archero/main.py
```

You'll see the banner for 2 seconds, then the TUI opens.

### Navigation

| Key | Action |
|---|---|
| `↑` / `↓` | Move between menu items |
| `Enter` | Open selected panel |
| `Esc` `Esc` | Return to menu from any panel |
| `Tab` | Move between fields inside a panel |
| `1`–`6` | Jump directly to a panel |
| `Ctrl+Q` | Quit |

### Panels

| # | Panel | Description |
|---|---|---|
| 1 | **snapshot** | Select sections, set output path, capture |
| 2 | **apply** | Load a snapshot, pick steps, dry-run or confirm |
| 3 | **diff** | Compare two files, or a file vs the live system |
| 4 | **stats** | Live power draw, battery, GPU temp, RAM — refreshes every 3s |
| 5 | **packages** | Full package list with AUR tags and live search |
| 6 | **history** | Browse saved snapshot files with metadata preview |

---

## CLI

All modes are available without the TUI:

### snapshot

```bash
# Capture everything
sudo ~/.dotfiles/archero/main.py snapshot

# Pretty-print JSON
sudo ~/.dotfiles/archero/main.py snapshot --pretty

# Custom output path
sudo ~/.dotfiles/archero/main.py snapshot --output ~/my-snapshot.json

# Specific sections only
sudo ~/.dotfiles/archero/main.py snapshot --sections packages dotfiles services
```

Available sections: `meta` `hardware` `kernel` `boot` `filesystem` `packages` `dotfiles` `services` `config` `power` `gpu` `development` `security` `notes`

### apply

Dry-run by default — shows every action without changing anything. Pass `--confirm` to actually apply.

```bash
# See what would happen
sudo ~/.dotfiles/archero/main.py apply my-snapshot.json

# Apply everything
sudo ~/.dotfiles/archero/main.py apply my-snapshot.json --confirm

# Apply specific steps only
sudo ~/.dotfiles/archero/main.py apply my-snapshot.json --confirm --steps packages dotfiles config

# Target a specific distro (auto-detected by default)
sudo ~/.dotfiles/archero/main.py apply my-snapshot.json --confirm --distro arch
```

Available steps: `locale` `packages` `dotfiles` `config` `services` `bootloader` `swap`

Apply order on a fresh install:

```
locale → packages → dotfiles → config → services → bootloader → swap
```

> **Note:** The `swap` step prints manual instructions — swapfile setup can't be fully automated safely because `resume_offset` must be recalculated on the new filesystem.

### diff

```bash
# Diff a saved snapshot vs your live system right now
~/.dotfiles/archero/main.py diff my-snapshot.json

# Diff two saved snapshots
~/.dotfiles/archero/main.py diff snapshot-old.json snapshot-new.json
```

Diff covers: native packages, AUR packages, kernel version + cmdline, enabled services, custom units, modprobe.d, udev rules, Ollama models, dev tool versions, and `.config` directories.

### Banner delay

```bash
# Skip the banner (useful in scripts)
~/.dotfiles/archero/main.py --banner-delay 0 snapshot

# Show banner for 10 seconds
~/.dotfiles/archero/main.py --banner-delay 10
```

---

## Snapshot format

Snapshots are plain JSON. Every section is a top-level key:

```json
{
  "meta":        { "generated_at": "...", "hostname": "...", "distro": "cachyos" },
  "hardware":    { "cpu": {}, "memory": {}, "storage": [], "pci_devices": [] },
  "kernel":      { "version": "6.19.8-1-cachyos", "cmdline": "...", "loaded_modules": [] },
  "boot":        { "bootloader": "grub", "bootloader_config": {}, "mkinitcpio": {} },
  "filesystem":  { "fstab": [], "btrfs_subvolumes": [], "swap": [] },
  "packages":    { "native_explicit": [], "aur_packages": [], "flatpak": [] },
  "dotfiles":    { "key_dotfiles": {}, "git_repos": [] },
  "services":    { "enabled_system_units": [], "custom_system_units": {} },
  "config":      { "modprobe_d": {}, "udev_rules": {}, "tmpfiles_d": {} },
  "power":       { "power_profile": "...", "hibernate": {}, "acpi_wakeup_sources": [] },
  "gpu":         { "gpu_devices": [], "psr": {}, "runtime_pm": {} },
  "development": { "tools": {}, "ollama_models": [], "shell": "fish" },
  "security":    { "secure_boot": "...", "firewall": {} },
  "notes":       { }
}
```

Snapshots are designed to be diffed, committed to git, and consumed by scripts — not just read by humans.

---

## Architecture

```
main.py
├── Primitives          run(), read(), sysfs(), etc.
├── Collectors          one function per section → dict
│   ├── collect_meta()
│   ├── collect_hardware()
│   ├── collect_packages()
│   └── ...
├── Applier             step_* methods, dry-run by default
├── diff_snapshots()    list comparison + key diffs
├── TUI (textual)
│   ├── SnapshotPanel
│   ├── ApplyPanel
│   ├── DiffPanel
│   ├── StatsPanel      live sysfs polling, 3s interval
│   ├── PackagesPanel   pacman -Q with live search
│   └── HistoryPanel    glob snapshot files
└── main()              CLI arg parsing + TUI launcher
```

Everything is read-only unless `apply --confirm` is explicitly passed. Applying always backs up files before overwriting.

---

## AUR helper detection

On first use of `apply --steps packages`, Archero detects your AUR helper. If none is found:

```
No AUR helper found (paru, yay, trizen, pikaur).
Choose one to install, or skip:
  1) paru     Rust-based, recommended for CachyOS/Arch
  2) yay      Go-based, most popular
  3) skip     Skip AUR helper
Choice [1/2/3]:
```

Choosing 1 or 2 bootstraps the helper from AUR via `git clone` + `makepkg` — no chicken-and-egg problem.

---

## Contributing

PRs welcome. A few things worth knowing:

- The author is actively working on an upstream kernel patch for the [AMD ISP4 driver](https://github.com/kinncj/amdisp4) suspend/resume hang on HP ZBook Ultra G1a (Strix Halo). Archero was built and tested on that machine.
- No AI attribution in commits.
- Code style: keep it readable, keep functions focused, no external runtime deps beyond `textual`.

---

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE) for full text.

```
Copyright (C) 2026 Kinn Coelho Juliao <kinncj@protonmail.com>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
```
