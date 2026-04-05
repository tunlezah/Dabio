# Failure Analysis: Prior Project (tunlezah/dab)

## Overview

The repository https://github.com/tunlezah/dab is a 25-commit, 2-week-old project (March 22 – April 5, 2026) that achieved a working DAB+ web radio quickly but then entered a cycle of bug-fix churn. Analysis reveals a project with sound architecture but implementation failures in scanning reliability, process management, and error handling.

## Architecture of Prior Project

### Tech Stack
- **Frontend:** Vanilla HTML/JS/CSS single-page app (no build tools, no framework)
- **Backend:** Python FastAPI (async) on uvicorn, port 8080
- **DAB+ engine:** welle-cli subprocess with built-in HTTP server on port 7979
- **Hardware:** RTL-SDR USB dongle (RTL2832U)
- **Server audio:** `mpg123` subprocess for local speaker output
- **Installer:** Bash script targeting Ubuntu 24.04, builds welle-cli from source, sets up systemd service
- **Key modules:** `config.py`, `scanner.py`, `welle_manager.py`, `routes.py`, `main.py`

### How It Worked
The backend proxied welle-cli's `/mp3/{serviceId}` endpoint via `httpx.AsyncClient` streaming through FastAPI's `StreamingResponse`. The browser played this via an `<audio>` element. welle-cli handled all DAB+ demodulation, FIC parsing, metadata extraction, and HE-AAC-to-MP3 transcoding. The Python layer was purely an orchestrator/proxy.

### What Worked Initially
The initial commit (March 22, 2026) delivered a functional end-to-end system:
- Station scanning across Band III channels
- Audio streaming to browser
- DLS metadata display
- Dark theme web UI
- systemd service and installer
- 22 passing tests

The core concept — welle-cli as HTTP-accessible DAB+ engine wrapped with a web UI — was sound and got to a working prototype within one day.

## Chronological Bug History

### PR #3 (Mar 23) — Scan crash: welle-cli label format mismatch
welle-cli returns station labels as nested dicts (`{"label": "...", "shortlabel": "..."}`) not plain strings. The scanner assumed strings, causing crashes. Required adding `_extract_label()` helper.

### PR #4 (Mar 23) — `[object Object]` in now-playing display
Same label-format issue hit the metadata endpoint and frontend. Also: slide images were flashing due to repeated re-fetching.

### PR #8 (Apr 5) — Reinstall leaves app permanently offline
Orphaned `welle-cli` and `rtl_test` processes from previous installs held the USB device and port 7979, preventing the new service from starting. Required adding process cleanup to the installer.

### PR #9 (Apr 5) — welle-cli HTTP API hangs on Debian package version
The Debian-packaged `welle-cli` (2.4+ds) does not respond to `POST /channel` for retuning. A `tune()` fallback with process restart was added, then reverted because the root fix was to always use the source-built binary. The installer was hardened to check `/usr/local/bin/welle-cli` specifically.

### PR #10 (Open, Apr 5) — Scanning still misses stations
Even after all fixes, the scanner finds fewer stations than a cheap consumer DAB+ radio. This is the problem that prompted a deep audit and remains unresolved.

## Identified Failure Patterns

### Failure 1: Insufficient Scanning Dwell Time

**Problem:** The scanner used a 4-second dwell time per channel. DAB FIC acquisition takes 2-10 seconds depending on signal strength. Consumer radios dwell 8-15 seconds.

**Symptom:** Weak ensembles were systematically missed during scans.

**Root cause:** DAB+ FIC information is broadcast cyclically. A short dwell means the scanner may miss services not yet announced in the cycle.

