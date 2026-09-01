"""Stream the defence's verdicts to the browser WHILE the run is still going.

WHY THIS EXISTS
    A full-mode run's stdout carries only the attack side: [TRIG] [GENUINE] [TARGET]
    [WARMUP] [SCHEDULE] [ROLES] [PQC] [LW] [KEYMGMT] [ABL]. There is no per-event line for
    any defence verdict — ZKP, GNN, LLM, the chain and `stopped_by_layer` exist ONLY in the
    CSVs. Before this file, runner.py read them once, after the process exited.

    That is fine undefended (the simulator outruns 1x display ~3.9x, so everything is in
    the client's buffer long before the playback cursor arrives). It is not fine defended:
    the measured cells ran 698-829 s wall for ~900 s of simulated time, about 1.3x. The map
    would show accusations firing for twelve minutes with the ribbon dark, then dump every
    verdict at once. The demo exists to show the defence acting.

HOW
    Poll the run directory, remember a BYTE OFFSET per file, and only ever parse whole
    lines — a row still being written is left for the next poll. New rows go through the
    SAME converters in csv_events.py that tools/import_run.py uses, so the live path and
    the imported path cannot disagree about the same run.

    This is an optimisation of ARRIVAL TIME, never a second source of truth. _finalise()
    still reads every CSV after exit; the dedupe key is what stops it re-announcing what
    was already sent.

NOT TAILED: _reputation.csv (153 MB), _beacons.csv (170 MB), _trust_refresh.csv (5.2 MB).
None feeds the event stream, and logger.cc deliberately does not flush them per row.
"""

import asyncio
import csv
import io
import os

import csv_events

POLL_SECONDS = 0.5

# (csv kind, converter). Order matters only cosmetically: within one poll, verdicts are
# emitted layer-first so a client that renders on arrival sees the pipeline, not the answer.
FILES = [
    ("zkp", csv_events.zkp_events),
    ("gnn_decision", csv_events.gnn_events),
    ("llm_incident", csv_events.llm_incident_events),
    ("audit", csv_events.chain_events),
    ("controller_audit", csv_events.controller_events),
    ("rsu_audit", csv_events.rsu_events),
    ("stake", csv_events.stake_events),
    ("decisions", csv_events.decision_events),
]

# _decisions.csv also feeds accusation_events(), which is NOT tailed: a live run already
# gets its accusations from stdout, where they carry the distance, victim density and
# in-range flag the CSV never recorded. Emitting both would double every accusation.


def event_key(ev):
    """Identity of an event for dedupe, shared with runner.py's post-exit replay.

    Keyed on what the simulator considers one fact, not on dict contents: the same row read
    twice must collapse, and two different layers' verdicts on one accusation must not.
    """
    t = ev.get("type")
    if t == "layer":
        return ("layer", ev.get("layer"), ev.get("event"))
    if t == "rsu_status":
        # rsu_audit has a row per event; only transitions are emitted, and the same
        # transition must not be re-announced if the file is re-read from the start.
        return ("rsu_status", ev.get("rsu", {}).get("r"), ev.get("state"))
    return (t, ev.get("event"))


class _File:
    """One CSV, read forward only."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.header = None

    def new_rows(self):
        """Rows completed since the last call. Never raises; a mid-write file is normal."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size <= self.offset:
            return []                       # nothing new (or truncated - leave it alone)
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read(size - self.offset)
        except OSError:
            return []

        # Stop at the last newline: the tail may be half a row. Those bytes stay unread and
        # are picked up whole on the next poll.
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return []
        self.offset += cut + 1
        text = chunk[:cut + 1].decode("utf-8", errors="replace")

        rows = list(csv.reader(io.StringIO(text)))
        if self.header is None:
            if not rows:
                return []
            self.header = [(c or "").strip() for c in rows.pop(0)]
        # Same normalisation as csv_events._rows: strip everything, because some cells are
        # space-padded and some are not.
        out = []
        for r in rows:
            if not r:
                continue
            out.append({k: (v or "").strip()
                        for k, v in zip(self.header, r)})
        return out


class Tailer:
    """One per live run. Owns the offsets, the rsu_events dedupe state, and its own task."""

    def __init__(self, run):
        self.run = run
        self.files = {}                  # kind -> _File
        self._rsu_seen = {}              # rsu_events() dedupes across rows; state lives here
        self._task = None
        self._stopping = asyncio.Event()

    def start(self):
        self._task = asyncio.create_task(self._loop())
        return self

    async def stop(self):
        """Drain once more before finishing, so the last rows are not left on the floor."""
        self._stopping.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def _loop(self):
        try:
            while True:
                done = self._stopping.is_set()
                await self._drain()
                if done:
                    return              # one full pass AFTER the stop request, then exit
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:                      # never take the run down with us
            await self.run.emit_tailer_error(repr(exc))

    async def _drain(self):
        for kind, convert in FILES:
            f = self.files.get(kind)
            if f is None:
                path = csv_events.find(self.run.store.dir, kind)
                if not path:
                    continue                          # not written yet, or not this run
                f = self.files[kind] = _File(path)
            rows = f.new_rows()
            if not rows:
                continue
            if convert is csv_events.rsu_events:
                events = convert(f.path, rows=rows, seen=self._rsu_seen)
            else:
                events = convert(f.path, rows=rows)
            for ev in events:
                await self.run.emit_derived(ev)
