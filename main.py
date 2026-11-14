import uvicorn

from arya.server import security
from arya.server.websocket import app


if __name__ == "__main__":
    # Loopback only by default — nothing else on the network can reach ARYA.
    # Set ARYA_HOST (and ARYA_ALLOWED_HOSTS) only if you really want LAN access.
    from arya.desktop.system import acquire_single_instance

    if not acquire_single_instance():
        raise SystemExit("ARYA is already running (tray app or another console) — close it first.")
    if not security.is_loopback_bind():
        print(f"WARNING: ARYA is listening on {security.HOST} — reachable from your network (token still required).")
    uvicorn.run(app, host=security.HOST, port=security.PORT, server_header=False)
