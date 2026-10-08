"""Alejandro remote bridge.

Pieces (see docs/ARCHITECTURE.md):

- ``protocol``          wire format shared by the gateway and the Windows connector
- ``registry``          device credentials (hashed tokens, revocation)
- ``policy``            tool policy levels + one-time approvals
- ``hub``               live device connections and in-flight tool calls
- ``alexa``             Alexa request verification and voice turn handling
- ``gateway``           FastAPI apps: public edge + loopback-only internal API
- ``server``            process entry point (runs both apps on one event loop)
- ``admin``             CLI to enrol / revoke devices
- ``windows_connector`` the agent that runs on the Windows PC
- ``desktop_client``    minimal client for the local desktop-mcp server
"""

__version__ = "0.2.0"
