#!/usr/bin/env python3
"""
DJ Pool Tool — Web UI
Run: python app.py
Opens: http://localhost:5000
"""

import json
import re
import sqlite3
import sys
import threading
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

try:
    from flask import Flask, jsonify, render_template, request
except ImportError:
    print("Missing dependency: flask. Run: pip install flask")
    sys.exit(1)

try:
    from mutagen.easyid3 import EasyID3
except ImportError:
    print("Missing dependency: mutagen. Run: pip install mutagen")
    sys.exit(1)

try:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
except ImportError:
    print("Missing dependency: spotipy. Run: pip install spotipy")
    sys.exit(1)

app = Flask(__name__)

DB_FILE     = Path("data/tracks.db")
CONFIG_FILE = Path("data/config.json")

AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".aac", ".m4a", ".ogg", ".wma"}

POOL_CONFIGS = {
    "mp3pool": {
        "name":         "MP3PoolOnline",
        "url_template": "https://mp3poolonline.com/results/?search={query}",
        "homepage":     "https://mp3poolonline.com/",
        "emoji":        "🎵",
    },
    "beatport": {
        "name":         "Beatport",
        "url_template": "https://www.beatport.com/search?q={query}",
        "homepage":     "https://www.beatport.com/",
        "emoji":        "🎧",
    },
    "bpmsupreme": {
        "name":         "BPM Supreme",
        "url_template": "https://app.bpmsupreme.com/d/search?searchTerm={query}",
        "homepage":     "https://app.bpmsupreme.com/",
        "emoji":        "🔥",
    },
}

# In-memory pool toggle state
pool_state = {k: True for k in POOL_CONFIGS}

# Background scan state
scan_status = {"running": False, "done": False, "found": 0, "scanned": 0, "total": 0}


