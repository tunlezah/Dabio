# Dabio — DAB+ Radio Web Player

Dabio turns a cheap **RTL-SDR USB dongle** into a polished, browser-based
**DAB+ digital radio** for Australian markets. It wraps
[`welle-cli`](https://github.com/AlbrechtL/welle.io) (the command-line decoder
from the welle.io project) with an async **FastAPI** backend and a dependency-free
single-page web UI. Tune, scan, see live metadata and station logos, and cast to
any Chromecast on your network — all from a web page, with no app to install on
your phone or laptop.

> **Status:** functional prototype targeting **Ubuntu 24.04**. Ships with a
> one-command installer, a systemd service, and a **mock mode** so you can run
> and develop the whole stack without any SDR hardware.

---

## Table of Contents

- [What it does](#what-it-does)
- [How it works](#how-it-works)
  - [Architecture](#architecture)
  - [The audio path](#the-audio-path)
  - [Playback request flow](#playback-request-flow)
- [Requirements](#requirements)
- [Installation](#installation)
  - [What the installer does](#what-the-installer-does)
  - [Running without the installer](#running-without-the-installer)
- [Configuration](#configuration-configyaml)
- [Using the web UI](#using-the-web-ui)
- [Scanning explained](#scanning-explained)
- [Gain and signal strength](#gain-and-signal-strength)
- [Station logos](#station-logos)
- [Mock mode (no hardware)](#mock-mode-no-hardware)
- [HTTP API reference](#http-api-reference)
- [Project structure](#project-structure)
- [welle-cli integration](#welle-cli-integration)
- [Reference tables](#reference-tables)
- [Runtime data files](#runtime-data-files)
- [Service management](#service-management)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Design documents](#design-documents)
- [License](#license)

---

## What it does

- **🔍 Automatic station scanning** — scans Australian DAB+ frequency blocks,
  discovers all stations on each multiplex, and caches them so restarts are
  instant. Priority-first (Canberra & national blocks first), with a one-click
  full-band scan available.
- **▶️ Browser audio playback** — streams MP3 straight to an HTML `<audio>`
  element. No plugins, no transcoding pipeline on your side.
- **📡 Live metadata** — shows **DLS** text (now-playing song/show), **SLS**
  slideshow images, codec (HE-AAC / MP2), bitrate, sample rate, stereo/mono
  mode, and real-time **SNR** (signal strength).
- **🖼️ Station logos** — fetches and caches Australian station logos so cards
  and the Now Playing panel look like a real radio.
- **📶 Signal-strength meters** — per-station SNR bars, blue for the last scan
  snapshot and green/amber/red live while a station is playing.
- **🎛️ Gain control + auto-gain** — pick an RTL-SDR gain index from the UI, or
  let Dabio probe gains automatically when a scan finds nothing.
- **📺 Chromecast** — discover Cast devices on your LAN and push the audio
  stream to them directly (no phone needed).
- **🩺 Health & logs** — a `/api/health` endpoint and an in-browser live log
  viewer (the same structured JSON logs the backend emits).
- **🧪 Mock mode** — a built-in fake welle-cli server generates synthetic audio
  and station metadata so the entire app runs with **zero hardware**.
- **📦 One-command install** — `sudo ./install.sh` builds welle-cli, sets up a
  virtualenv, blacklists the conflicting kernel driver, and installs a systemd
  service.

---

## How it works

Dabio is an **orchestrator and proxy**. It does not do any DSP itself — all the
hard radio work (RF tuning, OFDM demodulation, FIC parsing, HE-AAC decoding,
MP3 encoding) is done by `welle-cli`, which exposes everything over a small HTTP
server on a private internal port. Dabio drives that process, polls its
metadata, and re-serves a clean API + UI on top.

### Architecture

```
                                  ┌──────────────────────────────────────┐
   RTL-SDR USB dongle  ─────────► │  welle-cli  (subprocess)             │
   (RTL2832U + R820T)             │  -c <block> -w 7979 -g <gain> -T      │
                                  │  • OFDM demod + FIC parse             │
                                  │  • HE-AAC/MP2 decode → MP3 encode     │
                                  │  HTTP on 127.0.0.1:7979               │
                                  │   /mux.json  /mp3/<SID>  /slide/<SID> │
                                  │   /channel (GET/POST)                 │
                                  └──────────────┬───────────────────────┘
                                                 │  (localhost HTTP)
                                                 ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │  Dabio — FastAPI / uvicorn  (0.0.0.0:8800)                             │
   │                                                                        │
   │  WelleManager   — version-aware process control, tune/retune/restart   │
   │  Scanner        — block sweep, polling, retry, auto-gain, station cache │
   │  BroadcasterPool— one upstream MP3 reader per SID, fan-out to clients   │
   │  ChromecastMgr  — PyChromecast discovery + casting                     │
   │  Logos          — local logo cache lookup                              │
   │  WebLogHandler  — ring buffer of recent logs for the UI                │
   └───────────────┬───────────────────────────────┬───────────────────────┘
                   │ HTTP/JSON + MP3 stream         │ MP3 stream (LAN IP)
                   ▼                                ▼
        ┌────────────────────┐            ┌────────────────────┐
        │  Browser (SPA)     │            │  Chromecast device │
        │  static/index.html │            │  (pulls the stream)│
        └────────────────────┘            └────────────────────┘
```

Key design choices (full rationale in [`docs/architecture-decisions.md`](docs/architecture-decisions.md)):

- **welle-cli's built-in web server is the only audio path** — no FFmpeg, no
  custom codec pipeline. welle-cli decodes AAC and encodes MP3 internally;
  MP3 plays natively in every browser and on Chromecast.
- **Single upstream reader, fan-out to N clients** — for each active station,
  Dabio opens exactly **one** connection to welle-cli's `/mp3/<SID>` and a
  `AudioBroadcaster` distributes chunks to every connected browser via a bounded
  `asyncio.Queue` (drop-oldest for slow consumers). This prevents duplicate
  decode pipelines inside welle-cli.
- **A strict process state machine** — welle-cli can only tune one block at a
  time, so scanning and playback are mutually exclusive. An `asyncio.Lock`
  guards every transition (`STOPPED → STARTING → TUNED / SCANNING → …`).
- **Station identity = `ensemble_id`_`service_id`** — globally unique even when
  station names repeat across multiplexes.
- **No Docker** — RTL-SDR USB passthrough and Chromecast mDNS are fragile in
  containers, so Dabio runs directly on the host inside a Python virtualenv.

### The audio path

```
welle-cli :7979/mp3/0x<SID>
      │  (one httpx streaming GET per SID)
      ▼
AudioBroadcaster._feed_loop()      ← reads 4 KB chunks, auto-reconnects w/ backoff
      │  _publish() → put_nowait into each subscriber queue (maxsize 64)
      ├───────────────┬───────────────┐
      ▼               ▼               ▼
 Browser 1        Browser 2       Chromecast       ← each is a StreamingResponse
 <audio>          <audio>         media_controller    generator draining its queue
```

When the last subscriber for a SID disconnects, the feed loop stops and the
broadcaster is discarded.

### Playback request flow

Pressing a station card runs three steps:

1. **`POST /api/station/{id}/play`** — Dabio tunes welle-cli to that station's
   block (only if it isn't already there). It refuses with HTTP `409` if a scan
   is running. This does **not** start audio by itself.
2. **`GET /api/station/{id}/stream`** — the browser points its `<audio>` element
   here. This is what actually spins up the broadcaster and begins streaming MP3.
3. **`GET /api/station/{id}/metadata`** every 5 s — pulls DLS text, the slide
   image URL, and live SNR to update the Now Playing panel and the signal bars.

---

## Requirements

**Hardware**

- An **RTL-SDR USB dongle** (RTL2832U chipset, typically with an R820T/R820T2
  tuner — USB IDs `0bda:2838` / `0bda:2832`).
- A **Band III antenna** suitable for ~174–240 MHz.
- (Optional) one or more **Chromecast** devices on the same LAN.

> No hardware? Set `mock_mode: true` and everything runs on synthetic audio.

**Operating system**

- **Ubuntu 24.04 (Noble Numbat)** is the supported/tested target. The installer
  will warn but still attempt to run on other Debian-like systems.

**Software** (installed automatically by `install.sh`)

- `welle-cli` — built from source at tag **v2.7** (preferred), or the apt
  `welle.io` package (**v2.4**) as a fallback.
- **Python 3.12** with a virtualenv.
- System packages: `rtl-sdr`, `librtlsdr-dev`, `avahi-daemon` (Chromecast mDNS),
  `libusb-1.0-0-dev`, `ffmpeg`, `curl`.
- Python packages (`requirements.txt`):

  | Package | Purpose |
  |---|---|
  | `fastapi` | HTTP API + app framework |
  | `uvicorn[standard]` | ASGI server |
  | `httpx` | Async client to talk to welle-cli |
  | `pyyaml` | Read `config.yaml` |
  | `pychromecast` | Chromecast discovery & casting |
  | `lameenc` | MP3 encoding for mock-mode synthetic audio |

  `Pillow` is an **optional** extra used only by `scripts/fetch_logos.py` to
  resize logos.

---

## Installation

```bash
git clone <your-fork-or-this-repo> Dabio
cd Dabio
sudo ./install.sh
```

When it finishes you'll see the access URLs, e.g.:

```
Local:   http://127.0.0.1:8800
Network: http://192.168.1.50:8800
```

Open either in a browser. If an RTL-SDR is plugged in, click **Quick Scan** to
discover stations; otherwise enable mock mode (below) to explore the UI.

### What the installer does

`install.sh` is idempotent and runs as **root**. It performs 10 numbered steps
and prints a clear success/failure summary with troubleshooting hints. All
output is also teed to `data/install.log`.

| Step | Action |
|---|---|
| 1 | **Prerequisites** — verify root, detect Ubuntu 24.04, check internet reachability. |
| 2 | **Clean up** — stop any existing `dabio.service`, kill orphaned `welle-cli`/`uvicorn`/dabio processes, check that ports `8800` and `7979` are free. |
| 3 | **System dependencies** — apt-install the runtime packages and ensure `avahi-daemon` is running. |
| 4 | **Blacklist DVB-T drivers** — write `/etc/modprobe.d/rtl-sdr-blacklist.conf` (blacklists `dvb_usb_rtl28xxu`, `rtl2832`, etc.) and unload them so the SDR isn't claimed by the kernel's DVB-T driver. |
| 5 | **Install welle-cli** — compile **v2.7** from source (`cmake -DBUILD_WELLE_IO=OFF -DBUILD_WELLE_CLI=ON -DRTLSDR=1`); if that fails, fall back to `apt install welle.io` (**v2.4**). |
| 6 | **Python environment** — create `.venv`, install `requirements.txt`, and verify `from dabio.app import app` imports cleanly. |
| 7 | **Detect SDR** — look for the RTL-SDR on USB and run `rtl_test`; warn if a reboot is needed for the driver blacklist to take effect. |
| 8 | **systemd service** — write and enable `dabio.service`. |
| 9 | **Start** — release the port, start the service (or run in the background if systemd isn't active). |
| 10 | **Verify** — poll `/api/health`, confirm the frontend and stations API respond, and print the result. |

> The installer never flips `mock_mode` on automatically — that's a development
> setting. It will set `mock_mode: false` for production installs.

### Running without the installer

You can run the app directly once `welle-cli` and the Python deps are present:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=src .venv/bin/python -m dabio
```

The entry point (`src/dabio/__main__.py`) reads `config.yaml`, checks the port
is free (with a retry under systemd), writes `data/dabio.port`, and launches
uvicorn on `server.host:server.port`.

---

## Configuration (`config.yaml`)

All settings live in `config.yaml` at the project root. Missing keys fall back
to the defaults baked into `src/dabio/config.py`.

```yaml
sdr:
  gain: 20          # RTL-SDR R820T gain *index* (0–28), or -1 for AGC (auto).
                    # NOT a dB value — see the gain table below.
  driver: "auto"    # auto | rtl_sdr | rtl_tcp | soapysdr

server:
  port: 8800        # Web UI / API port (what you open in the browser)
  host: "0.0.0.0"   # 0.0.0.0 = reachable on the LAN; 127.0.0.1 = local only

welle_cli:
  internal_port: 7979   # welle-cli's private web server (not exposed to users)
  binary_path: null     # null = auto-detect; or an absolute path to welle-cli

scanning:
  priority_blocks: ["8D", "9C", "8C", "9A", "9B", "9D"]  # scanned first, in order
  dwell_time: 10        # seconds per block (minimum 8 — see "Scanning explained")
  poll_interval: 2      # seconds between /mux.json polls during a dwell
  full_scan: false      # default scan scope (UI "Full Scan" overrides per-run)
  retry_empty: true     # re-scan priority blocks that returned zero services

mock_mode: false        # true = run with synthetic audio, no SDR needed
```

Notes:

- **`sdr.gain` is an index, not decibels.** Index `20` ≈ `37.2 dB`, recommended
  for urban DAB+ reception. See the [gain table](#rtl-sdr-r820t-gain-table).
  The default `priority_blocks` order puts **Canberra (8D)** and the **ABC/SBS
  national mux (9C)** first.
- **Auto-saved settings:** if a scan finds nothing and auto-gain picks a better
  value, Dabio writes the new index back into `config.yaml` automatically.
  Setting the gain from the UI does the same.
- **Binary auto-detection** prefers the source-built `/usr/local/bin/welle-cli`
  (v2.7) over the apt `/usr/bin/welle-cli` (v2.4), then `PATH`.

---

## Using the web UI

The UI is a single static page (`static/index.html`) — vanilla HTML/CSS/JS, no
build step. It has a sticky header with a **live status indicator** and a
light/dark/system **theme toggle**, plus three tabs.

**Now Playing** (top panel, collapsible)
- Station name, ensemble, block, codec, sample rate, mode, bitrate.
- **DLS** scrolling text (current track/show) and the **SLS** slide image
  (falls back to the station logo if no slide is broadcast).
- A **Stop** button and a live stream-status dot (Playing / Buffering /
  Reconnecting). The player auto-reconnects with exponential backoff on errors.

**Stations tab**
- Cards grouped by ensemble/block, each showing the logo, name, service ID,
  codec badge, sample rate, mode, bitrate, and a **signal-strength meter**.
- A **Gain** selector (every index with its dB value) and **Quick Scan** /
  **Full Scan** buttons.
- Click a card to play it. The previously playing card reverts to its blue
  "snapshot" signal bars; the new one shows live green/amber/red bars.

**Logs tab**
- The backend's structured logs, filterable by severity, with an auto-refresh
  toggle (polls `/api/logs` every 3 s).

**Chromecast tab**
- **Discover Devices** lists Cast devices on your LAN. Play a station first,
  then click **Cast** on a device; a banner shows the active cast and a **Stop
  Casting** control.

A **Scan Progress** panel appears during scans showing the current block, phase
(priority / retry / auto-gain / complete), SNR, gain, percentage, elapsed time,
ETA, and a running station count. It also reappears automatically if you reload
the page mid-scan.

---

## Scanning explained

Scanning is the trickiest part of DAB+ reception, and Dabio's scanner is built
around lessons documented in [`docs/failure-analysis.md`](docs/failure-analysis.md).
For each block in the scan list, the scanner (`src/dabio/scanner.py`):

1. **Tunes** welle-cli to the block (POST `/channel` on v2.7, else restart).
2. **Dwells** for `dwell_time` seconds (default 10, minimum 8 — short dwells
   miss weak ensembles because FIC data is broadcast cyclically).
3. **Polls** `/mux.json` every `poll_interval` seconds and **keeps the best
   result** (the poll that returned the most services), instead of trusting a
   single fetch that might land mid-FIC-acquisition.
4. **Fast-skips** dead frequencies: if SNR is still `0` after ~3 s with no
   services, the block is abandoned early.
5. **Captures per-station metadata** from the FIC — codec (`ascty`), subchannel
   bitrate, protection level, and the best SNR seen during the dwell.
6. **Retries** any priority block that returned zero services (transient RF
   failures are common), when `retry_empty` is enabled.
7. **Auto-gain fallback:** if the whole scan finds **nothing**, it probes a
   spread of gain indices on block `9C`, picks whichever yields the most
   stations, saves it to `config.yaml`, and re-scans.
8. **Caches** results to `data/stations.json`, so a restart loads instantly
   without re-scanning.

A **Quick Scan** covers only `priority_blocks`; a **Full Scan** adds all 38
Band III blocks. Codec/bitrate/protection/SNR captured during the scan persist
across tunings and restarts; sample-rate and stereo/mono mode are only known for
the *currently tuned* service, so they're overlaid live from `/mux.json` when
available.

---

## Gain and signal strength

- **Gain** is set as an RTL-SDR **index** (`0`–`28`) mapped to dB by a fixed
  R820T table (see below), or `-1` for hardware AGC. Fixed gain is generally
  preferred — welle.io's AGC is known to drift upward over time and degrade SNR.
- **SNR** (signal-to-noise ratio, in dB) comes from welle-cli's demodulator
  metrics in `/mux.json`. The UI renders it as 5 bars:

  | SNR (dB) | Bars | Live colour |
  |---|---|---|
  | ≤ 0 | 0 | — |
  | 0–5 | 1 | red (weak) |
  | 5–10 | 2 | amber (medium) |
  | 10–15 | 3 | green |
  | 15–20 | 4 | green |
  | ≥ 20 | 5 | green |

  Blue bars are a **snapshot from the last scan**; green/amber/red bars are
  **live** for the station you're currently playing.

---

## Station logos

Logos are fetched by a standalone script and served from a local cache — the
app never scrapes the web at runtime.

```bash
# Optional: better resizing if Pillow is installed
.venv/bin/pip install Pillow

# Fetch & cache Australian station logos (defaults to 200×200 px)
PYTHONPATH=src .venv/bin/python scripts/fetch_logos.py
#   --size N    resize to N×N pixels (default 200)
#   --force     re-download even if a cache already exists
```

The script pulls images from the **Fandom logos wiki** (`Category:Radio_stations_in_Australia`),
resizes them (if Pillow is present), and writes them to `data/logos/` plus an
`index.json` mapping normalized station names to filenames. At runtime,
`GET /api/station/{id}/logo` does a fuzzy match (ignoring case, punctuation, and
suffixes like "FM"/"DAB+") against that index.

---

## Mock mode (no hardware)

Set `mock_mode: true` in `config.yaml` and restart. Instead of launching
`welle-cli`, Dabio starts an in-process **`MockWelleServer`** on the same
internal port that speaks the exact same HTTP contract:

- `/mux.json` → fake metadata for 12 Australian-style stations across blocks
  `9C` and `8D`, with rotating DLS messages and realistic codec/bitrate/SNR.
- `/mp3/<SID>` → an endless **synthetic sine-wave** MP3 stream (frequency varies
  per SID so stations sound different), encoded with `lameenc`.
- `/channel` (GET/POST) → accepts retune requests.

Because the contract is identical, **no backend code changes** are needed — the
proxy, scanner, broadcaster, and UI all behave normally. This is how you develop
and demo the full app without an SDR.

---

## HTTP API reference

All endpoints are served by `src/dabio/app.py` under the same origin as the UI
(default `http://<host>:8800`).

### Stations & playback

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/stations` | All known stations with metadata (codec, bitrate, protection, SNR; live sample-rate/mode for the tuned service). |
| `POST` | `/api/station/{id}/play` | Tune welle-cli to the station's block. `404` if unknown, `409` if scanning. |
| `POST` | `/api/station/stop` | Stop playback and tear down broadcasters. |
| `GET` | `/api/station/{id}/stream` | Live **MP3** audio stream (`audio/mpeg`) — point an `<audio>` element here. |
| `GET` | `/api/station/{id}/metadata` | DLS text, slide URL, logo URL, and live SNR. |
| `GET` | `/api/station/{id}/slide` | Current **SLS** slideshow image, or `404`. |
| `GET` | `/api/station/{id}/logo` | Cached station logo, or `404`. |
| `GET` | `/api/logos/status` | `{ "cached": bool }` — whether logos have been fetched. |

### Scanning

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/scan?full={bool}` | Start a scan (stops playback first). `full=true` adds all 38 blocks. |
| `GET` | `/api/scan/progress` | Current scan progress (block, phase, %, SNR, gain, ETA, count). |
| `POST` | `/api/scan/stop` | Request a graceful stop after the current block. |

### Tuner / signal

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/gain` | Current gain index + dB and the full index→dB table. |
| `POST` | `/api/gain` | Set the gain index (`{ "gain_index": 0–28 }`); persisted to `config.yaml`. |
| `GET` | `/api/signal` | Per-station SNR/block captured at the last scan. |

### Chromecast

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/chromecast/devices` | Discover Cast devices on the LAN. |
| `POST` | `/api/chromecast/cast` | Cast a station (`{ "device_uuid", "station_id" }`). |
| `POST` | `/api/chromecast/stop` | Stop the active cast. |
| `GET` | `/api/chromecast/status` | Whether a cast is active, and on which device. |

### System

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Overall status, welle-cli state/version/PID, station count, scanning flag. |
| `GET` | `/api/logs?limit={n}&severity={level}` | Recent log entries from the in-memory ring buffer (last 500). |
| `GET` | `/` | The web UI (`static/index.html`). |

---

## Project structure

```
Dabio/
├── README.md                  ← you are here
├── config.yaml                ← all runtime settings
├── requirements.txt           ← Python dependencies
├── install.sh                 ← Ubuntu 24.04 installer (10 steps, idempotent)
│
├── src/dabio/                 ← the application package (run via PYTHONPATH=src)
│   ├── __main__.py            ← entry point: port check, port file, uvicorn launch
│   ├── app.py                 ← FastAPI app, lifespan wiring, all API endpoints
│   ├── config.py              ← config dataclasses + YAML loader, block tables
│   ├── models.py              ← Station / StationMetadata / ScanResult dataclasses
│   ├── welle_manager.py       ← version-aware welle-cli process control
│   ├── scanner.py             ← block scanning, polling, retry, auto-gain, cache
│   ├── audio.py               ← AudioBroadcaster + BroadcasterPool (fan-out)
│   ├── chromecast.py          ← PyChromecast discovery & casting
│   ├── logos.py               ← cached-logo lookup with fuzzy matching
│   ├── logging_config.py      ← JSON log formatter + logger factory
│   ├── mock.py                ← synthetic stations + sine-wave MP3 generator
│   └── mock_server.py         ← fake welle-cli HTTP server for mock mode
│
├── static/
│   └── index.html             ← the entire single-page web UI (HTML/CSS/JS)
│
├── scripts/
│   └── fetch_logos.py         ← standalone logo fetcher (Fandom wiki)
│
├── docs/
│   ├── architecture-decisions.md  ← 12 ADRs explaining every major choice
│   ├── research.md                ← welle-cli, RTL-SDR, AU DAB+, Chromecast notes
│   └── failure-analysis.md        ← post-mortem of the prior project (lessons)
│
└── data/                      ← created at runtime (git-ignored)
    ├── stations.json          ← cached scan results
    ├── dabio.port             ← the port the server bound to
    ├── install.log            ← installer output
    └── logos/                 ← cached logo images + index.json
```

---

## welle-cli integration

Dabio is **version-aware** because the two common builds of `welle-cli` behave
differently (see ADR-6 and the failure analysis):

| Behaviour | v2.7 (source build, preferred) | v2.4 (apt `welle.io`) |
|---|---|---|
| Retune (`POST /channel`) | Works reliably | Can hang on Debian patches → Dabio **restarts the process** instead |
| RTL-SDR V4 | Supported | May crash |
| Phase sync | Improved | Older algorithm |

`WelleManager` detects the version at startup (`welle-cli -v`), then chooses the
retune strategy accordingly. welle-cli is always launched with `-T` (disable TII
decoding) to cut CPU, plus the configured channel (`-c`), internal web port
(`-w`), gain (`-g`), and driver (`-F`) flags. Its stderr is read in the
background, with noisy repetitive lines (e.g. `SyncOnPhase`) throttled in the
logs.

---

## Reference tables

### Australian DAB+ priority blocks

| Block | MHz | Markets |
|---|---|---|
| 8C | 199.360 | Mandurah |
| 8D | 201.072 | **Canberra** (commercial + community) |
| 9A | 202.928 | Sydney, Melbourne, Brisbane (mux 1), Darwin, Hobart |
| 9B | 204.640 | Sydney, Melbourne, Brisbane (mux 2), Adelaide, Perth |
| 9C | 206.352 | **All markets — ABC/SBS national multiplex** |
| 9D | 208.064 | Gold Coast |

A full scan additionally sweeps every Band III block from `5A` (174.928 MHz)
through `13F` (239.200 MHz) — the complete table lives in
`BAND_III_BLOCKS` in `src/dabio/config.py`.

### RTL-SDR R820T gain table

`sdr.gain` is the **index** on the left; the value on the right is the
approximate dB. `-1` selects hardware AGC.

| Idx | dB | Idx | dB | Idx | dB | Idx | dB |
|---|---|---|---|---|---|---|---|
| 0 | 0.0 | 8 | 14.4 | 16 | 29.7 | 24 | 43.4 |
| 1 | 0.9 | 9 | 15.7 | 17 | 32.8 | 25 | 43.9 |
| 2 | 1.4 | 10 | 16.6 | 18 | 33.8 | 26 | 44.5 |
| 3 | 2.7 | 11 | 19.7 | 19 | 36.4 | 27 | 48.0 |
| 4 | 3.7 | 12 | 20.7 | 20 | **37.2** | 28 | 49.6 |
| 5 | 7.7 | 13 | 22.9 | 21 | 38.6 | | |
| 6 | 8.7 | 14 | 25.4 | 22 | 40.2 | | |
| 7 | 12.5 | 15 | 28.0 | 23 | 42.1 | | |

---

## Runtime data files

Everything Dabio writes lives under `data/` (git-ignored, created on demand):

| File | Written by | Purpose |
|---|---|---|
| `data/stations.json` | Scanner | Cached scan results, reloaded at startup. |
| `data/dabio.port` | `__main__` | The port the server actually bound to. |
| `data/install.log` | `install.sh` | Full installer transcript. |
| `data/logos/*.png` + `index.json` | `fetch_logos.py` | Cached station logos + name→file index. |

---

## Service management

The installer creates a systemd unit, `dabio.service`, that runs
`.venv/bin/python -m dabio` from the project directory with `PYTHONPATH=src`,
restarts on failure, and starts after `network.target` and `avahi-daemon`.

```bash
sudo systemctl status dabio       # check status
sudo systemctl restart dabio      # restart (e.g. after plugging in the SDR)
sudo systemctl stop dabio         # stop
sudo journalctl -u dabio -f       # follow live logs
curl http://127.0.0.1:8800/api/health   # quick health check
```

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| **No stations found after a scan** | Check the antenna and that the SDR works (`rtl_test`). Let auto-gain run, or raise the gain index toward ~20. Try a Full Scan. |
| **`rtl_test` fails / "device busy"** | The DVB-T kernel driver is still loaded. The installer blacklists it, but a **reboot** is usually needed the first time. |
| **welle-cli not found** | Re-run the installer; check `data/install.log`. As a manual fallback: `sudo apt install welle.io`. |
| **Retuning/scanning seems stuck on apt v2.4** | v2.4's `POST /channel` can hang; Dabio falls back to process restart automatically, but the source-built v2.7 is more reliable — re-run the installer with internet access. |
| **Port 8800 already in use** | The installer force-frees it; otherwise change `server.port` in `config.yaml`, or `sudo ss -tlnp | grep 8800` to find the holder. |
| **No Chromecast devices** | `avahi-daemon` must be running (`systemctl status avahi-daemon`) and the device must be on the same LAN. `server.host` should be `0.0.0.0` so the Cast device can reach the stream. |
| **Audio won't autoplay in the browser** | Browsers block autoplay until you interact with the page — click a station card; the player retries automatically. |
| **Want to demo without hardware** | Set `mock_mode: true` in `config.yaml` and restart. |

For anything else, the **Logs** tab and `sudo journalctl -u dabio -f` show the
structured backend logs.

---

## Development

```bash
# Run from a checkout with the venv (mock_mode is great here)
PYTHONPATH=src .venv/bin/python -m dabio

# Verify the app imports
PYTHONPATH=src .venv/bin/python -c "from dabio.app import app; print('ok')"
```

- The package uses a **`src/` layout**; run it with `PYTHONPATH=src python -m dabio`
  (there is no `pyproject.toml`/`setup.py` — nothing is pip-installed for the app
  itself).
- Logs are **structured JSON** to stdout (`timestamp`, `component`, `severity`,
  `message`); the same records feed the in-browser log viewer via a 500-entry
  ring buffer.
- The frontend is a single static file — edit `static/index.html` and refresh;
  no build tooling.
- There is currently **no automated test suite** in this repository.

---

## Design documents

The `docs/` directory captures the thinking behind Dabio and is worth reading
before making significant changes:

- **[`docs/architecture-decisions.md`](docs/architecture-decisions.md)** — 12
  ADRs covering the welle-cli audio path, the FastAPI fan-out proxy, the process
  state machine, scanning strategy, station identity, version handling, mock
  mode, Chromecast, configuration, and logging.
- **[`docs/research.md`](docs/research.md)** — deep notes on welle-cli's CLI
  flags and web endpoints, RTL-SDR driver handling, Australian DAB+ frequency
  allocation, the concurrency model, and PyChromecast.
- **[`docs/failure-analysis.md`](docs/failure-analysis.md)** — a post-mortem of
  the prior project that motivated Dabio's scanning robustness, error handling,
  and installer idempotency.

---

## License

No license file is currently included in this repository. Until one is added,
all rights are reserved by the project owner. Note that `welle-cli`/welle.io is
licensed separately (GPL) and is installed as an external dependency, not
bundled here.
