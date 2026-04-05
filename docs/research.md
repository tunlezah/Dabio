# Research: welle-cli and DAB+ Web Streaming

## 1. welle-cli Capabilities

### Overview
welle-cli is the command-line interface component of the welle.io project (https://github.com/AlbrechtL/welle.io). It provides DAB/DAB+ reception, decoding, and streaming capabilities without requiring a GUI.

### Ubuntu 24.04 (Noble) Availability
- **Package name:** `welle.io` (in the universe repository)
- **Version:** 2.4+ds-2build5
- **Includes both:** `/usr/bin/welle-cli` AND `/usr/bin/welle-io` (GUI)
- **Also includes:** Web resources at `/usr/share/welle-io/html/` (index.html, index.js)
- **Man page:** `/usr/share/man/man1/welle-cli.1.gz`
- **Installation:** `sudo apt install -y welle.io` — no compilation needed

This is a significant advantage: we can use the apt package directly (Step 2 of the installer) and skip building from source in most cases.

### CLI Flags

| Flag | Description |
|------|-------------|
| `-c channel` | Tune to specified DAB channel (e.g., `9C`, `8D`, `10B`) |
| `-w port` | Activate built-in HTTP web server on given port |
| `-p programme` | Play radio programme via ALSA |
| `-D` | Export/dump FIC and all programmes (generates .fic, .msc, .wav files) |
| `-d` | Export single programme to .msc file |
| `-C number` | Decode programmes in carousel mode (incompatible with `-D`) |
| `-P` | Switch programmes after DLS/slide decode (max 80s); without flag, switches every 10s |
| `-O codec` | Output codec: `mp3` (default) or `flac` (if compiled with FLAC support) |
| `-g gain` | Set input gain; `-1` for AGC (auto). Internally calls `setAgc(true)` for -1 |
| `-F driver` | Select input driver (`airspy`, `rtl_sdr`, `rtl_tcp`, `soapysdr`). For rtl_tcp: `-F rtl_tcp,host:port` |
| `-f file` | Read IQ data from file (u8 format by default) |
| `-u` | Disable coarse corrector for low-offset receivers |
| `-s args` | SoapySDR driver arguments |
| `-A antenna` | Configure antenna (SoapySDR only) |
| `-T` | Disable TII decoding to reduce CPU load |
| `-t test_id` | Execute specific test |
| `-h` | Display help |
| `-v` | Show version |

### Web Server Mode (`-w port`)

When launched with `-w <port>`, welle-cli starts an HTTP server providing:

| Endpoint | Method | Content-Type | Description |
|----------|--------|-------------|-------------|
| `/` | GET | `text/html` | Built-in web UI (HTML/JS) |
| `/index.js` | GET | `text/javascript` | JS for built-in web UI |
| `/mux.json` | GET | `application/json` | Full ensemble/receiver metadata |
| `/mux.m3u` | GET | `application/mpegurl` | M3U playlist of all audio services |
| `/mp3/<SID>` | GET | `audio/mpeg` | Endless MP3 audio stream |
| `/flac/<SID>` | GET | `audio/flac` | FLAC audio stream (if compiled with FLAC) |
| `/stream/<SID>` | GET | (per codec) | Audio stream using codec from `-O` flag |
| `/slide/<SID>` | GET | `image/jpeg` or `image/png` | MOT slideshow image; 404 if none available |
| `/channel` | GET | `text/plain` | Returns currently tuned channel name |
| `/channel` | POST | — | Retune to new channel (body = channel name) |
| `/fic` | GET | `application/octet-stream` | Endless FIB stream |
| `/spectrum` | GET | `application/octet-stream` | float32 FFT magnitudes |
| `/constellation` | GET | `application/octet-stream` | float32 phase values |

**SID format:** Accepted as hex string (e.g., `0xD220`) OR decimal integer (e.g., `53792`). The M3U playlist generates URLs using 4-character zero-padded hex without prefix. Internally matched via `to_hex(srv.serviceId, 4)`.

**Example launch:**
```bash
welle-cli -c 9C -w 7979
# Browse to http://localhost:7979/ for built-in UI
# http://localhost:7979/mp3/D220 for audio stream (hex SID, no 0x prefix)
# http://localhost:7979/mux.json for metadata
# http://localhost:7979/slide/D220 for slideshow image
```

**Carousel mode (`-C N`):** When used with `-w`, decodes N programmes in rotation. With `-P`, switches after DLS/slide decode (max 80s per service); without `-P`, switches every 10s. Without `-C`, the web server decodes programmes on-demand when a client connects to `/mp3/<SID>`.

**Channel switching:** POST to `/channel` triggers full receiver teardown and rebuild: stops programme handler, destroys RadioReceiver, sets new frequency, reconstructs RadioReceiver, restarts handler. All active streams are interrupted. Expect 1-3 seconds of silence.

### Audio Output Options

| Mode | Flag | Format | Use Case |
|------|------|--------|----------|
| Web server | `-w` | MP3 (endless stream) | **Primary for this project** |
| Web server + FLAC | `-w -O flac` | FLAC stream | Lossless (if built with FLAC) |
| ALSA | `-p` | PCM to sound card | Local playback only |
| Dump | `-D` | WAV files (PCM 16-bit, 48kHz, stereo) | File export |
| Dump single | `-d` | MSC file | Raw programme data |

### Metadata via `/mux.json`

The `/mux.json` endpoint returns a JSON object containing:
- **Receiver metadata:** software name/version, hardware description, FFT window placement
- **Ensemble data:** label, ECC (Extended Country Code), ensemble ID
- **Services array** (per service):
  - SID (hex), programme type, language, label (station name)
  - Audio level (left/right for stereo), sample rate, audio mode
  - Error counters: frame errors, Reed-Solomon errors, AAC errors
  - `dls_label` — Dynamic Label Segment text (current song/show info)
  - `dls_time`, `dls_lastchange` — DLS timing
  - MOT (slideshow) timestamps
  - X-PAD error status
- **Demodulation metrics:** SNR, frequency correction values, FIB CRC error counts
- **UTC time** with local offset
- **TII data:** transmitter identification (comb, pattern, delay values)
- **CIR peaks:** up to 6 channel impulse response peaks
- **Message queue:** recent info/error log entries with timestamps

**SLS slideshow images:** Available at `/slide/<SID>`. Content-Type is auto-detected from MOT subtype (JPEG or PNG). Returns 404 when no image is available. Includes `Last-Modified` and `Cache-Control: no-cache` headers.

### Multi-Channel Behaviour

**Critical limitation:** welle-cli tunes to ONE channel (frequency block) at a time. All services on that block are available, but services on other blocks require retuning.

**Within a single ensemble:** Multiple services can be decoded simultaneously since they share the same multiplex:
- With `-C N -w <port>`: Decode N programmes in carousel rotation
- Without `-C`: Web server decodes programmes on-demand when clients connect

**Implication for our project:** To scan multiple channels, we must retune sequentially. Scanning MUST NOT occur while a user is listening to audio.

### Known Issues

1. **AGC drift ([#27](https://github.com/AlbrechtL/welle.io/issues/27)):** With AGC enabled (`-g -1`), gain gradually rises and SNR drops over time. Manual gain (e.g., `-g 12`) at ~14 dB SNR is more stable. Consider using fixed gain instead of AGC for long-running instances.
2. **RTL_TCP gain mode ([#211](https://github.com/AlbrechtL/welle.io/issues/211)):** Missing "set gain mode 1" command for some hardware.
3. **`-p` race condition ([#352](https://github.com/AlbrechtL/welle.io/issues/352)):** The `-p` flag tries to init a programme before channel scan completes. Not relevant to web server mode.
4. **Gain setting failures ([#402](https://github.com/AlbrechtL/welle.io/issues/402)):** RTL-SDR gain setting errors reported.
5. **No multi-channel support ([#712](https://github.com/AlbrechtL/welle.io/issues/712)):** Feature request exists but is not implemented. One channel per instance.
6. **Channel switch latency:** Full receiver teardown/rebuild = 1-3 seconds silence.
7. **CPU usage:** On Raspberry Pi 4B, single stream consumes 85-100% of one core. `-T` (disable TII) helps.
8. **Web UI polling overhead:** Idle built-in web UI generates ~110 kbps / 22 packets/sec of polling traffic. Our custom frontend should poll less aggressively.
9. **Stream drops:** Users report streams dropping after minutes, possibly AGC-related.

### Build Flags (for source compilation)

| CMake Flag | Description |
|------------|-------------|
| `-DRTLSDR=1` | Enable RTL-SDR support |
| `-DBUILD_WELLE_IO=OFF` | Skip GUI build (no Qt dependency) |
| `-DBUILD_WELLE_CLI=ON` | Build CLI binary |
| `-DFLAC=ON` | Enable FLAC output support |

Build dependencies: `cmake`, `libfaad-dev`, `libmpg123-dev`, `libfftw3-dev`, `librtlsdr-dev`, `libusb-1.0-0-dev`, `libmp3lame-dev`

---

## 2. RTL-SDR Device Handling

### Kernel Driver Conflict
The Linux kernel includes `dvb_usb_rtl28xxu`, a DVB-T driver that claims RTL2832U devices. This must be blacklisted for RTL-SDR (direct sampling mode) to work:

```bash
echo "blacklist dvb_usb_rtl28xxu" | sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf
sudo modprobe -r dvb_usb_rtl28xxu
```

### Device Detection
- `rtl_test` — verify device is accessible
- `lsusb` — check USB device presence (Realtek RTL2832U has vendor ID `0bda:2838`)
- welle-cli startup logs indicate device detection success/failure

### Common Failure Modes

| Failure | Symptom | Recovery |
|---------|---------|----------|
| Kernel driver loaded | "Device or resource busy" | Blacklist and rmmod |
| USB disconnect | Stream stops, process may crash | Detect via process health, restart |
| Device busy (another process) | "usb_open error" | Kill competing process |
| Buffer underruns | Audio gaps, dropped samples | Increase USB buffer, reduce CPU load |
| AGC drift | Gradual signal degradation | Restart welle-cli periodically |
| No device | "No devices found" | Display error in UI, retry on interval |

---

## 3. Australian DAB+ Frequency Allocation

### Active Blocks (Priority Scan)

| Block | MHz | Markets |
|-------|---------|---------|
| 8C | 199.360 | Mandurah |
| 8D | 201.072 | Canberra (commercial + community) |
| 9A | 202.928 | Sydney, Melbourne, Brisbane (mux 1), Darwin, Hobart |
| 9B | 204.640 | Sydney, Melbourne, Brisbane (mux 2), Adelaide, Perth |
| 9C | 206.352 | All markets — ABC/SBS national multiplex |
| 9D | 208.064 | Gold Coast |

### Scanning Strategy

1. **Priority scan:** 8C, 8D, 9A, 9B, 9C, 9D (~6 blocks)
2. **Per-block dwell time:** 10 seconds minimum (increased from earlier 4s based on prior project findings)
3. **Polling during dwell:** Check `/mux.json` every 2 seconds, keep best result
4. **Retry empty blocks:** After first pass, retry any block that returned zero services
5. **Full scan:** All 38 Band III blocks (5A–13F), user-triggered only
6. **Estimated priority scan time:** ~60-90 seconds (6 blocks x 10-15s each)

### Canberra Minimum Requirement
Must discover all stations on blocks **8D** (Canberra commercial) and **9C** (ABC/SBS national).

---

## 4. Concurrency Model

### Problem
Multiple browser clients need to consume audio from a single welle-cli instance. welle-cli serves MP3 via its `/mp3/<SID>` endpoint, but its behaviour with multiple concurrent clients is uncertain.

### Recommended Architecture: Proxy with Fan-Out

```
welle-cli /mp3/<SID> → FastAPI proxy (single reader) → Ring buffer → Multiple browser clients
```

**Single upstream reader:** The FastAPI backend maintains ONE connection to welle-cli's `/mp3/<SID>` for each active service. This avoids overwhelming welle-cli with duplicate requests.

**Ring buffer for fan-out:** A shared ring buffer (or asyncio broadcast pattern) distributes the MP3 chunks to all connected browser clients.

**Implementation pattern:**
```python
class AudioBroadcaster:
    """Reads from welle-cli once, broadcasts to N clients."""
    
    def __init__(self):
        self.clients: list[asyncio.Queue] = []
    
    async def feed(self, chunk: bytes):
        for queue in self.clients:
            try:
                queue.put_nowait(chunk)
            except asyncio.QueueFull:
                pass  # Drop frames for slow clients
    
    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=64)
        self.clients.append(q)
        return q
    
    def unsubscribe(self, q: asyncio.Queue):
        self.clients.remove(q)
```

### FastAPI Async Streaming

Use `StreamingResponse` with an async generator:

```python
from starlette.responses import StreamingResponse

@app.get("/api/station/{id}/stream")
async def stream_station(id: str):
    queue = broadcaster.subscribe()
    try:
        async def generate():
            while True:
                chunk = await queue.get()
                yield chunk
        return StreamingResponse(generate(), media_type="audio/mpeg")
    except Exception:
        broadcaster.unsubscribe(queue)
```

### Why async over sync
- Non-blocking I/O for multiple concurrent streams
- No thread-per-client overhead
- Natural fit with FastAPI/Starlette's async capabilities
- asyncio.Queue is an efficient inter-task communication primitive

---

## 5. PyChromecast

### Current State
PyChromecast is the de facto Python library for Chromecast control. Actively maintained.

### Key Points
- Uses mDNS (via zeroconf) to discover Chromecast devices on LAN
- Can cast HTTP URLs directly — Chromecast fetches the stream from the server
- **No TLS required** for LAN streaming — Chromecast accepts plain HTTP URLs
- Requires `avahi-daemon` for mDNS on Linux
- MP3 is a supported media type on Chromecast

### Cast Flow
```python
import pychromecast

# Discover
chromecasts, browser = pychromecast.get_chromecasts()
cast = chromecasts[0]
cast.wait()

# Play
mc = cast.media_controller
mc.play_media("http://192.168.1.100:8800/api/station/0x1001/stream", "audio/mpeg")
mc.block_until_active()
```

### Requirements
- `pychromecast` Python package
- `avahi-daemon` system package (for mDNS)
- Server must be reachable from Chromecast device (same LAN)
- Stream URL must use server's LAN IP (not localhost)

---

## 6. Similar Projects

### welle.io built-in web UI
welle-cli's own `-w` mode includes an HTML/JS frontend. This validates that browser-based DAB+ streaming works via the `/mp3/<SID>` endpoint. Our project builds a better UI on top of the same mechanism.

### tunlezah/dab (prior project)
Analysed separately in `failure-analysis.md`. Used FastAPI + welle-cli with custom scanning logic.

### ODR-DabMux / ODR-AudioEnc
Professional DAB multiplexer tools. Overkill for reception but confirm the PCM format expectations (48kHz, 16-bit, stereo).

---

## 7. Technology Stack Summary

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| DAB+ reception & decoding | welle-cli (apt package) | Proven, handles all RF/DSP |
| Audio format (browser) | MP3 via welle-cli web server | Zero additional encoding needed |
| Backend API | FastAPI (async) | Efficient streaming proxy |
| Frontend | Vanilla HTML/CSS/JS or lightweight framework | Simple, no build step |
| Chromecast | PyChromecast | Only viable Python option |
| mDNS | avahi-daemon | Required for Chromecast discovery |
| Process management | Python subprocess + asyncio | Direct control over welle-cli |
| Configuration | YAML (PyYAML) | Human-readable |
| Python | 3.12 (Ubuntu 24.04 default) | System Python, virtualenv |
