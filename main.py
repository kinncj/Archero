#!/usr/bin/env python3
"""
Archero — main.py
==================
System snapshot, restore, and diff tool for CachyOS and Arch Linux.
Includes a full TUI (launched when run with no arguments).

Author:  Kinn Coelho Juliao <kinncj@protonmail.com>
License: GNU General Public License v3.0 (GPLv3)
         https://www.gnu.org/licenses/gpl-3.0.html

MODES:
    (no args)   Launch interactive TUI
    snapshot    Dump current system state to a timestamped JSON file
    apply       Restore a system from a snapshot JSON (dry-run by default)
    diff        Compare two snapshot JSON files

USAGE:
    # TUI
    ./main.py

    # Capture
    sudo ./main.py snapshot
    sudo ./main.py snapshot --pretty
    sudo ./main.py snapshot --output my-snapshot.json

    # Restore (dry-run by default, shows what would happen)
    sudo ./main.py apply my-snapshot.json
    sudo ./main.py apply my-snapshot.json --confirm
    sudo ./main.py apply my-snapshot.json --steps packages dotfiles

    # Diff two snapshots (or one snapshot vs live system)
    ./main.py diff snapshot-old.json
    ./main.py diff snapshot-old.json snapshot-new.json
"""

import subprocess
import os
import sys
import json
import glob
import shutil
import argparse
from datetime import datetime
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════════════════════════════

SNAPSHOT_DIR = Path.home() / ".config" / "archero" / "snapshots"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════════════
# PRIMITIVES
# ═══════════════════════════════════════════════════════════════════════════════

def run(cmd: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or r.stderr or "").strip()
    except Exception as e:
        return f"ERROR: {e}"


def run_lines(cmd: str) -> list:
    return [l for l in run(cmd).splitlines() if l.strip()]


def run_live(cmd: str) -> int:
    """Run a command with live output. Returns exit code."""
    result = subprocess.run(cmd, shell=True)
    return result.returncode


def read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except Exception:
        return ""


def read_lines(path: str) -> list:
    return [l for l in read(path).splitlines() if l.strip()]


def read_glob(pattern: str) -> dict:
    return {f: read(f) for f in sorted(glob.glob(pattern))}


def owned_by_package(path: str) -> bool:
    r = subprocess.run(["pacman", "-Qo", path], capture_output=True, text=True)
    return r.returncode == 0


def custom_files(pattern: str) -> dict:
    return {p: c for p, c in read_glob(pattern).items() if not owned_by_package(p)}


def sysfs(path: str) -> str:
    return read(path)


def get_user_home() -> Path:
    sudo_user = os.environ.get("SUDO_USER", "")
    if sudo_user:
        return Path(f"/home/{sudo_user}")
    return Path.home()


def detect_distro() -> str:
    """Returns 'cachyos', 'arch', or 'unknown'."""
    os_release = read("/etc/os-release")
    if "cachyos" in os_release.lower():
        return "cachyos"
    if "arch" in os_release.lower():
        return "arch"
    return "unknown"


def backup(path: str) -> str:
    """Backup a file with a timestamp suffix. Returns backup path."""
    p = Path(path)
    if p.exists():
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = str(p) + f".bak-{ts}"
        shutil.copy2(str(p), backup_path)
        return backup_path
    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# SNAPSHOT COLLECTORS
# ═══════════════════════════════════════════════════════════════════════════════

def collect_meta() -> dict:
    return {
        "generated_at": datetime.now().isoformat(),
        "hostname": run("hostname"),
        "schema_version": "1.0.0",
        "tool": "archero",
        "distro": detect_distro(),
        "os_release": read("/etc/os-release"),
    }


def collect_hardware() -> dict:
    cpu_info = {}
    for line in run_lines("lscpu"):
        if ":" in line:
            k, _, v = line.partition(":")
            cpu_info[k.strip()] = v.strip()

    mem_slots = []
    current = {}
    for line in run_lines("dmidecode -t memory 2>/dev/null"):
        if line.startswith("Memory Device"):
            if current:
                mem_slots.append(current)
            current = {}
        elif ":" in line:
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if k in ("Size", "Type", "Speed", "Manufacturer", "Part Number",
                      "Configured Memory Speed", "Form Factor", "Locator"):
                current[k] = v
    if current:
        mem_slots.append(current)
    mem_slots = [m for m in mem_slots if m.get("Size", "No Module") not in
                 ("No Module Installed", "Not Installed")]

    storage = []
    lsblk_raw = run("lsblk -J -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL,SERIAL,VENDOR 2>/dev/null")
    try:
        storage = json.loads(lsblk_raw).get("blockdevices", [])
    except Exception:
        storage = run_lines("lsblk -f")

    pci = []
    for line in run_lines("lspci"):
        parts = line.split(" ", 1)
        if len(parts) == 2:
            pci.append({"address": parts[0], "description": parts[1]})

    bat = {
        attr: sysfs(f"/sys/class/power_supply/BAT0/{attr}")
        for attr in ["manufacturer", "model_name", "technology", "energy_full_design",
                     "energy_full", "cycle_count"]
        if sysfs(f"/sys/class/power_supply/BAT0/{attr}")
    }

    displays = []
    for edid_path in sorted(glob.glob("/sys/class/drm/card*-eDP-*/edid")):
        connector = Path(edid_path).parent.name
        modes = read_lines(str(Path(edid_path).parent / "modes"))
        displays.append({"connector": connector, "modes": modes})

    backlight = {}
    for f in glob.glob("/sys/class/backlight/*"):
        name = Path(f).name
        backlight[name] = {
            "max_brightness": sysfs(f"{f}/max_brightness"),
            "type": sysfs(f"{f}/type"),
        }

    bios = {}
    for line in run_lines("dmidecode -t bios 2>/dev/null"):
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip()
            if k in ("Vendor", "Version", "Release Date", "BIOS Revision"):
                bios[k] = v.strip()

    system_info = {}
    for line in run_lines("dmidecode -t system 2>/dev/null"):
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip()
            if k in ("Manufacturer", "Product Name", "Version", "SKU Number", "Family"):
                system_info[k] = v.strip()

    return {
        "cpu": cpu_info,
        "memory": {
            "total_bytes": run("free -b | awk '/^Mem:/{print $2}'"),
            "slots": mem_slots,
        },
        "storage": storage,
        "pci_devices": pci,
        "usb_devices": run_lines("lsusb"),
        "battery": bat,
        "displays": displays,
        "backlight": backlight,
        "bios": bios,
        "system": system_info,
        "wifi": run("iw dev 2>/dev/null"),
    }


def collect_kernel() -> dict:
    amdgpu_params = {
        Path(p).name: read(p)
        for p in sorted(glob.glob("/sys/module/amdgpu/parameters/*"))
    }
    return {
        "version": run("uname -r"),
        "full": run("uname -a"),
        "cmdline": read("/proc/cmdline"),
        "installed_kernels": run_lines("ls /boot/vmlinuz* 2>/dev/null"),
        "loaded_modules": run_lines("lsmod | awk 'NR>1{print $1}' | sort"),
        "amdgpu_parameters": amdgpu_params,
        "modprobe_configs": {
            Path(k).name: v for k, v in read_glob("/etc/modprobe.d/*.conf").items()
        },
    }


def collect_boot() -> dict:
    bootloader = "unknown"
    bootloader_config = {}
    if Path("/etc/default/grub").exists():
        bootloader = "grub"
        bootloader_config = {
            "default_grub": read("/etc/default/grub"),
            "active_cmdline": read("/proc/cmdline"),
        }
    elif glob.glob("/boot/limine.conf") or glob.glob("/efi/limine.conf"):
        bootloader = "limine"
        for f in glob.glob("/boot/limine.conf") + glob.glob("/efi/limine.conf"):
            bootloader_config["limine_conf"] = read(f)

    return {
        "bootloader": bootloader,
        "bootloader_config": bootloader_config,
        "mkinitcpio": {
            "conf": read("/etc/mkinitcpio.conf"),
            "presets": {
                Path(f).name: read(f)
                for f in sorted(glob.glob("/etc/mkinitcpio.d/*.preset"))
            },
        },
        "efi_entries": run("efibootmgr 2>/dev/null"),
    }


def collect_filesystem() -> dict:
    fstab_entries = []
    for line in read_lines("/etc/fstab"):
        if not line.startswith("#"):
            parts = line.split()
            if len(parts) >= 4:
                fstab_entries.append({
                    "device": parts[0], "mountpoint": parts[1],
                    "fstype": parts[2], "options": parts[3],
                    "dump": parts[4] if len(parts) > 4 else "0",
                    "pass": parts[5] if len(parts) > 5 else "0",
                })

    btrfs_subvols = []
    for line in run_lines("btrfs subvolume list / 2>/dev/null"):
        if "snapshot" not in line:
            parts = line.split()
            btrfs_subvols.append({
                "id": parts[1] if len(parts) > 1 else "",
                "top_level": parts[6] if len(parts) > 6 else "",
                "path": parts[-1] if parts else "",
            })

    swap_entries = []
    for line in run_lines("swapon --show --noheadings 2>/dev/null"):
        parts = line.split()
        if parts:
            swap_entries.append({
                "name": parts[0] if len(parts) > 0 else "",
                "type": parts[1] if len(parts) > 1 else "",
                "size": parts[2] if len(parts) > 2 else "",
            })

    return {
        "fstab": fstab_entries,
        "btrfs_subvolumes": btrfs_subvols,
        "btrfs_snapshot_count": len([
            l for l in run_lines("btrfs subvolume list / 2>/dev/null") if "snapshot" in l
        ]),
        "swap": swap_entries,
        "resume_uuid": run("grep -oP 'resume=UUID=\\K[^ ]+' /proc/cmdline 2>/dev/null || echo ''"),
        "resume_offset": run("btrfs inspect-internal map-swapfile -r /swap/swapfile 2>/dev/null || echo ''"),
    }


def collect_packages() -> dict:
    explicit = run_lines("pacman -Qqe")
    aur = run_lines("pacman -Qqm")
    native = [p for p in explicit if p not in aur]

    all_pkgs = {}
    for line in run_lines("pacman -Q"):
        parts = line.split(None, 1)
        if len(parts) == 2:
            all_pkgs[parts[0]] = parts[1]

    aur_helper = ""
    for helper in ["paru", "yay", "trizen", "pikaur"]:
        if run(f"which {helper} 2>/dev/null"):
            aur_helper = helper
            break

    return {
        "aur_helper": aur_helper,
        "counts": {
            "total": len(all_pkgs),
            "explicit": len(explicit),
            "native_explicit": len(native),
            "aur": len(aur),
            "orphans": len(run_lines("pacman -Qqdt 2>/dev/null")),
            "flatpak": len(run_lines("flatpak list --app --columns=application 2>/dev/null")),
        },
        "native_explicit": sorted(native),
        "aur_packages": sorted(aur),
        "all_installed_with_versions": all_pkgs,
        "orphans": run_lines("pacman -Qqdt 2>/dev/null"),
        "flatpak": run_lines("flatpak list --app --columns=application 2>/dev/null"),
        "pip_global": run_lines("pip list --format=freeze 2>/dev/null || pip3 list --format=freeze 2>/dev/null"),
        "npm_global": run_lines("npm list -g --depth=0 2>/dev/null | grep -v '^/' | tail -n +2"),
        "cargo": run_lines("cargo install --list 2>/dev/null | grep -v '    '"),
    }


def collect_dotfiles() -> dict:
    home = get_user_home()

    key_dotfiles_paths = [
        ".bashrc", ".zshrc", ".zprofile", ".profile",
        ".config/fish/config.fish", ".config/fish/fish_variables",
        ".gitconfig", ".config/git/config",
        ".vimrc", ".config/nvim/init.lua", ".config/nvim/init.vim",
        ".config/starship.toml",
        ".config/alacritty/alacritty.toml", ".config/alacritty/alacritty.yml",
        ".config/kitty/kitty.conf", ".config/ghostty/config",
        ".config/wezterm/wezterm.lua",
        ".config/hypr/hyprland.conf", ".config/sway/config",
        ".tmux.conf", ".config/tmux/tmux.conf",
        ".config/zellij/config.kdl",
        ".config/mimeapps.list", ".config/user-dirs.dirs",
    ]

    dotfiles = {}
    for rel in key_dotfiles_paths:
        full = home / rel
        if full.exists():
            dotfiles[rel] = "[REDACTED]" if "ssh" in rel.lower() else read(str(full))

    config_path = home / ".config"
    config_dirs = sorted([d.name for d in config_path.iterdir() if d.is_dir()]) \
        if config_path.exists() else []

    kde_configs = []
    if config_path.exists():
        for pattern in ["*.conf", "kde*", "plasma*", "kwin*", "k*rc"]:
            kde_configs += [f.name for f in config_path.glob(pattern) if f.is_file()]
    kde_configs = sorted(set(kde_configs))

    dotfile_manager = ""
    for manager in ["chezmoi", "stow", "yadm", "dotbot"]:
        if run(f"which {manager} 2>/dev/null"):
            dotfile_manager = manager
            break

    git_repos = []
    for gitdir in sorted(home.glob("*/.git")):
        git_repos.append({
            "path": str(gitdir.parent.relative_to(home)),
            "remote": run(f"git -C {gitdir.parent} remote get-url origin 2>/dev/null"),
            "branch": run(f"git -C {gitdir.parent} branch --show-current 2>/dev/null"),
        })

    return {
        "home_directory": str(home),
        "dotfile_manager": dotfile_manager,
        "key_dotfiles": dotfiles,
        "config_directories": config_dirs,
        "kde_config_files": kde_configs,
        "git_repos": git_repos,
    }


