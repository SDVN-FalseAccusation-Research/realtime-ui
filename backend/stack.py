"""Run `tools/demo_stack.sh reset` from the UI, safely.

WHY THIS IS NARROW ON PURPOSE
    The rest of this server starts a SIMULATOR. This starts and stops Fabric containers and
    the bridge, which is a bigger blast radius, so the surface is deliberately the smallest
    thing that does the job:

      * ONE fixed argv -- [<UI_ROOT>/tools/demo_stack.sh, "reset"] -- built here, never from
        the request. There is no parameter to inject into, because there is no parameter.
      * shell=False, as everywhere else in this codebase.
      * POST only, and Origin-checked, so a page on another site cannot drive it. The server
        is loopback-only, but "loopback" does not mean "unreachable from a browser": any
        website the user has open can issue a cross-origin POST to 127.0.0.1.
      * Refused while a simulation is running -- the reset stops the bridge, which would
        kill that run mid-flight.
      * One at a time.

WHY IT IS ASYNCHRONOUS
    A reset is ~80 s measured (down -> up -> deploy, then the bridge). Holding an HTTP
    request open that long invites a proxy or browser timeout and leaves the caller unsure
    whether it worked. So POST starts it and returns immediately; GET reports progress, and
    the log tail comes back with it so a failure is diagnosable without leaving the page.
"""

import os
import subprocess
import threading
import time

import config

SCRIPT = os.path.join(config.UI_ROOT, "tools", "demo_stack.sh")

_lock = threading.Lock()
_state = {"state": "idle", "started": None, "finished": None,
          "exit_code": None, "log": ""}


def status():
    with _lock:
        return dict(_state)


def running():
    with _lock:
        return _state["state"] == "running"


def _tail(path, n=40):
    try:
        with open(path, errors="replace") as fh:
            return "".join(fh.readlines()[-n:])
    except OSError:
        return ""


def _run():
    log = os.path.join(config.STACK_LOGS, "ui-reset.log")
    os.makedirs(config.STACK_LOGS, exist_ok=True)
    rc = -1
    try:
        with open(log, "w") as fh:
            # Fixed argv. cwd is the script's own directory so its relative paths resolve
            # exactly as they do when a person runs it by hand.
            p = subprocess.run([SCRIPT, "reset"], stdout=fh, stderr=subprocess.STDOUT,
                               cwd=config.UI_ROOT, timeout=600)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        rc = -2
    except OSError as exc:
        with open(log, "a") as fh:
            fh.write(f"\ncould not start {SCRIPT}: {exc}\n")
    with _lock:
        _state.update(state="ok" if rc == 0 else "failed", exit_code=rc,
                      finished=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      log=_tail(log))


def start():
    """Returns None on success, or a string saying why it was refused."""
    if not os.path.isfile(SCRIPT):
        return f"no such script: {SCRIPT}"
    with _lock:
        if _state["state"] == "running":
            return "a reset is already running"
        _state.update(state="running", exit_code=None, log="",
                      started=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      finished=None)
    threading.Thread(target=_run, daemon=True).start()
    return None
