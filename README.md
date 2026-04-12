# 🎧 DJ Song Manager

A unified DJ library manager. Sync Spotify playlists, track downloads across DJ pools, and scan your music library for duplicates — all from one script.

\---

## Features

* **Sync any Spotify playlist** by URL or ID
* **Work session mode** — steps through tracks one by one, opens search tabs in your browser automatically
* **Status tracking** — mark each track as `pending`, `found`, `downloaded`, `not\_found`, or `skipped`
* **Persistent SQLite database** — picks up exactly where you left off across sessions
* **CSV export** — includes pre-built search URLs for both pools
* **Multi-playlist support** — sync as many playlists as you want
* **Duplicate scanner** — built-in, scans your music folders using hash, ID3 metadata, and audio fingerprinting

\---

## Setup

### 1\. Install dependencies

```bash
pip install -r requirements.txt
```

### 2\. Run setup

```bash
python dj\_pool.py setup
```

Setup covers two things:

**Spotify API credentials**

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
2. Click **Create App**
3. Set **Redirect URI** to: `http://localhost:8888/callback`
4. Copy your **Client ID** and **Client Secret** — enter them when prompted

**Music library paths** (for the duplicate scanner)

* Enter the folder paths where your DJ music lives
* Saved to `data/config.json` so `dupes` finds them automatically without arguments

### 3\. Sync your first playlist

```bash
python dj\_pool.py sync https://open.spotify.com/playlist/YOUR\_PLAYLIST\_ID
```

On first run, a browser window opens to authorize Spotify. After that, auth is cached in `data/.spotify\_cache`.

\---

## Playlist Commands

### Sync a playlist

```bash
python dj\_pool.py sync https://open.spotify.com/playlist/...
```

### Start a work session

```bash
python dj\_pool.py work
```

For each track:

* Press **`o`** (or Enter) → opens search tabs in both MP3PoolOnline and Beatport
* Mark the result: **`d`** downloaded / **`f`** found / **`x`** not found / **`s`** skip / **`q`** quit

### List tracks

```bash
python dj\_pool.py list
python dj\_pool.py list --status pending
python dj\_pool.py list --status downloaded
python dj\_pool.py list --status not\_found
```

### Progress summary

```bash
python dj\_pool.py stats
```

### Export to CSV

```bash
python dj\_pool.py export
python dj\_pool.py export --out my\_downloads.csv
```

The CSV includes pre-built search URLs for both pools — handy for offline reference.

\---

## Duplicate Scanner

Scans your music library for duplicate files using three layered detection methods:

|Pass|Method|What it catches|
|-|-|-|
|1|**File hash (SHA-1)**|Byte-for-byte identical files — same song downloaded twice, different filename|
|2|**ID3 metadata**|Same Artist + Title tags across different files (different edits, re-encodes)|
|3|**Audio fingerprint**|Acoustically identical audio via chromaprint (catches re-encodes and different masters)|

### Scan using saved paths (set up in `setup`)

```bash
python dj\_pool.py dupes
```

### Scan specific paths (overrides saved paths)

```bash
python dj\_pool.py dupes "D:\\DJ Music" "E:\\Downloads\\DJ Pool"
```

### Fast scan — hash + metadata only, no extra tools required

```bash
python dj\_pool.py dupes --no-fingerprint
```

### Full scan with audio fingerprinting

```bash
# Install chromaprint first:
#   Ubuntu/Debian:  sudo apt install libchromaprint-tools
#   macOS:          brew install chromaprint
#   Windows:        https://acoustid.org/chromaprint  (add fpcalc.exe to PATH)

python dj\_pool.py dupes
```

### Export duplicate report to CSV

```bash
python dj\_pool.py dupes --export dupes.csv
```

The report shows each duplicate group with detection method, **\[KEEP?]** suggestion (highest bitrate file), Artist, Title, Bitrate, Size, Duration, and full folder path. The CSV is designed to be reviewed in Excel — groups are numbered and files are marked `KEEP` or `REVIEW`.

> \*\*The scanner never deletes anything.\*\* It reports only — you decide what to remove.

\---

## Status Codes

|Symbol|Status|Meaning|
|-|-|-|
|⬜|`pending`|Not yet searched|
|🟡|`found`|Found on a pool, not downloaded yet|
|🟢|`downloaded`|Downloaded ✓|
|🔴|`not\_found`|Not available on either pool|
|⚫|`skipped`|Deliberately skipped|

\---

## File Structure

```
dj-pool-tool/
├── dj\_pool.py          # Single unified script — all functionality
├── requirements.txt
├── README.md
├── .gitignore
└── data/
    ├── config.json         # Spotify credentials + music paths (gitignored)
    ├── .spotify\_cache      # Auth token cache (gitignored)
    ├── tracks.db           # SQLite track database
    ├── export\_\*.csv        # Playlist exports
    └── dupes\_\*.csv         # Duplicate scan reports
```

> Add `data/config.json` and `data/.spotify\_cache` to `.gitignore` if you version this project.

\---

## Tips

* **Resume a session anytime** — `work` only shows `pending` tracks by default
* **Re-check not-found tracks** — `python dj\_pool.py work --status not\_found`
* **Multiple playlists** — sync as many as you want; the tool prompts you to pick one
* **Fast dupe scan first** — `--no-fingerprint` catches \~90% of duplicates instantly; add fingerprinting for a thorough audit
* **Fingerprinting speed** — roughly 2–5 seconds per track depending on file size