def collect_services() -> dict:
    enabled = [l.split()[0] for l in run_lines(
        "systemctl list-unit-files --state=enabled --no-pager --no-legend"
    ) if l.split()]

    custom_units = {}
    for pattern in ["/etc/systemd/system/*.service", "/etc/systemd/system/*.timer",
                    "/etc/systemd/system/*.mount", "/etc/systemd/system/*.path"]:
        for path, content in custom_files(pattern).items():
            custom_units[Path(path).name] = content

    sudo_user = os.environ.get("SUDO_USER", "")
    user_enabled = []
    if sudo_user:
        user_enabled = [l.split()[0] for l in run_lines(
            f"sudo -u {sudo_user} systemctl --user list-unit-files "
            f"--state=enabled --no-pager --no-legend 2>/dev/null"
        ) if l.split()]

    return {
        "enabled_system_units": enabled,
        "failed_units": run_lines("systemctl --failed --no-pager --no-legend 2>/dev/null"),
        "custom_system_units": custom_units,
        "user_enabled_units": user_enabled,
    }


def collect_config() -> dict:
    return {
        "udev_rules": {Path(k).name: v for k, v in custom_files("/etc/udev/rules.d/*.rules").items()},
        "tmpfiles_d": {Path(k).name: v for k, v in custom_files("/etc/tmpfiles.d/*.conf").items()},
        "modprobe_d": {Path(k).name: v for k, v in read_glob("/etc/modprobe.d/*.conf").items()},
        "sysctl_d": {Path(k).name: v for k, v in custom_files("/etc/sysctl.d/*.conf").items()},
        "pacman_conf": read("/etc/pacman.conf"),
        "active_mirrors": [
            l for l in read_lines("/etc/pacman.d/mirrorlist") if not l.startswith("#")
        ][:20],
        "locale_conf": read("/etc/locale.conf"),
        "locale_gen": [l for l in read_lines("/etc/locale.gen") if not l.startswith("#")],
        "timezone": run("timedatectl show --property=Timezone --value 2>/dev/null"),
        "hostname": read("/etc/hostname"),
        "hosts": read("/etc/hosts"),
        "environment": read("/etc/environment"),
    }


