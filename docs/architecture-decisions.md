# Architecture Decisions

## ADR-1: Use welle-cli Built-in Web Server as Primary Audio Path

**Status:** Accepted

**Context:** welle-cli offers multiple audio output modes. The built-in web server (`-w`) provides MP3 streaming via `/mp3/<SID>` that is directly playable in browsers.

**Decision:** Use welle-cli's built-in web server mode as the primary and only audio path. No FFmpeg, no custom encoding pipeline.

**Rationale:**
- MP3 is universally supported in browsers via `<audio>` element
- Eliminates FFmpeg as a dependency for audio
- Simplest possible pipeline: welle-cli → HTTP → browser
- welle-cli handles AAC decoding AND MP3 encoding internally
- Fewer moving parts = fewer failure modes

**Consequences:**
- Audio quality limited to welle-cli's MP3 encoder settings
- Cannot offer Opus/OGG (not a real loss — MP3 is fine for radio)
- Must proxy through FastAPI for fan-out and consistent API

---

## ADR-2: FastAPI Async Proxy for Audio Streams

**Status:** Accepted

**Context:** Multiple browser clients may request the same station's audio stream. welle-cli's web server behaviour with multiple concurrent readers is uncertain.

**Decision:** FastAPI backend acts as a proxy between browsers and welle-cli. A single upstream connection reads from welle-cli's `/mp3/<SID>`, and an `AudioBroadcaster` class fans out to multiple clients.

**Architecture:**
```
welle-cli :7979/mp3/<SID>
    ↓ (single HTTP reader)
FastAPI AudioBroadcaster (in-memory)
    ↓ (asyncio.Queue per client)
Browser 1  Browser 2  Browser 3
```

**Rationale:**
- Guarantees single reader from welle-cli regardless of client count
- asyncio.Queue is lightweight and non-blocking
- Slow clients can be handled by dropping frames (QueueFull)
- Clean disconnect handling via unsubscribe

**Consequences:**
- Small added latency (~50-100ms for buffering)
- Memory usage scales with number of clients (one Queue per client)
- Must handle upstream disconnect and reconnect gracefully

---

## ADR-3: State Machine for welle-cli Management

**Status:** Accepted

**Context:** The prior project suffered from race conditions between scanning and playback. welle-cli can only tune one channel at a time.

**Decision:** Implement a strict state machine for the welle-cli process:

```
IDLE → SCANNING → IDLE
IDLE → TUNED (playing) → IDLE
TUNED → RETUNING → TUNED
```

**Rules:**
- Scanning is forbidden while in TUNED state
- Channel switch (retune) causes brief audio interruption (~1-3s)
- An asyncio.Lock guards all state transitions
- Playback request during scan: queue the request, complete current scan block, then tune

**Rationale:**
- Eliminates the race conditions that broke the prior project
- Clear, auditable state transitions
- Explicit handling of edge cases (scan during play, play during scan)

---

## ADR-4: Priority-First Scanning with Polling

**Status:** Accepted

**Context:** Scanning all 38 Band III blocks takes ~6-10 minutes. Most Australian markets use only 6 blocks.

**Decision:**
1. Scan priority blocks first: 8C, 8D, 9A, 9B, 9C, 9D
2. Dwell 10 seconds per block (configurable, minimum 8s)
3. Poll `/mux.json` every 2 seconds during dwell, keep the best result (most services)
4. Retry any blocks that returned zero services
5. Cache results to `data/stations.json`
6. Full scan (all 38 blocks) only on explicit user request

**Rationale:**
- Priority scan completes in ~60-90 seconds (acceptable startup time)
- Polling with best-result-retention addresses the prior project's scanning unreliability
- Retry handles transient interference
- Caching means restarts don't require re-scanning

---

## ADR-5: Station Identity by Ensemble ID + Service ID

**Status:** Accepted

**Context:** Station names can be duplicated across ensembles (e.g., "ABC News" might appear on multiple muxes).

**Decision:** Internal station ID = `<ensemble_id>_<service_id>` (e.g., `0x1001_0x0201`). All API endpoints use this composite ID.

**Rationale:**
- Globally unique within a DAB+ system
- Survives name changes
- Matches DAB+ standard identification

---

## ADR-6: Install welle-cli from Ubuntu apt (Preferred)

**Status:** Accepted

