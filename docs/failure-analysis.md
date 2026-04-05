# Failure Analysis: Prior Project (tunlezah/dab)

## Overview

The repository https://github.com/tunlezah/dab represents a prior attempt at building a DAB+ radio web application. Analysis of the repository reveals a project that achieved initial functionality but suffered from reliability issues, particularly in scanning and audio stability.

## Architecture of Prior Project

### Tech Stack
- **Backend:** Python (FastAPI)
- **DAB+ engine:** welle-cli
- **Key modules:** `config.py`, `scanner.py`, `welle_manager.py`, `routes.py`, `main.py`
- **Installer:** `install.sh`

### How It Worked
The prior project used welle-cli with a Python backend that managed the welle-cli process, handled scanning, and served API endpoints. This is architecturally similar to what we're building.

## Identified Failures

### Failure 1: Insufficient Scanning Dwell Time

**Problem:** The scanner used a 4-second dwell time per channel. This was not enough for welle-cli to fully decode the FIC (Fast Information Channel) and discover all services on a multiplex.

**Symptom:** Stations were intermittently missed during scans. Some channels that had stations would report zero services.

**Root cause:** DAB+ FIC information is broadcast in a cycle. A short dwell time means the scanner may miss services that haven't been announced yet in the cycle.

**Fix applied (PR #10):** Increased dwell time to 10 seconds.

**Lesson for us:** Use 10s minimum dwell time. Consider making this configurable.

### Failure 2: Single-Fetch Metadata Polling

**Problem:** The scanner made a single request to `/mux.json` at the end of the dwell period. If that single request happened to catch an incomplete decode state, stations would be missed.

**Symptom:** Unreliable station counts — same channel might show different numbers of stations across different scan runs.

**Root cause:** DAB+ service information decodes progressively. A single snapshot may not capture all services.

**Fix applied (PR #10):** Poll `/mux.json` every 2 seconds during the dwell period. Keep the result with the highest number of services discovered.

**Lesson for us:** Implement polling with best-result retention, not single-shot queries.

### Failure 3: No Retry for Empty Channels

**Problem:** If a channel scan returned zero stations (due to transient interference, slow decode, or timing), that channel was permanently marked as empty until the next full scan.

**Symptom:** Known-active channels (like 8D for Canberra) would sometimes show no stations.

**Root cause:** No retry mechanism. First failure was final.

**Fix applied (PR #10):** After the initial pass, retry all channels that returned zero stations.

**Lesson for us:** Always retry empty results at least once. Transient failures are common in RF reception.

### Failure 4: Hardcoded Binary Paths

**Problem:** The systemd service file had a hardcoded path to `welle-cli`, which broke when the installation method changed or the binary was in a different location.

**Symptom:** Service failed to start after certain install/update scenarios.

**Fix applied (PR #10):** Reverted to using PATH-based resolution.

**Lesson for us:** Use `shutil.which("welle-cli")` to find the binary dynamically, with a configurable override in config.

### Failure 5: Fragile Process Management (Inferred)

**Problem:** Based on the architecture (separate `welle_manager.py`), the process management for welle-cli was likely a source of issues — race conditions between scanning (which requires retuning) and playback.

**Symptom:** Audio interruption during scans, potential crashes when switching channels while streaming.

**Lesson for us:**
- Never scan while audio is playing
- Implement a clear state machine: IDLE → SCANNING → PLAYING
- Use a mutex/lock to prevent concurrent retune operations

### Failure 6: Architecture Grew Too Complex (Inferred)

**Problem:** Multiple PRs suggest ongoing fixes for issues that should have been caught by a simpler design. The project appears to have accreted complexity over time.

**Lesson for us:**
- Start with the simplest possible architecture (welle-cli web server → proxy → browser)
- Add complexity only when needed and documented
- Get audio working first, then optimize

## Summary of Patterns to Avoid

| Pattern | Risk | Mitigation |
|---------|------|------------|
| Short dwell time | Missed stations | 10s minimum, configurable |
| Single metadata fetch | Unreliable counts | Poll every 2s, keep best |
| No retry | Permanent misses | Retry empty channels |
| Hardcoded paths | Breaks on different installs | Dynamic PATH resolution |
| Scan during playback | Audio interruption | State machine with mutex |
| Complex pipeline | Fragility | Use welle-cli web server directly |
| No health monitoring | Silent failures | Poll welle-cli health, auto-restart |

## What the Prior Project Did Right

1. **Used welle-cli** — correct foundational choice
2. **FastAPI backend** — appropriate for async streaming
3. **Modular design** — separate scanner, manager, routes
4. **Had an installer** — automated setup

## Key Takeaway

The prior project's failures were primarily in **reliability of scanning** and **process management**, not in fundamental architecture. The core approach (welle-cli + Python backend + web frontend) is sound. We must be more disciplined about:

1. Robust scanning with adequate dwell times and retries
2. Clear state management (no concurrent scan + play)
3. Health monitoring and auto-recovery
4. Testing without hardware (mock mode)
