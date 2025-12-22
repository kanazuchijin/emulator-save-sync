#!/usr/bin/env python3
"""
Restore emulator saves from NAS central directory back to local emulator save dirs.

This is a *manual restore* tool, not a continuous sync:
- You search the NAS backup for a game folder (by keyword/titleID/etc).
- Then you restore that folder to the correct local emulator save directory.
- Safety-first: by default, backs up any existing local target before overwriting.
- Supports --dry-run.

Typical workflow (example: Cemu / BOTW):
1) List candidates on NAS:
     python restore_saves.py --source cemu_wiiu --find botw
     python restore_saves.py --source cemu_wiiu --find 101c   # title-id fragment, etc.

2) Restore a specific directory you found:
     python restore_saves.py --source cemu_wiiu --restore "mlc01/usr/save/00050000/101Cxxxx/user/80000001" --dry-run
     python restore_saves.py --source cemu_wiiu --restore "mlc01/usr/save/00050000/101Cxxxx/user/80000001"

Notes:
- For Cemu, the interesting saves usually live under: mlc01/usr/save/...
  and are organized by title IDs, region variants, and user slots.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Iterable


# ---------------------- CONFIG ---------------------- #

def get_nas_root() -> Path:
    """
    Must match your sync setup.

    Windows NAS mount: E:\\saves_backup
    Steam Deck/Linux NAS mount: /mnt/nasemulation/saves_backup
    """
    system = platform.system().lower()
    if system == "windows":
        return Path(r"E:\saves_backup")
    else:
        return Path("/mnt/nasemulation/saves_backup")


@dataclass
class SaveSource:
    name: str
    local_path: Path


def get_save_sources() -> List[SaveSource]:
    """
    Must match the local paths in your sync script.
    Edit if your emulator locations differ.
    """
    system = platform.system().lower()
    home = Path.home()
    sources: List[SaveSource] = []

    if system == "windows":
        retro_root = Path(r"D:\SteamLibrary\steamapps\common\RetroArch")
        sources += [
            SaveSource("retroarch_saves", retro_root / "saves"),
            SaveSource("retroarch_states", retro_root / "states"),
        ]

        dolphin_root = home / "AppData" / "Roaming" / "Dolphin Emulator"
        sources += [
            SaveSource("dolphin_gc", dolphin_root / "GC"),
            SaveSource("dolphin_wii", dolphin_root / "Wii"),
        ]

        cemu_root = Path(r"D:/Emulation/cemu")
        sources.append(SaveSource("cemu_wiiu", cemu_root / "mlc01" / "usr" / "save"))

    else:
        sources += [
            SaveSource("retroarch_saves", Path("~/.config/retroarch/saves").expanduser()),
            SaveSource("retroarch_states", Path("~/.config/retroarch/states").expanduser()),
        ]

        dolphin_root = Path("~/.local/share/dolphin-emu").expanduser()
        sources += [
            SaveSource("dolphin_gc", dolphin_root / "GC"),
            SaveSource("dolphin_wii", dolphin_root / "Wii"),
        ]

        cemu_root = Path("~/.var/app/info.cemu.Cemu/data/cemu").expanduser()
        sources.append(SaveSource("cemu_wiiu", cemu_root / "mlc01" / "usr" / "save"))

    # Keep even if missing; restore might be used to repopulate missing dirs
    return sources


# ---------------------- CORE HELPERS ---------------------- #

def iter_dirs(root: Path) -> Iterable[Path]:
    """Yield directories under root, including root itself, depth-first."""
    if not root.is_dir():
        return
    yield root
    for p in root.rglob("*"):
        if p.is_dir():
            yield p


def backup_existing(dest: Path, dry_run: bool) -> Path | None:
    """
    If dest exists, move it aside to a backup sibling with a timestamp.
    Returns backup path if created, else None.
    """
    if not dest.exists():
        return None

    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = dest.with_name(dest.name + f".pre-restore-backup-{ts}")

    print(f"  !! Backing up existing destination:")
    print(f"     {dest}  ->  {backup_path}")

    if not dry_run:
        # shutil.move works across filesystems, but usually this stays same FS
        shutil.move(str(dest), str(backup_path))

    return backup_path


def copy_tree(src: Path, dst: Path, dry_run: bool) -> None:
    """
    Copy src directory to dst directory, creating dst if needed.
    """
    print(f"  -> Restoring:")
    print(f"     {src}  ->  {dst}")

    if dry_run:
        return

    dst.parent.mkdir(parents=True, exist_ok=True)
    # Copy into dst; if dst doesn't exist, copytree is easiest.
    if not dst.exists():
        shutil.copytree(src, dst)
        return

    # If dst exists (rare here because we back it up), merge copy:
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def normalize_relpath(p: str) -> Path:
    """
    Accepts either a relative path like:
      mlc01/usr/save/...
    or a path with leading slashes/backslashes.
    """
    s = p.strip().lstrip("/").lstrip("\\")
    return Path(s)


# ---------------------- COMMANDS ---------------------- #

def cmd_list_sources(sources: List[SaveSource]) -> int:
    print("Available sources:")
    for s in sources:
        print(f"  - {s.name}")
        print(f"      local: {s.local_path}")
    return 0


def cmd_find(nas_root: Path, source_name: str, needle: str, max_results: int) -> int:
    src_root = nas_root / source_name
    if not src_root.is_dir():
        print(f"NAS source directory not found: {src_root}")
        return 2

    needle_l = needle.lower()
    hits: List[Path] = []
    for d in iter_dirs(src_root):
        # Match on folder name or full relative path
        rel = d.relative_to(src_root)
        hay = str(rel).lower()
        if needle_l in d.name.lower() or needle_l in hay:
            hits.append(rel)
            if len(hits) >= max_results:
                break

    if not hits:
        print(f"No matches for '{needle}' under {src_root}")
        return 1

    print(f"Matches for '{needle}' under NAS/{source_name}:")
    for h in hits:
        print(f"  {h}")

    print("\nTip: Use one of the above relative paths with --restore.")
    return 0


def cmd_restore(nas_root: Path, sources: List[SaveSource], source_name: str, rel_dir: str, dry_run: bool) -> int:
    source = next((s for s in sources if s.name == source_name), None)
    if not source:
        print(f"Unknown --source '{source_name}'. Run --list-sources to see options.")
        return 2

    rel = normalize_relpath(rel_dir)
    src_dir = nas_root / source_name / rel
    if not src_dir.is_dir():
        print(f"Restore source not found (must be a directory): {src_dir}")
        return 2

    dest_dir = source.local_path / rel

    print(f"NAS root:     {nas_root}")
    print(f"Source name:  {source_name}")
    print(f"Restore from: {src_dir}")
    print(f"Restore to:   {dest_dir}")
    print(f"Mode:         {'DRY RUN' if dry_run else 'LIVE'}\n")

    # Safety: back up existing destination directory if present
    backup_existing(dest_dir, dry_run=dry_run)
    # Restore
    copy_tree(src_dir, dest_dir, dry_run=dry_run)

    print("\nDone.")
    if dry_run:
        print("No changes made (dry run).")
    else:
        print("Restore completed. If anything looks wrong, revert using the .pre-restore-backup-* folder.")
    return 0


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Restore emulator saves from NAS backup to local emulator directories.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without copying/moving anything.")
    parser.add_argument("--list-sources", action="store_true", help="List known save sources and their local paths.")
    parser.add_argument("--source", help="Which source to search/restore (e.g., cemu_wiiu, dolphin_gc).")
    parser.add_argument("--find", metavar="TEXT", help="Search NAS/<source> for directories matching TEXT (case-insensitive).")
    parser.add_argument("--max-results", type=int, default=30, help="Max results for --find (default 30).")
    parser.add_argument("--restore", metavar="REL_DIR", help="Restore a directory REL_DIR (relative to NAS/<source>) back to local.")
    args = parser.parse_args(argv)

    nas_root = get_nas_root()
    sources = get_save_sources()

    if args.list_sources:
        return cmd_list_sources(sources)

    if not args.source:
        print("Error: --source is required unless --list-sources is used.")
        return 2

    if args.find:
        return cmd_find(nas_root, args.source, args.find, args.max_results)

    if args.restore:
        return cmd_restore(nas_root, sources, args.source, args.restore, dry_run=args.dry_run)

    print("Nothing to do. Use --find or --restore (or --list-sources).")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
