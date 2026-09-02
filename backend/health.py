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


# Remembered for the life of the server process. See _gnn_identity for why.
_GNN_CACHE = {}
_PORT_CACHE = {}


def ledger():
    """Has a defended run already consumed this ledger?

    ONE DEFENDED RUN PER LEDGER. A second run re-registers under SC1 without complaint and
    is then denied by the zk-STARK membership gate — every vehicle, silently, with exit 0.

    Three states, deliberately: `fresh` (demo_stack.sh reset it and nothing has used it),
    `used` (a defended run consumed it), and `unknown` (no marker at all — run_sweep.sh
    resets the ledger too and writes nothing). Only `used` blocks a run; `unknown` is a
    warning, because refusing on missing evidence would block a legitimate run.
    """
    try:
        with open(config.LEDGER_FRESH) as fh:
            return {"state": "fresh", "reset_utc": fh.read().strip()}
    except OSError:
        pass
    try:
        with open(config.LEDGER_USED) as fh:
            return {"state": "used", "used_by": fh.read().strip(),
                    "note": ("this ledger has already served a defended run. Run "
                             "`tools/demo_stack.sh reset` — otherwise the membership gate "
                             "denies every vehicle and the run files no accusations.")}
    except OSError:
        pass
    return {"state": "unknown",
            "note": ("no reset marker. If the ledger has not been reset since the last "
                     "defended run, this run will file no accusations — "
                     "`tools/demo_stack.sh reset` makes it certain.")}


def consume_ledger(run_id):
    """Called when a defended run starts: the marker moves fresh -> used."""
    try:
        os.replace(config.LEDGER_FRESH, config.LEDGER_USED)
    except OSError:
        os.makedirs(config.STACK_LOGS, exist_ok=True)
    try:
        with open(config.LEDGER_USED, "w") as fh:
            fh.write(run_id)
    except OSError:
        pass


def _gnn_identity(bridge_up):
    """Which GNN is being served — read once, then remembered.

    THE SIDECAR SERVES ONE CONNECTION AT A TIME. Once the bridge is attached — which is
    exactly when a defended run becomes possible — a digest probe is accepted and never
    answered (measured: 15 s of silence, and a growing pile of CLOSE-WAIT sockets on the
    sidecar from probes it never read). Probing regardless would do two bad things: report
    `can.gnn=false` for a completely healthy stack and so refuse the defended run, and
    leave half-closed sockets on the one service the run depends on.

    So: probe when nothing else holds it, cache the answer, and treat "could not ask" as
    unknown rather than as failure. The scaffold check still works, because the sidecars
    come up before the bridge does (tools/demo_stack.sh) and that is the window this runs in.
    """
    if _GNN_CACHE.get("result"):
        return dict(_GNN_CACHE["result"], probe="cached")
    if bridge_up:
        return {"expected_model": config.GNN_MODEL, "probe": "skipped_busy",
                "model_note": ("the sidecar is serving the bridge and answers one "
                               "connection at a time, so its model could not be read. "
                               "It is checked when the stack starts, before the bridge.")}
    d = _line_json(config.PORTS["gnn"], {"op": "digest"})
    if d is None:
        return {"expected_model": config.GNN_MODEL, "probe": "no_answer"}
    res = {"digest": d, "trained": "trained" in json.dumps(d), "probe": "live"}
    res.update(_gnn_model_check(d))
    _GNN_CACHE["result"] = res
    return res


def _gnn_model_check(digest):
    """Is the sidecar serving the model the results were produced with?

    Returns {} when it cannot be decided — an absent manifest or a sidecar too old to
    report `model_hash` is unknown, not a failure.
    """
    served = (digest or {}).get("model_hash")
    out = {"expected_model": config.GNN_MODEL, "model_hash": served}
    try:
        with open(config.GNN_MANIFEST) as fh:
            man = json.load(fh)
    except (OSError, ValueError):
        out["model_note"] = f"no manifest at {config.GNN_MANIFEST}"
        return out
    want = man.get("model_hash")
    out["tau_gnn"] = man.get("tau_gnn")
    if not (want and served):
        out["model_note"] = "sidecar reported no model_hash"
        return out
    out["model_ok"] = (served == want)
    if not out["model_ok"]:
        out["model_note"] = (
            f"the sidecar is NOT serving {config.GNN_MODEL}. Restart it with "
            f"GNN_ARTIFACTS=artifacts/{config.GNN_MODEL}")
    return out


