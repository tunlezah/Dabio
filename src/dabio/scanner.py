import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import AppConfig, BAND_III_BLOCKS, STATIONS_CACHE, DATA_DIR, CONFIG_PATH
from .logging_config import get_logger
from .models import ScanResult, Station
from .welle_manager import WelleManager, WelleState, WelleVersion

log = get_logger("scanner")

# R820T gain index → dB mapping
GAIN_TABLE = {
    0: 0.0, 1: 0.9, 2: 1.4, 3: 2.7, 4: 3.7, 5: 7.7, 6: 8.7, 7: 12.5,
    8: 14.4, 9: 15.7, 10: 16.6, 11: 19.7, 12: 20.7, 13: 22.9, 14: 25.4,
    15: 28.0, 16: 29.7, 17: 32.8, 18: 33.8, 19: 36.4, 20: 37.2, 21: 38.6,
    22: 40.2, 23: 42.1, 24: 43.4, 25: 43.9, 26: 44.5, 27: 48.0, 28: 49.6,
}


def _extract_label(value) -> str:
    if isinstance(value, dict):
        return value.get("label", "").strip()
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


@dataclass
class ScanProgress:
    scanning: bool = False
    phase: str = ""
    current_block: str = ""
    current_block_index: int = 0
    total_blocks: int = 0
    stations_found: int = 0
    blocks_scanned: int = 0
    dwell_elapsed: float = 0
    dwell_total: float = 0
    started_at: float = 0
    error: str = ""
    gain_index: int = 0
    gain_db: float = 0.0
    snr: float = 0.0

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
            return self.total_blocks * 6
        rate = self.elapsed_seconds / self.blocks_scanned
        remaining = self.total_blocks - self.blocks_scanned
        return remaining * rate

    def to_dict(self) -> dict:
        return {
            "scanning": self.scanning, "phase": self.phase,
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
            "gain_index": self.gain_index,
            "gain_db": self.gain_db,
            "snr": round(self.snr, 1),
        }


