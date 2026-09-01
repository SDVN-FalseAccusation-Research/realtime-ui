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
import tailer
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
        self.tailer = None
        self.admitted = None             # from "ZKP membership: admitted=N denied=M"
        # Every event the CSV path has already announced. The tailer emits verdicts DURING
        # the run and _finalise() re-reads the same files after exit, so without this every
        # decision would appear twice. See tailer.event_key.
        self._seen_keys = set()

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
        # Started after run_start is on the wire so the first verdict cannot precede it.
        self.tailer = tailer.Tailer(self).start()
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
            if self.tailer:
                await self.tailer.stop()
            await self.stop()
            raise
        finally:
            sim_log.close()
            err_log.close()

        self.state = "finished" if self.exit_code == 0 else "failed"
        # A DEFENDED RUN ON A DIRTY LEDGER EXITS 0 AND DOES NOTHING.
        #
        # SC1 re-registers happily ("registered=260 failed=0") but the zk-STARK membership
        # gate then denies every vehicle, so no accusation is ever filed. Measured: a second
        # UI run against the ledger left by the first printed
        #     ZKP membership: admitted=0 denied=200
        #     ... submitted=0 accepted=0 successRate=0
        # and exited 0. Every run with a FRESH ledger — all 16 sweep cells and the first UI
        # run — printed admitted=200 denied=0.
        #
        # Left alone, that is the worst kind of result: a green run with no events, which
        # reads either as a broken demo or, far worse, as a perfect defence. It is neither.
        if self.state == "finished" and self.admitted == 0:
            self.state = "degraded"
            await self._emit({
                "type": "run_degraded", "t": 0.0, "reason": "zkp_membership_denied_all",
                "detail": ("the zk-STARK membership gate admitted 0 of "
                           f"{self._topology()['vehicles']} vehicles, so no accusation "
                           "could be filed. The ledger still holds the previous run's "
                           "registrations — reset it with tools/demo_stack.sh reset "
                           "before each defended run.")})
        await self._finalise()

    async def _pump(self, stream, name, logfile):
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            logfile.write(line + "\n")
            for ev in self.parser.feed(line, stream=name):
                if ev.get("type") == "zkp_membership":
                    self.admitted = ev.get("admitted")
                await self._emit(self.timeline.stamp(ev))

    async def _emit(self, ev):
        ev.setdefault("t", 0.0)
        self.store.append(ev)            # durable first — resume and replay depend on it
        await self.hub.broadcast(self.run_id, ev)

    async def emit_derived(self, ev):
        """One CSV-derived event, from the live tailer or the post-exit sweep.

        Deduped, then timestamped. `decision` rows carry `t_detect` and are already exact;
        the layer CSVs carry no time of their own, so they inherit their accusation's from
        Timeline exactly as the stdout events do.
        """
        key = tailer.event_key(ev)
        if key in self._seen_keys:
            return False
        self._seen_keys.add(key)
        if not ("t" in ev and ev.get("t_exact")):
            self.timeline.stamp(ev)
        await self._emit(ev)
        return True

    async def emit_tailer_error(self, detail):
        """The tailer failing must degrade the run to its old behaviour, not end it —
        _finalise() still reads every CSV after exit."""
        await self._emit({"type": "tailer_error", "detail": detail, "t": 0.0})

    async def _finalise(self):
        """After the process exits: correct the timings, then close out.

        The CSVs are only complete now. `Logger::Close()` never closes
        _controller_audit.csv / _llm_incident.csv — they flush when the process exits — so
        this must not run at the summary line.
        """
        if self.tailer:
            await self.tailer.stop()     # one last drain: the final rows are written late

        fixed, shift = 0, 0.0
        csv_path = self.store.decisions_csv()
        if csv_path:
            fixed, shift = self.timeline.reconcile(self.store.events, csv_path)
            # Backstop, not the main path. The tailer has normally sent these already; what
            # survives the dedupe is whatever it missed — a run killed mid-buffer, a file
            # that only appeared at exit, or a tailer that errored out.
            late = 0
            for ev in csv_events.decision_events(csv_path):
                late += await self.emit_derived(ev)
            if late:
                await self._emit({"type": "late_decisions", "count": late, "t": 0.0})
            if fixed:
                self.store.rewrite_events()

        await self.hub.broadcast(self.run_id, self.store.append({
            "type": "run_closed", "t": self.timeline._last_t,
            "exit_code": self.exit_code, "state": self.state,
            "timing_reconciled": fixed, "timing_max_shift_s": shift,
        }))
        self.store.close(self.exit_code, fixed, shift, summary=self.parser.summary)
        await self.hub.finish(self.run_id)

    # Per-accusation verdicts live only in _decisions.csv — the simulator prints no verdict
    # line at all — so csv_events.decision_events() is shared with tools/import_run.py and
    # the live path and the importer can never disagree about the same run.
    #
    # They now arrive DURING the run via tailer.py rather than here. The "~3.9x faster than
    # display so the buffer covers it" argument that justified reading them only at exit
    # held for undefended runs and NOT for defended ones: the measured full-mode cells ran
    # 698-829 s wall for ~900 s of simulated time, so the client is not buffered ahead at
    # all. The pass above is the backstop for whatever the tailer missed.

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
