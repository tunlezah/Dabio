import asyncio
import enum
import re
import subprocess
from dataclasses import dataclass

from .config import AppConfig
from .logging_config import get_logger

log = get_logger("welle-cli")


class WelleVersion(enum.Enum):
    V2_4 = "2.4"
    V2_7 = "2.7"
    UNKNOWN = "unknown"


class WelleState(enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    IDLE = "idle"  # running but not tuned
    TUNED = "tuned"  # tuned to a channel, serving audio
    SCANNING = "scanning"
    ERROR = "error"


@dataclass
class WelleStatus:
    state: WelleState = WelleState.STOPPED
    channel: str | None = None
    version: WelleVersion = WelleVersion.UNKNOWN
    binary_path: str | None = None
    pid: int | None = None
    error: str | None = None


class WelleManager:
    """Manages a single welle-cli process with version-aware behaviour."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._status = WelleStatus()
        self._lock = asyncio.Lock()
        self._restart_count = 0
        self._max_backoff = 30

    @property
    def status(self) -> WelleStatus:
        return self._status

    @property
    def state(self) -> WelleState:
        return self._status.state

    @property
    def version(self) -> WelleVersion:
        return self._status.version

    def detect_version(self) -> tuple[str | None, WelleVersion]:
        binary = self.config.resolve_welle_cli_binary()
        if not binary:
            return None, WelleVersion.UNKNOWN
        try:
            result = subprocess.run(
                [binary, "-v"], capture_output=True, text=True, timeout=5
            )
            output = result.stdout + result.stderr
            # welle-cli -v outputs version string like "welle-cli version 2.7" or similar
            match = re.search(r"(\d+\.\d+)", output)
            if match:
                ver_str = match.group(1)
                major_minor = float(ver_str)
                if major_minor >= 2.7:
                    return binary, WelleVersion.V2_7
                elif major_minor >= 2.4:
                    return binary, WelleVersion.V2_4
            # If we can't parse but the binary exists and runs, assume v2.4 (apt)
            if binary == "/usr/local/bin/welle-cli":
                return binary, WelleVersion.V2_7
            return binary, WelleVersion.V2_4
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            log.warning(f"Failed to detect welle-cli version: {e}")
            return binary, WelleVersion.UNKNOWN

    async def start(self, channel: str, gain_override: int | None = None) -> None:
        async with self._lock:
            await self._start_locked(channel, gain_override)

    async def _start_locked(self, channel: str, gain_override: int | None = None) -> None:
        await self._stop_locked()

        binary, version = self.detect_version()
        if not binary:
            self._status = WelleStatus(
                state=WelleState.ERROR, error="welle-cli binary not found"
            )
            log.error("welle-cli binary not found. Run the installer.")
            return

        self._status.binary_path = binary
        self._status.version = version
        self._status.state = WelleState.STARTING

        port = self.config.welle_cli.internal_port
        cmd = [binary, "-c", channel, "-w", str(port)]

        # Add gain setting (override takes precedence for auto-gain probing)
        gain = gain_override if gain_override is not None else self.config.sdr.gain
        cmd.extend(["-g", str(gain)])

        # Add driver if specified
        if self.config.sdr.driver != "auto":
            cmd.extend(["-F", self.config.sdr.driver])

        # Disable TII to reduce CPU
        cmd.append("-T")

        log.info(f"Starting welle-cli: {' '.join(cmd)} (version {version.value})")

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._status.state = WelleState.TUNED
            self._status.channel = channel
            self._status.pid = self._process.pid
            self._status.error = None
            self._restart_count = 0

            # Start background log reader
            asyncio.create_task(self._read_output())

            log.info(f"welle-cli started on channel {channel}, PID {self._process.pid}")
        except OSError as e:
            self._status = WelleStatus(
                state=WelleState.ERROR, error=str(e), version=version, binary_path=binary
            )
            log.error(f"Failed to start welle-cli: {e}")

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        if self._process and self._process.returncode is None:
            log.info(f"Stopping welle-cli PID {self._process.pid}")
            try:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    log.warning("welle-cli did not terminate, killing")
                    self._process.kill()
                    await self._process.wait()
            except ProcessLookupError:
                pass
        self._process = None
        self._status.state = WelleState.STOPPED
        self._status.channel = None
        self._status.pid = None

    async def tune(self, channel: str) -> None:
        """Switch to a different channel. Version-aware: v2.7 uses POST /channel,
        v2.4 restarts the process (POST /channel may hang on Debian-patched builds)."""
        async with self._lock:
            if self._status.state == WelleState.SCANNING:
                log.warning("Cannot tune while scanning")
                return

            if self._status.channel == channel and self._status.state == WelleState.TUNED:
                return

            if self._status.version == WelleVersion.V2_7 and self._process:
                # v2.7: use POST /channel (reliable on source-built)
                success = await self._post_channel(channel)
                if success:
                    self._status.channel = channel
                    log.info(f"Retuned to {channel} via POST /channel")
                    return
                log.warning("POST /channel failed, falling back to restart")

            # v2.4 or POST failed: restart process
            await self._start_locked(channel)

    async def _post_channel(self, channel: str) -> bool:
        """Send POST /channel to welle-cli's web server."""
        import httpx
        port = self.config.welle_cli.internal_port
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"http://127.0.0.1:{port}/channel",
                    content=channel,
                    timeout=5.0,
                )
                return resp.status_code == 200
        except Exception as e:
            log.warning(f"POST /channel failed: {e}")
            return False

    async def restart_with_backoff(self, channel: str | None = None) -> None:
        """Restart welle-cli with exponential backoff."""
        channel = channel or self._status.channel
        if not channel:
            log.error("Cannot restart: no channel specified")
            return

        self._restart_count += 1
        delay = min(2 ** self._restart_count, self._max_backoff)
        log.info(f"Restarting welle-cli in {delay}s (attempt {self._restart_count})")
        await asyncio.sleep(delay)
        await self.start(channel)

    async def health_check(self) -> bool:
        """Check if welle-cli is alive and responding."""
        if not self._process or self._process.returncode is not None:
            return False
        import httpx
        port = self.config.welle_cli.internal_port
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"http://127.0.0.1:{port}/mux.json", timeout=3.0
                )
                return resp.status_code == 200
        except Exception:
            return False

    async def _read_output(self) -> None:
        """Read and log welle-cli's stderr output.
        Filters repetitive noise like SyncOnPhase to avoid log flooding."""
        if not self._process or not self._process.stderr:
            return
        # Messages that repeat rapidly and aren't useful at DEBUG level
        NOISE_PATTERNS = (
            "SyncOnPhase",
            "SyncOnEndNull",
            "coarse_corrector",
        )
        noise_count = 0
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                # Suppress known noisy patterns — log a summary every 50 occurrences
                if any(p in text for p in NOISE_PATTERNS):
                    noise_count += 1
                    if noise_count % 50 == 1:
                        log.debug(f"[welle-cli] {text} (repeated, showing every 50th)")
                    continue
                # Log everything else
                if "error" in text.lower() or "fail" in text.lower():
                    log.warning(f"[welle-cli] {text}")
                else:
                    log.debug(f"[welle-cli] {text}")
        except Exception:
            pass

        # Process exited
        if self._process and self._process.returncode is not None:
            code = self._process.returncode
            if code != 0 and self._status.state != WelleState.STOPPED:
                log.error(f"welle-cli exited with code {code}")
                self._status.state = WelleState.ERROR
                self._status.error = f"Process exited with code {code}"

    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None