class Scanner:
    def __init__(self, config: AppConfig, welle: WelleManager):
        self.config = config
        self.welle = welle
        self._stations: dict[str, Station] = {}
        self._scanning = False
        self._stop_requested = False
        self.progress = ScanProgress()
        self._signal_info: dict[str, dict] = {}  # station_id → {snr, block}
        self._load_cache()

    @property
    def stations(self) -> dict[str, Station]:
        return dict(self._stations)

    @property
    def is_scanning(self) -> bool:
        return self._scanning

    def get_station(self, station_id: str) -> Station | None:
        return self._stations.get(station_id)

    def get_signal_info(self) -> dict[str, dict]:
        return dict(self._signal_info)

    def request_stop(self) -> None:
        if self._scanning:
            self._stop_requested = True
            log.info("Scan stop requested — will finish current block")

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
                    "service_id": s.service_id, "ensemble_id": s.ensemble_id,
                    "name": s.name, "ensemble_name": s.ensemble_name,
                    "block": s.block, "station_id": s.station_id,
                }
                for s in self._stations.values()
            ]
            with open(STATIONS_CACHE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.warning(f"Failed to save station cache: {e}")

    async def scan(self, full: bool = False) -> dict[str, Station]:
        if self._scanning:
            log.warning("Scan already in progress")
            return self._stations

        if self.welle.state == WelleState.TUNED:
            log.warning("Cannot scan while audio is playing")
            return self._stations

        self._scanning = True
        self._stop_requested = False
        previous_channel = self.welle.status.channel

        try:
            blocks = list(self.config.scanning.priority_blocks)
            if full:
                for block in BAND_III_BLOCKS:
                    if block not in blocks:
                        blocks.append(block)

            gain = self.config.sdr.gain
            self.progress = ScanProgress(
                scanning=True,
                phase="priority" if not full else "full",
                total_blocks=len(blocks),
                started_at=time.time(),
                gain_index=gain,
                gain_db=GAIN_TABLE.get(gain, 0),
            )

            log.info(f"Scan start: {len(blocks)} blocks, gain={gain} ({GAIN_TABLE.get(gain, '?')} dB)")

            await self._scan_blocks(blocks)

            # Auto-gain if nothing found
            if len(self._stations) == 0 and not self._stop_requested:
                log.warning("No stations found — starting auto-gain detection")
                best_gain = await self._auto_detect_gain()
                if best_gain is not None and best_gain != self.config.sdr.gain:
                    self.config.sdr.gain = best_gain
                    _save_gain_to_config(best_gain)
                    self.progress.gain_index = best_gain
                    self.progress.gain_db = GAIN_TABLE.get(best_gain, 0)
                    self.progress.blocks_scanned = 0
                    self.progress.started_at = time.time()
                    self.progress.phase = "priority"
                    await self._scan_blocks(blocks)

            self._save_cache()
            self.progress.phase = "complete"
            self.progress.scanning = False
            self.progress.current_block = ""
            log.info(f"Scan complete: {len(self._stations)} stations")

        except Exception as e:
            log.error(f"Scan error: {e}", exc_info=True)
            self.progress.phase = "error"
            self.progress.error = str(e)
            self.progress.scanning = False
        finally:
            self._scanning = False
            self._stop_requested = False
            if previous_channel:
                await self._tune_for_scan(previous_channel, is_first=False)

        return self._stations

    async def _scan_blocks(self, blocks: list[str]) -> None:
        first = True
        empty_blocks = []
        for i, block in enumerate(blocks):
            if self._stop_requested:
                log.info("Scan stopped by user")
                break

            self.progress.current_block = block
            self.progress.current_block_index = i + 1
            self.progress.dwell_elapsed = 0

            await self._tune_for_scan(block, is_first=first)
            first = False

            result, snr = await self._dwell_and_poll(block)
            self.progress.blocks_scanned = i + 1
            self.progress.snr = snr

            if result and result.stations:
                for station in result.stations:
                    self._stations[station.station_id] = station
                    self._signal_info[station.station_id] = {"snr": snr, "block": block}
                self.progress.stations_found = len(self._stations)
                log.info(f"Block {block}: {len(result.stations)} stations, SNR={snr:.1f} dB")
            else:
                empty_blocks.append(block)

        # Retry empty priority blocks
        if self.config.scanning.retry_empty and empty_blocks and not self._stop_requested:
            retry = [b for b in empty_blocks if b in self.config.scanning.priority_blocks]
            if retry:
                self.progress.phase = "retry"
                self.progress.total_blocks += len(retry)
                log.info(f"Retrying {len(retry)} empty priority blocks")
                for i, block in enumerate(retry):
                    if self._stop_requested:
                        break
                    self.progress.current_block = block
                    self.progress.current_block_index = self.progress.blocks_scanned + i + 1
                    await self._tune_for_scan(block, is_first=False)
                    result, snr = await self._dwell_and_poll(block)
                    self.progress.blocks_scanned += 1
                    if result and result.stations:
                        for station in result.stations:
                            self._stations[station.station_id] = station
                            self._signal_info[station.station_id] = {"snr": snr, "block": block}
                        self.progress.stations_found = len(self._stations)
                        log.info(f"Block {block} (retry): {len(result.stations)} stations")

    async def _tune_for_scan(self, block: str, is_first: bool) -> None:
        if is_first or self.welle.version != WelleVersion.V2_7:
            await self.welle.start(block)
            await asyncio.sleep(3)
        else:
            success = await self.welle._post_channel(block)
            if success:
                self.welle._status.channel = block
                await asyncio.sleep(2)
            else:
                log.warning(f"POST /channel failed for {block}, restarting")
                await self.welle.start(block)
                await asyncio.sleep(3)

    async def _dwell_and_poll(self, block: str) -> tuple[ScanResult | None, float]:
        """Dwell on a block with fast-skip for empty frequencies.

        Returns (best_result, snr).
        Fast-skip: if SNR is 0 after 3 seconds, skip the block (no DAB signal).
        """
        port = self.config.welle_cli.internal_port
        dwell = self.config.scanning.dwell_time
        poll_interval = self.config.scanning.poll_interval
        self.progress.dwell_total = dwell

        best_result: ScanResult | None = None
        best_count = 0
        snr = 0.0
        elapsed = 0.0

        while elapsed < dwell:
            if self._stop_requested:
                break
            self.progress.dwell_elapsed = elapsed
            result, block_snr = await self._fetch_mux_with_snr(port, block)
            snr = max(snr, block_snr)

            if result and len(result.stations) > best_count:
                best_result = result
                best_count = len(result.stations)

            # Fast-skip: no signal after first poll = empty frequency, move on
            if elapsed >= 2 and snr == 0.0 and best_count == 0:
                log.debug(f"Block {block}: SNR=0 after {elapsed:.0f}s — fast-skipping")
                break

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        return best_result, snr

    async def _fetch_mux_with_snr(self, port: int, block: str) -> tuple[ScanResult | None, float]:
        """Fetch mux.json and return (result, snr)."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}/mux.json", timeout=5.0)
                if resp.status_code != 200:
                    return None, 0.0
                data = resp.json()
        except Exception as e:
            log.debug(f"mux.json fetch failed for {block}: {e}")
            return None, 0.0

        snr = data.get("demodulator", {}).get("snr", 0.0)
        ensemble_raw = data.get("ensemble", {})
        services_raw = data.get("services", [])

        log.debug(f"Block {block}: SNR={snr:.1f}, services={len(services_raw)}")

        ensemble_name = _extract_label(ensemble_raw.get("label", ""))
        ensemble_id = ensemble_raw.get("id", "")
        if isinstance(ensemble_id, int):
            ensemble_id = f"{ensemble_id:04X}"
        elif isinstance(ensemble_id, str):
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
                continue
            stations.append(Station(
                service_id=sid, ensemble_id=ensemble_id,
                name=name, ensemble_name=ensemble_name, block=block,
            ))

        if not ensemble_name and not stations:
            return None, snr

        return ScanResult(block=block, ensemble_name=ensemble_name,
                         ensemble_id=ensemble_id, stations=stations), snr

    async def _auto_detect_gain(self) -> int | None:
        self.progress.phase = "auto-gain"
        test_block = "9C"
        # Test from mid-range outward
        gain_candidates = [16, 18, 20, 14, 22, 12, 24, 10]
        best_gain = None
        best_count = 0

        log.info(f"Auto-gain: testing on {test_block}")

        for i, gain_idx in enumerate(gain_candidates):
            if self._stop_requested:
                break
            self.progress.current_block = f"Auto-gain {GAIN_TABLE.get(gain_idx, '?')} dB"
            self.progress.current_block_index = i + 1
            self.progress.total_blocks = len(gain_candidates)

            await self.welle.start(test_block, gain_override=gain_idx)
            await asyncio.sleep(4)

            port = self.config.welle_cli.internal_port
            count = 0
            for _ in range(3):
                result, _ = await self._fetch_mux_with_snr(port, test_block)
                if result:
                    count = max(count, len(result.stations))
                await asyncio.sleep(2)

            log.info(f"Auto-gain: index {gain_idx} ({GAIN_TABLE.get(gain_idx, '?')} dB) → {count} stations")
            if count > best_count:
                best_count = count
                best_gain = gain_idx
            if count >= 10:
                break

        if best_gain is not None and best_count > 0:
            log.info(f"Auto-gain: best = index {best_gain} ({GAIN_TABLE.get(best_gain, '?')} dB, {best_count} stations)")
        else:
            log.warning("Auto-gain: no stations at any gain. Check antenna.")
        return best_gain


def _save_gain_to_config(gain_idx: int) -> None:
    try:
        if CONFIG_PATH.exists():
            text = CONFIG_PATH.read_text()
            new_text = re.sub(r'^(\s*gain:\s*)\S+', f'\\g<1>{gain_idx}',
                              text, count=1, flags=re.MULTILINE)
            if new_text != text:
                CONFIG_PATH.write_text(new_text)
                log.info(f"Saved gain index {gain_idx} to {CONFIG_PATH}")
    except Exception as e:
        log.warning(f"Could not save gain to config: {e}")
