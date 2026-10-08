# remote_bridge — Alejandro

Extends a [MaxBot](https://github.com/twine003/maxbot) deployment with **voice (Alexa)** and
**remote Windows PCs** (through the `desktop/` MCP server of this repo), without exposing
desktop-mcp or opening inbound ports on the PC.

- `gateway` (server): public edge for Alexa + device WebSockets, loopback API for MaxBot.
- `windows_connector` (PC): outbound WSS, heartbeat, reconnection, local policy, audit.
- MaxBot side: plugin `remote_bridge` (branch `feature/remote-bridge-plugin` of maxbot).

Docs (Spanish): [ARCHITECTURE](docs/ARCHITECTURE.md) · [INSTALLATION](docs/INSTALLATION.md) ·
[CONFIGURATION](docs/CONFIGURATION.md) · [SECURITY](docs/SECURITY.md) ·
[OPERATIONS](docs/OPERATIONS.md) · [ROLLBACK](docs/ROLLBACK.md) · [TESTING](docs/TESTING.md) ·
[ALEXA](docs/ALEXA.md)
