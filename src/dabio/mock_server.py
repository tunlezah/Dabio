"""Mock welle-cli HTTP server that mimics the real web server endpoints.
Runs on the same internal port as welle-cli would, so the rest of the
application doesn't need code changes for mock mode.
"""
import asyncio
import json

from .logging_config import get_logger
from .mock import get_mock_mux_json, mock_mp3_stream

log = get_logger("mock-server")


class MockWelleServer:
    """Lightweight HTTP server mimicking welle-cli's web server endpoints."""

    def __init__(self, port: int):
        self.port = port
        self.channel = "9C"
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection, "127.0.0.1", self.port
        )
        log.info(f"Mock welle-cli server started on port {self.port}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            log.info("Mock welle-cli server stopped")

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request_line:
                writer.close()
                return

            request = request_line.decode("utf-8", errors="replace").strip()
            parts = request.split()
            if len(parts) < 2:
                writer.close()
                return

            method, path = parts[0], parts[1]

            # Read headers (discard)
            content_length = 0
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                header = line.decode("utf-8", errors="replace").strip()
                if not header:
                    break
                if header.lower().startswith("content-length:"):
                    content_length = int(header.split(":")[1].strip())

            # Read body if present
            body = b""
            if content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=5.0)

            # Route
            if path == "/mux.json" and method == "GET":
                await self._serve_mux_json(writer)
            elif path.startswith("/mp3/") and method == "GET":
                sid = path[5:]  # strip "/mp3/"
                await self._serve_mp3(writer, sid)
            elif path == "/channel" and method == "GET":
                await self._serve_channel_get(writer)
            elif path == "/channel" and method == "POST":
                self.channel = body.decode("utf-8", errors="replace").strip()
                await self._serve_text(writer, f"Retuning to {self.channel}...")
                log.info(f"Mock: channel switched to {self.channel}")
            else:
                await self._serve_404(writer)

        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            log.debug(f"Mock server connection error: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _serve_mux_json(self, writer: asyncio.StreamWriter) -> None:
        data = json.dumps(get_mock_mux_json(self.channel))
        header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(data)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        writer.write(header.encode() + data.encode())
        await writer.drain()

    async def _serve_mp3(self, writer: asyncio.StreamWriter, sid: str) -> None:
        header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: audio/mpeg\r\n"
            "Transfer-Encoding: chunked\r\n"
            "Cache-Control: no-cache, no-store\r\n"
            "Connection: keep-alive\r\n"
            "\r\n"
        )
        writer.write(header.encode())
        await writer.drain()

        try:
            async for chunk in mock_mp3_stream(sid):
                # HTTP chunked encoding
                chunk_header = f"{len(chunk):X}\r\n".encode()
                writer.write(chunk_header + chunk + b"\r\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass

    async def _serve_channel_get(self, writer: asyncio.StreamWriter) -> None:
        await self._serve_text(writer, self.channel)

    async def _serve_text(self, writer: asyncio.StreamWriter, text: str) -> None:
        data = text.encode()
        header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/plain\r\n"
            f"Content-Length: {len(data)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        writer.write(header.encode() + data)
        await writer.drain()

    async def _serve_404(self, writer: asyncio.StreamWriter) -> None:
        body = b"Not Found"
        header = (
            "HTTP/1.1 404 Not Found\r\n"
            "Content-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        writer.write(header.encode() + body)
        await writer.drain()
