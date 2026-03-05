<h1 align="center">Darkiarr</h1>

<p align="center">
  <strong>DarkiWorld indexer for Radarr & Sonarr</strong><br>
  <em>A single Python script that turns DarkiWorld into a native *arr source.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Torznab-compatible-4c1?logo=rss&logoColor=white" alt="Torznab">
  <img src="https://img.shields.io/badge/qBittorrent_API-emulated-2496ED?logo=qbittorrent&logoColor=white" alt="qBittorrent">
  <img src="https://img.shields.io/badge/python-3.9+-3776AB?logo=python&logoColor=white" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Radarr-ready-ffc107?logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZmlsbD0id2hpdGUiIGQ9Ik0xMiAyQTEwIDEwIDAgMCAyIDEyIDIyIDEwIDEwIDAgMCAxMiAyWiIvPjwvc3ZnPg==" alt="Radarr">
  <img src="https://img.shields.io/badge/Sonarr-ready-35c5f4?logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZmlsbD0id2hpdGUiIGQ9Ik0xMiAyQTEwIDEwIDAgMCAyIDEyIDIyIDEwIDEwIDAgMCAxMiAyWiIvPjwvc3ZnPg==" alt="Sonarr">
  <img src="https://img.shields.io/badge/AllDebrid-powered-e44d26" alt="AllDebrid">
</p>

---

## What is this?