def collect_power() -> dict:
    nvme_pci = run("lspci | grep -i nvme | awk '{print $1}'")
    nvme_power = {}
    if nvme_pci and not nvme_pci.startswith("ERROR"):
        base = f"/sys/bus/pci/devices/0000:{nvme_pci}/power"
        nvme_power = {
            "control": sysfs(f"{base}/control"),
            "runtime_status": sysfs(f"{base}/runtime_status"),
        }

    wakeup_sources = []
    for line in read_lines("/proc/acpi/wakeup"):
        if not line.startswith("Device"):
            parts = line.split()
            if len(parts) >= 3:
                wakeup_sources.append({
                    "device": parts[0],
                    "s_state": parts[1] if len(parts) > 1 else "",
                    "enabled": "*enabled" in (parts[2] if len(parts) > 2 else ""),
                    "sysfs": parts[3] if len(parts) > 3 else "",
                })

    return {
        "power_profile": run("powerprofilesctl get 2>/dev/null"),
        "cpu_frequency": {
            "amd_pstate_status": sysfs("/sys/devices/system/cpu/amd_pstate/status"),
            "scaling_governor": sysfs("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
            "scaling_driver": sysfs("/sys/devices/system/cpu/cpu0/cpufreq/scaling_driver"),
        },
        "acpi_wakeup_sources": wakeup_sources,
        "nvme_power": nvme_power,
        "wifi_power_save": run("iw dev wlan0 get power_save 2>/dev/null"),
        "battery_state": {
            attr: sysfs(f"/sys/class/power_supply/BAT0/{attr}")
            for attr in ["status", "capacity", "power_now", "energy_now", "energy_full"]
            if sysfs(f"/sys/class/power_supply/BAT0/{attr}")
        },
        "hibernate": {
            "swap_file": run("grep 'swapfile' /etc/fstab | awk '{print $1}'"),
            "resume_uuid": run("grep -oP 'resume=UUID=\\K[^ ]+' /proc/cmdline 2>/dev/null || echo ''"),
            "resume_offset": run("grep -oP 'resume_offset=\\K[^ ]+' /proc/cmdline 2>/dev/null || echo ''"),
            "disk_mode": sysfs("/sys/power/disk"),
        },
        "amd_pmf_loaded": bool(run("lsmod | grep amd_pmf")),
    }


def _find_dri_card() -> str:
    """Find the first DRI card directory with amdgpu_pm_info."""
    for i in range(8):
        if Path(f"/sys/kernel/debug/dri/{i}/amdgpu_pm_info").exists():
            return str(i)
    return "0"


def _find_gpu_pci_address() -> str:
    """Find the first GPU PCI address via lspci."""
    for line in run_lines("lspci | grep -iE 'vga|display|3d'"):
        addr = line.split(" ", 1)[0]
        if addr:
            return f"0000:{addr}"
    return ""


def collect_gpu() -> dict:
    dri = _find_dri_card()
    pci = _find_gpu_pci_address()
    psr = {}
    # Scan for PSR-capable connectors
    dri_path = Path(f"/sys/kernel/debug/dri/{dri}")
    if dri_path.is_dir():
        for conn in sorted(dri_path.iterdir()):
            if (conn / "psr_state").exists():
                psr[conn.name] = {
                    "state": sysfs(str(conn / "psr_state")),
                    "residency": sysfs(str(conn / "psr_residency")),
                    "capability": read(str(conn / "psr_capability")),
                }
    runtime_pm = {}
    if pci:
        pci_power = Path(f"/sys/bus/pci/devices/{pci}/power")
        if pci_power.is_dir():
            runtime_pm = {
                "pci_address": pci,
                "status": sysfs(str(pci_power / "runtime_status")),
                "control": sysfs(str(pci_power / "control")),
            }
    return {
        "gpu_devices": [
            {"address": l.split(" ", 1)[0], "description": l.split(" ", 1)[1] if " " in l else ""}
            for l in run_lines("lspci | grep -iE 'vga|display|3d'")
        ],
        "amdgpu_pm_info": run(f"cat /sys/kernel/debug/dri/{dri}/amdgpu_pm_info 2>/dev/null | head -20"),
        "psr": psr,
        "runtime_pm": runtime_pm,
        "session": {
            "wayland_display": os.environ.get("WAYLAND_DISPLAY", ""),
            "xdg_session_type": os.environ.get("XDG_SESSION_TYPE", ""),
            "desktop_environment": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        },
    }


def collect_development() -> dict:
    dev_tools = [
        ("python3", "python3 --version 2>&1"),
        ("node", "node --version 2>/dev/null"),
        ("npm", "npm --version 2>/dev/null"),
        ("git", "git --version 2>/dev/null"),
        ("docker", "docker --version 2>/dev/null"),
        ("podman", "podman --version 2>/dev/null"),
        ("rustc", "rustc --version 2>/dev/null"),
        ("go", "go version 2>/dev/null"),
        ("java", "java --version 2>/dev/null | head -1"),
        ("dotnet", "dotnet --version 2>/dev/null"),
        ("kubectl", "kubectl version --client --short 2>/dev/null | head -1"),
        ("terraform", "terraform version 2>/dev/null | head -1"),
        ("code", "code --version 2>/dev/null | head -1"),
        ("code-insiders", "code-insiders --version 2>/dev/null | head -1"),
        ("nvim", "nvim --version 2>/dev/null | head -1"),
        ("gh", "gh --version 2>/dev/null | head -1"),
        ("ollama", "ollama --version 2>/dev/null"),
        ("fish", "fish --version 2>/dev/null"),
        ("zsh", "zsh --version 2>/dev/null"),
        ("tmux", "tmux -V 2>/dev/null"),
        ("ghostty", "ghostty --version 2>/dev/null | head -1"),
        ("dkms", "dkms --version 2>/dev/null"),
    ]
    tools = {
        name: result for name, cmd in dev_tools
        if (result := run(cmd)) and not result.startswith("ERROR")
    }

    home = get_user_home()
    ssh_keys = sorted([f.name for f in (home / ".ssh").glob("*.pub")]) \
        if (home / ".ssh").exists() else []

    return {
        "shell": os.environ.get("SHELL", run("echo $SHELL")),
        "tools": tools,
        "ollama_models": run_lines("ollama list 2>/dev/null | awk 'NR>1{print $1}'"),
        "ssh_public_keys": ssh_keys,
        "gpg_keys": run_lines("gpg --list-keys --with-colons 2>/dev/null | grep '^pub' | cut -d: -f5,10"),
    }


def collect_security() -> dict:
    return {
        "secure_boot": run("mokutil --sb-state 2>/dev/null"),
        "firewall": {svc: run(f"systemctl is-active {svc} 2>/dev/null")
                    for svc in ["ufw", "firewalld", "nftables"]},
        "sshd": run("systemctl is-active sshd 2>/dev/null"),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# NOTE MODULES
# ═══════════════════════════════════════════════════════════════════════════════

def _load_archero_config() -> dict:
    """Load user config from ~/.config/archero/config.json."""
    cfg_file = Path.home() / ".config" / "archero" / "config.json"
    if cfg_file.exists():
        try:
            return json.loads(cfg_file.read_text())
        except Exception:
            return {}
    return {}


def _notes_amdgpu() -> dict | None:
    """AMD GPU notes — detect quirks and recommend fixes."""
    if not Path("/sys/module/amdgpu").exists():
        return None

    params_dir = Path("/sys/module/amdgpu/parameters")
    gpu_recovery = read(str(params_dir / "gpu_recovery")) if params_dir.exists() else ""
    dc = read(str(params_dir / "dc")) if params_dir.exists() else ""

    dri = _find_dri_card()
    pci = _find_gpu_pci_address()

    # PSR status
    psr_active = False
    dri_path = Path(f"/sys/kernel/debug/dri/{dri}")
    if dri_path.is_dir():
        for conn in sorted(dri_path.iterdir()):
            if (conn / "psr_state").exists():
                state = sysfs(str(conn / "psr_state"))
                if state and "inactive" not in state.lower():
                    psr_active = True

    # Runtime PM
    rpm_status = ""
    if pci:
        rpm_path = Path(f"/sys/bus/pci/devices/{pci}/power/runtime_status")
        if rpm_path.exists():
            rpm_status = sysfs(str(rpm_path))

    status = {
        "module": "amdgpu",
        "gpu_recovery": gpu_recovery,
        "dc": dc,
        "psr_active": psr_active,
        "runtime_pm": rpm_status,
        "dri_card": dri,
        "pci_address": pci,
    }

    quirks = []
    if not psr_active:
        quirks.append({
            "id": "psr-inactive",
            "summary": "Panel Self Refresh (PSR) not active — may increase idle power on laptops",
            "reference": "https://wiki.archlinux.org/title/AMDGPU#Panel_Self_Refresh_(PSR)",
        })
    if rpm_status and "active" not in rpm_status:
        quirks.append({
            "id": "runtime-pm-unavailable",
            "summary": f"GPU runtime PM status: {rpm_status} — expected on some APUs",
            "reference": "",
        })

    recommendations = []
    if gpu_recovery != "1":
        recommendations.append({
            "action": "Enable GPU recovery for hibernate stability",
            "reason": "amdgpu.gpu_recovery=1 helps resume from hibernate on AMD GPUs",
            "command": "Add 'amdgpu.gpu_recovery=1' to kernel cmdline in bootloader config",
        })
    if dc == "0":
        recommendations.append({
            "action": "Remove amdgpu.dc=0 from kernel cmdline",
            "reason": "Disabling display core breaks Plymouth and can cause visual glitches",
            "command": "Remove 'amdgpu.dc=0' from GRUB_CMDLINE_LINUX_DEFAULT",
        })

    return {"detected": True, "status": status, "quirks": quirks, "recommendations": recommendations}


def _notes_nvidia() -> dict | None:
    """Nvidia GPU notes — driver version, power, wayland compat."""
    if not Path("/sys/module/nvidia").exists():
        return None

    version = read("/sys/module/nvidia/version")
    power_mode = read("/sys/module/nvidia/parameters/NVreg_DynamicPowerManagement")
    session_type = os.environ.get("XDG_SESSION_TYPE", "")

    status = {
        "module": "nvidia",
        "driver_version": version or run("nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null"),
        "dynamic_power_mgmt": power_mode,
        "session_type": session_type,
    }

    quirks = []
    if session_type == "wayland" and version:
        try:
            major = int(version.split(".")[0])
            if major < 535:
                quirks.append({
                    "id": "nvidia-wayland-old-driver",
                    "summary": f"Nvidia driver {version} may have Wayland issues — 535+ recommended",
                    "reference": "https://wiki.archlinux.org/title/NVIDIA#Wayland",
                })
        except ValueError:
            pass

    recommendations = []
    if power_mode != "0x02":
        recommendations.append({
            "action": "Enable fine-grained power management",
            "reason": "Reduces idle power on Nvidia Turing+ GPUs",
            "command": "Set 'options nvidia NVreg_DynamicPowerManagement=0x02' in /etc/modprobe.d/nvidia.conf",
        })

    return {"detected": True, "status": status, "quirks": quirks, "recommendations": recommendations}


def _notes_intel_gpu() -> dict | None:
    """Intel GPU notes — GuC/HuC, PSR, power."""
    if not Path("/sys/module/i915").exists():
        return None

    params = Path("/sys/module/i915/parameters")
    enable_guc = read(str(params / "enable_guc")) if params.exists() else ""
    enable_psr = read(str(params / "enable_psr")) if params.exists() else ""

    status = {
        "module": "i915",
        "enable_guc": enable_guc,
        "enable_psr": enable_psr,
    }

    quirks = []
    if enable_psr == "1":
        quirks.append({
            "id": "i915-psr-flickering",
            "summary": "PSR enabled — may cause screen flickering on some panels",
            "reference": "https://wiki.archlinux.org/title/Intel_graphics#Screen_flickering",
        })

    recommendations = []
    if enable_guc in ("0", ""):
        recommendations.append({
            "action": "Consider enabling GuC/HuC firmware loading",
            "reason": "Enables hardware scheduling and video decode offload on Gen12+",
            "command": "Add 'i915.enable_guc=3' to kernel cmdline",
        })

    return {"detected": True, "status": status, "quirks": quirks, "recommendations": recommendations}


def _notes_hibernate() -> dict | None:
    """Hibernate/suspend notes — swap config, resume, wakeup sources."""
    cmdline = read("/proc/cmdline")
    has_resume = "resume=" in cmdline or "resume_offset=" in cmdline

    # Check swap
    swap_info = run_lines("swapon --show=NAME,TYPE,SIZE --noheadings 2>/dev/null")
    if not swap_info and not has_resume:
        return None

    # Wakeup sources
    wakeup_enabled = []
    acpi_wakeup = run("cat /proc/acpi/wakeup 2>/dev/null")
    for line in acpi_wakeup.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "*enabled":
            wakeup_enabled.append(parts[0])

    status = {
        "swap_devices": swap_info,
        "resume_in_cmdline": has_resume,
        "wakeup_sources_enabled": wakeup_enabled,
    }

    quirks = []
    if wakeup_enabled:
        known_problematic = {"GP11", "GP12", "XHC0", "XHC1"}
        problematic = [s for s in wakeup_enabled if s in known_problematic]
        if problematic:
            quirks.append({
                "id": "wakeup-source-conflict",
                "summary": f"Wakeup sources {', '.join(problematic)} may cause immediate wake from suspend/hibernate",
                "reference": "https://wiki.archlinux.org/title/Power_management/Suspend_and_hibernate#Instantaneous_wakeups_from_suspend",
            })

    recommendations = []
    if swap_info and not has_resume:
        recommendations.append({
            "action": "Add resume= to kernel cmdline for hibernate support",
            "reason": "Swap is configured but kernel has no resume device — hibernate won't work",
            "command": "Add 'resume=/dev/<swap_device>' and 'resume_offset=<offset>' to bootloader config",
        })

    return {"detected": True, "status": status, "quirks": quirks, "recommendations": recommendations}


def _notes_bootloader() -> dict | None:
    """Bootloader notes — detect current bootloader and snapshot support."""
    bootloader = "unknown"
    if Path("/boot/grub/grub.cfg").exists() or Path("/boot/grub2/grub.cfg").exists():
        bootloader = "grub"
    elif Path("/boot/loader/loader.conf").exists():
        bootloader = "systemd-boot"
    elif Path("/boot/limine.cfg").exists() or Path("/boot/limine/limine.conf").exists():
        bootloader = "limine"
    else:
        return None

    btrfs_root = run("findmnt -n -o FSTYPE / 2>/dev/null").strip() == "btrfs"
    grub_btrfs = Path("/etc/grub.d/41_snapshots-btrfs").exists()

    status = {
        "bootloader": bootloader,
        "btrfs_root": btrfs_root,
        "grub_btrfs_installed": grub_btrfs if bootloader == "grub" else None,
    }

    quirks = []
    recommendations = []

    if bootloader == "grub" and btrfs_root and not grub_btrfs:
        recommendations.append({
            "action": "Install grub-btrfs for snapshot boot entries",
            "reason": "Enables booting into btrfs snapshots from GRUB menu",
            "command": "paru -S grub-btrfs && sudo systemctl enable --now grub-btrfsd",
        })

    return {"detected": True, "status": status, "quirks": quirks, "recommendations": recommendations}


def _notes_desktop() -> dict | None:
    """Desktop environment notes — session type and known quirks."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    session = os.environ.get("XDG_SESSION_TYPE", "")
    if not desktop:
        return None

    status = {
        "desktop": desktop,
        "session_type": session,
        "wayland_display": os.environ.get("WAYLAND_DISPLAY", ""),
    }

    quirks = []
    recommendations = []

    desktop_lower = desktop.lower()
    if "kde" in desktop_lower and session == "wayland":
        if not Path("/usr/lib/qt6/plugins/wayland-decoration-client").exists():
            quirks.append({
                "id": "kde-wayland-decoration-missing",
                "summary": "Qt6 Wayland decoration plugin missing — some apps may lack window decorations",
                "reference": "",
            })

    if "hyprland" in desktop_lower:
        xdg_portal = run("pacman -Q xdg-desktop-portal-hyprland 2>/dev/null")
        if not xdg_portal:
            recommendations.append({
                "action": "Install xdg-desktop-portal-hyprland",
                "reason": "Required for screen sharing and file dialogs under Hyprland",
                "command": "paru -S xdg-desktop-portal-hyprland",
            })

    return {"detected": True, "status": status, "quirks": quirks, "recommendations": recommendations}


def _notes_power() -> dict | None:
    """Power notes — battery, wifi PM, NVMe APST."""
    bat_path = Path("/sys/class/power_supply/BAT0")
    if not bat_path.exists():
        bat_path = Path("/sys/class/power_supply/BAT1")
        if not bat_path.exists():
            return None

    wifi_ps = ""
    wifi_dev = run("iw dev 2>/dev/null | awk '/Interface/{print $2}' | head -1")
    if wifi_dev:
        ps_output = run(f"iw dev {wifi_dev} get power_save 2>/dev/null")
        wifi_ps = "on" if "on" in ps_output.lower() else "off" if "off" in ps_output.lower() else ps_output

    # NVMe power state
    nvme_pm = {}
    for rule_file in sorted(Path("/etc/udev/rules.d").glob("*nvme*")) if Path("/etc/udev/rules.d").is_dir() else []:
        nvme_pm[rule_file.name] = read(str(rule_file))[:200]

    status = {
        "battery": bat_path.name,
        "wifi_power_save": wifi_ps,
        "wifi_device": wifi_dev,
        "nvme_udev_rules": list(nvme_pm.keys()),
    }

    quirks = []
    recommendations = []

    if wifi_ps == "on":
        recommendations.append({
            "action": "Consider disabling wifi power save if experiencing disconnects",
            "reason": "Some wifi chipsets (MediaTek, Intel AX) have stability issues with power_save on",
            "command": f"iw dev {wifi_dev} set power_save off",
        })

    return {"detected": True, "status": status, "quirks": quirks, "recommendations": recommendations}


def _notes_kernel_modules() -> dict | None:
    """Kernel module blacklist notes — scan /etc/modprobe.d/ for blacklists."""
    modprobe_dir = Path("/etc/modprobe.d")
    if not modprobe_dir.is_dir():
        return None

    blacklisted = {}
    for conf in sorted(modprobe_dir.glob("*.conf")):
        content = read(str(conf))
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("blacklist "):
                mod = stripped.split(None, 1)[1].strip()
                blacklisted[mod] = conf.name

    if not blacklisted:
        return None

    status = {
        "blacklisted_modules": blacklisted,
        "config_files": sorted({v for v in blacklisted.values()}),
    }

    return {"detected": True, "status": status, "quirks": [], "recommendations": []}


NOTE_MODULES: dict[str, callable] = {
    "amdgpu": _notes_amdgpu,
    "nvidia": _notes_nvidia,
    "intel_gpu": _notes_intel_gpu,
    "hibernate": _notes_hibernate,
    "bootloader": _notes_bootloader,
    "desktop": _notes_desktop,
    "power": _notes_power,
    "kernel_modules": _notes_kernel_modules,
}


def collect_notes(note_modules: list[str] | None = None,
                  disable_note_modules: list[str] | None = None) -> dict:
    """Run auto-detecting note modules. Merge with user notes."""
    config = _load_archero_config()
    cfg_disabled = set(config.get("note_modules", {}).get("disabled", []))
    cfg_enabled = config.get("note_modules", {}).get("enabled", [])

    # CLI --note-modules overrides everything
    if note_modules:
        active = {m for m in note_modules if m in NOTE_MODULES}
    else:
        active = set(NOTE_MODULES.keys()) | set(cfg_enabled)
        active -= cfg_disabled

    # CLI --disable-note-modules stacks on top
    if disable_note_modules:
        active -= set(disable_note_modules)

    results = {}
    for name in sorted(active):
        if name not in NOTE_MODULES:
            continue
        try:
            result = NOTE_MODULES[name]()
            if result is not None:
                results[name] = result
        except Exception as e:
            results[name] = {"detected": False, "error": str(e)}

    # Merge user notes
    notes_file = Path.home() / ".config" / "archero" / "notes.json"
    if notes_file.exists():
        try:
            results["user"] = json.loads(notes_file.read_text())
        except Exception as e:
            results["user"] = {"error": str(e)}

    return results


ALL_COLLECTORS = {
    "meta":        collect_meta,
    "hardware":    collect_hardware,
    "kernel":      collect_kernel,
    "boot":        collect_boot,
    "filesystem":  collect_filesystem,
    "packages":    collect_packages,
    "dotfiles":    collect_dotfiles,
    "services":    collect_services,
    "config":      collect_config,
    "power":       collect_power,
    "gpu":         collect_gpu,
    "development": collect_development,
    "security":    collect_security,
    "notes":       collect_notes,
}


# ═══════════════════════════════════════════════════════════════════════════════
# APPLY ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class Applier:
    """
    Applies a snapshot to the current system.
    All steps are dry-run by default. Pass confirm=True to actually apply.
    Backups are always created before overwriting files.
    """

    def __init__(self, snapshot: dict, confirm: bool = False, distro: str = "auto"):
        self.snap = snapshot
        self.confirm = confirm
        self.distro = distro if distro != "auto" else detect_distro()
        self.actions: list[tuple[str, str]] = []  # (status, description)

    def log(self, status: str, msg: str):
        icon = {"DRY": "○", "OK": "✓", "SKIP": "–", "WARN": "⚠", "ERROR": "✗"}.get(status, "?")
        print(f"  {icon} [{status}] {msg}")
        self.actions.append((status, msg))

    def would(self, msg: str):
        """Log a dry-run action."""
        self.log("DRY", msg)

    def do(self, msg: str, fn):
        """Execute an action if confirm=True, else log as dry-run."""
        if self.confirm:
            try:
                fn()
                self.log("OK", msg)
            except Exception as e:
                self.log("ERROR", f"{msg} — {e}")
        else:
            self.would(msg)

    def write_file(self, path: str, content: str, mode: int = 0o644):
        """Write a file, backing up the original."""
        def _write():
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            bak = backup(path)
            if bak:
                print(f"    → backed up to {bak}")
            p.write_text(content)
            p.chmod(mode)
        self.do(f"Write {path}", _write)

    def run_cmd(self, description: str, cmd: str):
        """Run a shell command."""
        def _run():
            rc = run_live(cmd)
            if rc != 0:
                raise RuntimeError(f"Command failed with exit code {rc}: {cmd}")
        self.do(description, _run)

    # ── Steps ─────────────────────────────────────────────────────────────────

    def step_locale(self):
        print("\n── Locale & Timezone ──")
        cfg = self.snap.get("config", {})

        locale_conf = cfg.get("locale_conf", "")
        if locale_conf:
            self.write_file("/etc/locale.conf", locale_conf)

        locale_gen = cfg.get("locale_gen", [])
        if locale_gen:
            content = "\n".join(locale_gen) + "\n"
            self.write_file("/etc/locale.gen", content)
            self.run_cmd("Generate locales", "locale-gen")

        timezone = cfg.get("timezone", "")
        if timezone:
            self.run_cmd(f"Set timezone to {timezone}", f"timedatectl set-timezone {timezone}")

        hostname = cfg.get("hostname", "")
        if hostname:
            self.run_cmd(f"Set hostname to {hostname}", f"hostnamectl set-hostname {hostname}")

    def step_packages(self):
        print("\n── Packages ──")
        pkgs = self.snap.get("packages", {})
        distro = self.distro

        # Detect/install AUR helper
        aur_helper = pkgs.get("aur_helper", "paru")
        if not _has_cmd(aur_helper):
            aur_helper = ensure_aur_helper() or ""

        # Native packages
        native = pkgs.get("native_explicit", [])
        if native:
            pkg_list = " ".join(native)
            self.run_cmd(
                f"Install {len(native)} native packages",
                f"pacman -S --needed --noconfirm {pkg_list}"
            )

        # AUR packages
        aur = pkgs.get("aur_packages", [])
        if aur and aur_helper:
            pkg_list = " ".join(aur)
            self.run_cmd(
                f"Install {len(aur)} AUR packages via {aur_helper}",
                f"{aur_helper} -S --needed --noconfirm {pkg_list}"
            )

        # Flatpak
        flatpak_apps = pkgs.get("flatpak", [])
        if flatpak_apps:
            for app in flatpak_apps:
                self.run_cmd(f"Install flatpak: {app}", f"flatpak install -y {app}")

    def step_dotfiles(self):
        print("\n── Dotfiles ──")
        dots = self.snap.get("dotfiles", {})
        home = Path(dots.get("home_directory", str(get_user_home())))
        key_dotfiles = dots.get("key_dotfiles", {})

        for rel, content in key_dotfiles.items():
            if content == "[REDACTED]":
                self.log("SKIP", f"{rel} (redacted — restore manually)")
                continue
            full_path = str(home / rel)
            self.write_file(full_path, content)

        # Clone git repos
        git_repos = dots.get("git_repos", [])
        for repo in git_repos:
            remote = repo.get("remote", "")
            path = repo.get("path", "")
            if remote and path:
                target = str(home / path)
                if not Path(target).exists():
                    self.run_cmd(f"Clone {remote} -> {target}", f"git clone {remote} {target}")
                else:
                    self.log("SKIP", f"Repo already exists: {target}")

    def step_config(self):
        print("\n── System Config ──")
        cfg = self.snap.get("config", {})

        # modprobe.d
        for name, content in cfg.get("modprobe_d", {}).items():
            self.write_file(f"/etc/modprobe.d/{name}", content)

        # udev rules
        for name, content in cfg.get("udev_rules", {}).items():
            self.write_file(f"/etc/udev/rules.d/{name}", content)
        if cfg.get("udev_rules"):
            self.run_cmd("Reload udev rules", "udevadm control --reload-rules && udevadm trigger")

        # tmpfiles.d
        for name, content in cfg.get("tmpfiles_d", {}).items():
            self.write_file(f"/etc/tmpfiles.d/{name}", content)

        # sysctl.d
        for name, content in cfg.get("sysctl_d", {}).items():
            if name != "99-active":  # skip the runtime dump
                self.write_file(f"/etc/sysctl.d/{name}", content)
        if cfg.get("sysctl_d"):
            self.run_cmd("Apply sysctl", "sysctl --system")

        # environment
        env = cfg.get("environment", "")
        if env:
            self.write_file("/etc/environment", env)

    def step_services(self):
        print("\n── Systemd Services ──")
        svcs = self.snap.get("services", {})

        # Write custom unit files first
        for name, content in svcs.get("custom_system_units", {}).items():
            self.write_file(f"/etc/systemd/system/{name}", content, mode=0o644)

        if svcs.get("custom_system_units"):
            self.run_cmd("Reload systemd daemon", "systemctl daemon-reload")

        # Enable services that were enabled in snapshot
        # Filter to only custom units to avoid enabling everything
        custom_names = set(svcs.get("custom_system_units", {}).keys())
        for unit in svcs.get("enabled_system_units", []):
            if any(unit.startswith(Path(n).stem) for n in custom_names):
                self.run_cmd(f"Enable {unit}", f"systemctl enable {unit}")

    def step_bootloader(self):
        print("\n── Bootloader ──")
        boot = self.snap.get("boot", {})
        bootloader = boot.get("bootloader", "unknown")

        if bootloader == "grub":
            grub_conf = boot.get("bootloader_config", {}).get("default_grub", "")
            if grub_conf:
                self.write_file("/etc/default/grub", grub_conf)
                self.run_cmd("Regenerate GRUB config", "grub-mkconfig -o /boot/grub/grub.cfg")

            mkinitcpio_conf = boot.get("mkinitcpio", {}).get("conf", "")
            if mkinitcpio_conf:
                self.write_file("/etc/mkinitcpio.conf", mkinitcpio_conf)
                self.run_cmd("Rebuild initramfs", "mkinitcpio -P")

        elif bootloader == "limine":
            limine_conf = boot.get("bootloader_config", {}).get("limine_conf", "")
            if limine_conf:
                self.write_file("/boot/limine.conf", limine_conf)
        else:
            self.log("WARN", f"Unknown bootloader '{bootloader}' — skipping")

    def step_swap(self):
        print("\n── Swap & Hibernate ──")
        fs = self.snap.get("filesystem", {})
        power = self.snap.get("power", {})
        hibernate = power.get("hibernate", {})

        swap_file = hibernate.get("swap_file", "")
        resume_uuid = hibernate.get("resume_uuid", "")
        resume_offset = hibernate.get("resume_offset", "")

        if not swap_file:
            self.log("SKIP", "No swapfile configured in snapshot")
            return

        self.log("WARN", "Swap/hibernate setup requires manual steps on a new install:")
        steps = [
            "1. Mount top-level btrfs: mount -o subvolid=5 /dev/nvme0n1p2 /mnt",
            "2. Create @swap subvolume: btrfs subvolume create /mnt/@swap",
            "3. Add to fstab (nodatacow, no compress)",
            "4. Mount /swap, disable CoW: chattr +C /swap/swapfile",
            f"5. Create swapfile: fallocate -l 128G {swap_file}",
            "6. mkswap + swapon",
            "7. Get new resume_offset: btrfs inspect-internal map-swapfile -r /swap/swapfile",
            "8. Update GRUB cmdline with new resume= and resume_offset=",
        ]
        for step in steps:
            self.log("WARN", step)

        if resume_uuid and resume_offset:
            self.log("WARN", f"Original resume_offset={resume_offset} — recalculate on new system")

    # ── Plan (terraform-style) ──────────────────────────────────────────────

    def plan(self, steps: list = None):
        """Show what would change, comparing snapshot desired state vs live system."""
        available_steps = {
            "locale":     self._plan_locale,
            "packages":   self._plan_packages,
            "dotfiles":   self._plan_dotfiles,
            "config":     self._plan_config,
            "services":   self._plan_services,
            "bootloader": self._plan_bootloader,
            "swap":       self._plan_swap,
        }
        selected = steps or list(available_steps.keys())

        print(f"\n{'═' * 60}")
        print(f"  PLAN — comparing snapshot vs live system")
        print(f"  Distro: {self.distro}")
        print(f"  Steps: {', '.join(selected)}")
        print(f"{'═' * 60}")
        print(f"  + create/install   ~ modify   - remove   = unchanged")
        print(f"{'─' * 60}")

        changes = 0
        for step_name in selected:
            if step_name in available_steps:
                changes += available_steps[step_name]()

        print(f"\n{'═' * 60}")
        if changes:
            print(f"  Plan: {changes} change(s). Run with --confirm to apply.")
        else:
            print(f"  Plan: no changes needed. System matches snapshot.")
        print(f"{'═' * 60}\n")
        return changes

    def _plan_locale(self) -> int:
        changes = 0
        cfg = self.snap.get("config", {})
        print("\n── Locale & Timezone ──")

        locale_conf = cfg.get("locale_conf", "")
        if locale_conf:
            current = read("/etc/locale.conf")
            if current != locale_conf:
                print(f"  ~ /etc/locale.conf")
                changes += 1
            else:
                print(f"  = /etc/locale.conf (unchanged)")

        tz = cfg.get("timezone", "")
        if tz:
            current_tz = read("/etc/timezone").strip() or run("timedatectl show -p Timezone --value")
            if current_tz != tz:
                print(f"  ~ timezone: {current_tz} → {tz}")
                changes += 1
            else:
                print(f"  = timezone: {tz}")

        hostname = cfg.get("hostname", "")
        if hostname:
            import socket
            current_host = socket.gethostname()
            if current_host != hostname:
                print(f"  ~ hostname: {current_host} → {hostname}")
                changes += 1
            else:
                print(f"  = hostname: {hostname}")
        return changes

    def _plan_packages(self) -> int:
        changes = 0
        pkgs = self.snap.get("packages", {})
        print("\n── Packages ──")

        # Native explicit
        desired = set(pkgs.get("native_explicit", []))
        installed = set(run_lines("pacman -Qqe 2>/dev/null"))
        to_install = sorted(desired - installed)
        to_remove = sorted(installed - desired)
        if to_install:
            print(f"  + install {len(to_install)} native packages:")
            for p in to_install[:20]:
                print(f"    + {p}")
            if len(to_install) > 20:
                print(f"    ... and {len(to_install) - 20} more")
            changes += len(to_install)
        if to_remove:
            print(f"  - {len(to_remove)} packages not in snapshot (will NOT be removed automatically)")

        # AUR
        aur_desired = set(pkgs.get("aur_packages", []))
        aur_installed = set(run_lines("pacman -Qqm 2>/dev/null"))
        aur_to_install = sorted(aur_desired - aur_installed)
        if aur_to_install:
            print(f"  + install {len(aur_to_install)} AUR packages:")
            for p in aur_to_install[:20]:
                print(f"    + {p}")
            if len(aur_to_install) > 20:
                print(f"    ... and {len(aur_to_install) - 20} more")
            changes += len(aur_to_install)

        # Flatpak
        flatpak_desired = set(pkgs.get("flatpak", []))
        flatpak_installed = set(run_lines("flatpak list --app --columns=application 2>/dev/null"))
        flatpak_to_install = sorted(flatpak_desired - flatpak_installed)
        if flatpak_to_install:
            print(f"  + install {len(flatpak_to_install)} flatpak apps:")
            for p in flatpak_to_install:
                print(f"    + {p}")
            changes += len(flatpak_to_install)

        if changes == 0:
            print(f"  = all packages match")
        return changes

    def _plan_dotfiles(self) -> int:
        changes = 0
        dots = self.snap.get("dotfiles", {})
        home = Path(dots.get("home_directory", str(get_user_home())))
        print("\n── Dotfiles ──")

        for rel, content in dots.get("key_dotfiles", {}).items():
            if content == "[REDACTED]":
                print(f"  - {rel} (redacted — manual restore)")
                continue
            full = home / rel
            if not full.exists():
                print(f"  + {rel} (new)")
                changes += 1
            elif full.read_text().strip() != content.strip():
                print(f"  ~ {rel} (modified)")
                changes += 1
            else:
                print(f"  = {rel}")

        if changes == 0:
            print(f"  = all dotfiles match")
        return changes

    def _plan_config(self) -> int:
        changes = 0
        cfg = self.snap.get("config", {})
        print("\n── System Config ──")

        for section, basedir in [
            ("modprobe_d", "/etc/modprobe.d"),
            ("udev_rules", "/etc/udev/rules.d"),
            ("tmpfiles_d", "/etc/tmpfiles.d"),
            ("sysctl_d",   "/etc/sysctl.d"),
        ]:
            for name, content in cfg.get(section, {}).items():
                if section == "sysctl_d" and name == "99-active":
                    continue
                path = f"{basedir}/{name}"
                current = read(path)
                if not current:
                    print(f"  + {path} (new)")
                    changes += 1
                elif current.strip() != content.strip():
                    print(f"  ~ {path} (modified)")
                    changes += 1
                else:
                    print(f"  = {path}")

        env_desired = cfg.get("environment", "")
        if env_desired:
            current_env = read("/etc/environment")
            if current_env.strip() != env_desired.strip():
                print(f"  ~ /etc/environment")
                changes += 1
            else:
                print(f"  = /etc/environment")

        if changes == 0:
            print(f"  = all config files match")
        return changes

    def _plan_services(self) -> int:
        changes = 0
        svcs = self.snap.get("services", {})
        print("\n── Systemd Services ──")

        # Custom unit files
        for name, content in svcs.get("custom_system_units", {}).items():
            path = f"/etc/systemd/system/{name}"
            current = read(path)
            if not current:
                print(f"  + {path} (new)")
                changes += 1
            elif current.strip() != content.strip():
                print(f"  ~ {path} (modified)")
                changes += 1
            else:
                print(f"  = {path}")

        if changes == 0:
            print(f"  = all services match")
        return changes

    def _plan_bootloader(self) -> int:
        changes = 0
        boot = self.snap.get("boot", {})
        bootloader = boot.get("bootloader", "unknown")
        print(f"\n── Bootloader ({bootloader}) ──")

        if bootloader == "grub":
            grub_conf = boot.get("bootloader_config", {}).get("default_grub", "")
            if grub_conf:
                current = read("/etc/default/grub")
                if current.strip() != grub_conf.strip():
                    print(f"  ~ /etc/default/grub (modified → will regenerate grub.cfg)")
                    changes += 1
                else:
                    print(f"  = /etc/default/grub")

            mkinit = boot.get("mkinitcpio", {}).get("conf", "")
            if mkinit:
                current = read("/etc/mkinitcpio.conf")
                if current.strip() != mkinit.strip():
                    print(f"  ~ /etc/mkinitcpio.conf (modified → will rebuild initramfs)")
                    changes += 1
                else:
                    print(f"  = /etc/mkinitcpio.conf")
        elif bootloader == "limine":
            limine_conf = boot.get("bootloader_config", {}).get("limine_conf", "")
            if limine_conf:
                for p in ["/boot/limine.conf", "/boot/limine/limine.conf"]:
                    current = read(p)
                    if current:
                        if current.strip() != limine_conf.strip():
                            print(f"  ~ {p}")
                            changes += 1
                        else:
                            print(f"  = {p}")
                        break
        else:
            print(f"  ? unknown bootloader: {bootloader}")

        if changes == 0:
            print(f"  = bootloader config matches")
        return changes

    def _plan_swap(self) -> int:
        power = self.snap.get("power", {})
        hibernate = power.get("hibernate", {})
        swap_file = hibernate.get("swap_file", "")
        print("\n── Swap & Hibernate ──")
        if not swap_file:
            print(f"  = no swapfile in snapshot")
            return 0
        if Path(swap_file).exists():
            print(f"  = {swap_file} exists")
        else:
            print(f"  + {swap_file} (manual setup required)")
        print(f"  ⚠ swap/hibernate always requires manual verification")
        return 0

    def apply(self, steps: list = None):
        available_steps = {
            "locale":     self.step_locale,
            "packages":   self.step_packages,
            "dotfiles":   self.step_dotfiles,
            "config":     self.step_config,
            "services":   self.step_services,
            "bootloader": self.step_bootloader,
            "swap":       self.step_swap,
        }

        selected = steps or list(available_steps.keys())
        mode = "APPLYING" if self.confirm else "DRY RUN"

        print(f"\n{'═' * 60}")
        print(f"  {mode} — distro: {self.distro}")
        print(f"  Steps: {', '.join(selected)}")
        if not self.confirm:
            print("  Pass --confirm to actually apply changes")
        print(f"{'═' * 60}")

        for step_name in selected:
            if step_name in available_steps:
                available_steps[step_name]()
            else:
                print(f"\n⚠ Unknown step: {step_name}")

        print(f"\n{'═' * 60}")
        ok = sum(1 for s, _ in self.actions if s == "OK")
        dry = sum(1 for s, _ in self.actions if s == "DRY")
        warn = sum(1 for s, _ in self.actions if s == "WARN")
        err = sum(1 for s, _ in self.actions if s == "ERROR")
        print(f"  Summary: {ok} applied, {dry} dry-run, {warn} warnings, {err} errors")
        if not self.confirm:
            print("  Run with --confirm to apply all changes")
        print(f"{'═' * 60}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# DIFF ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def diff_snapshots(old: dict, new: dict):
    """Compare two snapshots and print a human-readable diff."""

    def compare_lists(label: str, old_list: list, new_list: list):
        old_set = set(old_list)
        new_set = set(new_list)
        added = sorted(new_set - old_set)
        removed = sorted(old_set - new_set)
        if added or removed:
            print(f"\n  {label}:")
            for p in added:
                print(f"    + {p}")
            for p in removed:
                print(f"    - {p}")

    def compare_dicts(label: str, old_d: dict, new_d: dict):
        all_keys = set(old_d) | set(new_d)
        changes = []
        for k in sorted(all_keys):
            ov = old_d.get(k, "<missing>")
            nv = new_d.get(k, "<missing>")
            if ov != nv:
                changes.append((k, ov, nv))
        if changes:
            print(f"\n  {label}:")
            for k, ov, nv in changes:
                print(f"    {k}:")
                print(f"      old: {str(ov)[:100]}")
                print(f"      new: {str(nv)[:100]}")

    print(f"\n{'═' * 60}")
    print("  SNAPSHOT DIFF")
    print(f"  Old: {old.get('meta', {}).get('generated_at', 'unknown')}")
    print(f"  New: {new.get('meta', {}).get('generated_at', 'unknown')}")
    print(f"{'═' * 60}")

    # Meta
    print("\n── Meta ──")
    old_host = old.get("meta", {}).get("hostname", "")
    new_host = new.get("meta", {}).get("hostname", "")
    if old_host != new_host:
        print(f"  hostname: {old_host} → {new_host}")
    old_distro = old.get("meta", {}).get("distro", "")
    new_distro = new.get("meta", {}).get("distro", "")
    if old_distro != new_distro:
        print(f"  distro: {old_distro} → {new_distro}")

    # Packages
    print("\n── Packages ──")
    compare_lists(
        "Native explicit",
        old.get("packages", {}).get("native_explicit", []),
        new.get("packages", {}).get("native_explicit", []),
    )
    compare_lists(
        "AUR packages",
        old.get("packages", {}).get("aur_packages", []),
        new.get("packages", {}).get("aur_packages", []),
    )
    compare_lists(
        "Flatpak",
        old.get("packages", {}).get("flatpak", []),
        new.get("packages", {}).get("flatpak", []),
    )

    # Kernel
    print("\n── Kernel ──")
    old_k = old.get("kernel", {}).get("version", "")
    new_k = new.get("kernel", {}).get("version", "")
    if old_k != new_k:
        print(f"  kernel: {old_k} → {new_k}")

    old_cmd = old.get("kernel", {}).get("cmdline", "")
    new_cmd = new.get("kernel", {}).get("cmdline", "")
    if old_cmd != new_cmd:
        print(f"  cmdline changed:")
        print(f"    old: {old_cmd}")
        print(f"    new: {new_cmd}")

    compare_lists(
        "Loaded modules",
        old.get("kernel", {}).get("loaded_modules", []),
        new.get("kernel", {}).get("loaded_modules", []),
    )

    # Services
    print("\n── Services ──")
    compare_lists(
        "Enabled units",
        old.get("services", {}).get("enabled_system_units", []),
        new.get("services", {}).get("enabled_system_units", []),
    )
    compare_lists(
        "Custom units",
        list(old.get("services", {}).get("custom_system_units", {}).keys()),
        list(new.get("services", {}).get("custom_system_units", {}).keys()),
    )

    # Config
    print("\n── Config ──")
    compare_dicts(
        "modprobe.d",
        old.get("config", {}).get("modprobe_d", {}),
        new.get("config", {}).get("modprobe_d", {}),
    )
    compare_dicts(
        "udev rules",
        old.get("config", {}).get("udev_rules", {}),
        new.get("config", {}).get("udev_rules", {}),
    )

    # Power
    print("\n── Power ──")
    old_profile = old.get("power", {}).get("power_profile", "")
    new_profile = new.get("power", {}).get("power_profile", "")
    if old_profile != new_profile:
        print(f"  power_profile: {old_profile} → {new_profile}")

    # Development tools
    print("\n── Development Tools ──")
    compare_dicts(
        "Tool versions",
        old.get("development", {}).get("tools", {}),
        new.get("development", {}).get("tools", {}),
    )
    compare_lists(
        "Ollama models",
        old.get("development", {}).get("ollama_models", []),
        new.get("development", {}).get("ollama_models", []),
    )

    # Dotfiles
    print("\n── Dotfiles ──")
    compare_lists(
        ".config directories",
        old.get("dotfiles", {}).get("config_directories", []),
        new.get("dotfiles", {}).get("config_directories", []),
    )

    print(f"\n{'═' * 60}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_snapshot(args):
    if os.geteuid() != 0:
        print("⚠  Warning: not running as root. Some data will be unavailable.", file=sys.stderr)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    outfile = args.output or str(SNAPSHOT_DIR / f"archero-snapshot-{ts}.json")
    selected = args.sections or list(ALL_COLLECTORS.keys())
    snapshot = {}

    for name in selected:
        print(f"  [{name}]...", end=" ", flush=True)
        try:
            if name == "notes":
                snapshot[name] = collect_notes(
                    note_modules=getattr(args, 'note_modules', None),
                    disable_note_modules=getattr(args, 'disable_note_modules', None),
                )
            else:
                snapshot[name] = ALL_COLLECTORS[name]()
            print("ok")
        except Exception as e:
            snapshot[name] = {"error": str(e)}
            print(f"ERROR: {e}")

    indent = 2 if args.pretty else None
    output = json.dumps(snapshot, indent=indent, ensure_ascii=False, default=str)
    Path(outfile).write_text(output)

    size_kb = len(output) / 1024
    print(f"\n✓ Snapshot: {outfile} ({size_kb:.1f} KB)")
    print(f"  Sections: {', '.join(selected)}")


def cmd_apply(args):
    snap_path = args.snapshot
    if not Path(snap_path).exists():
        print(f"✗ Snapshot file not found: {snap_path}", file=sys.stderr)
        sys.exit(1)

    with open(snap_path) as f:
        snapshot = json.load(f)

    applier = Applier(
        snapshot=snapshot,
        confirm=args.confirm,
        distro=args.distro,
    )
    if not args.confirm:
        # Plan mode — no root needed, just compare
        applier.plan(steps=args.steps)
    else:
        if os.geteuid() != 0:
            print("✗ apply --confirm requires root.", file=sys.stderr)
            sys.exit(1)
        applier.apply(steps=args.steps)


def cmd_diff(args):
    files = args.files

    if len(files) == 1:
        # One file: diff that file against the live system
        if not Path(files[0]).exists():
            print(f"✗ File not found: {files[0]}", file=sys.stderr)
            sys.exit(1)
        with open(files[0]) as f:
            old = json.load(f)
        print("  Capturing live system snapshot for comparison...")
        new = {name: fn() for name, fn in ALL_COLLECTORS.items()}
        new["meta"] = collect_meta()
        new["meta"]["generated_at"] += " (live)"
    elif len(files) == 2:
        # Two files: diff old against new
        for path in files:
            if not Path(path).exists():
                print(f"✗ File not found: {path}", file=sys.stderr)
                sys.exit(1)
        with open(files[0]) as f:
            old = json.load(f)
        with open(files[1]) as f:
            new = json.load(f)
    else:
        print("✗ diff requires 1 or 2 snapshot files.", file=sys.stderr)
        sys.exit(1)

    diff_snapshots(old, new)


# ═══════════════════════════════════════════════════════════════════════════════
# TUI BOOTSTRAP — auto-installs textual if missing
# ═══════════════════════════════════════════════════════════════════════════════

TEXTUAL_MIN = "0.47.0"


def _emoji_supported() -> bool:
    """Best-effort check for emoji support in the current terminal."""
    term = os.environ.get("TERM", "")
    colorterm = os.environ.get("COLORTERM", "")
    lang = os.environ.get("LANG", "") + os.environ.get("LC_ALL", "")
    # Most modern terminals on UTF-8 locales support emoji
    return "UTF" in lang.upper() or colorterm in ("truecolor", "24bit") or "256" in term


APP_NAME = "Archero 🏹" if _emoji_supported() else "Archero"


def _has_cmd(cmd: str) -> bool:
    return subprocess.run(f"which {cmd}", shell=True, capture_output=True).returncode == 0


def ensure_aur_helper() -> str | None:
    """
    Detect installed AUR helper. If none found, ask user which to install.
    Returns the helper name, or None if skipped.
    """
    for helper in ["paru", "yay", "trizen", "pikaur"]:
        if _has_cmd(helper):
            return helper

    # None found — ask
    print("\nNo AUR helper found (paru, yay, trizen, pikaur).")
    print("Choose one to install, or skip:")
    options = [
        ("1", "paru",   "Rust-based, recommended for CachyOS/Arch"),
        ("2", "yay",    "Go-based, most popular"),
        ("3", "skip",   "Skip AUR helper (AUR packages won't install)"),
    ]
    for key, name, desc in options:
        print(f"  {key}) {name:<8} {desc}")

    while True:
        choice = input("Choice [1/2/3]: ").strip()
        if choice == "1":
            helper = "paru"
            break
        elif choice == "2":
            helper = "yay"
            break
        elif choice == "3":
            print("Skipping AUR helper.")
            return None
        else:
            print("Enter 1, 2, or 3.")

    print(f"\nInstalling {helper} from AUR bootstrap...")
    bootstrap = f"""
        sudo pacman -S --needed --noconfirm base-devel git && \
        tmp=$(mktemp -d) && \
        git clone https://aur.archlinux.org/{helper}.git "$tmp/{helper}" && \
        cd "$tmp/{helper}" && \
        makepkg -si --noconfirm && \
        rm -rf "$tmp"
    """
    rc = subprocess.run(bootstrap, shell=True).returncode
    if rc == 0 and _has_cmd(helper):
        print(f"✓ {helper} installed.")
        return helper
    else:
        print(f"✗ Failed to install {helper}. Install it manually and retry.")
        return None


def ensure_textual() -> bool:
    """
    Install textual if not present. Tries in order:
      1. python-textual from pacman (CachyOS/Arch repos)
      2. paru/yay -S python-textual (AUR fallback)
      3. pip install textual --break-system-packages
      4. install python-pip via pacman, then pip install
    Returns True if textual is importable after attempts.
    """
    import importlib.util
    import importlib

    # Already installed?
    if importlib.util.find_spec("textual") is not None:
        return True

    print("textual not found — installing...")

    attempts = []

    # 1. pacman (official repos on Arch/CachyOS)
    if _has_cmd("pacman"):
        attempts.append((
            "pacman -S --noconfirm --needed python-textual",
            "pacman"
        ))

    # 2. paru or yay (AUR)
    for helper in ["paru", "yay", "trizen", "pikaur"]:
        if _has_cmd(helper):
            attempts.append((
                f"{helper} -S --noconfirm --needed python-textual",
                helper
            ))
            break

    # 3. pip --break-system-packages
    attempts.append((
        f"{sys.executable} -m pip install 'textual>={TEXTUAL_MIN}' --break-system-packages -q",
        "pip --break-system-packages"
    ))

    # 4. pip without flag (venv)
    attempts.append((
        f"{sys.executable} -m pip install 'textual>={TEXTUAL_MIN}' -q",
        "pip"
    ))

    for cmd, label in attempts:
        print(f"  trying {label}...", end=" ", flush=True)

        # If pip itself is missing, install python-pip first
        if "pip" in label:
            pip_check = run(f"{sys.executable} -m pip --version 2>&1")
            if "No module named pip" in pip_check:
                print("pip missing — installing python-pip first...")
                subprocess.run("pacman -S --noconfirm --needed python-pip", shell=True)

        rc = subprocess.run(cmd, shell=True).returncode
        if rc == 0:
            importlib.invalidate_caches()
            if importlib.util.find_spec("textual") is not None:
                print("ok")
                return True
            print("installed but not importable, trying next...")
        else:
            print("failed")

    print("\n✗ Could not install textual automatically. Try manually:")
    print("    sudo pacman -S python-textual")
    print("    # or:")
    print("    sudo pip install textual --break-system-packages")
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# TUI — full textual application
# ═══════════════════════════════════════════════════════════════════════════════

TUI_CSS = """
Screen {
    layout: horizontal;
}

#sidebar {
    width: 22;
    background: $surface;
    border-right: solid $primary-darken-2;
    padding: 0;
}

#sidebar-title {
    background: $primary-darken-2;
    color: $text;
    padding: 0 1;
    text-style: bold;
    height: 3;
    content-align: center middle;
}

