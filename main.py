"""Entry point for Router Manager."""
from __future__ import annotations

import argparse
import atexit
import signal
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routes import create_router
from process_mgr import ProcessManager
from store import Store


def build_app(data_dir: str) -> tuple[FastAPI, Store, ProcessManager]:
    store = Store(str(Path(data_dir) / "data.json"))
    store.load()

    proc_mgr = ProcessManager()

    app = FastAPI(title="Router Manager", version="1.0.0")

    # CORS — allow all origins (LAN dashboard convenience)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API routes
    router = create_router(store, proc_mgr)
    app.include_router(router)

    # Static web UI — mount at root, after API routes
    web_dir = Path(__file__).parent / "web"
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")

    return app, store, proc_mgr


def main() -> None:
    parser = argparse.ArgumentParser(description="Router Manager daemon (Python)")
    parser.add_argument(
        "-d", "--data-dir",
        default="/etc/router_manager",
        help="Data directory for config/state (default: /etc/router_manager)",
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=8080,
        help="Management HTTP port (default: 8080)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--auto-start-mihomo",
        action="store_true",
        help="Start mihomo core when Router Manager starts",
    )
    parser.add_argument(
        "--auto-start-sing-box",
        action="store_true",
        help="Start sing-box core when Router Manager starts",
    )
    args = parser.parse_args()

    app, _store, _proc = build_app(args.data_dir)

    atexit.register(_proc.stop_sing_box)
    atexit.register(_proc.stop)

    def _shutdown(signum, frame):  # noqa: ANN001
        _proc.stop_sing_box()
        _proc.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGHUP, _shutdown)

    if args.auto_start_mihomo:
        if not _proc.start(_store.get_state()):
            print(f"Failed to auto-start mihomo: {_proc.last_error}", file=sys.stderr)
    if args.auto_start_sing_box:
        if not _proc.start_sing_box(_store.get_state()):
            print(f"Failed to auto-start sing-box: {_proc.sing_box_last_error}", file=sys.stderr)

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
