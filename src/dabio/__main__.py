"""Entry point: python -m dabio"""
import sys
import uvicorn
from .config import AppConfig


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
        try:
            alt = input(f"Enter alternative port (or Ctrl+C to exit): ").strip()
            port = int(alt)
        except (KeyboardInterrupt, ValueError):
            print("\nExiting.")
            sys.exit(1)

    print(f"Starting Dabio on http://{host}:{port}")
    print(f"Mock mode: {config.mock_mode}")

    uvicorn.run(
        "dabio.app:app",
        host=host,
        port=port,
        log_level="warning",  # We use our own JSON logger
    )


if __name__ == "__main__":
    main()