.nav-item {
    padding: 0 2;
    height: 3;
    color: $text-muted;
    content-align: left middle;
}

.nav-item:hover {
    background: $surface-lighten-1;
    color: $text;
}

.nav-item.active {
    background: $primary-darken-2;
    color: $success;
    text-style: bold;
}

.nav-sep {
    height: 1;
    border-bottom: solid $primary-darken-2;
    margin: 0;
}

#main {
    width: 1fr;
    background: $background;
}

#panel-header {
    height: 3;
    background: $surface;
    border-bottom: solid $primary-darken-2;
    padding: 0 2;
    color: $success;
    text-style: bold;
    content-align: left middle;
}

#content {
    padding: 1 2;
    height: 1fr;
    overflow-y: auto;
}

#statusbar {
    height: 1;
    background: $surface;
    border-top: solid $primary-darken-2;
    padding: 0 1;
    color: $text-muted;
    content-align: left middle;
}

/* Stats panel */
.stat-grid {
    layout: grid;
    grid-size: 4;
    grid-gutter: 1;
    height: auto;
    margin-bottom: 1;
}

.stat-card {
    background: $surface;
    border: solid $primary-darken-2;
    padding: 0 1;
    height: 5;
}

.stat-label {
    color: $text-muted;
    text-style: italic;
}

