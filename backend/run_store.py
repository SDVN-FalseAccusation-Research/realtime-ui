"""Run directories: allocation, the durable event log, and listing.

LAYOUT (chosen so `pem.compute_cell(run_dir)` works with no adaptation — it globs
`*_<kind>.csv`, and `--csvPrefix=<run_dir>/run` produces exactly that)

    <ns3>/results/ui/<run-id>/
        run_config.json   the request, the effective flags and the exact argv
        events.jsonl      every event, in arrival order, with a monotonic seq
        sim.log           raw stdout
        stderr.log        raw stderr
        run_*.csv         the simulator's own output

WHY events.jsonl IS WRITTEN BEFORE BROADCAST
    It is the durable log, and one mechanism then serves three cases:
      * live      — tail it as it is written
      * resume    — a client reconnects with ?from_seq=N and the gap is replayed from it
      * replay    — a finished run is pushed through the same socket
    That is why P4 (replay + history) is nearly free once P1 exists.
"""

import json
import os
import secrets
import threading
import time

import config

_RUN_ID_TIME = "%Y%m%d-%H%M%S"


def new_run_id(attack_type="run"):
    """Sortable, readable, and unique. The random suffix avoids collisions when two runs
    start in the same second."""
    safe = "".join(c for c in str(attack_type) if c.isalnum() or c in "_-")[:24] or "run"
    return f"{time.strftime(_RUN_ID_TIME, time.gmtime())}-{safe}-{secrets.token_hex(4)}"


def run_dir(run_id):
    return os.path.join(config.RESULTS_UI, run_id)


class RunStore:
    """One per run. Append-only while the run is live, then read-only."""

    def __init__(self, run_id):
        self.run_id = run_id
        self.dir = run_dir(run_id)
        self.events_path = os.path.join(self.dir, "events.jsonl")
        self.seq = 0
        self.events = []                 # kept in memory: a run is a few thousand events
        self._lock = threading.Lock()
        self._fh = None

    # -- lifecycle ----------------------------------------------------------------------
    def open(self, request_cfg, effective, argv):
        # The simulator creates its own CSV directory, but events.jsonl is ours and is
        # written before the process starts, so create it here regardless.
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "run_config.json"), "w") as fh:
            json.dump({"run_id": self.run_id,
                       "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "request": request_cfg, "effective": effective, "argv": argv},
                      fh, indent=2)
        self._fh = open(self.events_path, "w", buffering=1)   # line-buffered
        return self

    def append(self, ev):
        """Stamp with a sequence number, persist, and return it for broadcast."""
        with self._lock:
            self.seq += 1
            ev["seq"] = self.seq
            self.events.append(ev)
            if self._fh:
                self._fh.write(json.dumps(ev, separators=(",", ":")) + "\n")
        return ev

    def close(self, exit_code=None, timing_fixed=0, timing_shift=0.0):
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None
        path = os.path.join(self.dir, "run_config.json")
        try:
            with open(path) as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            meta = {"run_id": self.run_id}
        meta.update(finished_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    exit_code=exit_code, events=self.seq,
                    timing_reconciled=timing_fixed, timing_max_shift_s=timing_shift)
        with open(path, "w") as fh:
            json.dump(meta, fh, indent=2)

    def rewrite_events(self):
        """Persist the in-memory events again after reconcile() corrected their times.

        Atomic (tmp + rename) so a reader never sees a half-written log.
        """
        tmp = self.events_path + ".tmp"
        with open(tmp, "w") as fh:
            for ev in self.events:
                fh.write(json.dumps(ev, separators=(",", ":")) + "\n")
        os.replace(tmp, self.events_path)

    # -- reading ------------------------------------------------------------------------
    def since(self, from_seq):
        return [e for e in self.events if e.get("seq", 0) >= from_seq]

    def decisions_csv(self):
        """The authoritative timing source, once the process has exited."""
        p = os.path.join(self.dir, "run_decisions.csv")
        return p if os.path.exists(p) else None


def read_events(run_id, from_seq=1):
    """Replay source: read a finished run's log off disk."""
    path = os.path.join(run_dir(run_id), "events.jsonl")
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue                       # a torn last line on a killed run
            if ev.get("seq", 0) >= from_seq:
                out.append(ev)
    return out


def list_runs(limit=200):
    """Newest first. Directories without run_config.json are SKIPPED, not errors —
    results/ui/ already contains hand-made dirs (e.g. `timing/`) that predate the UI."""
    root = config.RESULTS_UI
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root), reverse=True):
        d = os.path.join(root, name)
        cfg = os.path.join(d, "run_config.json")
        if not os.path.isdir(d) or not os.path.exists(cfg):
            continue
        try:
            with open(cfg) as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        eff = meta.get("effective", {})
        out.append({
            "run_id": meta.get("run_id", name),
            "started": meta.get("started_utc"),
            "finished": meta.get("finished_utc"),
            "exit_code": meta.get("exit_code"),
            "events": meta.get("events"),
            "attackType": eff.get("attackType"),
            "attackPercent": eff.get("attackPercent"),
            "numVehicles": eff.get("numVehicles"),
            "numRsus": eff.get("numRsus"),
            "numControllers": eff.get("numControllers"),
            "trace": eff.get("trace"),
            "blockchain": eff.get("blockchain"),
            "has_events": os.path.exists(os.path.join(d, "events.jsonl")),
        })
        if len(out) >= limit:
            break
    return out
