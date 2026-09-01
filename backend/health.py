"""Service probes for the config page's availability dots.

The config page must not let someone enable a defence layer whose service is down —
that produces a run that looks live but silently degrades. This mirrors the checks
`run_sweep.sh` already performs in its full-mode preflight, so the UI and the sweep script
agree on what "up" means.

Probes are cheap and non-destructive: a TCP connect, plus one line-JSON ping where the
sidecar protocol supports it.
"""

import json
import os
import shutil
import socket
import subprocess

import config

TIMEOUT = 1.5


def _port_open(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return True
    except OSError:
        return False


def _line_json(port, request, host="127.0.0.1"):
    """The zkp/gnn/llm sidecars all speak newline-delimited JSON."""
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT) as s:
            s.sendall((json.dumps(request) + "\n").encode())
            s.settimeout(TIMEOUT)
            buf = b""
            while not buf.endswith(b"\n") and len(buf) < 65536:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.decode(errors="replace").strip() or "{}")
    except (OSError, ValueError):
        return None


def probe():
    out = {}

    for name, port in config.PORTS.items():
        out[name] = {"port": port, "up": _port_open(port)}

    # The GNN must be serving the TRAINED model, not the scaffold — a scaffold answers
    # ping happily and then scores nothing. run_sweep.sh checks this too.
    if out["gnn"]["up"]:
        d = _line_json(config.PORTS["gnn"], {"op": "digest"})
        text = json.dumps(d) if d else ""
        out["gnn"]["trained"] = "trained" in text
        out["gnn"]["digest"] = d

    if out["llm"]["up"]:
        out["llm"]["ping"] = _line_json(config.PORTS["llm"], {"op": "ping"}) is not None

    docker = shutil.which("docker")
    if docker:
        rc = subprocess.run([docker, "info"], capture_output=True, timeout=10).returncode
        out["docker"] = {"present": True, "up": rc == 0}
    else:
        out["docker"] = {"present": False, "up": False}

    out["binary"] = {"path": config.BINARY, "up": os.path.exists(config.BINARY)}
    out["trace"] = {"path": config.TRACES["manhattan"],
                    "up": os.path.exists(config.TRACES["manhattan"])}

    # What the UI actually needs to know: which toggles are safe to offer.
    out["can"] = {
        "run": out["binary"]["up"],
        # --blockchain=1 needs the bridge, and the bridge refuses to start without the
        # mandatory zk-STARK sidecar
        "blockchain": out["bridge"]["up"] and out["zkp"]["up"],
        "gnn": out["gnn"]["up"] and out["gnn"].get("trained", False),
        "llm": out["llm"]["up"],
    }
    return out
