"""Real session-isolated sidecar; offline Node integration tests only."""
from pathlib import Path
import json
import sys
import threading
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "dpswarm-plugin"))
from dpswarm.session_server import create_server, SessionHub
workspace = Path(sys.argv[1])
server = create_server(workspace, 0)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
print(json.dumps({"port": server.server_port}), flush=True)
try:
    for line in sys.stdin:
        request = json.loads(line)
        if request["command"] == "restart":
            server.hub.close()
            server.hub = SessionHub(workspace)
            print(json.dumps({"restarted": True}), flush=True)
        elif request["command"] == "stop":
            break
finally:
    server.shutdown()
    server.server_close()
    server.hub.close()
