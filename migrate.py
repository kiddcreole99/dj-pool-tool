#!/usr/bin/env python3
"""
DJ Pool Tool — Migration Helper

Export:  python migrate.py export
         Creates dj-pool-data.zip with config, auth cache, and database.

Import:  python migrate.py import dj-pool-data.zip [--music-paths /mnt/music1 /mnt/music2]
         Extracts data files and optionally updates music library paths.
         Updates Windows paths in local_path column to Linux-style paths.
"""

import argparse
import json
import re
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path

DATA_DIR = Path("data")
EXPORT_FILES = ["config.json", ".spotify_cache", "tracks.db"]


def cmd_export(args):
    out = args.output or "dj-pool-data.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in EXPORT_FILES:
            path = DATA_DIR / name
            if path.exists():
                zf.write(path, name)
                print(f"  + {name}")
            else:
                print(f"  - {name} (not found, skipping)")
    print(f"\nExported to {out}")


def cmd_import(args):
    archive = Path(args.archive)
    if not archive.exists():
        print(f"Error: {archive} not found")
        sys.exit(1)

    DATA_DIR.mkdir(exist_ok=True)

    with zipfile.ZipFile(archive, "r") as zf:
        for name in EXPORT_FILES:
            if name in zf.namelist():
                zf.extract(name, DATA_DIR)
                print(f"  + data/{name}")
            else:
                print(f"  - {name} (not in archive)")

    # Update music paths if provided
    if args.music_paths:
        cfg_path = DATA_DIR / "config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            old_paths = cfg.get("music_paths", [])
            cfg["music_paths"] = args.music_paths
            cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            print(f"\nUpdated music_paths:")
            for old in old_paths:
                print(f"  - {old}")
            for new in args.music_paths:
                print(f"  + {new}")

    # Convert Windows paths in tracks.db to Linux paths
    db_path = DATA_DIR / "tracks.db"
    if db_path.exists() and args.music_paths:
        _convert_db_paths(db_path, args.path_map or [])

    print("\nImport complete. Review data/config.json before starting the app.")


def _convert_db_paths(db_path, path_maps):
    """Convert Windows-style local_path entries to Linux paths using path mappings."""
    if not path_maps:
        print("\nTip: Use --path-map to convert Windows paths in the database.")
        print('  Example: --path-map "C:\\DJ_Music=/mnt/dj_music"')
        return

    conn = sqlite3.connect(db_path)
    updated = 0
    for mapping in path_maps:
        if "=" not in mapping:
            print(f"  Warning: invalid path-map '{mapping}', expected 'old=new'")
            continue
        old, new = mapping.split("=", 1)
        # Normalize: handle both forward and back slashes
        cursor = conn.execute(
            "SELECT id, local_path FROM tracks WHERE local_path IS NOT NULL"
        )
        for row in cursor.fetchall():
            track_id, local_path = row
            # Normalize Windows backslashes for comparison
            normalized = local_path.replace("\\", "/")
            old_normalized = old.replace("\\", "/")
            if normalized.startswith(old_normalized):
                new_path = new + normalized[len(old_normalized):]
                conn.execute(
                    "UPDATE tracks SET local_path=? WHERE id=?",
                    (new_path, track_id),
                )
                updated += 1

    conn.commit()
    conn.close()
    if updated:
        print(f"\nConverted {updated} file path(s) in database.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DJ Pool Tool — Data Migration")
    sub = parser.add_subparsers(dest="command")

    exp = sub.add_parser("export", help="Export data to zip archive")
    exp.add_argument("-o", "--output", help="Output filename (default: dj-pool-data.zip)")

    imp = sub.add_parser("import", help="Import data from zip archive")
    imp.add_argument("archive", help="Path to dj-pool-data.zip")
    imp.add_argument("--music-paths", nargs="+", help="New music library paths (replaces existing)")
    imp.add_argument("--path-map", nargs="+",
                     help='Map Windows paths to Linux, e.g. "C:\\DJ_Music=/mnt/dj_music"')

    args = parser.parse_args()
    if args.command == "export":
        cmd_export(args)
    elif args.command == "import":
        cmd_import(args)
    else:
        parser.print_help()
