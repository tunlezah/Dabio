import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import AppConfig, BAND_III_BLOCKS, STATIONS_CACHE, DATA_DIR
from .logging_config import get_logger
from .models import ScanResult, Station
from .welle_manager import WelleManager, WelleState, WelleVersion

log = get_logger("scanner")


def _extract_label(value) -> str:
    """Extract label string from welle-cli's mux.json, handling both
    plain strings and nested dicts like {"label": "...", "shortlabel": "..."}."""
    if isinstance(value, dict):
        return value.get("label", "").strip()
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


@dataclass
class ScanProgress:
    """Live scan progress state, polled by the frontend."""
    scanning: bool = False
    phase: str = ""  # "priority", "full", "retry", "complete", "error"
    current_block: str = ""
    current_block_index: int = 0
    total_blocks: int = 0
    stations_found: int = 0
    blocks_scanned: int = 0
    dwell_elapsed: float = 0
    dwell_total: float = 0
    started_at: float = 0
    error: str = ""

    @property
    def percent(self) -> float:
        if self.total_blocks == 0:
            return 0
        return min(100.0, (self.blocks_scanned / self.total_blocks) * 100)

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at == 0:
            return 0
        return time.time() - self.started_at

    @property
    def eta_seconds(self) -> float:
        if self.blocks_scanned == 0 or self.total_blocks == 0:
            return self.total_blocks * 14  # rough estimate
        rate = self.elapsed_seconds / self.blocks_scanned
        remaining = self.total_blocks - self.blocks_scanned
        return remaining * rate

    def to_dict(self) -> dict:
        return {
            "scanning": self.scanning,
            "phase": self.phase,
            "current_block": self.current_block,
            "current_block_index": self.current_block_index,
            "total_blocks": self.total_blocks,
            "blocks_scanned": self.blocks_scanned,
            "stations_found": self.stations_found,
            "percent": round(self.percent, 1),
            "dwell_elapsed": round(self.dwell_elapsed, 1),
            "dwell_total": round(self.dwell_total, 1),
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "eta_seconds": round(self.eta_seconds, 1),
            "error": self.error,
        }


