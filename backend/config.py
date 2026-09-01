"""Absolute paths and demo presets. Every other module imports locations from here.

Nothing in this file comes from a client request. Overridable by environment variable so
the UI can be pointed at a different checkout without editing code.
"""

import os

UI_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FYP_ROOT = os.path.dirname(UI_ROOT)


def _env(name, default):
    return os.path.abspath(os.environ.get(name, default))


SIM_ROOT = _env("FYP_SIM", os.path.join(FYP_ROOT, "project-simulation"))
NS3_ROOT = _env("FYP_NS3", os.path.join(SIM_ROOT, "ns-3.35"))
SUMO_DIR = _env("FYP_SUMO", os.path.join(SIM_ROOT, "sumo-manhattan"))
BCD_DIR = _env("FYP_BCD", os.path.join(SIM_ROOT, "blockchain-defence"))

BINARY = os.path.join(NS3_ROOT, "build", "scratch",
                      "sdvn_false_accusation_realnet",
                      "sdvn_false_accusation_realnet")

# All UI runs live here, kept apart from sweep output so neither can confuse the other.
RESULTS_UI = os.path.join(NS3_ROOT, "results", "ui")

ASSETS = os.path.join(UI_ROOT, "assets")
FRONTEND = os.path.join(UI_ROOT, "frontend")

# liboqs lives in ~/.local; the ns-3 libs are in the build tree. The simulator will not
# start without both on LD_LIBRARY_PATH.
LD_PATHS = [os.path.join(NS3_ROOT, "build", "lib"),
            os.path.expanduser("~/.local/lib")]

# --- mobility traces the client may choose from -------------------------------------
# A CHOICE, never a path. `traceFile` is not a client-settable flag: a request names a key
# here and the server substitutes the value. This is what keeps arbitrary filesystem paths
# out of the argv.
TRACES = {
    "manhattan": os.path.join(SUMO_DIR, "manhattan.tcl"),
    "synthetic": "",          # empty => the simulator's built-in synthetic grid
}

# The Manhattan trace contains exactly 200 vehicles and ends at t=1199 s. Both are hard
# limits: more vehicles than nodes leaves cars stranded at the origin, and past the end of
# the trace every vehicle freezes at its last waypoint.
TRACE_VEHICLES = 200
TRACE_END_S = 1199.0

# Sidecar / infrastructure ports probed by health.py
PORTS = {"zkp": 7070, "gnn": 7071, "llm": 7072, "bridge": 7545}

# --- demo presets --------------------------------------------------------------------
# Derived from measured timings (see new-task/UI_DESIGN.md 11.2). numRsus is 56, not 64:
# there is an open heap-corruption bug at >=60 RSUs with 200 vehicles (TASK 5b).
BASE = {
    "numVehicles": 200, "numRsus": 56, "numControllers": 4,
    "trace": "manhattan", "warmupTime": 60, "warmupAccusationStart": 35,
    "attackType": "single_data", "seed": 1,
}
PRESETS = {
    "smoke": {**BASE, "attackPercent": 2, "attackRounds": 1, "attackWindow": 120,
              "_label": "Smoke", "_desc": "~4 accusations, ~3 min display"},
    "standard": {**BASE, "attackPercent": 5, "attackRounds": 1, "attackWindow": 300,
                 "_label": "Standard", "_desc": "10 attacks + 3 warmup, ~6 min display"},
    "extended": {**BASE, "attackPercent": 9, "attackRounds": 1, "attackWindow": 540,
                 "_label": "Extended", "_desc": "18 attacks + 3 warmup, ~10 min display"},
    "evidence": {**BASE, "attackPercent": 40, "attackRounds": 3, "attackWindow": 840,
                 "_label": "Full evidence", "_desc": "240 opportunities, report-comparable"},
}
DEFAULT_PRESET = "standard"


def ld_library_path():
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    parts = LD_PATHS + ([existing] if existing else [])
    return ":".join(parts)


def sim_env():
    """A deliberately minimal environment for the child process.

    Only what the simulator needs. The ambient environment is not inherited, so nothing
    the server happens to have set can leak into a run and change its behaviour.
    """
    return {
        "LD_LIBRARY_PATH": ld_library_path(),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": os.path.expanduser("~"),
    }
