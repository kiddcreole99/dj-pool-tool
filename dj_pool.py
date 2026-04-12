#!/usr/bin/env python3
"""
DJ Pool Tool — Unified DJ Library Manager
  • Sync Spotify playlists and track downloads across DJ pools
  • Scan your music library for duplicate files

Usage:
  python dj_pool.py                          — show help
  python dj_pool.py setup                    — configure Spotify credentials & music paths
  python dj_pool.py sync [URL]               — sync a Spotify playlist
  python dj_pool.py list [--status ...]      — list tracks
  python dj_pool.py work [--status ...]      — interactive download session
  python dj_pool.py stats                    — progress summary
  python dj_pool.py export [--out FILE]      — export playlist to CSV
  python dj_pool.py dupes                    — scan saved music paths for duplicates
  python dj_pool.py dupes /path1 /path2      — scan specific paths
  python dj_pool.py dupes --no-fingerprint   — fast scan (hash + metadata only)
  python dj_pool.py dupes --export FILE      — save duplicate report to CSV
"""

import csv
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
import webbrowser
import argparse
from collections import defaultdict
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# ── Dependencies ───────────────────────────────────────────────────────────────

try:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
except ImportError:
    print("Missing dependency: spotipy. Run: pip install spotipy")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.table import Table
    from rich.prompt import Prompt, Confirm
    from rich.panel import Panel
    from rich.text import Text
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
    from rich import box
    from rich.markup import escape
except ImportError:
    print("Missing dependency: rich. Run: pip install rich")
    sys.exit(1)

try:
    import mutagen
    from mutagen.mp3 import MP3
    from mutagen.easyid3 import EasyID3
except ImportError:
    print("Missing dependency: mutagen. Run: pip install mutagen")
    sys.exit(1)

# ── Constants ──────────────────────────────────────────────────────────────────

console = Console(legacy_windows=False)

CONFIG_FILE = Path("data/config.json")
DB_FILE     = Path("data/tracks.db")

POOL_CONFIGS = {
    "mp3pool": {
        "name":         "MP3PoolOnline",
        "url_template": "https://mp3poolonline.com/results/?search={query}",
        "color":        "cyan",
        "emoji":        "🎵",
    },
    "beatport": {
        "name":         "Beatport",
        "url_template": "https://www.beatport.com/search?q={query}",
        "color":        "green",
        "emoji":        "🎧",
    },
}

STATUS_STYLES = {
    "pending":    ("⬜", "dim white", "Not started"),
    "found":      ("🟡", "yellow",   "Found - not downloaded"),
    "downloaded": ("🟢", "green",    "Downloaded"),
    "not_found":  ("🔴", "red",      "Not found on pools"),
    "skipped":    ("⚫", "dim",      "Skipped"),
}

AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".aac", ".m4a", ".ogg", ".wma"}

DUPE_METHOD_STYLE = {
    "hash":        ("🔴", "red",     "Exact duplicate (same bytes)"),
    "metadata":    ("🟡", "yellow",  "Same Artist + Title tags"),
    "fingerprint": ("🟠", "orange3", "Same audio (fingerprint match)"),
}

FINGERPRINT_THRESHOLD = 0.82

