"""Process entry point: both gateway apps on one event loop (they share the hub).

    python -m remote_bridge.server \
        --public-host 172.17.0.1 --public-port 8770 \
        --internal-port 8771

The internal app always binds 127.0.0.1. The public app should bind an address
only the reverse proxy can reach (behind a containerised Traefik: the Docker bridge gateway, typically
172.17.0.1).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

import uvicorn

from .gateway import Gateway, Settings


async def _serve(args) -> None:
    settings = Settings.from_env()
    settings.validate()
    gw = Gateway(settings)

    def server(app, host, port):
        cfg = uvicorn.Config(app, host=host, port=port, log_level=args.log_level.lower(),
                             proxy_headers=False, server_header=False, ws_max_size=8 * 1024 * 1024,
                             ws_ping_interval=20, ws_ping_timeout=20)
        s = uvicorn.Server(cfg)
        s.install_signal_handlers = lambda: None   # handled once below
        return s

    public = server(gw.public, args.public_host, args.public_port)
    internal = server(gw.internal, "127.0.0.1", args.internal_port)
    watchdog = asyncio.create_task(gw.hub.watchdog())

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    try:
        import signal
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
    except (NotImplementedError, AttributeError):  # Windows dev runs
        pass

    tasks = [asyncio.create_task(public.serve()), asyncio.create_task(internal.serve())]
    logging.getLogger("remote_bridge").info(
        "gateway up: public %s:%d, internal 127.0.0.1:%d, alexa=%s",
        args.public_host, args.public_port, args.internal_port, gw.alexa is not None)
    done, _ = await asyncio.wait([*tasks, asyncio.create_task(stop.wait())],
                                 return_when=asyncio.FIRST_COMPLETED)
    public.should_exit = internal.should_exit = True
    watchdog.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for t in done:
        if t in tasks and t.exception():
            raise t.exception()


def main() -> None:
    ap = argparse.ArgumentParser(description="Alejandro bridge gateway")
    ap.add_argument("--public-host", default=os.environ.get("BRIDGE_PUBLIC_HOST", "127.0.0.1"))
    ap.add_argument("--public-port", type=int, default=int(os.environ.get("BRIDGE_PUBLIC_PORT", "8770")))
    ap.add_argument("--internal-port", type=int, default=int(os.environ.get("BRIDGE_INTERNAL_PORT", "8771")))
    ap.add_argument("--log-level", default=os.environ.get("BRIDGE_LOG_LEVEL", "INFO"))
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(_serve(args))


if __name__ == "__main__":
    main()
