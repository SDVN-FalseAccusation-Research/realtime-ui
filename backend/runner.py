"""Spawn the simulator and turn its output into a live event stream.

    argv  ->  process  ->  stdout lines  ->  Parser  ->  Timeline  ->  RunStore  ->  Hub

NO PACING HAPPENS HERE. Events are shipped as fast as they are parsed and the *frontend*
paces playback against simulation time. Two reasons: the frontend needs a playback clock
anyway (positions are interpolated client-side), and a second clock in the backend could
disagree with it; and pause / speed / step / backward-scrub are all local UI state that
would otherwise need a round trip each. A run is a few thousand events, so buffering the
whole thing client-side is free.

Consequence worth knowing: because the simulator runs ~3.9x faster than real time, the
client is always buffered ahead. ns-3 can finish while the audience is still watching.

BACKPRESSURE. The stdout pipe is the only real backpressure surface — if this reader
stalls, the pipe fills and ns-3 blocks. So the per-line path is regex + dict + a buffered
write, and nothing slow is ever awaited inside it. Slow WebSocket clients are dropped by
the hub, never allowed to stall the parser.
"""

import asyncio
import os

import config
import csv_events
from parse import Parser
from timeline import Timeline


class Run:
    """One simulator process and everything derived from it."""

    def __init__(self, run_id, argv, effective, store, hub):
        self.run_id = run_id
        self.argv = argv
        self.effective = effective
        self.store = store
        self.hub = hub
        self.parser = Parser()
        self.timeline = Timeline(config=effective)
        self.proc = None
        self.exit_code = None
        self.state = "starting"          # starting | running | finished | failed
        self._task = None

    # -- lifecycle ----------------------------------------------------------------------
    async def start(self):
        os.makedirs(self.store.dir, exist_ok=True)
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=config.NS3_ROOT,
            env=config.sim_env(),
        )
        self.state = "running"
        await self._emit({"type": "run_start", "run_id": self.run_id,
                          "config": self.effective, "argv": self.argv,
                          "topology": self._topology()})
        self._task = asyncio.create_task(self._supervise())
        return self

    def _topology(self):
        eff = self.effective
        return {"vehicles": int(eff.get("numVehicles", 200)),
                "rsus": int(eff.get("numRsus", 56)),
                "controllers": int(eff.get("numControllers", 4)),
                "trace": eff.get("trace", "manhattan")}

    async def _supervise(self):
        sim_log = open(os.path.join(self.store.dir, "sim.log"), "w", buffering=1)
        err_log = open(os.path.join(self.store.dir, "stderr.log"), "w", buffering=1)
        try:
            # stdout and stderr are read on SEPARATE tasks and never interleaved: one of
            # the simulator's warnings is a 7-line banner that would otherwise be spliced
            # into the middle of a stdout line and corrupt parsing.
            await asyncio.gather(
                self._pump(self.proc.stdout, "stdout", sim_log),
                self._pump(self.proc.stderr, "stderr", err_log),
            )
            self.exit_code = await self.proc.wait()
        except asyncio.CancelledError:
            await self.stop()
            raise
        finally:
            sim_log.close()
            err_log.close()

        self.state = "finished" if self.exit_code == 0 else "failed"
        await self._finalise()

    async def _pump(self, stream, name, logfile):
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            logfile.write(line + "\n")
            for ev in self.parser.feed(line, stream=name):
                await self._emit(self.timeline.stamp(ev))

    async def _emit(self, ev):
        ev.setdefault("t", 0.0)
        self.store.append(ev)            # durable first — resume and replay depend on it
        await self.hub.broadcast(self.run_id, ev)

    async def _finalise(self):
        """After the process exits: correct the timings, then close out.

        The CSVs are only complete now. `Logger::Close()` never closes
        _controller_audit.csv / _llm_incident.csv — they flush when the process exits — so
        this must not run at the summary line.
        """
        fixed, shift = 0, 0.0
        csv_path = self.store.decisions_csv()
        if csv_path:
            fixed, shift = self.timeline.reconcile(self.store.events, csv_path)
            for ev in csv_events.decision_events(csv_path):
                self.store.append(ev)
                await self.hub.broadcast(self.run_id, ev)
            if fixed:
                self.store.rewrite_events()

        await self.hub.broadcast(self.run_id, self.store.append({
            "type": "run_closed", "t": self.timeline._last_t,
            "exit_code": self.exit_code, "state": self.state,
            "timing_reconciled": fixed, "timing_max_shift_s": shift,
        }))
        self.store.close(self.exit_code, fixed, shift, summary=self.parser.summary)
        await self.hub.finish(self.run_id)

    # Per-accusation verdicts live only in _decisions.csv — the simulator prints no
    # verdict line at all. csv_events.decision_events() is shared with tools/import_run.py
    # so the live path and the importer can never disagree about the same run.
    #
    # This is not as late as it sounds: the backend does not pace, and the simulator runs
    # ~3.9x faster than 1x display, so these are normally in the client's buffer well
    # before the playback cursor reaches them. The frontend back-fills the first one or two.

    async def stop(self):
        """SIGTERM, then SIGKILL if it will not go."""
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()

    @property
    def alive(self):
        return self.proc is not None and self.proc.returncode is None


class RunManager:
    """Exactly one live simulator at a time.

    Not an arbitrary limit: a full-defence run owns the bridge on :7545 and the three
    sidecars, none of which can be shared. Allowing two would produce two subtly wrong
    runs rather than an honest error.
    """

    def __init__(self, hub):
        self.hub = hub
        self.runs = {}
        self.current = None

    def busy(self):
        return self.current is not None and self.current.alive

    async def start(self, run_id, argv, effective, store):
        if self.busy():
            raise RuntimeError(
                f"a simulation is already running ({self.current.run_id}); "
                f"stop it first")
        run = Run(run_id, argv, effective, store, self.hub)
        self.runs[run_id] = run
        self.current = run
        await run.start()
        return run

    async def stop(self, run_id):
        run = self.runs.get(run_id)
        if run:
            await run.stop()
        return run

    def get(self, run_id):
        return self.runs.get(run_id)
