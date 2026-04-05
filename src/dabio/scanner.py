import asyncio
import json
from pathlib import Path

import httpx

from .config import AppConfig, BAND_III_BLOCKS, STATIONS_CACHE, DATA_DIR
from .logging_config import get_logger
from .models import ScanResult, Station
from .welle_manager import WelleManager, WelleState

log = get_logger("scanner")


def _extract_label(value) -> str:
    """Extract label string from welle-cli's mux.json, handling both
    plain strings and nested dicts like {"label": "...", "shortlabel": "..."}."""
    if isinstance(value, dict):
        return value.get("label", "").strip()
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


class Scanner:
    def __init__(self, config: AppConfig, welle: WelleManager):
        self.config = config
        self.welle = welle
        self._stations: dict[str, Station] = {}
        self._scanning = False
        self._load_cache()

    @property
    def stations(self) -> dict[str, Station]:
        return dict(self._stations)

    @property
    def is_scanning(self) -> bool:
        return self._scanning

    def get_station(self, station_id: str) -> Station | None:
        return self._stations.get(station_id)

    def _load_cache(self) -> None:
        if STATIONS_CACHE.exists():
            try:
                with open(STATIONS_CACHE) as f:
                    data = json.load(f)
                for s in data:
                    st = Station(**s)
                    self._stations[st.station_id] = st
                log.info(f"Loaded {len(self._stations)} cached stations")
            except Exception as e:
                log.warning(f"Failed to load station cache: {e}")

    def _save_cache(self) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        try:
            data = [
                {
                    "service_id": s.service_id,
                    "ensemble_id": s.ensemble_id,
                    "name": s.name,
                    "ensemble_name": s.ensemble_name,
                    "block": s.block,
                    "station_id": s.station_id,
                }
                for s in self._stations.values()
            ]
            with open(STATIONS_CACHE, "w") as f:
                json.dump(data, f, indent=2)
            log.info(f"Saved {len(data)} stations to cache")
        except Exception as e:
            log.warning(f"Failed to save station cache: {e}")

    async def scan(self, full: bool = False) -> dict[str, Station]:
        """Scan for DAB+ stations. Priority blocks first, optionally all blocks."""
        if self._scanning:
            log.warning("Scan already in progress")
            return self._stations

        if self.welle.state == WelleState.TUNED:
            log.warning("Cannot scan while audio is playing")
            return self._stations

        self._scanning = True
        previous_channel = self.welle.status.channel

        try:
            blocks = list(self.config.scanning.priority_blocks)
            if full:
                # Add remaining blocks not already in priority list
                for block in BAND_III_BLOCKS:
                    if block not in blocks:
                        blocks.append(block)

            log.info(f"Starting scan: {len(blocks)} blocks (full={full})")

            # First pass
            empty_blocks = []
            for block in blocks:
                result = await self._scan_block(block)
                if result and result.stations:
                    for station in result.stations:
                        self._stations[station.station_id] = station
                    log.info(f"Block {block}: found {len(result.stations)} stations in '{result.ensemble_name}'")
                else:
                    empty_blocks.append(block)
                    log.debug(f"Block {block}: no stations found")

            # Retry empty blocks (only priority blocks are retried)
            if self.config.scanning.retry_empty and empty_blocks:
                retry_blocks = [b for b in empty_blocks if b in self.config.scanning.priority_blocks]
                if retry_blocks:
                    log.info(f"Retrying {len(retry_blocks)} empty priority blocks")
                    for block in retry_blocks:
                        result = await self._scan_block(block)
                        if result and result.stations:
                            for station in result.stations:
                                self._stations[station.station_id] = station
                            log.info(f"Block {block} (retry): found {len(result.stations)} stations")

            self._save_cache()
            log.info(f"Scan complete: {len(self._stations)} total stations")

        finally:
            self._scanning = False
            # Restore previous channel if we were tuned
            if previous_channel:
                await self.welle.start(previous_channel)

        return self._stations

    async def _scan_block(self, block: str) -> ScanResult | None:
        """Scan a single DAB block with polling for best result."""
        if block not in BAND_III_BLOCKS:
            log.warning(f"Unknown block: {block}")
            return None

        # Tune to this block
        await self.welle.start(block)

        # Wait briefly for initial lock
        await asyncio.sleep(2)

        port = self.config.welle_cli.internal_port
        dwell = self.config.scanning.dwell_time
        poll_interval = self.config.scanning.poll_interval

        best_result: ScanResult | None = None
        best_count = 0
        elapsed = 0

        while elapsed < dwell:
            result = await self._fetch_mux(port, block)
            if result and len(result.stations) > best_count:
                best_result = result
                best_count = len(result.stations)
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        return best_result

    async def _fetch_mux(self, port: int, block: str) -> ScanResult | None:
        """Fetch and parse /mux.json from welle-cli."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"http://127.0.0.1:{port}/mux.json", timeout=5.0
                )
                if resp.status_code != 200:
                    return None

                data = resp.json()
        except Exception as e:
            log.debug(f"Failed to fetch mux.json for block {block}: {e}")
            return None

        ensemble_name = _extract_label(data.get("ensemble", {}).get("label", ""))
        ensemble_id = data.get("ensemble", {}).get("id", "")
        if isinstance(ensemble_id, int):
            ensemble_id = f"{ensemble_id:04X}"
        elif isinstance(ensemble_id, str) and not ensemble_id.startswith("0"):
            # Try to normalize
            try:
                ensemble_id = f"{int(ensemble_id, 0):04X}"
            except (ValueError, TypeError):
                pass

        services = data.get("services", [])
        stations = []

        for svc in services:
            sid = svc.get("sid", "")
            if isinstance(sid, int):
                sid = f"{sid:04X}"
            elif isinstance(sid, str):
                # Normalize: strip 0x prefix if present, uppercase
                sid = sid.replace("0x", "").replace("0X", "").upper()
                if not sid:
                    continue

            name = _extract_label(svc.get("label", ""))
            if not name:
                continue  # Skip services still being decoded

            station = Station(
                service_id=sid,
                ensemble_id=ensemble_id,
                name=name,
                ensemble_name=ensemble_name,
                block=block,
            )
            stations.append(station)

        if not ensemble_name and not stations:
            return None

        return ScanResult(
            block=block,
            ensemble_name=ensemble_name,
            ensemble_id=ensemble_id,
            stations=stations,
        )