.stat-value {
    color: $success;
    text-style: bold;
}

/* Package browser */
#pkg-search {
    margin-bottom: 1;
    border: solid $primary;
}

#pkg-list {
    height: 1fr;
    border: solid $primary-darken-2;
    background: $surface;
}

/* Snapshot history */
#history-list {
    height: 1fr;
    border: solid $primary-darken-2;
    background: $surface;
}

/* Log / apply */
#apply-log {
    height: 1fr;
    border: solid $primary-darken-2;
    background: $surface;
    overflow-y: auto;
}

.log-ok    { color: $success; }
.log-dry   { color: $text-muted; }
.log-warn  { color: $warning; }
.log-error { color: $error; }

/* Diff viewer */
#diff-container {
    layout: grid;
    grid-size: 2;
    grid-gutter: 1;
    min-height: 20;
    height: auto;
}

.diff-panel {
    border: solid $primary-darken-2;
    background: $surface;
    overflow-y: auto;
    padding: 0 1;
    min-height: 18;
}

.diff-added   { color: $success; }
.diff-removed { color: $error; }
.diff-neutral { color: $text-muted; }

/* Section checkboxes */
.section-grid {
    layout: grid;
    grid-size: 4;
    grid-gutter: 1;
    height: auto;
    margin-bottom: 1;
}

