"""Real loopback sidecar fixture for Node contract tests; no model/provider calls."""
from pathlib import Path
import json
import sys
import threading
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "dpswarm-plugin"))
from dpswarm.server import Handler, PanelState

workspace = Path(sys.argv[1]) / ".dpswarm-panel"
Handler.state = PanelState(workspace)
server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
print(json.dumps({"port": server.server_address[1]}), flush=True)
try:
    for line in sys.stdin:
        request = json.loads(line)
        if request["command"] == "restart":
            Handler.state.cp.close()
            Handler.state = PanelState(workspace)
            print(json.dumps({"restarted": True}), flush=True)
        elif request["command"] == "stop":
            break
finally:
    server.shutdown()
    server.server_close()
    Handler.state.cp.close()