def _docker_check():
    """`docker info` failing does not mean Docker is down.

    The common case on this machine is a SESSION that predates the docker group being
    granted: the daemon is fine, our credentials are stale, and the fix is to restart this
    server from a fresh shell rather than to touch Docker at all. Reported separately
    because a red dot with no cause sends people to debug the wrong thing.
    """
    docker = shutil.which("docker")
    if not docker:
        return {"present": False, "up": False}
    try:
        p = subprocess.run([docker, "info"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"present": True, "up": False, "note": repr(exc)}
    if p.returncode == 0:
        return {"present": True, "up": True}
    err = (p.stderr or b"").decode(errors="replace").strip()
    out = {"present": True, "up": False, "error": err[:400]}
    if "permission denied" in err.lower():
        out["reason"] = "no_permission"
        out["note"] = ("the daemon is reachable but this process cannot use the socket. "
                       "If `docker info` works in a terminal, this server was started "
                       "from a session that predates the docker group — restart it from a "
                       "fresh shell.")
    else:
        out["reason"] = "down"
    return out


def probe():
    out = {}

    # Probe the bridge first: whether it is attached decides how the sidecars may be probed.
    out["bridge"] = {"port": config.PORTS["bridge"],
                     "up": _port_open(config.PORTS["bridge"])}

    # A BARE TCP CONNECT TO THESE SIDECARS IS NOT FREE. They serve one connection at a time,
    # and _port_open opens a connection, sends nothing, and closes. Each one the sidecar
    # accepts but never drains becomes a CLOSE-WAIT socket. Measured: after ~24 h of this
    # page polling, the zk-STARK sidecar held 82 CLOSE-WAIT sockets and was permanently
    # wedged -- still accepting, so every health check reported it green, while the bridge
    # logged "zkp-stark sidecar unreachable: i/o timeout" and every defended run died with
    # "blockchain InitConfig failed". The monitoring caused the outage it was watching for.
    #
    # So: probe a sidecar only when the bridge is NOT holding it, and remember the answer.
    for name in ("zkp", "gnn", "llm"):
        port = config.PORTS[name]
        if out["bridge"]["up"] and name in _PORT_CACHE:
            out[name] = {"port": port, "up": _PORT_CACHE[name], "probe": "cached",
                         "note": "not probed: the bridge holds this sidecar, and an idle "
                                 "connect would be left half-open"}
        else:
            up = _port_open(port)
            _PORT_CACHE[name] = up
            out[name] = {"port": port, "up": up}

    # The GNN must be serving the TRAINED model, not the scaffold — a scaffold answers
    # ping happily and then scores nothing. run_sweep.sh checks this too.
    #
    # "Trained" is NOT enough on its own. The sidecar serves whatever GNN_ARTIFACTS points
    # at and defaults to a different model at a different threshold; every one of them
    # answers "gnn-v1 (trained)". So the descriptor is checked for the scaffold case AND
    # the served model_hash is compared against the manifest of the model the banked
    # results were produced with. A mismatch is a wrong-evidence bug, not a warning.
    if out["gnn"]["up"]:
        out["gnn"].update(_gnn_identity(out["bridge"]["up"]))

    # Same one-connection-at-a-time constraint as the GNN: while the bridge holds the
    # sidecar, a ping is accepted and never answered, and reporting that as ping=false reads
    # like a dead service. `up` (the port) is the honest signal in that state.
    if out["llm"]["up"] and not out["bridge"]["up"]:
        out["llm"]["ping"] = _line_json(config.PORTS["llm"], {"op": "ping"}) is not None

    out["docker"] = _docker_check()

    out["ledger"] = ledger()
    out["binary"] = {"path": config.BINARY, "up": os.path.exists(config.BINARY)}
    out["trace"] = {"path": config.TRACES["manhattan"],
                    "up": os.path.exists(config.TRACES["manhattan"])}

    # What the UI actually needs to know: which toggles are safe to offer.
    out["can"] = {
        "run": out["binary"]["up"],
        # --blockchain=1 needs the bridge, and the bridge refuses to start without the
        # mandatory zk-STARK sidecar
        "blockchain": out["bridge"]["up"] and out["zkp"]["up"],
        # ...and a ledger that has not already been consumed. "unknown" is allowed through:
        # the ledger may well have been reset by run_sweep.sh, which writes no marker, and
        # refusing on an absence of evidence would block a perfectly good run.
        "defended_run": (out["bridge"]["up"] and out["zkp"]["up"]
                         and out["ledger"]["state"] != "used"),
        # False on a model mismatch or a scaffold, because serving the wrong GNN silently
        # produces a run whose numbers cannot be compared with anything already reported.
        # NOT false merely because the model could not be read: an unanswerable probe means
        # the sidecar is busy with the bridge, which is the healthy case, not the broken one.
        "gnn": (out["gnn"]["up"] and out["gnn"].get("trained") is not False
                and out["gnn"].get("model_ok") is not False),
        "llm": out["llm"]["up"],
    }
    return out