**Fix applied (PR #10):** Increased dwell time to 10 seconds.

**Lesson:** Use 10s minimum dwell time. Consumer radios use 8-15s for good reason.

### Failure 2: FIC Race Condition (Single-Fetch Polling)

**Problem:** `mux.json` was fetched once at the end of the dwell period. If welle-cli hadn't finished FIC parsing, the response contained zero or partial services. Services without labels (still being decoded) were silently filtered out by the SID/name check.

**Symptom:** Unreliable station counts — same channel showed different numbers across scan runs.

**Root cause:** No mechanism to distinguish "empty channel" from "still loading."

**Fix applied (PR #10):** Poll `/mux.json` every 2 seconds during dwell. Keep best result (most services).

**Lesson:** Implement polling with best-result retention. Track ensemble lock status separately from service count.

### Failure 3: No Retry for Empty Channels

**Problem:** Each channel got exactly one scan attempt. A single transient failure — USB hiccup, interference burst, slow FIC lock — permanently discarded that channel.

**Symptom:** Known-active channels (like 8D for Canberra) sometimes showed no stations.

**Fix applied (PR #10):** After initial pass, retry channels that returned zero stations.

**Lesson:** Always retry empty results at least once. Transient failures are common in RF reception.

### Failure 4: Silent Error Swallowing

**Problem:** `_run_scan` wrapped each channel in bare `except Exception` with no logging. HTTP timeouts, connection errors, and welle-cli crashes all produced the same silent "no stations" result. The 5-second httpx timeout could race against FIC acquisition, producing `None` returns indistinguishable from genuinely empty channels.

**Symptom:** Debugging was nearly impossible — all failures looked like "no stations found."

**Lesson:** Every scan failure must be logged with the specific cause (timeout, connection refused, malformed JSON, empty FIC). Never swallow exceptions silently.

### Failure 5: Hardcoded Binary Paths

**Problem:** The systemd service file had a hardcoded path to `welle-cli`, which broke when the installation method changed.

**Symptom:** Service failed to start after certain install/update scenarios.

**Fix applied (PR #10):** Reverted to PATH-based resolution.

**Lesson:** Use `shutil.which("welle-cli")` dynamically, with configurable override.

### Failure 6: Debian vs Source-Built API Differences

**Problem:** The Debian-packaged `welle-cli` (2.4+ds) behaves differently from the source-built version — specifically, `POST /channel` hangs or doesn't work.

**Symptom:** Channel retuning silently failed, breaking scanning entirely.

**Lesson:** If using apt-installed welle-cli, verify `POST /channel` works during smoke test. If it doesn't, fall back to process restart for retuning.

### Failure 7: Orphaned Process Contamination

**Problem:** Previous installs left `welle-cli` and `rtl_test` processes running, holding the USB device and port 7979. The installer didn't clean these up.

**Symptom:** Fresh installs immediately failed — "device busy" or "port in use."

**Lesson:** Installer MUST kill prior processes and verify port/device availability before starting. This is an idempotency requirement.

### Failure 8: Audio Stream Proxy Without Backpressure

**Problem:** The stream proxy used `httpx.AsyncClient(timeout=None)` streaming through a generator. No buffering strategy, no reconnection on interruption, no handling if welle-cli stream stalls. If the browser paused consumption, chunks accumulated in memory.

**Symptom:** Memory growth under load, stalled streams.

**Lesson:** Audio proxy needs bounded buffering (ring buffer or bounded queue), reconnection logic, and explicit timeout handling.

### Failure 9: No SDR Gain Optimization

**Problem:** welle-cli was launched with no gain flags. The RTL-SDR operated at whatever default gain welle-cli chose, which could be wrong for the RF environment.

**Symptom:** Missed stations due to ADC saturation on strong signals or insufficient sensitivity on weak ones.

**Lesson:** Start with AGC (`-g -1`) but be prepared for AGC drift issues. Expose gain as a configurable parameter. Consider fixed gain if AGC proves unstable.

## Summary of Patterns to Avoid

| Pattern | Risk | Mitigation |
|---------|------|------------|
| Short dwell time (4s) | Missed stations | 10s minimum, configurable |
| Single metadata fetch | Unreliable counts | Poll every 2s, keep best result |
| No retry | Permanent misses | Retry empty channels at least once |
| Silent exception swallowing | Impossible debugging | Log every failure with specific cause |
| Hardcoded paths | Breaks on different installs | Dynamic PATH resolution |
| Assume apt == source build | API differences | Smoke test critical endpoints |
| No process cleanup | Orphaned processes block device | Kill priors in installer |
| Scan during playback | Audio interruption | State machine with mutex |
| Unbounded stream proxy | Memory leaks | Bounded queue with drop-oldest |
| No gain control | Poor reception | Configurable gain, default AGC |

## What the Prior Project Did Right

1. **Used welle-cli** — correct foundational choice
2. **FastAPI backend** — appropriate for async streaming
3. **Modular design** — separate scanner, manager, routes
4. **Had an installer** — automated setup
5. **Vanilla JS frontend** — no build step, simple deployment
6. **Produced a self-audit document** — correctly identified scanning root causes
7. **22 passing tests** — at least basic test coverage existed

## Key Takeaway

The prior project's failures were in **reliability of scanning**, **process management**, **error handling**, and **deployment robustness** — not in fundamental architecture. The core approach (welle-cli HTTP API + FastAPI proxy + browser audio element) is validated. We must be more disciplined about:

1. Robust scanning with adequate dwell times, polling, and retries
2. Clear state management (no concurrent scan + play)
3. Explicit error logging (never swallow exceptions)
4. Installer idempotency (clean up prior state)
5. Audio proxy with backpressure (bounded queues)
6. Health monitoring and auto-recovery
7. Testing without hardware (mock mode)
8. Smoke testing critical welle-cli endpoints after install