[DarkiWorld](https://darkiworld.com) is one of the best French-language DDL indexers: MULTI, TrueFrench, VFi content in every quality from 720p to 4K REMUX. But it has no integration with the \*arr ecosystem.

**Darkiarr bridges that gap.** It's a single `darkiarr.py` that exposes two standard APIs on one port:

| Darkiarr speaks... | So that... | Thinks it's talking to... |
|---|---|---|
| **Torznab** on `/torznab/api` | Radarr / Sonarr can **search** DarkiWorld | Jackett, Prowlarr |
| **qBittorrent WebAPI** on `/api/v2/` | Radarr / Sonarr can **download** from DarkiWorld | A real qBittorrent instance |

Add it in Settings, and your entire \*arr stack works with DarkiWorld natively: quality profiles, language filters, automatic upgrades, Discord notifications with correct metadata.

---

## The Flow

```
                You request a movie on Jellyseerr
                              |
                              v
                   Radarr / Sonarr (your profiles, your rules)
                    |                           |
          1. Search via Torznab           4. Import file
                    |                           ^
                    v                           |
        +----- Darkiarr (port 8720) -----+     |
        |                                |     |
        |  2. Query DarkiWorld           |     |
        |     - Cloudflare Turnstile     |     |
        |     - darki.zone redirect      |     |
        |                                |     |
        |  3. Download pipeline          |     |
        |     - AllDebrid unlock & save  |     |
        |     - rclone mount detection   |     |
        |     - Symlink in staging dir -------+
        +--------------------------------+
```

1. Radarr/Sonarr search DarkiWorld through Darkiarr's Torznab API
2. **They** pick the best release using **your** quality & language profiles
3. They send the grab to Darkiarr's qBittorrent API
4. Darkiarr resolves the link, debrids it, waits for the file, symlinks it
5. Radarr/Sonarr import with their own naming - Discord shows the correct quality

---

## Why Darkiarr?

<table>
<tr><th></th><th>Without Darkiarr</th><th>With Darkiarr</th></tr>
<tr>
  <td><strong>Quality selection</strong></td>
  <td>Hardcoded scoring, one-size-fits-all</td>
  <td>Radarr/Sonarr quality profiles (you choose)</td>
</tr>
<tr>
  <td><strong>Language handling</strong></td>
  <td>Basic priority list</td>
  <td>Native language profiles with preferred/required</td>
</tr>
<tr>
  <td><strong>Upgrades</strong></td>
  <td>Manual only</td>
  <td>Automatic - Radarr/Sonarr upgrade when better releases appear</td>
</tr>
<tr>
  <td><strong>Discord notifications</strong></td>
  <td>"720p" when it's actually 1080p</td>
  <td>Correct quality, codec, language metadata</td>
</tr>
<tr>
  <td><strong>Library naming</strong></td>
  <td>DarkiWorld filenames as-is</td>
  <td>Radarr/Sonarr naming conventions (Plex/Jellyfin compatible)</td>
</tr>
<tr>
  <td><strong>Integration</strong></td>
  <td>Custom webhooks, parallel symlink systems</td>
  <td>Standard Torznab + qBittorrent APIs, zero special config</td>
</tr>
</table>

---

## Quick Start

### Prerequisites

- Linux host (Debian/Ubuntu/Raspberry Pi OS)
- [AllDebrid](https://alldebrid.com) account + API key
- [DarkiWorld](https://darkiworld.com) account
- Radarr and/or Sonarr

### 1. Install

```bash
# System packages
sudo apt install -y chromium-browser chromium-chromedriver xvfb

# Python packages
pip3 install requests undetected-chromedriver

# rclone (AllDebrid WebDAV mount)
curl https://rclone.org/install.sh | sudo bash
```

### 2. Configure rclone

```ini
# ~/.config/rclone/rclone.conf
[adlinks]
type = webdav
url = https://webdav.debrid.it/links
vendor = other
user = apikey
pass = YOUR_OBSCURED_KEY   # rclone obscure YOUR_ALLDEBRID_API_KEY
```

```bash
sudo sed -i 's/^#user_allow_other/user_allow_other/' /etc/fuse.conf
```

### 3. Configure Darkiarr

```bash
cp darkiarr.env.example ~/.darkiarr.env
nano ~/.darkiarr.env        # fill in your credentials
chmod 600 ~/.darkiarr.env
```

Minimum required:
```bash
ALLDEBRID_KEY=your_key
DW_EMAIL=your_email
DW_PASSWORD=your_password
```

### 4. Create services

<details>
<summary><code>/etc/systemd/system/rclone-darkiarr.service</code></summary>

```ini
[Unit]
Description=rclone mount for AllDebrid links
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
ExecStartPre=/bin/mkdir -p /mnt/darkiarr/links
ExecStart=/usr/bin/rclone mount adlinks: /mnt/darkiarr/links \
  --allow-other --dir-cache-time 10s --vfs-cache-mode off \
  --read-only --config /home/YOUR_USER/.config/rclone/rclone.conf
ExecStop=/bin/fusermount -u /mnt/darkiarr/links
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```
</details>

<details>
<summary><code>/etc/systemd/system/darkiarr.service</code></summary>

```ini
[Unit]
Description=Darkiarr
After=network-online.target rclone-darkiarr.service
Requires=rclone-darkiarr.service

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER
EnvironmentFile=/home/YOUR_USER/.darkiarr.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 /home/YOUR_USER/darkiarr.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```
</details>

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rclone-darkiarr darkiarr
```

### 5. Add to Radarr

**Indexer** (Settings > Indexers > + > Torznab):

| Field | Value |
|-------|-------|
| Name | `DarkiWorld` |
| URL | `http://YOUR_IP:8720/torznab/api` |
| API Key | `darkiarr` |
| Categories | `2000` |
| Minimum Seeders | `1` |

**Download Client** (Settings > Download Clients > + > qBittorrent):

| Field | Value |
|-------|-------|
| Name | `Darkiarr` |
| Host | `YOUR_IP` |
| Port | `8720` |
| Username | `admin` |
| Password | `admin` |
| Category | `radarr` |

> Link the download client to the indexer in the indexer's settings.

### 6. Add to Sonarr

Same steps. Differences:
- Torznab categories: **`5000`**
- qBittorrent category: **`tv-sonarr`**

### 7. Verify

```bash
# Torznab caps (should return XML)
curl "http://localhost:8720/torznab/api?t=caps&apikey=darkiarr"

# Search for a movie
curl "http://localhost:8720/torznab/api?t=movie&tmdbid=98&apikey=darkiarr"

# Health check
curl http://localhost:8720/health
```

---

## Architecture

```
 Jellyseerr ──► Radarr/Sonarr
                    │
       ┌────────────┼──────────────┐
       │ search     │ grab         │ import
       ▼            ▼              │
  ┌─────────────────────────┐      │
  │    Darkiarr  (:8720)    │      │
  │                         │      │
  │  Torznab    qBittorrent │      │
  │  /torznab/  /api/v2/    │      │
  └─────┬───────────┬───────┘      │
        │           │              │
        ▼           ▼              │
   DarkiWorld    AllDebrid         │
   (search)    (unlock+save)      │
                    │              │
                    ▼              │
                 rclone            │
              /mnt/.../links/      │
                    │              │
                 symlink           │
              /mnt/.../qbit/ ──────┘
```

**Staging directory** — Darkiarr creates symlinks in a staging area. Radarr/Sonarr import from there into your media library with their own naming:

```
/mnt/darkiarr/qbit/
├── radarr/
│   └── Gladiator.2000.MULTi.1080p.BluRay.x265-DarkiWorld/
│       └── movie.mkv → /mnt/darkiarr/links/actual_file.mkv
└── tv-sonarr/
    └── Breaking.Bad.S01E01.MULTi.1080p.BluRay.x265-DarkiWorld/
        └── episode.mkv → /mnt/darkiarr/links/actual_file.mkv
```

---

## How the download works (under the hood)

When Radarr grabs a release from the Torznab results:

1. **Radarr downloads a `.torrent`** from `/torznab/download/{id}` - Darkiarr generates a minimal valid `.torrent` file containing the DarkiWorld link ID as metadata

2. **Radarr sends the `.torrent`** to `/api/v2/torrents/add` - Darkiarr parses the file, extracts the link ID, and starts the pipeline in background

3. **Pipeline resolves the link** - Navigates to DarkiWorld's download page, solves the Cloudflare Turnstile challenge via headless Chromium, follows the darki.zone redirect to get the actual 1fichier URL

4. **AllDebrid debrids the link** - Unlocks via API to get the filename, saves to AllDebrid cloud (persistent WebDAV storage)

5. **rclone mount detection** - Polls `/mnt/darkiarr/links/` every 15s until the file appears (up to 5 min)

6. **Symlink creation** - Creates a symlink in the staging directory under the release name

7. **Radarr imports** - Sees the job as `pausedUP` with `progress: 1.0`, scans the content path, hardlinks/moves the file into the media library

---

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `ALLDEBRID_KEY` | **Yes** | | AllDebrid API key |
| `DW_EMAIL` | **Yes** | | DarkiWorld email |
| `DW_PASSWORD` | **Yes** | | DarkiWorld password |
| `DARKIARR_API_KEY` | | `darkiarr` | Torznab authentication key |
| `DARKIARR_BASE_URL` | | `http://localhost:8720` | URL reachable from Radarr/Sonarr |
| `RADARR_URL` | | | Radarr URL - enables TMDB ID to title lookup |
| `RADARR_KEY` | | | Radarr API key |
| `SONARR_URL` | | | Sonarr URL - enables TMDB ID to title lookup |
| `SONARR_KEY` | | | Sonarr API key |
| `LISTEN_PORT` | | `8720` | Server port |
| `MOUNT_PATH` | | `/mnt/darkiarr/links` | rclone mount point |
| `STAGING_PATH` | | `/mnt/darkiarr/qbit` | Staging directory for symlinks |
| `DW_DOMAIN` | | `darkiworld2026.com` | DarkiWorld domain |
| `DW_IP` | | `188.114.97.2` | DarkiWorld IP (DNS bypass) |

> **RADARR/SONARR keys are recommended.** When Radarr searches by TMDB ID (which it always does), Darkiarr uses the \*arr lookup API to find the title name, then searches DarkiWorld by name. Without these keys, TMDB-only searches may return no results.

> **DARKIARR_BASE_URL is required if Radarr/Sonarr run in Docker.** Set it to your host's LAN IP (e.g. `http://192.168.1.100:8720`). Darkiarr embeds this URL in Torznab results for `.torrent` downloads.

---

## Docker setup

If Radarr/Sonarr run in containers, two things are needed:

**1. Reachable URL** - Containers can't access `localhost` on the host:
```env
DARKIARR_BASE_URL=http://192.168.1.100:8720
```

**2. Shared mount** - Containers need to see the staging directory and rclone mount:
```yaml
# In your Radarr/Sonarr docker-compose.yml
volumes:
  - /mnt/darkiarr:/mnt/darkiarr:shared
```

---

## Quality mapping

DarkiWorld qualities are translated to scene release names that Radarr/Sonarr parse natively:

| DarkiWorld | Torznab release name |
|---|---|
| HDLight 1080p x265 | `1080p.BluRay.x265` |
| WEB 1080p x265 | `1080p.WEB-DL.x265` |
| WEB 1080p | `1080p.WEB-DL` |
| Blu-Ray 1080p | `1080p.BluRay` |
| ULTRA HD x265 | `2160p.BluRay.x265` |
| REMUX UHD | `2160p.REMUX.BluRay` |
| WEB 720p | `720p.WEB-DL` |
| DVDRip | `DVDRip` |

Languages: `MULTi`, `TRUEFRENCH`, `FRENCH`, `VOSTFR`, `ENGLISH`

---

## Reliability

| Feature | Detail |
|---|---|
| Browser auto-recovery | Chrome restarts and re-authenticates on crash |
| Periodic recycling | Browser restarts every ~10 operations to prevent memory leaks |
| Retry logic | Up to 3 attempts per link resolution |
| Rate limiting | 30s backoff on DarkiWorld HTTP 429 |
| Error isolation | Failed downloads don't block the queue |
| Health monitoring | `GET /health` returns browser status and job count |

---

## Troubleshooting

| Symptom | Solution |
|---|---|
| Radarr can't connect to qBittorrent | Check `DARKIARR_BASE_URL` - must be reachable from Radarr's network |
| Torznab test fails | `curl http://YOUR_IP:8720/torznab/api?t=caps&apikey=darkiarr` - should return XML |
| Search returns no results | Check logs: browser may not be logged in yet, or configure `RADARR_URL`/`RADARR_KEY` for TMDB lookups |
| Download stuck at 0% | Verify AllDebrid key is valid and rclone mount is working: `ls /mnt/darkiarr/links/` |
| Browser login keeps failing | DarkiWorld may be down, or Turnstile sitekey changed - check `DW_TURNSTILE_SITEKEY` |

---

## API reference

<details>
<summary><strong>Torznab</strong></summary>

```
GET /torznab/api?t=caps&apikey=KEY                              # Capabilities
GET /torznab/api?t=search&q=gladiator&apikey=KEY                # Text search
GET /torznab/api?t=movie&tmdbid=98&apikey=KEY                   # Movie by TMDB ID
GET /torznab/api?t=tvsearch&q=breaking+bad&season=1&apikey=KEY  # TV search
GET /torznab/api?t=tvsearch&tmdbid=1396&season=1&ep=3&apikey=KEY
GET /torznab/download/{lien_id}?apikey=KEY&name=...&size=...    # .torrent file
```
</details>

<details>
<summary><strong>qBittorrent WebAPI</strong></summary>

```
POST /api/v2/auth/login                    # Auth (always OK)
GET  /api/v2/app/version                   # "v4.6.7"
GET  /api/v2/app/webapiVersion             # "2.9.3"
GET  /api/v2/app/preferences               # Save path, limits
GET  /api/v2/torrents/info?category=radarr  # List jobs
GET  /api/v2/torrents/files?hash=X          # Files in job
GET  /api/v2/torrents/properties?hash=X     # Job details
POST /api/v2/torrents/add                   # Add download (.torrent or URL)
POST /api/v2/torrents/delete                # Remove job
GET  /api/v2/torrents/categories            # radarr, tv-sonarr, sonarr
```
</details>

<details>
<summary><strong>Utility</strong></summary>

```
GET /health                    # {"status": "ok", "browser": true, "jobs": 0}
GET /status                    # Current job details
GET /search?q=...&type=movie   # Direct DarkiWorld search (JSON)
GET /liens/{title_id}?season=1 # Raw DarkiWorld links (JSON)
```
</details>

---

## License

MIT
