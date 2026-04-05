"""Chromecast integration via PyChromecast. LAN-only, no TLS required."""
import asyncio
import socket
from dataclasses import dataclass

from .logging_config import get_logger

log = get_logger("chromecast")


@dataclass
class ChromecastDevice:
    name: str
    uuid: str
    model: str
    host: str
    port: int


def get_local_ip() -> str:
    """Get this machine's LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class ChromecastManager:
    def __init__(self, server_port: int):
        self.server_port = server_port
        self._devices: dict[str, ChromecastDevice] = {}
        self._active_cast = None
        self._browser = None

    async def discover(self, timeout: float = 5.0) -> list[ChromecastDevice]:
        """Discover Chromecast devices on the local network."""
        try:
            import pychromecast
        except ImportError:
            log.warning("pychromecast not installed, Chromecast disabled")
            return []

        def _discover():
            chromecasts, browser = pychromecast.get_chromecasts(timeout=timeout)
            self._browser = browser
            devices = []
            for cc in chromecasts:
                dev = ChromecastDevice(
                    name=cc.cast_info.friendly_name,
                    uuid=str(cc.cast_info.uuid),
                    model=cc.cast_info.model_name,
                    host=str(cc.cast_info.host),
                    port=cc.cast_info.port,
                )
                devices.append(dev)
                self._devices[dev.uuid] = dev
            return devices

        loop = asyncio.get_event_loop()
        devices = await loop.run_in_executor(None, _discover)
        log.info(f"Discovered {len(devices)} Chromecast devices")
        return devices

    async def cast(self, device_uuid: str, station_id: str) -> bool:
        """Cast a station's audio stream to a Chromecast device."""
        try:
            import pychromecast
        except ImportError:
            log.error("pychromecast not installed")
            return False

        device = self._devices.get(device_uuid)
        if not device:
            log.error(f"Chromecast device {device_uuid} not found")
            return False

        local_ip = get_local_ip()
        stream_url = f"http://{local_ip}:{self.server_port}/api/station/{station_id}/stream"

        def _cast():
            chromecasts, browser = pychromecast.get_chromecasts(timeout=5)
            for cc in chromecasts:
                if str(cc.cast_info.uuid) == device_uuid:
                    cc.wait(timeout=10)
                    mc = cc.media_controller
                    mc.play_media(stream_url, "audio/mpeg")
                    mc.block_until_active(timeout=10)
                    self._active_cast = cc
                    return True
            return False

        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(None, _cast)
        if success:
            log.info(f"Casting station {station_id} to {device.name}")
        else:
            log.error(f"Failed to cast to {device.name}")
        return success

    async def stop_cast(self) -> bool:
        """Stop the active Chromecast playback."""
        if not self._active_cast:
            return False

        def _stop():
            try:
                self._active_cast.media_controller.stop()
                self._active_cast.quit_app()
                return True
            except Exception as e:
                log.error(f"Failed to stop cast: {e}")
                return False

        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(None, _stop)
        if success:
            self._active_cast = None
            log.info("Chromecast playback stopped")
        return success

    def shutdown(self) -> None:
        if self._browser:
            try:
                self._browser.stop_discovery()
            except Exception:
                pass
