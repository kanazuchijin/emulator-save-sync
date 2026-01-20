#!/usr/bin/env python3
"""
This script synchronizes local emulator save files with a central *NAS* directory. This two-stage design ensures consistent behavior across Windows, Linux, and Steam Deck, and avoids direct file thrashing between multiple clients. Key features:
- Only overwrites destination if logic decides source is "newer."
- Uses content hashing (SHA-256) + timestamp with skew allowance; it, does NOT rely purely on timestamps to detect changes.
- Handles potential conflicts by keeping backups with hostname + timestamp.
- Safe by default: never deletes anything, just copies/updates and keeps conflict backups when in doubt.

The sync flow is local device ↔ NAS-location. Each machine runs this script, which performs the following steps in order:
    1) Local emulator save dirs -> NAS central directory
    2) NAS central directory    -> Local emulator saves dirs

How this handles conflicts:
- Step 1: Hash check first.
    - If content is identical, timestamps don't matter; the file is skipped.
- Step 2: Timestamp + skew.
    - If one file's mtime is clearly newer (beyond SKEW_ALLOWANCE_SECONDS), that version wins.
    - The other side will be updated when it is the "destination" in that direction.
- Step 3: Conflict window.
    - If mtimes are close (within skew allowance), that usually means:
        - Both changed around the same time, or
        - Clock skew is large enough, so don't trust the ordering.
    - In that case:
        - Copy the source over the destination (so sync still happens),
        - But first save a conflict backup of the overwritten file.

Edit the CONFIG section below to match your setup. For example, my paths are:
- Local central directory on Windows:           E:\saves_backup
- NAS central directory on Linux/Steam Deck:    /mnt/nasemulation/saves_backup

Run with:
    python savesync.py           # normal
    python savesync.py --dry-run # no actual copying, just logs
"""

from __future__ import annotations
import argparse
import hashlib
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List
import platform
import time

# ===================================================================== #
# ------------------------------ CONFIG ------------------------------- #
# ===================================================================== #
# Where all consolidated saves should live on THIS machine.
# You can point this at a NAS mount, local folder, etc.
# Example Windows:  r"D:/Emulation/saves"
# Example Linux:    "/mnt/nasemulation/saves" or "~/Emulation/saves"

def get_central_nas_root() -> Path:
    """
    Return the central NAS save root for this OS. For example:
      - Windows: E:\saves_backup
      - Linux/Steam Deck: /mnt/nasemulation/saves_backup
    """
    system = platform.system().lower()
    if system == "windows":
        return Path(r"E:\saves_backup")
    else:
        return Path("/mnt/nasemulation/saves_backup")

# How much clock skew (seconds) we tolerate before trusting
# "newer timestamp wins". Inside this window we treat it as a
# potential conflict and keep a backup of the overwritten file.
SKEW_ALLOWANCE_SECONDS = 300  # 5 minutes


@dataclass
class SaveSource:
    name: str          # human-friendly name
    path: Path         # directory to sync (must exist)

def get_save_sources() -> List[SaveSource]:
    """Return the list of save directories for this OS."""
    system = platform.system().lower()
    home = Path.home()

    sources: List[SaveSource] = []

    if system == "windows":
        # ---- WINDOWS PATHS (edit to match your setup) ---- #

        # RetroArch (Steam) – saves & states
        # Adjust Steam drive / path if different.
        retro_root = Path(r"D:\SteamLibrary\steamapps\common\RetroArch")
        sources += [
            SaveSource("retroarch_saves", retro_root / "saves"),
            SaveSource("retroarch_states", retro_root / "states"),
        ]

        # Dolphin – GameCube + Wii saves
        dolphin_root = home / "AppData" / "Roaming" / "Dolphin Emulator"
        sources += [
            SaveSource("dolphin_gc", dolphin_root / "GC"),
            SaveSource("dolphin_wii", dolphin_root / "Wii"),
        ]

        # Cemu – Wii U saves
        cemu_root = Path(r"D:/Emulation/cemu")
        sources.append(SaveSource("cemu_wiiu", cemu_root / "mlc01" / "usr" / "save"))

        # Add more here if you want:
        # sources.append(SaveSource("pcsx2", Path(r"D:/Emulation/pcsx2/memcards")))

    else:
        # ---- LINUX / STEAM DECK PATHS (edit as needed) ---- #

        # RetroArch (EmuDeck / Flatpak target paths)
        retro_root = Path("/home/deck/.var/app/org.libretro.RetroArch/config/retroarch")
        sources += [
            SaveSource("retroarch_saves", retro_root / "saves"),
            SaveSource("retroarch_states", retro_root / "states"),
        ]

        # Dolphin (EmuDeck / Flatpak target paths)
        dolphin_root = Path("/home/deck/.var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu")
        sources += [
            SaveSource("dolphin_gc", dolphin_root / "GC"),
            SaveSource("dolphin_wii", dolphin_root / "Wii"),
            SaveSource("dolphin_states", dolphin_root / "StateSaves"),
        ]

        # Cemu (EmuDeck)
        cemu_root = Path("~/Emulation/roms/wiiu").expanduser()
        sources.append(SaveSource("cemu_wiiu", cemu_root / "mlc01" / "usr" / "save"))

        # Add Deck-specific or other emulators here if you like.

    # Filter out non-existent dirs so the script doesn't crash
    existing = [s for s in sources if s.path.is_dir()]
    missing = [s for s in sources if not s.path.is_dir()]
    if missing:
        print("Warning: these save roots do not exist (edit paths or ignore):")
        for m in missing:
            print(f"  - {m.name}: {m.path}")
        print()

    return existing