class Scanner:
    def __init__(self, config: AppConfig, welle: WelleManager):
        self.config = config
        self.welle = welle
        self._stations: dict[str, Station] = {}
        self._scanning = False
        self.progress = ScanProgress()
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
        """Scan for DAB+ stations. Priority blocks first, optionally all blocks.

        For v2.7: starts welle-cli once, then uses POST /channel to retune.
        For v2.4: restarts welle-cli per block (POST /channel unreliable on Debian builds).
        """
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
                for block in BAND_III_BLOCKS:
                    if block not in blocks:
                        blocks.append(block)

            retry_estimate = len(self.config.scanning.priority_blocks) if self.config.scanning.retry_empty else 0
            total_blocks_estimate = len(blocks) + retry_estimate

            self.progress = ScanProgress(
                scanning=True,
                phase="priority" if not full else "full",
                total_blocks=total_blocks_estimate,
                started_at=time.time(),
            )

            log.info(f"Starting scan: {len(blocks)} blocks (full={full}), version={self.welle.version.value}")

            # Start welle-cli on the first block
            first_block = blocks[0]
            await self._tune_for_scan(first_block, is_first=True)

            # First pass
            empty_blocks = []
            for i, block in enumerate(blocks):
                self.progress.current_block = block
                self.progress.current_block_index = i + 1
                self.progress.dwell_elapsed = 0
                self.progress.dwell_total = self.config.scanning.dwell_time

                # Retune if not already on this block (first block already tuned above)
                if i > 0:
                    await self._tune_for_scan(block, is_first=False)

                result = await self._dwell_and_poll(block)
                self.progress.blocks_scanned = i + 1

                if result and result.stations:
                    for station in result.stations:
                        self._stations[station.station_id] = station
                    self.progress.stations_found = len(self._stations)
                    log.info(f"Block {block}: found {len(result.stations)} stations in '{result.ensemble_name}'")
                else:
                    empty_blocks.append(block)
                    log.info(f"Block {block}: no stations found")

            # Retry empty priority blocks
            if self.config.scanning.retry_empty and empty_blocks:
                retry_blocks = [b for b in empty_blocks if b in self.config.scanning.priority_blocks]
                if retry_blocks:
                    self.progress.phase = "retry"
                    self.progress.total_blocks = len(blocks) + len(retry_blocks)
                    log.info(f"Retrying {len(retry_blocks)} empty priority blocks")

                    for i, block in enumerate(retry_blocks):
                        overall_idx = len(blocks) + i + 1
                        self.progress.current_block = block
                        self.progress.current_block_index = overall_idx
                        self.progress.dwell_elapsed = 0
                        self.progress.dwell_total = self.config.scanning.dwell_time

                        await self._tune_for_scan(block, is_first=False)
                        result = await self._dwell_and_poll(block)
                        self.progress.blocks_scanned = len(blocks) + i + 1

                        if result and result.stations:
                            for station in result.stations:
                                self._stations[station.station_id] = station
                            self.progress.stations_found = len(self._stations)
                            log.info(f"Block {block} (retry): found {len(result.stations)} stations")
                else:
                    self.progress.total_blocks = len(blocks)

            self._save_cache()
            self.progress.phase = "complete"
            self.progress.scanning = False
            self.progress.current_block = ""
            log.info(f"Scan complete: {len(self._stations)} total stations")

        except Exception as e:
            log.error(f"Scan error: {e}", exc_info=True)
            self.progress.phase = "error"
            self.progress.error = str(e)
            self.progress.scanning = False
        finally:
            self._scanning = False
            if previous_channel:
                await self._tune_for_scan(previous_channel, is_first=False)

        return self._stations

    async def _tune_for_scan(self, block: str, is_first: bool) -> None:
        """Tune welle-cli to a block.

        v2.7 (source-built): Start once, then use POST /channel to retune.
          This avoids killing/restarting the process and losing SDR state.
        v2.4 (apt/fallback): Restart welle-cli each time because POST /channel
          is unreliable on Debian-patched builds.
        """
        if is_first or self.welle.version != WelleVersion.V2_7:
            # First block or v2.4: start (or restart) welle-cli
            log.info(f"Starting welle-cli on block {block}")
            await self.welle.start(block)
            # Longer initial wait for SDR initialization + signal acquisition
            await asyncio.sleep(3)
        else:
            # v2.7: retune via POST /channel (keeps SDR warm, faster lock)
            log.info(f"Retuning to block {block} via POST /channel")
            success = await self.welle._post_channel(block)
            if success:
                self.welle._status.channel = block
                # Wait for retune + FIC acquisition
                await asyncio.sleep(3)
            else:
                # POST /channel failed — fall back to restart
                log.warning(f"POST /channel failed for {block}, restarting welle-cli")
                await self.welle.start(block)
                await asyncio.sleep(3)

    async def _dwell_and_poll(self, block: str) -> ScanResult | None:
        """Dwell on a block, polling mux.json for best result."""
        port = self.config.welle_cli.internal_port
        dwell = self.config.scanning.dwell_time
        poll_interval = self.config.scanning.poll_interval

        best_result: ScanResult | None = None
        best_count = 0
        elapsed = 0.0

        while elapsed < dwell:
            self.progress.dwell_elapsed = elapsed
            result = await self._fetch_mux(port, block)
            if result and len(result.stations) > best_count:
                best_result = result
                best_count = len(result.stations)
                log.info(f"Block {block}: {best_count} stations found so far (dwell {elapsed:.0f}/{dwell}s)")
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        self.progress.dwell_elapsed = dwell
        return best_result

    async def _fetch_mux(self, port: int, block: str) -> ScanResult | None:
        """Fetch and parse /mux.json from welle-cli."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"http://127.0.0.1:{port}/mux.json", timeout=5.0
                )
                if resp.status_code != 200:
                    log.debug(f"mux.json returned status {resp.status_code} for block {block}")
                    return None

                data = resp.json()
        except Exception as e:
            log.debug(f"Failed to fetch mux.json for block {block}: {e}")
            return None

        # Log raw response for debugging (first poll per block)
        ensemble_raw = data.get("ensemble", {})
        services_raw = data.get("services", [])
        log.debug(
            f"mux.json for {block}: ensemble={json.dumps(ensemble_raw, default=str)}, "
            f"services_count={len(services_raw)}"
        )
        if services_raw:
            # Log first service for format debugging
            log.debug(f"First service sample: {json.dumps(services_raw[0], default=str)[:300]}")

        ensemble_name = _extract_label(ensemble_raw.get("label", ""))
        ensemble_id = ensemble_raw.get("id", "")
        if isinstance(ensemble_id, int):
            ensemble_id = f"{ensemble_id:04X}"
        elif isinstance(ensemble_id, str):
            # Normalize: strip 0x, uppercase
            clean = ensemble_id.replace("0x", "").replace("0X", "").strip()
            if clean:
                try:
                    ensemble_id = f"{int(clean, 16):04X}"
                except (ValueError, TypeError):
                    ensemble_id = clean.upper()

        stations = []
        for svc in services_raw:
            sid = svc.get("sid", "")
            if isinstance(sid, int):
                sid = f"{sid:04X}"
            elif isinstance(sid, str):
                sid = sid.replace("0x", "").replace("0X", "").upper().strip()
                if not sid:
                    continue

            name = _extract_label(svc.get("label", ""))
            if not name:
                # Don't skip — log it. The service might have a SID but label not yet decoded
                log.debug(f"Service SID={sid} has no label yet (still decoding FIC)")
                continue

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