# ── Database ─────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def migrate_db():
    conn = get_db()
    # Add columns if they don't exist
    for col, typ in [("local_path", "TEXT"), ("match_status", "TEXT"), ("match_confidence", "REAL")]:
        try:
            conn.execute(f"ALTER TABLE tracks ADD COLUMN {col} {typ}")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    # Rejected matches persist across rescans
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rejected_matches (
            track_id   TEXT,
            file_path  TEXT,
            rejected_at TEXT,
            PRIMARY KEY (track_id, file_path)
        )
    """)
    conn.commit()
    conn.close()


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {}


# ── File scanner ─────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    return re.sub(r"[^\w\s]", " ", text.lower())


def _strip_extras(text: str) -> str:
    """Remove content in parentheses, brackets, and Spotify version suffixes."""
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"\[[^\]]*\]", "", text)
    # Strip version/edit suffixes like " - Radio Edit", " - Extended Mix"
    text = text.split(" - ")[0]
    return text.strip()


def _words(text: str) -> set:
    return {w for w in _normalize(text).split() if len(w) > 2}


def _core_words(title: str) -> set:
    """Words from the core title only (brackets/parens stripped). Falls back to full title."""
    core = _words(_strip_extras(title))
    return core if core else _words(title)


def _file_matches(artist: str, title: str, path: Path) -> bool:
    title_words  = _core_words(title)
    first_artist = artist.split(",")[0].strip()
    artist_words = _words(first_artist)

    # Try ID3 tags first (most reliable)
    try:
        tags        = EasyID3(str(path))
        file_title  = _normalize(_strip_extras(tags.get("title",  [""])[0]))
        file_artist = _normalize(" ".join(tags.get("artist", [])))
        title_hit   = bool(title_words) and title_words.issubset(set(file_title.split()))
        artist_hit  = bool(artist_words) and bool(artist_words & set(file_artist.split()))
        if title_hit and artist_hit:
            return True
    except Exception:
        pass

    # Fallback: filename matching (strip extras from filename too)
    stem_core  = _normalize(_strip_extras(path.stem))
    stem_words = set(stem_core.split()) if stem_core.strip() else set(_normalize(path.stem).split())
    matched    = len(title_words & stem_words) / max(len(title_words), 1)
    artist_hit = bool(artist_words & set(_normalize(path.stem).split()))
    return matched >= 0.75 and artist_hit


def _build_file_index(music_paths: list) -> list:
    """Read all audio files once, extract metadata into an in-memory index."""
    index = []  # list of (path, title_words, artist_words, stem_words)
    for path_str in music_paths:
        p = Path(path_str)
        if not p.exists():
            continue
        for f in p.rglob("*"):
            if f.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            tag_title_words  = set()
            tag_artist_words = set()
            try:
                tags = EasyID3(str(f))
                tag_title_words  = set(_normalize(_strip_extras(
                    tags.get("title", [""])[0])).split())
                tag_artist_words = set(_normalize(
                    " ".join(tags.get("artist", []))).split())
            except Exception:
                pass
            stem_core  = _normalize(_strip_extras(f.stem))
            stem_words = set(stem_core.split()) if stem_core.strip() else set(_normalize(f.stem).split())
            full_stem_words = set(_normalize(f.stem).split())
            index.append((f, tag_title_words, tag_artist_words, stem_words, full_stem_words))
    return index


AUTO_CONFIDENCE = 0.85  # above this = auto-approve as downloaded


def _match_from_index(artist: str, title: str, index: list, rejected: set = None):
    """Match a track against the pre-built file index. Returns (path, confidence) or (None, 0)."""
    rejected     = rejected or set()
    title_words  = _core_words(title)
    first_artist = artist.split(",")[0].strip()
    artist_words = _words(first_artist)

    best_path = None
    best_conf = 0.0

    for path, tag_tw, tag_aw, stem_w, full_stem_w in index:
        if str(path) in rejected:
            continue

        conf = 0.0

        # ID3 tag matching
        if tag_tw and tag_aw:
            t_fwd = len(title_words & tag_tw) / max(len(title_words), 1)
            a_fwd = len(artist_words & tag_aw) / max(len(artist_words), 1)
            if t_fwd >= 0.75 and a_fwd > 0:
                conf = max(conf, t_fwd * a_fwd)

        # Filename matching
        t_fwd = len(title_words & stem_w) / max(len(title_words), 1)
        a_fwd = len(artist_words & full_stem_w) / max(len(artist_words), 1)
        if t_fwd >= 0.75 and a_fwd > 0:
            conf = max(conf, t_fwd * a_fwd)

        if conf > best_conf:
            best_conf = conf
            best_path = path

    return (best_path, best_conf) if best_path else (None, 0.0)


def _scan_library():
    global scan_status
    try:
        cfg         = load_config()
        music_paths = cfg.get("music_paths", [])

        if not music_paths:
            scan_status.update(running=False, done=True)
            return

        # Phase 1: Build index (read all files once)
        scan_status.update(total=0, scanned=0, found=0)
        index = _build_file_index(music_paths)

        # Phase 2: Match tracks against index
        conn   = get_db()
        tracks = conn.execute(
            "SELECT id, artist, track_name FROM tracks WHERE local_path IS NULL AND COALESCE(match_status, '') != 'skip_scan'"
        ).fetchall()

        # Load previously rejected matches so we skip them
        rejected_rows = conn.execute("SELECT track_id, file_path FROM rejected_matches").fetchall()
        rejected_by_track = {}
        for r in rejected_rows:
            rejected_by_track.setdefault(r["track_id"], set()).add(r["file_path"])

        scan_status["total"] = len(tracks)

        for track in tracks:
            track_rejected = rejected_by_track.get(track["id"], set())
            match, confidence = _match_from_index(
                track["artist"], track["track_name"], index, rejected=track_rejected
            )
            if match:
                is_auto = confidence >= AUTO_CONFIDENCE
                conn.execute(
                    "UPDATE tracks SET local_path=?, match_status=?, match_confidence=? WHERE id=?",
                    (str(match), "auto" if is_auto else "review", confidence, track["id"]),
                )
                if is_auto:
                    conn.execute(
                        "UPDATE tracks SET status='downloaded' WHERE id=? AND status='pending'",
                        (track["id"],),
                    )
                conn.commit()
                scan_status["found"] += 1
            scan_status["scanned"] += 1

        conn.close()
    except Exception as e:
        print(f"Scan error: {e}", file=sys.stderr)
    finally:
        scan_status.update(running=False, done=True)


def start_scan(rescan=False):
    if not scan_status["running"]:
        if rescan:
            # Clear matches so every track gets re-evaluated (rejected_matches table persists)
            conn = get_db()
            conn.execute("""UPDATE tracks SET local_path=NULL, match_confidence=NULL,
                match_status = NULL,
                status = CASE WHEN match_status = 'auto' THEN 'pending' ELSE status END
                WHERE match_status NOT IN ('skip_scan', 'approved') OR match_status IS NULL""")
            conn.commit()
            conn.close()
        scan_status.update(running=True, done=False, found=0, scanned=0, total=0)
        threading.Thread(target=_scan_library, daemon=True).start()


# ── Spotify sync ──────────────────────────────────────────────────────────────

sync_status = {"running": False, "done": False, "error": None, "imported": 0, "total": 0, "name": ""}


def _extract_playlist_id(url_or_id: str) -> str:
    m = re.search(r"playlist[/:]([A-Za-z0-9]+)", url_or_id)
    return m.group(1) if m else url_or_id.strip()


def _get_spotify_client():
    cfg = load_config()
    return spotipy.Spotify(auth_manager=SpotifyOAuth(
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        redirect_uri=cfg.get("redirect_uri", "http://127.0.0.1:8888/callback"),
        scope="playlist-read-private playlist-read-collaborative",
        cache_path="data/.spotify_cache",
    ))


def _do_sync(playlist_url: str):
    global sync_status
    try:
        sp          = _get_spotify_client()
        playlist_id = _extract_playlist_id(playlist_url)
        pl          = sp.playlist(playlist_id)

        items_obj = pl.get("items") or pl.get("tracks") or {}
        pl_name   = pl["name"]
        pl_owner  = pl["owner"]["display_name"]
        pl_total  = items_obj.get("total", 0)

        sync_status.update(name=pl_name, total=pl_total, imported=0)

        conn = get_db()
        conn.execute("""
            INSERT INTO playlists (id, name, owner, total, last_sync)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, total=excluded.total, last_sync=excluded.last_sync
        """, (playlist_id, pl_name, pl_owner, pl_total, datetime.now().isoformat()))
        conn.commit()

        results = sp.playlist_items(playlist_id)
        imported = 0

        while results:
            for item in results["items"]:
                track = item.get("track") or item.get("item")
                if not track or track.get("is_local") or item.get("is_local"):
                    continue
                artists = ", ".join(a["name"] for a in track.get("artists", []))
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
                """, {
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
                conn.commit()
                imported += 1
                sync_status["imported"] = imported
            results = sp.next(results) if results.get("next") else None

        conn.close()
        sync_status.update(running=False, done=True, error=None)

    except Exception as e:
        sync_status.update(running=False, done=True, error=str(e))


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/playlist")
def api_playlist():
    conn = get_db()
    pl   = conn.execute(
        "SELECT * FROM playlists ORDER BY last_sync DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return jsonify(dict(pl) if pl else None)


@app.route("/api/playlists")
def api_playlists():
    conn = get_db()
    rows = conn.execute("SELECT * FROM playlists ORDER BY last_sync DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/tracks")
def api_tracks():
    conn = get_db()
    playlist_id = request.args.get("playlist_id")
    if playlist_id:
        rows = conn.execute(
            "SELECT id, artist, track_name, album, status, local_path, match_status, match_confidence "
            "FROM tracks WHERE playlist_id=? ORDER BY artist, track_name", (playlist_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, artist, track_name, album, status, local_path, match_status, match_confidence "
            "FROM tracks ORDER BY artist, track_name"
        ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/tracks/<track_id>/status", methods=["POST"])
def api_update_status(track_id):
    status = (request.json or {}).get("status")
    if status not in {"pending", "found", "downloaded", "not_found", "skipped"}:
        return jsonify({"error": "Invalid status"}), 400
    conn = get_db()
    conn.execute(
        "UPDATE tracks SET status=?, updated_at=? WHERE id=?",
        (status, datetime.now().isoformat(), track_id),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/pools")
def api_pools():
    return jsonify({
        k: {"name": v["name"], "emoji": v["emoji"], "homepage": v["homepage"], "enabled": pool_state[k]}
        for k, v in POOL_CONFIGS.items()
    })


@app.route("/api/pools/<pool_key>/toggle", methods=["POST"])
def api_toggle_pool(pool_key):
    if pool_key not in POOL_CONFIGS:
        return jsonify({"error": "Unknown pool"}), 404
    pool_state[pool_key] = not pool_state[pool_key]
    return jsonify({"enabled": pool_state[pool_key]})


@app.route("/api/search-url/<track_id>/<pool_key>")
def api_search_url(track_id, pool_key):
    if pool_key not in POOL_CONFIGS:
        return jsonify({"error": "Unknown pool"}), 404
    conn  = get_db()
    track = conn.execute(
        "SELECT artist, track_name FROM tracks WHERE id=?", (track_id,)
    ).fetchone()
    conn.close()
    if not track:
        return jsonify({"error": "Track not found"}), 404
    query = urllib.parse.quote(f"{track['artist']} {track['track_name']}")
    url   = POOL_CONFIGS[pool_key]["url_template"].format(query=query)
    return jsonify({"url": url})


@app.route("/api/stats")
def api_stats():
    conn  = get_db()
    playlist_id = request.args.get("playlist_id")
    where = "WHERE playlist_id=?" if playlist_id else "WHERE 1=1"
    params = (playlist_id,) if playlist_id else ()
    rows  = conn.execute(
        f"SELECT status, COUNT(*) as n FROM tracks {where} GROUP BY status", params
    ).fetchall()
    local = conn.execute(
        f"SELECT COUNT(*) as n FROM tracks {where} AND local_path IS NOT NULL", params
    ).fetchone()
    conn.close()
    stats = {r["status"]: r["n"] for r in rows}
    stats["local"] = local["n"] if local else 0
    return jsonify(stats)


@app.route("/api/scan", methods=["POST"])
def api_scan():
    rescan = (request.json or {}).get("rescan", False)
    start_scan(rescan=rescan)
    return jsonify({"started": True})


@app.route("/api/scan/status")
def api_scan_status():
    return jsonify(scan_status)


@app.route("/api/sync", methods=["POST"])
def api_sync():
    if sync_status["running"]:
        return jsonify({"error": "Sync already in progress"}), 409
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    sync_status.update(running=True, done=False, error=None, imported=0, total=0, name="")
    threading.Thread(target=_do_sync, args=(url,), daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/sync/status")
def api_sync_status():
    return jsonify(sync_status)


@app.route("/api/reviews")
def api_reviews():
    conn = get_db()
    rows = conn.execute("""
        SELECT id, artist, track_name, local_path, match_confidence
        FROM tracks WHERE match_status = 'review'
        ORDER BY match_confidence DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/reviews/<track_id>/approve", methods=["POST"])
def api_approve(track_id):
    conn = get_db()
    conn.execute(
        "UPDATE tracks SET match_status='approved', status='downloaded' WHERE id=?",
        (track_id,),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/reviews/<track_id>/reject", methods=["POST"])
def api_reject(track_id):
    conn = get_db()
    track = conn.execute(
        "SELECT local_path FROM tracks WHERE id=?", (track_id,)
    ).fetchone()
    if track and track["local_path"]:
        conn.execute(
            "INSERT OR IGNORE INTO rejected_matches (track_id, file_path, rejected_at) VALUES (?, ?, ?)",
            (track_id, track["local_path"], datetime.now().isoformat()),
        )
    conn.execute(
        "UPDATE tracks SET local_path=NULL, match_status=NULL, match_confidence=NULL WHERE id=?",
        (track_id,),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/reviews/<track_id>/skip", methods=["POST"])
def api_skip_scan(track_id):
    """Permanently skip scanning this track — won't be matched on future rescans."""
    conn = get_db()
    conn.execute(
        "UPDATE tracks SET local_path=NULL, match_status='skip_scan', match_confidence=NULL WHERE id=?",
        (track_id,),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/tracks/<track_id>/unmatch", methods=["POST"])
def api_unmatch(track_id):
    """Undo a match — sends it to the review modal so user can Match or No Match."""
    conn = get_db()
    conn.execute(
        "UPDATE tracks SET match_status='review', status='pending' WHERE id=?",
        (track_id,),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/export/csv")
def api_export_csv():
    import csv
    import io
    from flask import Response

    conn = get_db()
    playlist_id = request.args.get("playlist_id")
    if playlist_id:
        rows = conn.execute(
            "SELECT artist, track_name, album, status, match_status, local_path, match_confidence "
            "FROM tracks WHERE playlist_id=? ORDER BY artist, track_name", (playlist_id,)
        ).fetchall()
        pl = conn.execute("SELECT name FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        filename = f"{pl['name']}.csv" if pl else "playlist.csv"
    else:
        rows = conn.execute(
            "SELECT artist, track_name, album, status, match_status, local_path, match_confidence "
            "FROM tracks ORDER BY artist, track_name"
        ).fetchall()
        filename = "all_tracks.csv"
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Artist", "Title", "Album", "Status", "Match Status", "File Path", "Confidence"])
    for r in rows:
        status_label = {"pending": "Pending", "found": "Found", "downloaded": "Downloaded",
                        "not_found": "Not Found", "skipped": "Skipped"}.get(r["status"], r["status"])
        match_label = {"auto": "Auto-matched", "approved": "Approved", "review": "Needs Review",
                       "skip_scan": "Rejected"}.get(r["match_status"] or "", "")
        conf = f"{round(r['match_confidence'] * 100)}%" if r["match_confidence"] else ""
        writer.writerow([r["artist"], r["track_name"], r["album"], status_label,
                         match_label, r["local_path"] or "", conf])

    # Sanitize filename
    safe_name = re.sub(r'[^\w\s\-.]', '_', filename)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="Port (default: 5000)")
    parser.add_argument("--no-scan", action="store_true", help="Skip library scan on startup")
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser on startup")
    args = parser.parse_args()

    Path("data").mkdir(exist_ok=True)
    migrate_db()
    if not args.no_scan:
        start_scan()
    if not args.no_browser and sys.platform == "win32":
        webbrowser.open(f"http://127.0.0.1:{args.port}")
    app.run(debug=False, host=args.host, port=args.port)