# ===================================================================== #
# ---------------------- HELPER / CONFLICT LOGIC ---------------------- #
# ===================================================================== #
def copy_file_nfs_safe(src: Path, dst: Path) -> None:
    """
    Copy file contents without failing on NFS metadata operations.
    Many NFS servers allow writing file contents but reject setting atime/mtime from clients.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)   # content only

def sha256_of_file(path: Path) -> str:
    """Return SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def copy_with_smart_conflict(src: Path, dst: Path, dry_run: bool = False) -> bool:
    """
    Copy src -> dst with smarter conflict logic:
      - If dst does not exist: copy.
      - If dst exists:
          * If contents identical (hash equal): do nothing.
          * Else:
              - If timestamps differ by more than SKEW_ALLOWANCE_SECONDS:
                    newer timestamp wins.
              - Else (within skew window, possible conflict):
                    keep a backup of dst with .conflict-<hostname>-<time>
                    then copy src over dst.

    Returns True if a copy would happen (or did happen), False otherwise.
    """
    if not src.is_file():
        return False

    hostname = platform.node() or "unknownhost"

    if not dst.exists():
        print(f"  -> {dst}  (from {src}) [new file]")
        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            #shutil.copy2(src, dst)
            copy_file_nfs_safe(src, dst)
        return True

    # Both exist: check contents first
    src_hash = sha256_of_file(src)
    dst_hash = sha256_of_file(dst)

    if src_hash == dst_hash:
        # No real change
        return False

    src_mtime = src.stat().st_mtime
    dst_mtime = dst.stat().st_mtime
    dt = src_mtime - dst_mtime

    # Clear "newest" decision based on skew allowance
    if abs(dt) > SKEW_ALLOWANCE_SECONDS:
        if dt > 0:
            # Source clearly newer
            print(f"  -> {dst}  (from {src}) [src newer]")
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                #shutil.copy2(src, dst)
                copy_file_nfs_safe(src, dst)
            return True
        else:
            # Destination clearly newer – do nothing.
            # When we iterate from the other side, dst will act as src.
            return False

    # Within skew window: potential conflict
    # We choose to let src win but keep a backup of old dst.
    conflict_path = dst.with_suffix(
        dst.suffix + f".conflict-{hostname}-{int(time.time())}"
    )
    print(f"  !! Possible conflict on {dst}")
    print(f"     Keeping backup at {conflict_path}")
    print(f"     Overwriting with {src}")

    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        #shutil.copy2(dst, conflict_path)
        copy_file_nfs_safe(dst, conflict_path)
        #shutil.copy2(src, dst)
        copy_file_nfs_safe(src, dst)

    return True

# ==================================================================== #
# ---------------------------- SYNC LOGIC ---------------------------- #
# ==================================================================== #
def sync_source_to_central(source: SaveSource, central_root: Path, dry_run: bool = False) -> None:
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
        print(f"  !! Skipping: source directory not found.")
        return

    copied = 0
    for path in src_root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(src_root)
            dest = dest_root / rel
            if copy_with_smart_conflict(path, dest, dry_run=dry_run):
                copied += 1

    print(f"  Done: {copied} file(s) {'would be ' if dry_run else ''}copied/updated.")


def sync_central_to_source(source: SaveSource, central_root: Path, dry_run: bool = False) -> None:
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
        print(f"  !! Skipping: central directory not found.")
        return

    copied = 0
    for path in src_root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(src_root)
            dest = dest_root / rel
            if copy_with_smart_conflict(path, dest, dry_run=dry_run):
                copied += 1

    print(f"  Done: {copied} file(s) {'would be ' if dry_run else ''}copied/updated.")


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Sync emulator save files between local and central NAS."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be copied without actually copying.",
    )
    args = parser.parse_args(argv)

    central = get_central_nas_root()
    central.mkdir(parents=True, exist_ok=True)

    print(f"Central save root: {central}")
    print(f"Platform: {platform.system()} ({platform.platform()})")

    sources = get_save_sources()
    if not sources:
        print("No existing save sources found. Edit get_save_sources() paths.")
        return 1

    start = time.time()
    for src in sources:
        # 1) Push local -> NAS
        sync_source_to_central(src, central, dry_run=args.dry_run)
        # 2) Pull NAS -> local
        sync_central_to_source(src, central, dry_run=args.dry_run)

    elapsed = time.time() - start
    print(f"\nAll done in {elapsed:.1f} seconds.")
    if args.dry_run:
        print("No files were actually copied (dry run).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))