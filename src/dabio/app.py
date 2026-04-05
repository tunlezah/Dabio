"""FastAPI application — Dabio DAB+ Radio Web Player."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .audio import BroadcasterPool, stream_audio
from .chromecast import ChromecastManager
from .config import AppConfig, PROJECT_ROOT
from .logging_config import get_logger, setup_logging
from .mock import get_mock_stations_for_scan, MOCK_STATIONS
from .mock_server import MockWelleServer
from .models import Station
from .scanner import Scanner
from .welle_manager import WelleManager, WelleState

log = get_logger("api")

# Global state — initialized in lifespan
config: AppConfig
welle: WelleManager
scanner: Scanner
broadcasters: BroadcasterPool
chromecast_mgr: ChromecastManager
mock_server: MockWelleServer | None = None


@asynccontextmanager
async def lifespan(application: FastAPI):
    global config, welle, scanner, broadcasters, chromecast_mgr, mock_server

    setup_logging()
    config = AppConfig.load()
    log.info(f"Dabio starting (mock_mode={config.mock_mode})")

    welle = WelleManager(config)
    scanner = Scanner(config, welle)
    broadcasters = BroadcasterPool(config.welle_cli.internal_port)
    chromecast_mgr = ChromecastManager(config.server.port)

    if config.mock_mode:
        mock_server = MockWelleServer(config.welle_cli.internal_port)
        await mock_server.start()
        # Pre-populate stations in mock mode
        for s in MOCK_STATIONS:
            station = Station(
                service_id=s["sid"],
                ensemble_id=s.get("eid", "FFFF"),
                name=s["label"],
                ensemble_name=s.get("ensemble", "Mock"),
                block=s.get("block", "9C"),
            )
            scanner._stations[station.station_id] = station
        log.info(f"Mock mode: loaded {len(scanner._stations)} fake stations")
    else:
        # Detect welle-cli
        binary, version = welle.detect_version()
        if binary:
            log.info(f"Found welle-cli: {binary} (version {version.value})")
        else:
            log.error("welle-cli not found! Install it or enable mock_mode.")

    yield

    # Shutdown
    await broadcasters.stop_all()
    await welle.stop()
    if mock_server:
        await mock_server.stop()
    chromecast_mgr.shutdown()
    log.info("Dabio stopped")


app = FastAPI(title="Dabio", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")


# --- API Endpoints ---


@app.get("/api/stations")
async def list_stations():
    stations = scanner.stations
    result = []
    for s in stations.values():
        result.append({
            "id": s.station_id,
            "name": s.name,
            "ensemble": s.ensemble_name,
            "ensemble_id": s.ensemble_id,
            "service_id": s.service_id,
            "block": s.block,
        })
    # Group by block for frontend
    result.sort(key=lambda x: (x["block"], x["name"]))
    return {"stations": result, "count": len(result)}


@app.post("/api/station/{station_id}/play")
async def play_station(station_id: str):
    station = scanner.get_station(station_id)
    if not station:
        return JSONResponse({"error": "Station not found"}, status_code=404)

    if scanner.is_scanning:
        return JSONResponse({"error": "Scan in progress, try again later"}, status_code=409)

    # Tune to the station's block if not already there
    current_channel = welle.status.channel
    if current_channel != station.block:
        if config.mock_mode and mock_server:
            mock_server.channel = station.block
        else:
            await welle.tune(station.block)
            # Wait for welle-cli to stabilize
            await asyncio.sleep(1.5)

    return {"status": "playing", "station": station.station_id, "block": station.block}


@app.get("/api/station/{station_id}/stream")
async def stream_station(station_id: str, request: Request):
    station = scanner.get_station(station_id)
    if not station:
        return JSONResponse({"error": "Station not found"}, status_code=404)

    broadcaster = broadcasters.get_or_create(station.service_id)

    async def audio_gen():
        q = broadcaster.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=10.0)
                    if chunk:
                        yield chunk
                except asyncio.TimeoutError:
                    continue
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            broadcaster.unsubscribe(q)
            broadcasters.remove_if_empty(station.service_id)

    return StreamingResponse(
        audio_gen(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/station/{station_id}/metadata")
async def station_metadata(station_id: str):
    station = scanner.get_station(station_id)
    if not station:
        return JSONResponse({"error": "Station not found"}, status_code=404)

    port = config.welle_cli.internal_port
    dls_text = ""
    slide_url = None

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{port}/mux.json", timeout=3.0)
            if resp.status_code == 200:
                data = resp.json()
                for svc in data.get("services", []):
                    sid = svc.get("sid", "")
                    if isinstance(sid, int):
                        sid = f"{sid:04X}"
                    else:
                        sid = str(sid).replace("0x", "").replace("0X", "").upper()
                    if sid == station.service_id:
                        dls_text = svc.get("dls_label", "")
                        if isinstance(dls_text, dict):
                            dls_text = dls_text.get("label", "")
                        # Check for slide
                        slide_url = f"/api/station/{station_id}/slide"
                        break
    except Exception as e:
        log.debug(f"Metadata fetch failed: {e}")

    return {
        "station_id": station_id,
        "name": station.name,
        "ensemble": station.ensemble_name,
        "dls_text": dls_text,
        "slide_url": slide_url,
    }


@app.get("/api/station/{station_id}/slide")
async def station_slide(station_id: str):
    station = scanner.get_station(station_id)
    if not station:
        return JSONResponse({"error": "Station not found"}, status_code=404)

    port = config.welle_cli.internal_port
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{port}/slide/{station.service_id}",
                timeout=3.0,
            )
            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "image/jpeg")
                return StreamingResponse(
                    iter([resp.content]),
                    media_type=content_type,
                    headers={"Cache-Control": "no-cache"},
                )
    except Exception:
        pass
    return JSONResponse({"error": "No slide available"}, status_code=404)


@app.get("/api/scan")
async def trigger_scan(full: bool = False):
    if scanner.is_scanning:
        return {"status": "already_scanning"}

    if welle.state == WelleState.TUNED and not config.mock_mode:
        return JSONResponse(
            {"error": "Cannot scan while playing. Stop playback first."},
            status_code=409,
        )

    asyncio.create_task(_run_scan(full))
    return {"status": "scan_started", "full": full}


async def _run_scan(full: bool) -> None:
    try:
        await scanner.scan(full=full)
    except Exception as e:
        log.error(f"Scan failed: {e}")


@app.get("/api/health")
async def health_check():
    binary, version = welle.detect_version() if not config.mock_mode else (None, None)
    is_healthy = config.mock_mode or welle.is_running()

    return {
        "status": "ok" if is_healthy else "degraded",
        "mock_mode": config.mock_mode,
        "welle_cli": {
            "running": welle.is_running(),
            "state": welle.state.value,
            "channel": welle.status.channel,
            "version": welle.status.version.value if not config.mock_mode else "mock",
            "binary": welle.status.binary_path,
            "pid": welle.status.pid,
            "error": welle.status.error,
        },
        "stations_count": len(scanner.stations),
        "scanning": scanner.is_scanning,
    }


# --- Chromecast Endpoints ---


@app.get("/api/chromecast/devices")
async def chromecast_devices():
    devices = await chromecast_mgr.discover()
    return {
        "devices": [
            {"uuid": d.uuid, "name": d.name, "model": d.model, "host": d.host}
            for d in devices
        ]
    }


@app.post("/api/chromecast/cast")
async def chromecast_cast(request: Request):
    body = await request.json()
    device_uuid = body.get("device_uuid")
    station_id = body.get("station_id")
    if not device_uuid or not station_id:
        return JSONResponse({"error": "device_uuid and station_id required"}, status_code=400)
    success = await chromecast_mgr.cast(device_uuid, station_id)
    return {"status": "casting" if success else "failed"}


@app.post("/api/chromecast/stop")
async def chromecast_stop():
    success = await chromecast_mgr.stop_cast()
    return {"status": "stopped" if success else "no_active_cast"}


# --- Frontend ---


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = PROJECT_ROOT / "static" / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Dabio</h1><p>Frontend not found. Check static/index.html</p>")
