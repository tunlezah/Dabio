"""Entry point: python -m dabio"""
import sys
from pathlib import Path

import uvicorn

from .config import AppConfig, DATA_DIR


def main():
    config = AppConfig.load()
    port = config.server.port
    host = config.server.host

    # Check if port is available
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        sock.close()
    except OSError:
        print(f"Port {port} is unavailable.")
        # When running under systemd, don't prompt — just fail
        if not sys.stdin.isatty():
            print(f"Cannot bind to port {port}. Check config.yaml or free the port.")
            sys.exit(1)
        try:
            alt = input("Enter alternative port (or Ctrl+C to exit): ").strip()
            port = int(alt)
        except (KeyboardInterrupt, ValueError):
            print("\nExiting.")
            sys.exit(1)

    # Write port file so the port is always discoverable
    DATA_DIR.mkdir(exist_ok=True)
    port_file = DATA_DIR / "dabio.port"
    port_file.write_text(f"{port}\n")

    # Determine the best URL to display
    local_ip = _get_local_ip()
    print(f"Starting Dabio on http://{host}:{port}")
    if host == "0.0.0.0" and local_ip != "127.0.0.1":
        print(f"  Local:   http://127.0.0.1:{port}")
        print(f"  Network: http://{local_ip}:{port}")
    else:
        print(f"  URL:     http://{host}:{port}")
    print(f"  Mock mode: {config.mock_mode}")

    uvicorn.run(
        "dabio.app:app",
        host=host,
        port=port,
        log_level="warning",  # We use our own JSON logger
    )


def _get_local_ip() -> str:
    """Get this machine's LAN IP address."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    main()
