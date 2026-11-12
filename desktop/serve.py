"""Desktop tools ONLY (no shell / git / deploy tools), on Windows or Linux.

    python desktop/serve.py [--host 127.0.0.1] [--port 8011]

Use it where the agent must see and drive the screen but must not get a
shell (a shared desktop, an agent from another vendor...). For the full
server (ops tools + desktop) use server_new.py --desktop (Windows) or
server_linux.py --desktop (Linux).

Key: DESKTOP_MCP_API_KEY (environment). Without a key it only listens on a
loopback address. It must run inside the user's graphical session (DISPLAY
on Linux, the logged-on session on Windows): see install_desktop_linux.sh.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desktop import desktop_server as ds  # noqa: E402  (sets DPI awareness / opens X first)


def main() -> int:
    p = argparse.ArgumentParser(description="desktop-mcp (desktop tools only)")
    p.add_argument("--host", default=ds.Config.HOST)
    p.add_argument("--port", type=int, default=ds.Config.PORT)
    a = p.parse_args()
    if not ds._is_loopback(a.host) and not ds.Config.API_KEY:
        ds.logger.critical("Refusing to listen on %s without DESKTOP_MCP_API_KEY.", a.host)
        return 2
    if ds.Config.API_KEY and getattr(ds.mcp, "auth", None) is None:
        ds.logger.critical("DESKTOP_MCP_API_KEY is set but this fastmcp has no bearer provider: refusing to run open.")
        return 2
    if not ds.Config.API_KEY:
        ds.logger.warning("No DESKTOP_MCP_API_KEY: loopback only, no authentication")
    if ds.OVERLAY.error:
        ds.logger.error("overlay: %s", ds.OVERLAY.error)
    ds.start_message_button()
    ds.logger.info("desktop-mcp %s listening on http://%s:%s/mcp", ds.VERSION, a.host, a.port)
    ds.mcp.run(transport="streamable-http", host=a.host, port=a.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