TRACK_COLS = ["id","playlist_id","playlist_name","track_name","artist","album",
              "duration_ms","added_at","status","found_on","notes","updated_at"]


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def save_config(cfg: dict):
    CONFIG_FILE.parent.mkdir(exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def cmd_setup(_args=None):
    """Interactive setup: Spotify credentials + saved music paths."""
    console.print(Panel.fit(
        "[bold cyan]DJ Pool Tool — Setup[/bold cyan]\n\n"
        "[bold]Step 1 — Spotify API[/bold]\n"
        "Create a free app at [link=https://developer.spotify.com/dashboard]"
        "developer.spotify.com/dashboard[/link]\n"
        "Add [bold]http://localhost:8888/callback[/bold] as a Redirect URI.\n\n"
        "[bold]Step 2 — Music Paths[/bold]\n"
        "Save your DJ music locations so [cyan]dupes[/cyan] can find them "
        "without typing paths every time.",
        border_style="cyan",
    ))

    cfg = load_config()

    # Spotify credentials
    cfg["client_id"]     = Prompt.ask("Spotify Client ID",     default=cfg.get("client_id", ""))
    cfg["client_secret"] = Prompt.ask("Spotify Client Secret", default=cfg.get("client_secret", ""), password=True)
    cfg.setdefault("redirect_uri", "http://127.0.0.1:8888/callback")

    # Music library paths
    existing = cfg.get("music_paths", [])
    console.print(f"\n[bold]Music library paths[/bold]")
    if existing:
        console.print("Current paths:")
        for i, p in enumerate(existing, 1):
            console.print(f"  {i}. {p}")
        if not Confirm.ask("Keep existing paths?", default=True):
            existing = []

    while True:
        new_path = Prompt.ask("Add a music path (Enter to finish)", default="", show_default=False)
        if not new_path.strip():
            break
        resolved = str(Path(new_path.strip()).expanduser().resolve())
        if resolved not in existing:
            existing.append(resolved)
            console.print(f"  [green]✓ Added:[/green] {resolved}")
        else:
            console.print(f"  [dim]Already in list:[/dim] {resolved}")

    cfg["music_paths"] = existing
    save_config(cfg)

    console.print("\n[green]✓ Setup saved to data/config.json[/green]")
    if existing:
        console.print(f"  Music paths ({len(existing)}):")
        for p in existing:
            console.print(f"    • {p}")
    else:
        console.print("  [yellow]No music paths saved — you can pass them directly to 'dupes'[/yellow]")


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tracks (
            id              TEXT PRIMARY KEY,
            playlist_id     TEXT,
            playlist_name   TEXT,
            track_name      TEXT,
            artist          TEXT,
            album           TEXT,
            duration_ms     INTEGER,
            added_at        TEXT,
            status          TEXT DEFAULT 'pending',
            found_on        TEXT DEFAULT '',
            notes           TEXT DEFAULT '',
            updated_at      TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS playlists (
            id          TEXT PRIMARY KEY,
            name        TEXT,
            owner       TEXT,
            total       INTEGER,
            last_sync   TEXT
        )
    """)
    conn.commit()
    return conn


def upsert_track(conn, track_data: dict):
    conn.execute("""
        INSERT INTO tracks (id, playlist_id, playlist_name, track_name, artist, album,
                            duration_ms, added_at, status, updated_at)
        VALUES (:id, :playlist_id, :playlist_name, :track_name, :artist, :album,
                :duration_ms, :added_at, 'pending', :updated_at)
        ON CONFLICT(id) DO UPDATE SET
            playlist_name = excluded.playlist_name,
            track_name    = excluded.track_name,
            artist        = excluded.artist,
            album         = excluded.album,
            updated_at    = excluded.updated_at
    """, track_data)
    conn.commit()


def update_track_status(conn, track_id: str, status: str, found_on: str = "", notes: str = ""):
    conn.execute("""
        UPDATE tracks SET status=?, found_on=?, notes=?, updated_at=? WHERE id=?
    """, (status, found_on, notes, datetime.now().isoformat(), track_id))
    conn.commit()


def get_tracks(conn, playlist_id: str = None, status_filter: str = None) -> list:
    q, params = "SELECT * FROM tracks WHERE 1=1", []
    if playlist_id:
        q += " AND playlist_id=?"; params.append(playlist_id)
    if status_filter and status_filter != "all":
        q += " AND status=?"; params.append(status_filter)
    q += " ORDER BY artist, track_name"
    return conn.execute(q, params).fetchall()


def get_playlists(conn) -> list:
    return conn.execute("SELECT * FROM playlists ORDER BY last_sync DESC").fetchall()


# ══════════════════════════════════════════════════════════════════════════════
# SPOTIFY
# ══════════════════════════════════════════════════════════════════════════════

def get_spotify_client(cfg: dict) -> spotipy.Spotify:
    return spotipy.Spotify(auth_manager=SpotifyOAuth(
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        redirect_uri=cfg.get("redirect_uri", "http://127.0.0.1:8888/callback"),
        scope="playlist-read-private playlist-read-collaborative",
        cache_path="data/.spotify_cache",
    ))


def extract_playlist_id(url_or_id: str) -> str:
    if "spotify.com/playlist/" in url_or_id:
        part = url_or_id.split("spotify.com/playlist/")[1]
        return part.split("?")[0].split("/")[0]
    return url_or_id.strip()


def sync_playlist(sp, conn, playlist_url: str) -> str:
    playlist_id = extract_playlist_id(playlist_url)

    with console.status("[cyan]Fetching playlist info...[/cyan]"):
        pl = sp.playlist(playlist_id)

    items_obj = pl.get("items") or pl.get("tracks") or {}
    pl_name  = pl["name"]
    pl_owner = pl["owner"]["display_name"]
    pl_total = items_obj.get("total", 0)
    console.print(f"\n[bold cyan]📋 {pl_name}[/bold cyan] by [dim]{pl_owner}[/dim] — {pl_total} tracks\n")

    conn.execute("""
        INSERT INTO playlists (id, name, owner, total, last_sync)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET name=excluded.name, total=excluded.total, last_sync=excluded.last_sync
    """, (playlist_id, pl_name, pl_owner, pl_total, datetime.now().isoformat()))
    conn.commit()

    results = sp.playlist_items(playlist_id)
    imported = skipped = 0

    with console.status("[cyan]Importing tracks...[/cyan]") as status:
        while results:
            for item in results["items"]:
                track = item.get("track") or item.get("item")
                if not track or track.get("is_local") or item.get("is_local"):
                    skipped += 1
                    continue
                artists = ", ".join(a["name"] for a in track.get("artists", []))
                upsert_track(conn, {
                    "id":            track["id"],
                    "playlist_id":   playlist_id,
                    "playlist_name": pl_name,
                    "track_name":    track["name"],
                    "artist":        artists,
                    "album":         track.get("album", {}).get("name", ""),
                    "duration_ms":   track.get("duration_ms", 0),
                    "added_at":      item.get("added_at", ""),
                    "updated_at":    datetime.now().isoformat(),
                })
                imported += 1
                status.update(f"[cyan]Imported {imported} tracks...[/cyan]")
            results = sp.next(results) if results["next"] else None

    console.print(f"[green]✓ Synced {imported} tracks[/green]" +
                  (f" [dim]({skipped} skipped)[/dim]" if skipped else ""))
    return playlist_id


# ══════════════════════════════════════════════════════════════════════════════
# POOL SEARCH
# ══════════════════════════════════════════════════════════════════════════════

def build_search_url(pool_key: str, artist: str, track_name: str) -> str:
    query = urllib.parse.quote(f"{artist} {track_name}")
    return POOL_CONFIGS[pool_key]["url_template"].format(query=query)


def open_search_urls(artist: str, track_name: str, pools: list = None):
    for pool_key in (pools or list(POOL_CONFIGS)):
        url = build_search_url(pool_key, artist, track_name)
        webbrowser.open(url)
        pcfg = POOL_CONFIGS[pool_key]
        console.print(f"  [{pcfg['color']}]→ Opened {pcfg['name']}[/]")


# ══════════════════════════════════════════════════════════════════════════════
# DISPLAY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def fmt_duration(ms: int) -> str:
    if not ms: return "--:--"
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


def fmt_size(b: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if b < 1024: return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} GB"


def fmt_dur(s: float) -> str:
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


def print_track_table(tracks: list, title: str = "Tracks"):
    if not tracks:
        console.print("[dim]No tracks found.[/dim]")
        return
    table = Table(title=title, box=box.SIMPLE_HEAD, show_lines=False,
                  header_style="bold cyan", expand=True)
    table.add_column("#",        width=4,  style="dim")
    table.add_column("Status",   width=4,  justify="center")
    table.add_column("Artist",   min_width=20)
    table.add_column("Track",    min_width=25)
    table.add_column("Album",    min_width=15, style="dim")
    table.add_column("Time",     width=6,  justify="right", style="dim")
    table.add_column("Found On", min_width=12, style="yellow")
    for i, row in enumerate(tracks, 1):
        t = dict(zip(TRACK_COLS, row))
        emoji, color, _ = STATUS_STYLES.get(t["status"], STATUS_STYLES["pending"])
        table.add_row(
            str(i), emoji,
            Text(t["artist"][:35],     style=color),
            Text(t["track_name"][:40], style=color),
            Text((t["album"] or "")[:25]),
            fmt_duration(t["duration_ms"]),
            t["found_on"] or "",
        )
    console.print(table)


def print_summary(tracks: list):
    counts = {}
    for row in tracks:
        t = dict(zip(TRACK_COLS, row))
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    parts = [
        f"{emoji} [{color}]{label}: {counts[s]}[/]"
        for s, (emoji, color, label) in STATUS_STYLES.items()
        if counts.get(s)
    ]
    console.print(Panel(
        f"[bold]Total: {len(tracks)}[/bold]  •  " + "  •  ".join(parts),
        border_style="dim", expand=False,
    ))


def pick_playlist(conn) -> str | None:
    playlists = get_playlists(conn)
    if not playlists:
        console.print("[yellow]No playlists synced yet. Run: python dj_pool.py sync[/yellow]")
        return None
    if len(playlists) == 1:
        return playlists[0][0]
    pl_cols = ["id","name","owner","total","last_sync"]
    table = Table(box=box.SIMPLE_HEAD, header_style="bold cyan")
    table.add_column("#",         width=4)
    table.add_column("Name",      min_width=25)
    table.add_column("Tracks",    width=7,  justify="right")
    table.add_column("Last Sync", width=20, style="dim")
    for i, row in enumerate(playlists, 1):
        p = dict(zip(pl_cols, row))
        table.add_row(str(i), p["name"], str(p["total"]), p["last_sync"][:16])
    console.print(table)
    idx = int(Prompt.ask("Select playlist #", default="1")) - 1
    return playlists[idx][0]


# ══════════════════════════════════════════════════════════════════════════════
# PLAYLIST COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

def cmd_sync(args, sp, conn):
    url = args.url or Prompt.ask("Playlist URL or ID")
    playlist_id = sync_playlist(sp, conn, url)
    print_summary(get_tracks(conn, playlist_id))


def cmd_list(args, conn):
    playlist_id = pick_playlist(conn)
    if playlist_id:
        status = args.status if args.status != "all" else None
        print_track_table(get_tracks(conn, playlist_id, status), f"Tracks — {args.status}")
        print_summary(get_tracks(conn, playlist_id))


def cmd_work(args, conn):
    playlist_id = pick_playlist(conn)
    if not playlist_id:
        return
    tracks = get_tracks(conn, playlist_id, args.status)
    if not tracks:
        console.print(f"[yellow]No tracks with status '{args.status}'.[/yellow]")
        return

    console.print(f"\n[bold cyan]Work Session[/bold cyan] — {len(tracks)} tracks to process\n")
    console.print("[dim]For each track, search links open in your browser.[/dim]\n")

    for i, row in enumerate(tracks, 1):
        t = dict(zip(TRACK_COLS, row))
        emoji, color, _ = STATUS_STYLES.get(t["status"], STATUS_STYLES["pending"])
        console.rule(f"[bold]{i}/{len(tracks)}[/bold]")
        console.print(
            f"\n  {emoji} [bold]{escape(t['artist'])}[/bold] — "
            f"[cyan]{escape(t['track_name'])}[/cyan]  "
            f"[dim]({escape(t['album'] or '')})[/dim]\n"
        )
        for pool_key, pcfg in POOL_CONFIGS.items():
            url = build_search_url(pool_key, t["artist"], t["track_name"])
            console.print(f"  [{pcfg['color']}]{pcfg['emoji']} {pcfg['name']}:[/] [link={url}]{url[:80]}[/link]")

        console.print()
        console.print("  [dim]o[/dim] open in browser    [dim]d[/dim] mark downloaded    [dim]f[/dim] mark found")
        console.print("  [dim]x[/dim] mark not found     [dim]s[/dim] skip               [dim]q[/dim] quit session\n")

        choice = Prompt.ask("  Action", choices=["o","d","f","x","s","q",""], default="o", show_choices=False)

        if choice == "q":
            console.print("[yellow]Session ended.[/yellow]")
            break
        if choice in ("o", ""):
            open_search_urls(t["artist"], t["track_name"])
            choice = Prompt.ask("  Now mark as", choices=["d","f","x","s",""],
                                default="", show_choices=False, show_default=False) or choice

        if choice == "d":
            found_on = Prompt.ask("  Found on which pool?", default="mp3pool/beatport")
            update_track_status(conn, t["id"], "downloaded", found_on)
            console.print("[green]  ✓ Marked as downloaded[/green]\n")
        elif choice == "f":
            found_on = Prompt.ask("  Found on which pool?", default="")
            update_track_status(conn, t["id"], "found", found_on)
            console.print("[yellow]  ✓ Marked as found[/yellow]\n")
        elif choice == "x":
            update_track_status(conn, t["id"], "not_found")
            console.print("[red]  ✗ Marked as not found[/red]\n")
        elif choice == "s":
            update_track_status(conn, t["id"], "skipped")
            console.print("[dim]  – Skipped[/dim]\n")

    print_summary(get_tracks(conn, playlist_id))


def cmd_stats(_args, conn):
    playlist_id = pick_playlist(conn)
    if playlist_id:
        print_summary(get_tracks(conn, playlist_id))


def cmd_export(args, conn):
    playlist_id = pick_playlist(conn)
    if not playlist_id:
        return
    tracks = get_tracks(conn, playlist_id)
    out = args.out or f"data/export_{playlist_id[:8]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Artist","Track","Album","Duration","Status","Found On","Notes",
                         "Spotify ID","MP3Pool Search","Beatport Search"])
        for row in tracks:
            t = dict(zip(TRACK_COLS, row))
            writer.writerow([
                t["artist"], t["track_name"], t["album"],
                fmt_duration(t["duration_ms"]),
                t["status"], t["found_on"], t["notes"], t["id"],
                build_search_url("mp3pool",  t["artist"], t["track_name"]),
                build_search_url("beatport", t["artist"], t["track_name"]),
            ])
    console.print(f"[green]✓ Exported {len(tracks)} tracks to {out}[/green]")


# ══════════════════════════════════════════════════════════════════════════════
# DUPLICATE SCANNER
# ══════════════════════════════════════════════════════════════════════════════

def discover_audio_files(paths: list) -> list:
    found = []
    for p in paths:
        root = Path(p).expanduser().resolve()
        if not root.exists():
            console.print(f"[yellow]⚠  Path not found, skipping: {root}[/yellow]")
            continue
        targets = [root] if root.is_file() else list(root.rglob("*"))
        for f in targets:
            if Path(f).is_file() and Path(f).suffix.lower() in AUDIO_EXTENSIONS:
                found.append(Path(f))
    return sorted(set(found))


def get_audio_info(path: Path) -> dict:
    info = {
        "path": str(path), "filename": path.name,
        "size": path.stat().st_size, "ext": path.suffix.lower(),
        "title": "", "artist": "", "album": "",
        "bitrate": 0, "duration": 0, "hash": "", "fingerprint": "",
    }
    try:
        if path.suffix.lower() == ".mp3":
            audio = MP3(path)
            info["bitrate"]  = int(audio.info.bitrate / 1000)
            info["duration"] = audio.info.length
            try:
                tags = EasyID3(path)
                info["title"]  = (tags.get("title",  [""])[0] or "").strip()
                info["artist"] = (tags.get("artist", [""])[0] or "").strip()
                info["album"]  = (tags.get("album",  [""])[0] or "").strip()
            except Exception:
                pass
        else:
            audio = mutagen.File(path)
            if audio and hasattr(audio, "info"):
                info["duration"] = getattr(audio.info, "length", 0)
                info["bitrate"]  = int(getattr(audio.info, "bitrate", 0) / 1000)
            if audio and audio.tags:
                def _tag(key):
                    v = audio.tags.get(key)
                    if isinstance(v, list): return str(v[0]).strip()
                    return str(v).strip() if v else ""
                info["title"]  = _tag("title") or _tag("TIT2")
                info["artist"] = _tag("artist") or _tag("TPE1")
                info["album"]  = _tag("album") or _tag("TALB")
    except Exception:
        pass
    return info


def compute_hash(path: Path, chunk: int = 65536) -> str:
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            while data := f.read(chunk):
                h.update(data)
        return h.hexdigest()
    except OSError:
        return ""


def compute_fingerprint(path: Path, fpcalc_bin: str = "fpcalc") -> str:
    try:
        result = subprocess.run(
            [fpcalc_bin, "-raw", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        for line in result.stdout.splitlines():
            if line.startswith("FINGERPRINT="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def normalize_tag(s: str) -> str:
    return re.sub(r"[\s\-_\(\)\[\]\.,'\"]+", " ", s.lower()).strip()


def metadata_key(info: dict) -> str:
    a, t = normalize_tag(info["artist"]), normalize_tag(info["title"])
    return f"{a}|{t}" if (a or t) else ""


def fingerprint_similarity(fp1: str, fp2: str, sample: int = 200) -> float:
    if not fp1 or not fp2:
        return 0.0
    try:
        v1 = [int(x) for x in fp1.split(",")[:sample]]
        v2 = [int(x) for x in fp2.split(",")[:sample]]
        length = min(len(v1), len(v2))
        if not length: return 0.0
        matches = sum(bin(v1[i] ^ v2[i]).count("0") for i in range(length))
        return matches / (length * 32)
    except Exception:
        return 0.0


def find_duplicates(file_infos: list, use_fingerprint: bool = True) -> list:
    groups, handled = [], set()

    # Pass 1 — exact file hash
    console.print("[cyan]  Pass 1/3:[/cyan] File hash (exact duplicates)...")
    hash_map = defaultdict(list)
    for fi in file_infos:
        if fi["hash"]:
            hash_map[fi["hash"]].append(fi)
    hash_count = 0
    for files in hash_map.values():
        if len(files) > 1:
            groups.append({"method": "hash", "files": files})
            handled.update(f["path"] for f in files)
            hash_count += 1
    console.print(f"    → [green]{hash_count} hash group(s)[/green]")

    # Pass 2 — ID3 metadata
    console.print("[cyan]  Pass 2/3:[/cyan] ID3 metadata (Artist + Title)...")
    meta_map = defaultdict(list)
    for fi in file_infos:
        key = metadata_key(fi)
        if key:
            meta_map[key].append(fi)
    meta_count = 0
    for files in meta_map.values():
        if len(files) < 2:
            continue
        paths = {f["path"] for f in files}
        in_hash_group = any(
            g["method"] == "hash" and paths <= {f["path"] for f in g["files"]}
            for g in groups
        )
        if not in_hash_group:
            groups.append({"method": "metadata", "files": files})
            handled.update(paths)
            meta_count += 1
    console.print(f"    → [green]{meta_count} metadata group(s)[/green]")

    # Pass 3 — acoustic fingerprint
    if use_fingerprint:
        console.print("[cyan]  Pass 3/3:[/cyan] Audio fingerprint (chromaprint)...")
        unmatched = [fi for fi in file_infos if fi["path"] not in handled and fi["fingerprint"]]
        fp_groups, assigned = [], set()
        for i, fi in enumerate(unmatched):
            if fi["path"] in assigned:
                continue
            cluster = [fi]
            for fj in unmatched[i + 1:]:
                if fj["path"] in assigned:
                    continue
                if fingerprint_similarity(fi["fingerprint"], fj["fingerprint"]) >= FINGERPRINT_THRESHOLD:
                    cluster.append(fj)
                    assigned.add(fj["path"])
            if len(cluster) > 1:
                assigned.add(fi["path"])
                fp_groups.append({"method": "fingerprint", "files": cluster})
        groups.extend(fp_groups)
        console.print(f"    → [green]{len(fp_groups)} fingerprint group(s)[/green]")
    else:
        console.print("[cyan]  Pass 3/3:[/cyan] [dim]Fingerprint skipped (--no-fingerprint)[/dim]")

    return groups


def print_dupe_report(groups: list, total_files: int):
    if not groups:
        console.print(Panel("[green]✓ No duplicates found![/green]", border_style="green"))
        return

    total_dupes  = sum(len(g["files"]) - 1 for g in groups)
    wasted_bytes = sum(sum(sorted(f["size"] for f in g["files"])[:-1]) for g in groups)

    console.print(Panel(
        f"[bold]Found [red]{len(groups)} duplicate group(s)[/red] "
        f"({total_dupes} redundant file(s)) across {total_files} scanned files\n"
        f"Potential space recovered: [yellow]{fmt_size(wasted_bytes)}[/yellow][/bold]",
        border_style="red", title="[bold red]Duplicate Report[/bold red]",
    ))
    console.print()

    for i, group in enumerate(groups, 1):
        emoji, color, label = DUPE_METHOD_STYLE.get(group["method"], ("⚪","white",group["method"]))
        console.print(f"[bold]{emoji} Group {i}[/bold]  [{color}]{label}[/]")
        table = Table(box=box.SIMPLE, show_header=True, header_style="dim",
                      expand=True, padding=(0, 1))
        table.add_column("File",     min_width=40)
        table.add_column("Artist",   min_width=18)
        table.add_column("Title",    min_width=20)
        table.add_column("Bitrate",  width=8,  justify="right")
        table.add_column("Size",     width=9,  justify="right")
        table.add_column("Duration", width=7,  justify="right")
        table.add_column("Folder",   min_width=25, style="dim")

        for j, f in enumerate(sorted(group["files"], key=lambda x: x["bitrate"], reverse=True)):
            p = Path(f["path"])
            name_cell = (
                Text.assemble((p.name[:46], "bold"), (" [KEEP?]", "bold green"))
                if j == 0 else Text(p.name[:55])
            )
            table.add_row(
                name_cell,
                Text(f["artist"][:22] or "--"),
                Text(f["title"][:25]  or "--"),
                Text(f"{f['bitrate']}kbps" if f["bitrate"] else "--",
                     style="cyan" if j == 0 else ""),
                Text(fmt_size(f["size"])),
                Text(fmt_dur(f["duration"]) if f["duration"] else "--"),
                Text(str(p.parent)[:40]),
            )
        console.print(table)
        console.print()


def export_dupe_csv(groups: list, output_path: str):
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Group","Method","Suggested Action","Filename","Full Path",
                         "Artist","Title","Album","Bitrate (kbps)","Size (bytes)",
                         "Size","Duration","Folder"])
        for i, group in enumerate(groups, 1):
            for j, f in enumerate(sorted(group["files"], key=lambda x: x["bitrate"], reverse=True)):
                p = Path(f["path"])
                writer.writerow([
                    i, group["method"], "KEEP" if j == 0 else "REVIEW",
                    p.name, f["path"], f["artist"], f["title"], f["album"],
                    f["bitrate"], f["size"], fmt_size(f["size"]),
                    fmt_dur(f["duration"]) if f["duration"] else "", str(p.parent),
                ])
    console.print(f"[green]✓ Duplicate report exported to {output_path}[/green]")


def check_fpcalc() -> tuple:
    for candidate in ["fpcalc", "/usr/bin/fpcalc", "/usr/local/bin/fpcalc"]:
        if shutil.which(candidate):
            return True, candidate
    return False, ""


def cmd_dupes(args, cfg: dict):
    paths  = list(args.paths) if args.paths else cfg.get("music_paths", [])
    use_fp = not args.no_fingerprint

    if not paths:
        console.print(Panel(
            "[yellow]No music paths specified.[/yellow]\n\n"
            "Either pass paths directly:\n"
            "  [cyan]python dj_pool.py dupes /path/to/music /another/path[/cyan]\n\n"
            "Or save your paths in setup:\n"
            "  [cyan]python dj_pool.py setup[/cyan]",
            border_style="yellow",
        ))
        return

    # Check fpcalc availability
    fpcalc_available, fpcalc_bin = check_fpcalc()
    if use_fp and not fpcalc_available:
        console.print(Panel(
            "[yellow]⚠  fpcalc (chromaprint) not found — fingerprint detection disabled.[/yellow]\n\n"
            "To enable audio fingerprinting:\n"
            "  [cyan]Ubuntu/Debian:[/cyan]  sudo apt install libchromaprint-tools\n"
            "  [cyan]macOS:[/cyan]          brew install chromaprint\n"
            "  [cyan]Windows:[/cyan]        https://acoustid.org/chromaprint  (add fpcalc.exe to PATH)",
            border_style="yellow", title="Fingerprint Unavailable",
        ))
        use_fp = False

    console.print(f"\n[bold cyan]🔍 Scanning {len(paths)} location(s)...[/bold cyan]")
    for p in paths:
        console.print(f"  [dim]{p}[/dim]")
    console.print()

    files = discover_audio_files(paths)
    if not files:
        console.print("[red]No audio files found in the specified paths.[/red]")
        return

    console.print(f"Found [bold]{len(files)}[/bold] audio files\n")

    # Read metadata + hashes
    file_infos = []
    with Progress(SpinnerColumn(), TextColumn("[cyan]{task.description}[/cyan]"),
                  BarColumn(), TextColumn("{task.completed}/{task.total}"),
                  TimeRemainingColumn(), console=console, transient=True) as progress:
        task = progress.add_task("Reading files...", total=len(files))
        for f in files:
            fi = get_audio_info(f)
            fi["hash"] = compute_hash(f)
            if use_fp:
                fi["fingerprint"] = compute_fingerprint(f, fpcalc_bin)
            file_infos.append(fi)
            progress.advance(task)

    console.print(f"[green]✓ Read {len(file_infos)} files[/green]\n")

    console.print("[bold]Running duplicate detection...[/bold]")
    groups = find_duplicates(file_infos, use_fingerprint=use_fp)
    console.print()

    print_dupe_report(groups, len(file_infos))

    if args.export:
        export_dupe_csv(groups, args.export)
    elif groups:
        if Confirm.ask("Export duplicate report to CSV?", default=True):
            out = f"data/dupes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            Path("data").mkdir(exist_ok=True)
            export_dupe_csv(groups, out)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

HELP_TEXT = """
[bold]Playlist commands[/bold]
  [cyan]python dj_pool.py setup[/cyan]                    — Configure Spotify credentials & music paths
  [cyan]python dj_pool.py sync [URL][/cyan]               — Sync a Spotify playlist
  [cyan]python dj_pool.py list [--status STATUS][/cyan]   — List tracks
  [cyan]python dj_pool.py work [--status STATUS][/cyan]   — Interactive download session
  [cyan]python dj_pool.py stats[/cyan]                    — Progress summary
  [cyan]python dj_pool.py export [--out FILE][/cyan]      — Export playlist to CSV

[bold]Library commands[/bold]
  [cyan]python dj_pool.py dupes[/cyan]                    — Scan saved music paths for duplicates
  [cyan]python dj_pool.py dupes /path1 /path2[/cyan]      — Scan specific paths (overrides saved)
  [cyan]python dj_pool.py dupes --no-fingerprint[/cyan]   — Fast scan (hash + metadata only)
  [cyan]python dj_pool.py dupes --export FILE[/cyan]      — Save duplicate report to CSV

[dim]Status values: all · pending · found · downloaded · not_found · skipped[/dim]
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dj_pool.py",
        description="DJ Pool Tool — Spotify playlist manager + duplicate scanner",
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("setup", help="Configure Spotify credentials and music paths")

    p_sync = sub.add_parser("sync", help="Sync a Spotify playlist")
    p_sync.add_argument("url", nargs="?", help="Playlist URL or ID")

    p_list = sub.add_parser("list", help="List tracks")
    p_list.add_argument("--status", default="all",
                        choices=["all","pending","found","downloaded","not_found","skipped"])

    p_work = sub.add_parser("work", help="Interactive download session")
    p_work.add_argument("--status", default="pending",
                        choices=["all","pending","found","not_found"])

    sub.add_parser("stats",  help="Show progress summary")

    p_export = sub.add_parser("export", help="Export playlist to CSV")
    p_export.add_argument("--out", default=None)

    p_dupes = sub.add_parser("dupes", help="Scan music library for duplicate files")
    p_dupes.add_argument("paths", nargs="*", metavar="PATH",
                         help="Paths to scan (uses paths saved in setup if omitted)")
    p_dupes.add_argument("--no-fingerprint", action="store_true",
                         help="Skip chromaprint fingerprinting (faster)")
    p_dupes.add_argument("--export", metavar="FILE", default=None,
                         help="Save duplicate report to this CSV file")

    return parser


def main():
    parser = build_parser()
    args   = parser.parse_args()

    console.print(Panel.fit(
        "[bold cyan]🎧 DJ Pool Tool[/bold cyan]\n"
        "[dim]Spotify playlists  •  Pool search  •  Duplicate scanner[/dim]",
        border_style="cyan",
    ))

    DB_FILE.parent.mkdir(exist_ok=True)

    if not args.cmd:
        console.print(HELP_TEXT)
        return

    if args.cmd == "setup":
        cmd_setup(args)
        return

    cfg = load_config()

    if args.cmd == "dupes":
        cmd_dupes(args, cfg)
        return

    # Remaining commands require Spotify credentials
    if not cfg.get("client_id"):
        console.print(
            "[red]No Spotify credentials found.[/red]  "
            "Run: [cyan]python dj_pool.py setup[/cyan]"
        )
        sys.exit(1)

    conn = init_db()
    try:
        if args.cmd == "sync":
            cmd_sync(args, get_spotify_client(cfg), conn)
        elif args.cmd == "list":
            cmd_list(args, conn)
        elif args.cmd == "work":
            cmd_work(args, conn)
        elif args.cmd == "stats":
            cmd_stats(args, conn)
        elif args.cmd == "export":
            cmd_export(args, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