**Context:** welle.io 2.4 is available in Ubuntu 24.04 Noble universe repository. The apt package includes both `welle-cli` and `welle-io` binaries.

**Decision:** Prefer apt installation (`sudo apt install -y welle.io`). Fall back to source compilation only if the apt version is broken or missing required features.

**Rationale:**
- Simplest installation path
- Automatic dependency resolution
- Security updates via apt
- Avoids compilation complexity (cmake, dev libraries)
- Verified: package includes `/usr/bin/welle-cli` with man page

**Verification steps after install:**
```bash
welle-cli -v          # Check version (expect 2.4+)
welle-cli -h          # Confirm -w flag exists
```

---

## ADR-7: No Docker

**Status:** Accepted

**Context:** Docker adds complexity for USB device passthrough (RTL-SDR), mDNS (Chromecast), and network access.

**Decision:** Run directly on host OS. Use a Python virtualenv for isolation.

**Rationale:**
- RTL-SDR USB passthrough in Docker is fragile
- mDNS for Chromecast discovery requires host network access
- Simpler debugging and operation
- Project requirement explicitly forbids Docker

---

## ADR-8: Async FastAPI with asyncio

**Status:** Accepted

**Context:** The backend must handle concurrent audio streams, metadata polling, scanning, and API requests.

**Decision:** Use async FastAPI with `asyncio` throughout. No threading for core operations.

**Rationale:**
- FastAPI's async support is mature and well-documented
- `StreamingResponse` with async generators is the natural pattern for audio proxy
- `asyncio.subprocess` for welle-cli process management
- `asyncio.Lock` for state machine guards
- Single event loop, no threading complexity

**Exception:** PyChromecast uses threading internally (zeroconf). This is acceptable — it runs in its own thread pool and doesn't interact with the asyncio event loop directly.

---

## ADR-9: Mock Mode Architecture

**Status:** Accepted

**Context:** Development and testing must be possible without SDR hardware.

**Decision:** Mock mode replaces the welle-cli process with a lightweight Python HTTP server that mimics welle-cli's web server endpoints:
- `/mp3/<SID>` → serves a generated sine wave encoded as MP3
- `/mux.json` → serves fake station metadata
- `/channel` → accepts but ignores channel changes

**Rationale:**
- Exact same API contract as real welle-cli
- No code changes needed in the backend proxy layer
- Synthetic audio generation (sine wave) requires no external files
- Mock server runs on the same internal port as welle-cli would

**Implementation:** Use Python's `http.server` or a lightweight ASGI app. Generate PCM sine wave, encode to MP3 using `lameenc` Python library (or shell out to `lame` if available).

---

## ADR-10: Chromecast via PyChromecast (LAN-only, No TLS)

**Status:** Accepted

**Context:** Chromecast devices on the local network should be able to play DAB+ audio.

**Decision:** Use PyChromecast library. Server sends the stream URL (HTTP, not HTTPS) to the Chromecast, which pulls the MP3 stream directly from the FastAPI backend.

**Architecture:**
```
Chromecast device
    ↓ (HTTP GET)
FastAPI :8800/api/station/{id}/stream?format=mp3
    ↓ (proxy)
welle-cli :7979/mp3/<SID>
```

**Rationale:**
- PyChromecast is the only viable Python Chromecast library
- Chromecast accepts HTTP (no TLS needed on LAN)
- MP3 is natively supported by Chromecast
- No browser involvement in the cast — server controls Chromecast directly
- Requires `avahi-daemon` for mDNS discovery

---

## ADR-11: Configuration via YAML

**Status:** Accepted

**Decision:** Use `config.yaml` at project root. Load with PyYAML. Environment variables can override specific settings.

**Config structure:**
```yaml
sdr:
  gain: -1
  driver: "auto"
server:
  port: 8800
  host: "0.0.0.0"
welle_cli:
  internal_port: 7979
scanning:
  priority_blocks: ["8C", "8D", "9A", "9B", "9C", "9D"]
  dwell_time: 10
  full_scan: false
mock_mode: false
```

---

## ADR-12: Structured JSON Logging

**Status:** Accepted

**Decision:** All backend logs as JSON lines to stdout. Use Python `logging` module with a custom JSON formatter.

**Fields:** timestamp (ISO 8601), component, severity, message.

**Rationale:**
- Machine-parseable for future log aggregation
- Human-readable enough for development
- systemd journal captures stdout automatically
