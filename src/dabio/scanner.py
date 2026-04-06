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

        If no stations are found with the current gain, automatically probes
        different gain values on a known-active block (9C) to find the best one.
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

            # Run the actual block scan
            await self._scan_blocks(blocks)

            # If no stations found at all, try auto-gain detection
            if len(self._stations) == 0:
                log.warning("No stations found with current gain — starting auto-gain detection")
                best_gain = await self._auto_detect_gain()
                if best_gain is not None and best_gain != self.config.sdr.gain:
                    log.info(f"Auto-gain selected index {best_gain} — rescanning")
                    self.config.sdr.gain = best_gain
                    self._save_gain_to_config(best_gain)
                    # Rescan with the new gain
                    self.progress.blocks_scanned = 0
                    self.progress.started_at = time.time()
                    self.progress.phase = "priority"
                    await self._scan_blocks(blocks)

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

    async def _scan_blocks(self, blocks: list[str]) -> None:
        """Scan a list of blocks, including retry pass."""
        # Start welle-cli on the first block
        await self._tune_for_scan(blocks[0], is_first=True)

        # First pass
        empty_blocks = []
        for i, block in enumerate(blocks):
            self.progress.current_block = block
            self.progress.current_block_index = i + 1
            self.progress.dwell_elapsed = 0
            self.progress.dwell_total = self.config.scanning.dwell_time

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

    async def _auto_detect_gain(self) -> int | None:
        """Probe different gain indices on block 9C to find the best one.

        Tests a spread of gain values, picks the one that finds the most stations.
        Returns the best gain index, or None if nothing works.

        R820T gain table:
          Index:  0    5    10   14   16   18   20   22   24   26   28
          dB:     0.0  7.7  16.6 25.4 29.7 33.8 37.2 40.2 43.4 44.5 49.6
        """
        self.progress.phase = "auto-gain"
        test_block = "9C"  # ABC/SBS national — should be available everywhere in Australia

        # Test a spread of gain values (low → high)
        gain_candidates = [10, 14, 16, 18, 20, 22, 24, 26]
        best_gain = None
        best_count = 0

        log.info(f"Auto-gain: testing {len(gain_candidates)} gain values on block {test_block}")

        for i, gain_idx in enumerate(gain_candidates):
            self.progress.current_block = f"{test_block} (gain {gain_idx})"
            self.progress.current_block_index = i + 1
            self.progress.total_blocks = len(gain_candidates)
            self.progress.dwell_elapsed = 0
            self.progress.dwell_total = 8  # shorter dwell for gain probing

            # Restart welle-cli with this gain
            log.info(f"Auto-gain: trying index {gain_idx}")
            await self.welle.start(test_block, gain_override=gain_idx)
            await asyncio.sleep(4)  # Wait for signal acquisition

            # Quick poll — just 3 checks over 6 seconds
            port = self.config.welle_cli.internal_port
            station_count = 0
            for poll in range(3):
                self.progress.dwell_elapsed = poll * 2
                result = await self._fetch_mux(port, test_block)
                if result:
                    station_count = max(station_count, len(result.stations))
                await asyncio.sleep(2)

            log.info(f"Auto-gain: index {gain_idx} → {station_count} stations")

            if station_count > best_count:
                best_count = station_count
                best_gain = gain_idx

            # If we found a good number of stations, no need to keep testing
            if station_count >= 10:
                log.info(f"Auto-gain: index {gain_idx} found {station_count} stations — good enough")
                break

        if best_gain is not None and best_count > 0:
            log.info(f"Auto-gain: best gain index = {best_gain} ({best_count} stations)")
        else:
            log.warning("Auto-gain: no stations found at any gain level. Check antenna.")

        return best_gain

    def _save_gain_to_config(self, gain_idx: int) -> None:
        """Persist the discovered gain value to config.yaml."""
        from .config import CONFIG_PATH
        try:
            if CONFIG_PATH.exists():
                text = CONFIG_PATH.read_text()
                import re
                # Replace the gain line
                new_text = re.sub(
                    r'^(\s*gain:\s*)\S+',
                    f'\\g<1>{gain_idx}',
                    text,
                    count=1,
                    flags=re.MULTILINE,
                )
                if new_text != text:
                    CONFIG_PATH.write_text(new_text)
                    log.info(f"Saved gain index {gain_idx} to {CONFIG_PATH}")
        except Exception as e:
            log.warning(f"Could not save gain to config: {e}")

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
