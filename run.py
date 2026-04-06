#!/usr/bin/env python3
"""
Dev runner — finds a free port and starts uvicorn with auto-reload.

Usage:
    python run.py            # picks any free port
    python run.py 8080       # use a specific port

Auto-reloads on:
  - Any .py file change
  - .env file change (config values update without restart)
"""
import socket
import subprocess
import sys


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080

    print(f"\n  Civic API  →  http://0.0.0.0:{port}")
    print(f"  Docs       →  http://localhost:{port}/docs")
    print(f"  Redoc      →  http://localhost:{port}/redoc")
    print(f"  Auto-reload: .py + .env\n")

    try:
        subprocess.run([
            sys.executable, "-m", "uvicorn",
            "app.main:app",
            "--host", "0.0.0.0",
            "--port", str(port),
            "--reload",
            "--reload-include", "*.env",
            "--reload-include", ".env",
        ])
    except KeyboardInterrupt:
        print("\n✓ API stopped cleanly")
        sys.exit(0)