#!/usr/bin/env python3
"""
Synchronize local emulator save files with a central NAS directory.

This profile-based version supports three machines:
  - Windows desktop
  - Steam Deck
  - Linux desktop hostname: hammer-kubuntu

The sync flow is local device <-> NAS-location. Each machine runs this script,
which performs the following steps for each configured save source:
  1) Local emulator save dir -> NAS central directory
  2) NAS central directory -> Local emulator save dir

Conflict behavior:
  - Content hashes are checked first, so identical files are skipped even if
    timestamps differ.
  - If contents differ and one file is clearly newer, the newer file wins.
  - If contents differ but mtimes are close, the destination is backed up with
    a conflict suffix before being overwritten.

Run with:
    python savesync2.py
    python savesync2.py --dry-run
    python savesync2.py --profile linux_desktop --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence


# ===================================================================== #
# ------------------------------ CONFIG ------------------------------- #
# ===================================================================== #

SKEW_ALLOWANCE_SECONDS = 300  # 5 minutes

WINDOWS_CENTRAL_ROOT = Path(r"L:\saves_backup")
LINUX_CENTRAL_ROOT = Path("/mnt/nasemulation/saves_backup")


@dataclass(frozen=True)
class SaveSource:
    name: str
    path: Path


@dataclass(frozen=True)
class MachineProfile:
    name: str
    description: str
    hostnames: Sequence[str]
    central_root: Path
    sources: Sequence[SaveSource]


def flatpak_app_root(home: Path, app_id: str) -> Path:
    """Return the per-user Flatpak app root."""
    return home / ".var" / "app" / app_id


def build_windows_profile(home: Path) -> MachineProfile:
    """Return save sources for the Windows desktop."""
    windows_home = home if platform.system().lower() == "windows" else Path("C:/Users/B")
    retro_root = Path("D:/SteamLibrary/steamapps/common/RetroArch")
    dolphin_root = windows_home / "AppData" / "Roaming" / "Dolphin Emulator"
    cemu_root = Path(r"D:/Emulation/cemu")
    ryujinx_root = Path(r"C:/Users/B/AppData/Roaming/Ryujinx")

    return MachineProfile(
        name="windows",
        description="Windows desktop",
        hostnames=(),
        central_root=WINDOWS_CENTRAL_ROOT,
        sources=(
            SaveSource("retroarch_saves", retro_root / "saves"),
            SaveSource("retroarch_states", retro_root / "states"),
            SaveSource("dolphin_gc", dolphin_root / "GC"),
            SaveSource("dolphin_wii", dolphin_root / "Wii"),
            SaveSource("cemu_wiiu", cemu_root / "mlc01" / "usr" / "save"),
            SaveSource("ryujinx_switch", ryujinx_root / "bis"),
        ),
    )


def build_steam_deck_profile(home: Path) -> MachineProfile:
    """Return save sources for the Steam Deck."""
    deck_home = home if home.name == "deck" else Path("/home/deck")
    retro_root = flatpak_app_root(deck_home, "org.libretro.RetroArch") / "config" / "retroarch"
    dolphin_root = (
        flatpak_app_root(deck_home, "org.DolphinEmu.dolphin-emu")
        / "data"
        / "dolphin-emu"
    )
    cemu_root = deck_home / "Emulation" / "roms" / "wiiu"
    ryujinx_root = deck_home / ".config" / "Ryujinx"

    return MachineProfile(
        name="steam_deck",
        description="Steam Deck",
        hostnames=("steamdeck", "deck"),
        central_root=LINUX_CENTRAL_ROOT,
        sources=(
            SaveSource("retroarch_saves", retro_root / "saves"),
            SaveSource("retroarch_states", retro_root / "states"),
            SaveSource("dolphin_gc", dolphin_root / "GC"),
            SaveSource("dolphin_wii", dolphin_root / "Wii"),
            SaveSource("dolphin_states", dolphin_root / "StateSaves"),
            SaveSource("cemu_wiiu", cemu_root / "mlc01" / "usr" / "save"),
            SaveSource("ryujinx_switch", ryujinx_root / "bis"),
        ),
    )


def build_linux_desktop_profile(home: Path) -> MachineProfile:
    """Return save sources for the Linux desktop."""
    retro_root = flatpak_app_root(home, "org.libretro.RetroArch") / "config" / "retroarch"
    dolphin_root = (
        flatpak_app_root(home, "org.DolphinEmu.dolphin-emu")
        / "data"
        / "dolphin-emu"
    )
    cemu_mlc_root = Path("/mnt/gaming/emulation/cemu/mlc")
    ryujinx_root = home / ".config" / "Ryujinx"

    return MachineProfile(
        name="linux_desktop",
        description="Linux desktop",
        hostnames=("hammer-kubuntu",),
        central_root=LINUX_CENTRAL_ROOT,
        sources=(
            SaveSource("retroarch_saves", retro_root / "saves"),
            SaveSource("retroarch_states", retro_root / "states"),
            SaveSource("dolphin_gc", dolphin_root / "GC"),
            SaveSource("dolphin_wii", dolphin_root / "Wii"),
            SaveSource("dolphin_states", dolphin_root / "StateSaves"),
            SaveSource("cemu_wiiu", cemu_mlc_root / "usr" / "save"),
            # Future Ryujinx install. Missing paths are reported and skipped.
            SaveSource("ryujinx_switch", ryujinx_root / "bis"),
        ),
    )


def build_profiles(home: Path) -> Dict[str, MachineProfile]:
    profiles = (
        build_windows_profile(home),
        build_steam_deck_profile(home),
        build_linux_desktop_profile(home),
    )
    return {profile.name: profile for profile in profiles}


def normalized_hostname() -> str:
    """Return a lowercase short hostname for profile matching."""
    hostname = platform.node().strip().lower()
    if not hostname:
        return "unknownhost"
    return hostname.split(".", 1)[0]


def select_profile(profile_name: str, profiles: Dict[str, MachineProfile]) -> MachineProfile:
    """Select a machine profile by explicit name or by platform/hostname."""
    if profile_name != "auto":
        return profiles[profile_name]

    system = platform.system().lower()
    hostname = normalized_hostname()

    if system == "windows":
        return profiles["windows"]

    if system == "linux":
        for profile in profiles.values():
            if hostname in profile.hostnames:
                return profile

        linux_profiles = [
            profile
            for profile in profiles.values()
            if profile.central_root == LINUX_CENTRAL_ROOT
        ]
        known_hosts = ", ".join(
            host
            for profile in linux_profiles
            for host in profile.hostnames
        )
        raise RuntimeError(
            f"No Linux save-sync profile matches hostname '{hostname}'. "
            f"Known Linux hostnames: {known_hosts}. "
            "Run with --profile steam_deck or --profile linux_desktop, "
            "or add this hostname to the right profile."
        )

    raise RuntimeError(
        f"Unsupported platform '{platform.system()}'. "
        "Run with --profile if this machine should use an existing profile."
    )


def get_save_sources(profile: MachineProfile) -> List[SaveSource]:
    """Return the existing save directories for a profile."""
    existing = [source for source in profile.sources if source.path.is_dir()]
    missing = [source for source in profile.sources if not source.path.is_dir()]

    if missing:
        print("Warning: these save roots do not exist (edit paths or ignore):")
        for source in missing:
            print(f"  - {source.name}: {source.path}")
        print()

    return existing


# ===================================================================== #
# ---------------------- HELPER / CONFLICT LOGIC ---------------------- #
# ===================================================================== #

def copy_file_nfs_safe(src: Path, dst: Path) -> None:
    """
    Copy file contents without failing on NFS metadata operations.

    Some NFS servers allow writing file contents but reject setting atime/mtime
    from clients, so this avoids shutil.copy2 metadata writes.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def sha256_of_file(path: Path) -> str:
    """Return SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_with_smart_conflict(src: Path, dst: Path, dry_run: bool = False) -> bool:
    """
    Copy src -> dst with conflict-aware logic.

    Returns True if a copy would happen or did happen, False otherwise.
    """
    if not src.is_file():
        return False

    hostname = normalized_hostname()

    if not dst.exists():
        print(f"  -> {dst}  (from {src}) [new file]")
        if not dry_run:
            copy_file_nfs_safe(src, dst)
        return True

    src_hash = sha256_of_file(src)
    dst_hash = sha256_of_file(dst)

    if src_hash == dst_hash:
        return False

    src_mtime = src.stat().st_mtime
    dst_mtime = dst.stat().st_mtime
    dt = src_mtime - dst_mtime

    if abs(dt) > SKEW_ALLOWANCE_SECONDS:
        if dt > 0:
            print(f"  -> {dst}  (from {src}) [src newer]")
            if not dry_run:
                copy_file_nfs_safe(src, dst)
            return True

        return False

    conflict_path = dst.with_suffix(
        dst.suffix + f".conflict-{hostname}-{int(time.time())}"
    )
    print(f"  !! Possible conflict on {dst}")
    print(f"     Keeping backup at {conflict_path}")
    print(f"     Overwriting with {src}")

    if not dry_run:
        copy_file_nfs_safe(dst, conflict_path)
        copy_file_nfs_safe(src, dst)

    return True


# ===================================================================== #
# ---------------------------- SYNC LOGIC ----------------------------- #
# ===================================================================== #

def sync_source_to_central(
    source: SaveSource,
    central_root: Path,
    dry_run: bool = False,
) -> None:
    """
    Sync one SaveSource directory into central_root / <source.name> / ...
    (local emulator -> central NAS)
    """
    src_root = source.path
    dest_root = central_root / source.name

    print(f"\n=== Pushing {source.name} -> NAS ===")
    print(f"Source:      {src_root}")
    print(f"Destination: {dest_root}")
    print(f"Mode:        {'DRY RUN' if dry_run else 'LIVE'}")

    if not src_root.is_dir():
        print("  !! Skipping: source directory not found.")
        return

    copied = 0
    for path in src_root.rglob("*"):
        if path.is_file():
            rel_path = path.relative_to(src_root)
            dest = dest_root / rel_path
            if copy_with_smart_conflict(path, dest, dry_run=dry_run):
                copied += 1

    print(f"  Done: {copied} file(s) {'would be ' if dry_run else ''}copied/updated.")


def sync_central_to_source(
    source: SaveSource,
    central_root: Path,
    dry_run: bool = False,
) -> None:
    """
    Sync central_root / <source.name> / ... back into source.path
    (central NAS -> local emulator)
    """
    dest_root = source.path
    src_root = central_root / source.name

    print(f"\n=== Pulling NAS -> {source.name} ===")
    print(f"Source:      {src_root}")
    print(f"Destination: {dest_root}")
    print(f"Mode:        {'DRY RUN' if dry_run else 'LIVE'}")

    if not src_root.is_dir():
        print("  !! Skipping: central directory not found.")
        return

    copied = 0
    for path in src_root.rglob("*"):
        if path.is_file():
            rel_path = path.relative_to(src_root)
            dest = dest_root / rel_path
            if copy_with_smart_conflict(path, dest, dry_run=dry_run):
                copied += 1

    print(f"  Done: {copied} file(s) {'would be ' if dry_run else ''}copied/updated.")


def ensure_central_root(central_root: Path, dry_run: bool) -> bool:
    """Ensure the central root exists, with dry-run staying read-only."""
    if central_root.is_dir():
        return True

    if central_root.exists():
        print(f"Central save root exists but is not a directory: {central_root}")
        return False

    if dry_run:
        print(f"Central save root does not exist: {central_root}")
        print("Dry run mode: not creating central save root.")
        return False

    central_root.mkdir(parents=True, exist_ok=True)
    return True


def print_profiles(profiles: Dict[str, MachineProfile]) -> None:
    """Print configured profiles and sources."""
    for profile in profiles.values():
        hosts = ", ".join(profile.hostnames) if profile.hostnames else "platform auto"
        print(f"{profile.name}: {profile.description}")
        print(f"  Hostnames:    {hosts}")
        print(f"  Central root: {profile.central_root}")
        for source in profile.sources:
            print(f"  - {source.name}: {source.path}")
        print()


def main(argv: Sequence[str]) -> int:
    profiles = build_profiles(Path.home())
    profile_names = sorted(profiles)

    parser = argparse.ArgumentParser(
        description="Sync emulator save files between local machine and central NAS."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be copied without actually copying.",
    )
    parser.add_argument(
        "--profile",
        choices=["auto", *profile_names],
        default="auto",
        help="Machine profile to use. Default: auto-detect from platform/hostname.",
    )
    parser.add_argument(
        "--central-root",
        type=Path,
        help="Override the central NAS root for this run.",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="Print configured machine profiles and exit.",
    )
    args = parser.parse_args(argv)

    if args.list_profiles:
        print_profiles(profiles)
        return 0

    try:
        profile = select_profile(args.profile, profiles)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    central_root = (
        args.central_root.expanduser()
        if args.central_root is not None
        else profile.central_root
    )

    if not ensure_central_root(central_root, dry_run=args.dry_run):
        return 1

    print(f"Profile:           {profile.name} ({profile.description})")
    print(f"Hostname:          {normalized_hostname()}")
    print(f"Central save root: {central_root}")
    print(f"Platform:          {platform.system()} ({platform.platform()})")

    sources = get_save_sources(profile)
    if not sources:
        print("No existing save sources found. Edit the selected profile paths.")
        return 1

    start = time.time()
    for source in sources:
        sync_source_to_central(source, central_root, dry_run=args.dry_run)
        sync_central_to_source(source, central_root, dry_run=args.dry_run)

    elapsed = time.time() - start
    print(f"\nAll done in {elapsed:.1f} seconds.")
    if args.dry_run:
        print("No files were actually copied (dry run).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