.snap-list {
    max-height: 6;
    height: auto;
    border: solid $primary-darken-2;
    margin-bottom: 1;
}

Button {
    margin-top: 1;
}

/* Snapshot panel */
#output-path {
    margin-bottom: 1;
    border: solid $primary;
}
"""


def launch_tui():
    """Launch the full textual TUI. Called when no CLI args given."""
    from textual.app import App, ComposeResult
    from textual.widgets import (
        Static, Input, ListView, ListItem, Log,
        Button, Checkbox, Label,
    )
    from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
    from textual.reactive import reactive
    from textual import work
    import threading
    import time

    SECTIONS = list(ALL_COLLECTORS.keys())

    PANELS = [
        ("snapshot",  "snapshot"),
        ("apply",     "apply"),
        ("diff",      "diff"),
        ("─────────", None),
        ("stats",     "stats"),
        ("packages",  "packages"),
        ("history",   "history"),
    ]

    class NavItem(Static):
        def __init__(self, label: str, panel_id: str | None, **kwargs):
            super().__init__(label, **kwargs)
            self.panel_id = panel_id
            self.add_class("nav-item")
            if panel_id is None:
                self.add_class("nav-sep")

        def on_click(self):
            if self.panel_id:
                nav_order = ["snapshot", "apply", "diff", "stats", "packages", "history"]
                if self.panel_id in nav_order:
                    self.app._sidebar_idx = nav_order.index(self.panel_id)
                self.app._switch_to_panel(self.panel_id)
                self.app._highlight_sidebar(self.app._sidebar_idx)
                self.app._in_sidebar = False
                self.app._esc_count = 0
                self.app._refresh_help()

    # ── Snapshot Panel ────────────────────────────────────────────────────────

    class SnapshotPanel(Container):
        def compose(self) -> ComposeResult:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            default_path = str(Path.home() / f".config/archero/snapshots/archero-snapshot-{ts}.json")
            yield Label("output path")
            yield Input(value=default_path, id="output-path")
            yield Label("sections")
            with Container(classes="section-grid"):
                for sec in SECTIONS:
                    yield Checkbox(sec, value=True, id=f"sec-{sec}")
            yield Button("run snapshot ↵", id="btn-snapshot", variant="success")
            yield Log(id="snap-log", auto_scroll=True)

        def on_button_pressed(self, event: Button.Pressed):
            if event.button.id == "btn-snapshot":
                self.run_snapshot()

        @work(thread=True)
        def run_snapshot(self):
            log = self.query_one("#snap-log", Log)
            output_path = self.query_one("#output-path", Input).value.strip()
            selected = [
                sec for sec in SECTIONS
                if self.query_one(f"#sec-{sec}", Checkbox).value
            ]
            log.clear()
            self.app.call_from_thread(
                self.app.notify, f"Generating snapshot ({len(selected)} sections)...",
                title="snapshot", severity="information"
            )
            log.write_line(f"capturing {len(selected)} sections...")
            snapshot = {}
            for name in selected:
                log.write_line(f"  [{name}]...")
                try:
                    snapshot[name] = ALL_COLLECTORS[name]()
                    log.write_line(f"  [{name}] ok")
                except Exception as e:
                    snapshot[name] = {"error": str(e)}
                    log.write_line(f"  [{name}] ERROR: {e}")
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False, default=str))
            size_kb = out.stat().st_size / 1024
            log.write_line(f"\n✓ saved: {output_path} ({size_kb:.1f} KB)")
            self.app.call_from_thread(
                self.app.notify, f"Saved → {Path(output_path).name} ({size_kb:.1f} KB)",
                title="snapshot done", severity="information"
            )

    # ── Snapshot file list helper ────────────────────────────────────────────────────────────────────────────────

    def _list_snapshots() -> list:
        found = sorted(glob.glob(str(SNAPSHOT_DIR / "*.json")), reverse=True)
        found += sorted(glob.glob(str(Path.home() / "*.json")), reverse=True)
        return list(dict.fromkeys(found))

    # ── Apply Panel ────────────────────────────────────────────────────────────────────────────────

    class ApplyPanel(Container):
        STEPS = ["locale", "packages", "dotfiles", "config", "services", "bootloader", "swap"]
        _filtered: list = []

        def compose(self) -> ComposeResult:
            yield Label("snapshot file")
            yield Input(placeholder="type to search snapshots...", id="apply-path")
            yield ListView(id="apply-snap-list", classes="snap-list")
            yield Label("steps")
            with Container(classes="section-grid"):
                for step in self.STEPS:
                    yield Checkbox(step, value=True, id=f"step-{step}")
            with Horizontal():
                yield Button("plan ↵", id="btn-dry", variant="default")
                yield Button("apply (confirm) ↵", id="btn-apply", variant="warning")
            yield Log(id="apply-log", auto_scroll=True)

        def on_mount(self):
            self._rebuild_list("")

        def _rebuild_list(self, q: str):
            snaps = _list_snapshots()
            lv = self.query_one("#apply-snap-list", ListView)
            lv.clear()
            self._filtered = []
            for path in snaps:
                if not q or q.lower() in path.lower():
                    lv.append(ListItem(Label(Path(path).name)))
                    self._filtered.append(path)
            if not self._filtered:
                lv.append(ListItem(Label("[dim]no snapshots found[/]")))

        def on_input_changed(self, event: Input.Changed):
            if event.input.id == "apply-path":
                self._rebuild_list(event.value)

        def on_list_view_selected(self, event: ListView.Selected):
            if event.list_view.id == "apply-snap-list":
                idx = event.list_view.index
                if idx is not None and idx < len(self._filtered):
                    self.query_one("#apply-path", Input).value = self._filtered[idx]

        def on_button_pressed(self, event: Button.Pressed):
            if event.button.id in ("btn-dry", "btn-apply"):
                self.run_apply(confirm=(event.button.id == "btn-apply"))

        @work(thread=True)
        def run_apply(self, confirm: bool):
            log = self.query_one("#apply-log", Log)
            snap_path = self.query_one("#apply-path", Input).value.strip()
            selected_steps = [s for s in self.STEPS if self.query_one(f"#step-{s}", Checkbox).value]
            log.clear()
            if not snap_path or not Path(snap_path).exists():
                log.write_line("✗ snapshot file not found")
                self.app.call_from_thread(self.app.notify, "Snapshot file not found", title="apply error", severity="error")
                return
            with open(snap_path) as f:
                snapshot = json.load(f)
            mode = "APPLYING" if confirm else "DRY RUN"
            log.write_line("═" * 50)
            log.write_line(f"  {mode} — steps: {', '.join(selected_steps)}")
            log.write_line("═" * 50)
            self.app.call_from_thread(self.app.notify, f"{mode} — {', '.join(selected_steps)}", title="apply", severity="warning" if confirm else "information")
            import io
            from contextlib import redirect_stdout
            class LogWriter(io.StringIO):
                def __init__(self, lw):
                    super().__init__()
                    self._log = lw
                def write(self, s):
                    if s.strip():
                        self._log.write_line(s.rstrip())
                    return len(s)
            applier = Applier(snapshot=snapshot, confirm=confirm, distro="auto")
            with redirect_stdout(LogWriter(log)):
                if not confirm:
                    n = applier.plan(steps=selected_steps)
                    self.app.call_from_thread(self.app.notify, f"Plan: {n} change(s)" if n else "No changes needed", title="plan done", severity="information")
                else:
                    applier.apply(steps=selected_steps)
                    ok  = sum(1 for s, _ in applier.actions if s == "OK")
                    dry = sum(1 for s, _ in applier.actions if s == "DRY")
                    err = sum(1 for s, _ in applier.actions if s == "ERROR")
                    self.app.call_from_thread(self.app.notify, f"{ok} applied · {dry} dry-run · {err} errors", title="apply done", severity="error" if err else "information")

    # ── Diff Panel ────────────────────────────────────────────────────────────────────────────────

    class DiffPanel(Container):
        _snaps: list = []
        _filtered_a: list = []  # filtered paths shown in list A
        _filtered_b: list = []  # filtered paths shown in list B

        def compose(self) -> ComposeResult:
            yield Label("file A  (older / saved snapshot)")
            yield Input(placeholder="type to search snapshots...", id="diff-a")
            yield ListView(id="diff-list-a", classes="snap-list")
            yield Label("file B  (newer — leave blank to compare vs live system)")
            yield Input(placeholder="type to search or leave empty for live system", id="diff-b")
            yield ListView(id="diff-list-b", classes="snap-list")
            yield Button("compare ↵", id="btn-diff", variant="success")
            with Horizontal(id="diff-container"):
                yield Log(id="diff-left",  auto_scroll=False, classes="diff-panel")
                yield Log(id="diff-right", auto_scroll=False, classes="diff-panel")

        def on_mount(self):
            self._snaps = _list_snapshots()
            self._rebuild_list("a", "")
            self._rebuild_list("b", "")

        def _rebuild_list(self, which: str, q: str):
            self._snaps = _list_snapshots()
            lv = self.query_one(f"#diff-list-{which}", ListView)
            lv.clear()
            filtered = []
            if which == "b":
                lv.append(ListItem(Label("(live system)")))
                filtered.append("")  # blank = live system
            for path in self._snaps:
                if not q or q.lower() in path.lower():
                    lv.append(ListItem(Label(Path(path).name)))
                    filtered.append(path)
            if which == "a":
                self._filtered_a = filtered
            else:
                self._filtered_b = filtered

        def on_input_changed(self, event: Input.Changed):
            if event.input.id == "diff-a":
                self._rebuild_list("a", event.value)
            elif event.input.id == "diff-b":
                self._rebuild_list("b", event.value)

        def on_list_view_selected(self, event: ListView.Selected):
            lv_id = event.list_view.id
            idx = event.list_view.index
            if lv_id == "diff-list-a" and idx is not None:
                if idx < len(self._filtered_a):
                    self.query_one("#diff-a", Input).value = self._filtered_a[idx]
            elif lv_id == "diff-list-b" and idx is not None:
                if idx < len(self._filtered_b):
                    self.query_one("#diff-b", Input).value = self._filtered_b[idx]

        def on_button_pressed(self, event: Button.Pressed):
            if event.button.id == "btn-diff":
                self.run_diff()

        @work(thread=True)
        def run_diff(self):
            left  = self.query_one("#diff-left",  Log)
            right = self.query_one("#diff-right", Log)
            path_a = self.query_one("#diff-a", Input).value.strip()
            path_b = self.query_one("#diff-b", Input).value.strip()
            left.clear()
            right.clear()
            if not path_a or not Path(path_a).exists():
                left.write_line("✗ file A not found")
                self.app.call_from_thread(self.app.notify, "File A not found", title="diff error", severity="error")
                return
            self.app.call_from_thread(self.app.notify, "Comparing..." if path_b else "Capturing live system...", title="diff", severity="information")
            with open(path_a) as f:
                old = json.load(f)
            if path_b and Path(path_b).exists():
                with open(path_b) as f:
                    new = json.load(f)
                right.write_line(f"file: {path_b}")
            else:
                right.write_line("capturing live system...")
                new = {name: fn() for name, fn in ALL_COLLECTORS.items()}
                right.write_line("live system captured")
            left.write_line(f"file: {path_a}")
            left.write_line(f"date: {old.get('meta', {}).get('generated_at', 'unknown')}")
            right.write_line(f"date: {new.get('meta', {}).get('generated_at', 'unknown')}")

            found_diff = False

            def cmp_lists(label, a, b):
                nonlocal found_diff
                added = sorted(set(b) - set(a))
                removed = sorted(set(a) - set(b))
                if added or removed:
                    found_diff = True
                    left.write_line(f"\n── {label} ──")
                    right.write_line(f"\n── {label} ──")
                    for p in removed:
                        left.write_line(f"  - {p}")
                        right.write_line("")
                    for p in added:
                        left.write_line("")
                        right.write_line(f"  + {p}")

            def cmp_dicts(label, old_d, new_d):
                nonlocal found_diff
                all_keys = set(old_d) | set(new_d)
                changes = []
                for k in sorted(all_keys):
                    ov = old_d.get(k, "<missing>")
                    nv = new_d.get(k, "<missing>")
                    if ov != nv:
                        changes.append((k, ov, nv))
                if changes:
                    found_diff = True
                    left.write_line(f"\n── {label} ──")
                    right.write_line(f"\n── {label} ──")
                    for k, ov, nv in changes:
                        left.write_line(f"  {k}: {str(ov)[:80]}")
                        right.write_line(f"  {k}: {str(nv)[:80]}")

            def cmp_scalar(label, a, b):
                nonlocal found_diff
                if a != b:
                    found_diff = True
                    left.write_line(f"\n── {label} ──")
                    right.write_line(f"\n── {label} ──")
                    left.write_line(f"  {a}")
                    right.write_line(f"  {b}")

            # Meta
            cmp_scalar("hostname", old.get("meta", {}).get("hostname", ""), new.get("meta", {}).get("hostname", ""))
            cmp_scalar("distro", old.get("meta", {}).get("distro", ""), new.get("meta", {}).get("distro", ""))

            # Hardware
            cmp_scalar("cpu", old.get("hardware", {}).get("cpu_model", ""), new.get("hardware", {}).get("cpu_model", ""))
            cmp_scalar("ram", old.get("hardware", {}).get("ram_total", ""), new.get("hardware", {}).get("ram_total", ""))

            # Boot
            cmp_scalar("bootloader", old.get("boot", {}).get("bootloader", ""), new.get("boot", {}).get("bootloader", ""))

            # Packages
            cmp_lists("native packages", old.get("packages", {}).get("native_explicit", []), new.get("packages", {}).get("native_explicit", []))
            cmp_lists("AUR packages", old.get("packages", {}).get("aur_packages", []), new.get("packages", {}).get("aur_packages", []))
            cmp_lists("flatpak", old.get("packages", {}).get("flatpak", []), new.get("packages", {}).get("flatpak", []))

            # Kernel
            cmp_scalar("kernel", old.get("kernel", {}).get("version", ""), new.get("kernel", {}).get("version", ""))
            cmp_lists("loaded modules", old.get("kernel", {}).get("loaded_modules", []), new.get("kernel", {}).get("loaded_modules", []))

            # Services
            cmp_lists("enabled services", old.get("services", {}).get("enabled_system_units", []), new.get("services", {}).get("enabled_system_units", []))
            cmp_lists("custom units", list(old.get("services", {}).get("custom_system_units", {}).keys()), list(new.get("services", {}).get("custom_system_units", {}).keys()))

            # Config
            cmp_dicts("modprobe.d", old.get("config", {}).get("modprobe_d", {}), new.get("config", {}).get("modprobe_d", {}))
            cmp_dicts("udev rules", old.get("config", {}).get("udev_rules", {}), new.get("config", {}).get("udev_rules", {}))

            # Power
            cmp_scalar("power profile", old.get("power", {}).get("power_profile", ""), new.get("power", {}).get("power_profile", ""))

            # Development
            cmp_dicts("tool versions", old.get("development", {}).get("tools", {}), new.get("development", {}).get("tools", {}))
            cmp_lists("ollama models", old.get("development", {}).get("ollama_models", []), new.get("development", {}).get("ollama_models", []))

            # Dotfiles
            cmp_lists(".config directories", old.get("dotfiles", {}).get("config_directories", []), new.get("dotfiles", {}).get("config_directories", []))

            if not found_diff:
                left.write_line("\n✓ no differences found")
                right.write_line("\n✓ no differences found")

            self.app.call_from_thread(self.app.notify, "Diff complete" if found_diff else "No differences found", title="diff done", severity="information")

    # ── Stats Panel ───────────────────────────────────────────────────────────

    class StatsPanel(Container):
        power_w  = reactive("–")
        battery  = reactive("–")
        gpu_temp = reactive("–")
        profile  = reactive("–")
        sclk     = reactive("–")
        cpu_gov  = reactive("–")
        mem_used = reactive("–")
        swap_used = reactive("–")

        def compose(self) -> ComposeResult:
            yield Label("live system stats  (refreshes every 3s)")
            with Container(classes="stat-grid"):
                yield Static("", id="s-power")
                yield Static("", id="s-battery")
                yield Static("", id="s-gpu")
                yield Static("", id="s-profile")
                yield Static("", id="s-sclk")
                yield Static("", id="s-gov")
                yield Static("", id="s-mem")
                yield Static("", id="s-swap")
            yield Log(id="stats-log", auto_scroll=True)

        def on_mount(self):
            self.refresh_stats()
            self.set_interval(3, self.refresh_stats)

        def _stat(self, widget_id, label, value, unit=""):
            w = self.query_one(f"#{widget_id}", Static)
            w.update(f"[dim]{label}[/]\n[bold green]{value}[/] [dim]{unit}[/]")

        @work(thread=True)
        def refresh_stats(self):
            power_raw  = read("/sys/class/power_supply/BAT0/power_now")
            energy_raw = read("/sys/class/power_supply/BAT0/energy_now")
            energy_full = read("/sys/class/power_supply/BAT0/energy_full")
            cap        = read("/sys/class/power_supply/BAT0/capacity")
            status     = read("/sys/class/power_supply/BAT0/status")
            profile    = run("powerprofilesctl get 2>/dev/null")
            gov        = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")

            # amdgpu pm info
            dri = _find_dri_card()
            pm_raw = run(f"cat /sys/kernel/debug/dri/{dri}/amdgpu_pm_info 2>/dev/null")
            gpu_temp = "–"
            sclk = "–"
            soc_w = "–"
            for line in pm_raw.splitlines():
                if "GPU Temperature" in line:
                    gpu_temp = line.split(":")[-1].strip()
                elif "(SCLK)" in line:
                    sclk = line.strip().split()[0] + " MHz"
                elif "average SoC" in line:
                    soc_w = line.strip().split()[0] + " W"

            # Memory
            mem_raw = run("free -m | awk '/^Mem:/{print $3, $2}'").split()
            mem_str = f"{mem_raw[0]}M / {mem_raw[1]}M" if len(mem_raw) == 2 else "–"

            swap_raw = run("free -m | awk '/^Swap:/{print $3, $2}'").split()
            swap_str = f"{swap_raw[0]}M / {swap_raw[1]}M" if len(swap_raw) == 2 else "–"

            try:
                power_w = f"{int(power_raw) / 1_000_000:.1f} W"
            except Exception:
                power_w = "–"

            self.app.call_from_thread(self._stat, "s-power",   "power draw",  power_w)
            self.app.call_from_thread(self._stat, "s-battery",  "battery",    f"{cap}% ({status})")
            self.app.call_from_thread(self._stat, "s-gpu",      "gpu temp",   gpu_temp)
            self.app.call_from_thread(self._stat, "s-profile",  "profile",    profile)
            self.app.call_from_thread(self._stat, "s-sclk",     "GPU clock",  sclk)
            self.app.call_from_thread(self._stat, "s-gov",      "CPU gov",    gov)
            self.app.call_from_thread(self._stat, "s-mem",      "RAM used",   mem_str)
            self.app.call_from_thread(self._stat, "s-swap",     "swap used",  swap_str)

    # ── Package Browser ───────────────────────────────────────────────────────

    class PackagesPanel(Container):
        _all_pkgs: list = []

        def compose(self) -> ComposeResult:
            yield Input(placeholder="search packages...", id="pkg-search")
            yield ListView(id="pkg-list")
            yield Label("", id="pkg-count")

        def on_mount(self):
            self.load_packages()

        @work(thread=True)
        def load_packages(self):
            pkgs = []
            aur_set = set(run_lines("pacman -Qqm"))
            for line in run_lines("pacman -Q"):
                parts = line.split(None, 1)
                if len(parts) == 2:
                    name, ver = parts
                    tag = "[AUR]" if name in aur_set else "     "
                    pkgs.append((name, ver, tag))
            self._all_pkgs = pkgs
            self.app.call_from_thread(self._populate, pkgs)
            self.app.call_from_thread(
                self.app.notify, f"{len(pkgs)} packages loaded",
                title="packages", severity="information"
            )

        def _populate(self, pkgs):
            lv = self.query_one("#pkg-list", ListView)
            lv.clear()
            for name, ver, tag in pkgs:
                lv.append(ListItem(Label(f"{tag}  {name}  [dim]{ver}[/]")))
            self.query_one("#pkg-count", Label).update(f"{len(pkgs)} packages")

        def on_input_changed(self, event: Input.Changed):
            q = event.value.lower()
            filtered = [(n, v, t) for n, v, t in self._all_pkgs if q in n.lower()]
            self._populate(filtered)

    # ── History Panel ─────────────────────────────────────────────────────────

    class HistoryPanel(Container):
        def compose(self) -> ComposeResult:
            yield Label("snapshot files found")
            yield ListView(id="history-list")
            yield Label("", id="history-detail")

        def on_mount(self):
            self.load_history()

        def load_history(self):
            lv = self.query_one("#history-list", ListView)
            lv.clear()
            patterns = [
                str(Path.home() / ".config/archero/snapshots/*.json"),
                str(Path.home() / "*.json"),
                "/tmp/archero-snapshot-*.json",
            ]
            found = []
            for pat in patterns:
                found.extend(sorted(glob.glob(pat), reverse=True))
            found = list(dict.fromkeys(found))  # dedupe

            if not found:
                lv.append(ListItem(Label("[dim]no snapshots found[/]")))
            else:
                for f in found:
                    stat = Path(f).stat()
                    size_kb = stat.st_size / 1024
                    ts = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                    lv.append(ListItem(Label(f"{ts}  {size_kb:6.0f} KB  {f}")))

        def on_list_view_selected(self, event: ListView.Selected):
            text = str(event.item.query_one(Label).renderable)
            parts = text.strip().split()
            path = parts[-1] if parts else ""
            if Path(path).exists():
                try:
                    with open(path) as f:
                        snap = json.load(f)
                    meta = snap.get("meta", {})
                    pkgs = snap.get("packages", {})
                    kernel = snap.get("kernel", {})
                    detail = (
                        f"host: {meta.get('hostname', '–')}  "
                        f"date: {meta.get('generated_at', '–')}  "
                        f"kernel: {kernel.get('version', '–')}  "
                        f"packages: {pkgs.get('counts', {}).get('total', '–')}"
                    )
                    self.query_one("#history-detail", Label).update(detail)
                except Exception as e:
                    self.query_one("#history-detail", Label).update(f"error: {e}")

    # ── Main App ──────────────────────────────────────────────────────────────

    class ArcheroApp(App):
        CSS = TUI_CSS
        TITLE = APP_NAME

        # No global arrow key bindings — handled via on_key with focus awareness
        BINDINGS = [
            ("ctrl+q",  "quit",          "quit"),
            ("1",       "nav_snapshot",  "snapshot"),
            ("2",       "nav_apply",     "apply"),
            ("3",       "nav_diff",      "diff"),
            ("4",       "nav_stats",     "stats"),
            ("5",       "nav_packages",  "packages"),
            ("6",       "nav_history",   "history"),
        ]

        _active_panel = reactive("snapshot")
        _NAV_ORDER = ["snapshot", "apply", "diff", "stats", "packages", "history"]
        _in_sidebar = True   # True = sidebar mode, False = panel mode
        _sidebar_idx = 0     # which sidebar item is highlighted
        _esc_count = 0       # track double-Esc

        def compose(self) -> ComposeResult:
            with Container(id="sidebar"):
                yield Static(APP_NAME, id="sidebar-title")
                for label, pid in PANELS:
                    yield NavItem(label, pid)

            with Container(id="main"):
                yield Static("snapshot", id="panel-header")
                with ScrollableContainer(id="content"):
                    yield SnapshotPanel(id="panel-snapshot")
                    yield ApplyPanel(id="panel-apply")
                    yield DiffPanel(id="panel-diff")
                    yield StatsPanel(id="panel-stats")
                    yield PackagesPanel(id="panel-packages")
                    yield HistoryPanel(id="panel-history")
                yield Static(self._help_text(), id="statusbar")

        def _help_text(self) -> str:
            distro = detect_distro()
            kernel = run("uname -r")
            profile = run("powerprofilesctl get 2>/dev/null")
            sys_info = f"{distro} · {kernel} · {profile}"
            if self._in_sidebar:
                keys = "↑↓ navigate · enter select · ctrl+q quit"
            else:
                keys = "esc esc → menu · tab next field · ctrl+q quit"
            return f" {sys_info}  │  {keys}"

        def on_mount(self):
            self._sidebar_idx = 0
            self._in_sidebar = True
            self._switch_to_panel("snapshot")
            self._highlight_sidebar(0)
            self.set_focus(None)  # sidebar handles focus via on_key
            self.set_interval(5, self._refresh_help)

        def _refresh_help(self):
            try:
                self.query_one("#statusbar", Static).update(self._help_text())
            except Exception:
                pass

        def _navigable_panels(self) -> list:
            """Return list of (label, panel_id) for navigable sidebar items."""
            return [(label, pid) for label, pid in PANELS if pid is not None]

        def _highlight_sidebar(self, idx: int):
            navigable = self._navigable_panels()
            for i, item in enumerate(self.query(NavItem)):
                item.remove_class("active")
                if item.panel_id and i == idx:
                    item.add_class("active")

        def _switch_to_panel(self, panel_id: str):
            self._active_panel = panel_id
            self.query_one("#panel-header", Static).update(panel_id)
            for _, pid in PANELS:
                if pid:
                    try:
                        self.query_one(f"#panel-{pid}").display = (pid == panel_id)
                    except Exception:
                        pass

        def _enter_panel(self):
            """Enter the currently highlighted panel — focus first widget inside it."""
            self._in_sidebar = False
            self._esc_count = 0
            panel_id = self._active_panel
            try:
                panel = self.query_one(f"#panel-{panel_id}")
                # Focus first focusable widget inside the panel
                focusable = panel.query("Input, ListView, Button, Checkbox")
                if focusable:
                    self.set_focus(focusable.first())
                else:
                    self.set_focus(panel)
            except Exception:
                pass
            self._refresh_help()

        def _return_to_sidebar(self):
            """Return focus to sidebar navigation."""
            self._in_sidebar = True
            self._esc_count = 0
            self.set_focus(None)
            self._highlight_sidebar(self._sidebar_idx)
            self._refresh_help()

        def on_key(self, event) -> None:
            key = event.key
            # ctrl+q always quits
            if key == "ctrl+q":
                self.action_quit()
                return

            # Esc handling — double-Esc returns to sidebar from panel
            if key == "escape":
                if not self._in_sidebar:
                    self._esc_count += 1
                    if self._esc_count >= 2:
                        event.stop()
                        self._return_to_sidebar()
                    else:
                        # First Esc — defocus current widget, stay in panel
                        self.set_focus(None)
                return

            # Number shortcuts always work
            num_map = {"1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5}
            if key in num_map:
                navigable = self._navigable_panels()
                idx = num_map[key]
                if idx < len(navigable):
                    self._sidebar_idx = idx
                    self._switch_to_panel(navigable[idx][1])
                    self._highlight_sidebar(idx)
                    self._in_sidebar = True
                    self.set_focus(None)
                    self._esc_count = 0
                    self._refresh_help()
                event.stop()
                return

            # Sidebar mode — arrow keys navigate, Enter enters panel
            if self._in_sidebar:
                navigable = self._navigable_panels()
                if key in ("up", "k"):
                    event.stop()
                    self._sidebar_idx = (self._sidebar_idx - 1) % len(navigable)
                    self._switch_to_panel(navigable[self._sidebar_idx][1])
                    self._highlight_sidebar(self._sidebar_idx)
                elif key in ("down", "j"):
                    event.stop()
                    self._sidebar_idx = (self._sidebar_idx + 1) % len(navigable)
                    self._switch_to_panel(navigable[self._sidebar_idx][1])
                    self._highlight_sidebar(self._sidebar_idx)
                elif key == "enter":
                    event.stop()
                    self._enter_panel()

            # Panel mode
            else:
                if key != "escape":
                    self._esc_count = 0
                focused = self.focused

                # ListView: let it handle up/down/enter natively
                if isinstance(focused, ListView):
                    if key in ("up", "down", "enter"):
                        return  # widget handles it
                    # Tab / right to move to next widget
                    if key in ("tab", "right"):
                        event.stop()
                        self.action_focus_next()
                        return
                    return

                # Input: down-arrow jumps to the associated ListView below
                if isinstance(focused, Input):
                    if key == "down":
                        event.stop()
                        # Map inputs to their paired ListView
                        input_list_map = {
                            "diff-a": "diff-list-a",
                            "diff-b": "diff-list-b",
                            "apply-path": "apply-snap-list",
                        }
                        list_id = input_list_map.get(focused.id)
                        try:
                            if list_id:
                                self.set_focus(self.query_one(f"#{list_id}", ListView))
                            else:
                                panel = self.query_one(f"#panel-{self._active_panel}")
                                lv = panel.query("ListView")
                                if lv:
                                    self.set_focus(lv.first())
                        except Exception:
                            self.action_focus_next()
                        return
                    if key == "tab":
                        event.stop()
                        self.action_focus_next()
                        return
                    return  # let Input handle all other keys (typing, left/right, etc.)

                if key in ("down", "right", "tab"):
                    event.stop()
                    self.action_focus_next()
                elif key in ("up", "left"):
                    event.stop()
                    self.action_focus_previous()

        # Number key actions (keep for binding table completeness)
        def action_nav_snapshot(self):
            self._sidebar_idx = 0; self._switch_to_panel("snapshot")
            self._highlight_sidebar(0); self._in_sidebar = True
        def action_nav_apply(self):
            self._sidebar_idx = 1; self._switch_to_panel("apply")
            self._highlight_sidebar(1); self._in_sidebar = True
        def action_nav_diff(self):
            self._sidebar_idx = 2; self._switch_to_panel("diff")
            self._highlight_sidebar(2); self._in_sidebar = True
        def action_nav_stats(self):
            self._sidebar_idx = 3; self._switch_to_panel("stats")
            self._highlight_sidebar(3); self._in_sidebar = True
        def action_nav_packages(self):
            self._sidebar_idx = 4; self._switch_to_panel("packages")
            self._highlight_sidebar(4); self._in_sidebar = True
        def action_nav_history(self):
            self._sidebar_idx = 5; self._switch_to_panel("history")
            self._highlight_sidebar(5); self._in_sidebar = True

    ArcheroApp().run()


def _show_loading(delay: float = 2.0):
    """Print ASCII art loading screen. delay=0 skips, delay>0 shows for that many seconds."""
    if delay <= 0:
        return

    import time

    R  = "\033[0m"
    B  = "\033[1m"
    C1 = "\033[38;5;39m"   # bright blue
    C2 = "\033[38;5;33m"   # mid blue
    C3 = "\033[38;5;27m"   # dark blue
    DM = "\033[38;5;240m"  # dim gray
    GR = "\033[38;5;46m"   # green
    YL = "\033[38;5;226m"  # yellow

    arrow = "\U0001f3f9 " if _emoji_supported() else "> "

    # Color full lines — no mid-line ANSI switches that corrupt column alignment
    W = 60  # content width between the two ║ characters

    def row(text="", color=""):
        """Pad text to exactly W chars and wrap in box borders."""
        padded = text.ljust(W)
        return f"{C1}{B}  \u2551{R}{color}{B}{padded}{R}{C1}{B}\u2551{R}"

    lines = [
        f"{C1}{B}  \u2554{'\u2550' * W}\u2557{R}",
        row(),
        row("   \u2588\u2588\u2588\u2588\u2588\u2557 \u2588\u2588\u2588\u2588\u2588\u2588\u2557  \u2588\u2588\u2588\u2588\u2588\u2588\u2557\u2588\u2588\u2557  \u2588\u2588\u2557\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557\u2588\u2588\u2588\u2588\u2588\u2588\u2557  \u2588\u2588\u2588\u2588\u2588\u2588\u2557 ", C1),
        row("  \u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2554\u2550\u2550\u2550\u2550\u255d\u2588\u2588\u2551  \u2588\u2588\u2551\u2588\u2588\u2554\u2550\u2550\u2550\u2550\u255d\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2554\u2550\u2550\u2550\u2588\u2588\u2557", C2),
        row("  \u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2551\u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d\u2588\u2588\u2551     \u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2551\u2588\u2588\u2588\u2588\u2588\u2557  \u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d\u2588\u2588\u2551   \u2588\u2588\u2551 ", C2),
        row("  \u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2551\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2551     \u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2551\u2588\u2588\u2554\u2550\u2550\u255d  \u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2551   \u2588\u2588\u2551 ", C3),
        row("  \u2588\u2588\u2551  \u2588\u2588\u2551\u2588\u2588\u2551  \u2588\u2588\u2551\u255a\u2588\u2588\u2588\u2588\u2588\u2588\u2557\u2588\u2588\u2551  \u2588\u2588\u2551\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557\u2588\u2588\u2551  \u2588\u2588\u2551\u255a\u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d ", C3),
        row("  \u255a\u2550\u255d  \u255a\u2550\u255d\u255a\u2550\u255d  \u255a\u2550\u255d \u255a\u2550\u2550\u2550\u2550\u2550\u255d\u255a\u2550\u255d  \u255a\u2550\u255d\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u255d\u255a\u2550\u255d  \u255a\u2550\u255d \u255a\u2550\u2550\u2550\u2550\u2550\u255d ", C3),
        row(),
        row("  CachyOS & Arch Linux  --  snapshot · apply · diff · tui  ", YL),
        row("  github.com/kinncj/archero · GPLv3                        ", DM),
        row(),
        f"{C1}{B}  \u255a{'\u2550' * W}\u255d{R}",
    ]

    print()
    for line in lines:
        print(line)
    print()
    print(f"  {GR}{B}{arrow}loading TUI...{R}")
    print()
    time.sleep(delay)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    # Pull --banner-delay early, before subcommand parsing
    banner_delay = 2.0
    args_copy = sys.argv[1:]
    if "--banner-delay" in args_copy:
        idx = args_copy.index("--banner-delay")
        try:
            banner_delay = float(args_copy[idx + 1])
            args_copy = args_copy[:idx] + args_copy[idx + 2:]
        except (IndexError, ValueError):
            pass

    # No args → launch TUI
    if not args_copy:
        if not ensure_textual():
            sys.exit(1)
        _show_loading(delay=banner_delay)
        launch_tui()
        return

    parser = argparse.ArgumentParser(
        description="Archero — CachyOS/Arch system snapshot, apply, and diff tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--banner-delay", type=float, default=2.0, metavar="SECONDS",
        help="Banner display duration in seconds (0 to skip, default: 2)"
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    # snapshot
    p_snap = sub.add_parser("snapshot", help="Capture current system state to JSON")
    p_snap.add_argument("--output", "-o", help="Output file (default: archero-snapshot-TIMESTAMP.json)")
    p_snap.add_argument("--pretty", "-p", action="store_true", help="Pretty-print JSON")
    p_snap.add_argument("--sections", "-s", nargs="+", choices=list(ALL_COLLECTORS.keys()),
                        help="Only collect specific sections")
    p_snap.add_argument("--note-modules", nargs="+",
                        choices=list(NOTE_MODULES.keys()),
                        help="Only run these note modules (overrides config)")
    p_snap.add_argument("--disable-note-modules", nargs="+",
                        choices=list(NOTE_MODULES.keys()),
                        help="Skip these note modules")

    # apply
    p_apply = sub.add_parser("apply", help="Restore system from a snapshot JSON")
    p_apply.add_argument("snapshot", help="Snapshot JSON file to apply")
    p_apply.add_argument("--confirm", action="store_true",
                         help="Actually apply changes (default is dry-run)")
    p_apply.add_argument("--distro", choices=["auto", "cachyos", "arch"], default="auto",
                         help="Target distro (default: auto-detect)")
    p_apply.add_argument("--steps", nargs="+",
                         choices=["locale", "packages", "dotfiles", "config",
                                  "services", "bootloader", "swap"],
                         help="Only apply specific steps")

    # diff
    p_diff = sub.add_parser(
        "diff",
        help="Compare snapshots. One arg: file vs live system. Two args: file vs file."
    )
    p_diff.add_argument(
        "files", nargs="+", metavar="SNAPSHOT",
        help="One snapshot (diff vs live system) or two snapshots (diff vs each other)"
    )

    args = parser.parse_args()

    if args.mode == "snapshot":
        cmd_snapshot(args)
    elif args.mode == "apply":
        cmd_apply(args)
    elif args.mode == "diff":
        cmd_diff(args)


if __name__ == "__main__":
    main()
