import asyncio
from collections.abc import AsyncGenerator

import httpx

from .logging_config import get_logger

log = get_logger("audio")

CHUNK_SIZE = 4096
MAX_QUEUE_SIZE = 64


class AudioBroadcaster:
    """Reads MP3 from welle-cli once, broadcasts to N browser clients.

    Single upstream reader prevents overwhelming welle-cli with duplicate
    decode+encode pipelines per client connection.
    """

    def __init__(self, welle_port: int, sid: str):
        self.welle_port = welle_port
        self.sid = sid
        self._subscribers: set[asyncio.Queue[bytes]] = set()
        self._feed_task: asyncio.Task | None = None
        self._running = False

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue[bytes]:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self._subscribers.add(q)
        log.info(f"Client subscribed to SID {self.sid} ({self.subscriber_count} total)")
        if not self._running:
            self._start_feed()
        return q

    def unsubscribe(self, q: asyncio.Queue[bytes]) -> None:
        self._subscribers.discard(q)
        log.info(f"Client unsubscribed from SID {self.sid} ({self.subscriber_count} remaining)")
        if self.subscriber_count == 0:
            self._stop_feed()

    def _start_feed(self) -> None:
        if self._feed_task and not self._feed_task.done():
            return
        self._running = True
        self._feed_task = asyncio.create_task(self._feed_loop())

    def _stop_feed(self) -> None:
        self._running = False
        if self._feed_task and not self._feed_task.done():
            self._feed_task.cancel()

    async def _feed_loop(self) -> None:
        """Read from welle-cli's /mp3/<SID> and push to all subscribers."""
        # welle-cli expects the 0x prefix in the URL path (e.g. /mp3/0x1309)
        sid_for_url = self.sid if self.sid.startswith("0x") else f"0x{self.sid}"
        url = f"http://127.0.0.1:{self.welle_port}/mp3/{sid_for_url}"
        retry_delay = 1
        MAX_RETRY_DELAY = 30

        while self._running and self.subscriber_count > 0:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", url) as resp:
                        if resp.status_code == 404:
                            log.info(
                                f"Station SID {self.sid} not yet available from "
                                f"welle-cli (404), retrying in {retry_delay}s"
                            )
                            await asyncio.sleep(retry_delay)
                            retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)
                            continue
                        if resp.status_code != 200:
                            log.warning(f"welle-cli /mp3/{self.sid} returned {resp.status_code}")
                            await asyncio.sleep(retry_delay)
                            retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)
                            continue

                        retry_delay = 1
                        log.info(f"Connected to welle-cli stream for SID {self.sid}")

                        async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                            if not self._running:
                                break
                            await self._publish(chunk)
            except httpx.HTTPError as e:
                log.warning(f"Stream error for SID {self.sid}: {e}")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Unexpected error in feed loop for SID {self.sid}: {e}")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)

        self._running = False
        log.info(f"Feed loop ended for SID {self.sid}")

    async def _publish(self, chunk: bytes) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                # Drop oldest chunk for slow consumers
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    async def stop(self) -> None:
        self._running = False
        self._subscribers.clear()
        if self._feed_task and not self._feed_task.done():
            self._feed_task.cancel()
            try:
                await self._feed_task
            except asyncio.CancelledError:
                pass


class BroadcasterPool:
    """Manages AudioBroadcaster instances per SID. One broadcaster per active station."""

    def __init__(self, welle_port: int):
        self.welle_port = welle_port
        self._broadcasters: dict[str, AudioBroadcaster] = {}

    def get_or_create(self, sid: str) -> AudioBroadcaster:
        if sid not in self._broadcasters:
            self._broadcasters[sid] = AudioBroadcaster(self.welle_port, sid)
        return self._broadcasters[sid]

    async def stop_all(self) -> None:
        for b in self._broadcasters.values():
            await b.stop()
        self._broadcasters.clear()

    def remove_if_empty(self, sid: str) -> None:
        b = self._broadcasters.get(sid)
        if b and b.subscriber_count == 0:
            del self._broadcasters[sid]


async def stream_audio(broadcaster: AudioBroadcaster) -> AsyncGenerator[bytes, None]:
    """Async generator for streaming audio to a single client."""
    q = broadcaster.subscribe()
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=10.0)
                yield chunk
            except asyncio.TimeoutError:
                # Keep-alive: yield empty to check if client disconnected
                yield b""
    except (asyncio.CancelledError, GeneratorExit):
        pass
    finally:
        broadcaster.unsubscribe(q)
