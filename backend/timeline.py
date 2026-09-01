"""Give every event a simulation timestamp.

WHY THIS FILE EXISTS
    The simulator never prints one. All 49 `std::cout` sites were checked: none contain
    `Simulator::Now()`. And the one line that looks like it carries time does not —
    `[TRIG] w=19 t=37` has `t` = the target VEHICLE ID (scenario.cc:311).

    So `t` is reconstructed here from the schedule the simulator announces in its preamble,
    then corrected from the CSV once the run finishes.

TWO SOURCES, IN ORDER OF TRUTH
    1. Synthesised (live). Deterministic from the schedule; good to the second.
    2. `_decisions.csv` `t_attack_start` / `t_detect` (after the run). Authoritative.

    `reconcile()` rewrites the synthesised values from the CSV, so **live playback is
    best-effort and replay is always exact**. Events keep `t_exact` so nothing downstream
    mistakes a cosmetic ordering offset for a measurement.

THE ARITHMETIC (verified against a real run, see tests/fixtures/standard_decisions.csv)
    warmup  k = 0,1,2...   t = warmupAccusationStart + k * warmupAccusationSpacing
                           -> 35, 43, 51   with start=35 spacing=8
    attack  j = 0,1,2...   t = warmupTime + j * spacing
                           -> 60, 90, ... 330   with warmupTime=60 spacing=30
                           and `[SCHEDULE] last fires at t=330s` agrees exactly.
    genuine i = 0,1,2...   t = warmupTime + i * genuine_step   (interleaved)

    Indexing is by ORDER OF OBSERVATION WITHIN EACH KIND, not by event id. Event ids come
    from one shared counter across all three kinds, so their numbering shifts as soon as
    `--misbehaveModel=1` introduces genuine events. Observation order does not.

    `spacing` MUST come from `[SCHEDULE] ... spacing=Ss` and never be recomputed: the
    `--attackSpacing` flag defaults to 0 (auto) and the value is mutated during the
    preamble by the attackWindow-widening and simTime-extension branches.
"""

import csv
import os

# Deterministic intra-event offsets, in simulated seconds, so the pieces of one accusation
# render in causal order instead of all landing on the same instant. Cosmetic only —
# everything stamped with these carries t_exact=False.
STAGE_OFFSET = {
    "accusation": 0.0,
    "trigger_probe": 0.05,
    "sybil": 0.05,
    "colluder": 0.08,
    "rsu_tamper": 0.60,
    "pqc_reject": 0.70,
    "lw_block": 0.90,
    "rsu_status": 1.00,
    "controller_failover": 1.10,
    "accusation_skipped": 0.0,
}


class Timeline:
    """Stamps parser events with `t`. Pure; construct one per run."""

    def __init__(self, config=None, schedule=None):
        self.config = dict(config or {})
        self.schedule = dict(schedule or {})
        self._n = {"warmup": 0, "attack": 0, "genuine": 0}
        self._t_of_event = {}      # event id -> sim seconds
        self._last_t = 0.0

    # -- configuration ------------------------------------------------------------------
    def update_schedule(self, sched):
        """Fold in a `schedule` event as the preamble reveals/mutates it."""
        self.schedule.update({k: v for k, v in sched.items() if k != "type"})

    def _cfg(self, key, default):
        v = self.config.get(key, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return float(default)

    # -- the synthesiser ----------------------------------------------------------------
    def _accusation_time(self, kind):
        """Next firing time for this kind, from its own independent cadence."""
        i = self._n[kind]
        self._n[kind] = i + 1
        if kind == "warmup":
            return (self._cfg("warmupAccusationStart", 15.0)
                    + i * self._cfg("warmupAccusationSpacing", 8.0))
        if kind == "genuine":
            step = float(self.schedule.get("genuine_step", 0.0)) or \
                self._cfg("warmupAccusationSpacing", 8.0)
            return self._cfg("warmupTime", 40.0) + i * step
        spacing = float(self.schedule.get("spacing", 0.0))
        if spacing <= 0:
            # No [SCHEDULE] line seen yet — fall back to the pipeline floor so ordering is
            # still monotonic. reconcile() will correct it.
            spacing = 3.5
        return self._cfg("warmupTime", 40.0) + i * spacing

    def stamp(self, ev):
        """Attach `t` and `t_exact` to one parser event. Returns the same dict."""
        etype = ev.get("type")

        if etype == "schedule":
            self.update_schedule(ev)

        if etype == "accusation":
            t = self._accusation_time(ev.get("kind", "attack"))
            self._t_of_event[ev.get("event")] = t
            ev["t"] = round(t, 3)
            ev["t_exact"] = False
            self._last_t = t
            return ev

        # Anything tied to an event inherits that event's time plus a cosmetic offset.
        eid = ev.get("event")
        if eid is not None and eid in self._t_of_event:
            base = self._t_of_event[eid]
            ev["t"] = round(base + STAGE_OFFSET.get(etype, 0.5), 3)
            ev["t_exact"] = False
            self._last_t = max(self._last_t, ev["t"])
            return ev

        # Preamble / summary / untied lines ride the current clock. Preamble events land at
        # t=0 so the UI can show them before the first accusation.
        if etype in ("ablation", "roles", "schedule", "bridge", "keymgmt",
                     "zkp_membership"):
            ev["t"] = 0.0
        elif etype in ("run_end", "pqc_summary", "custody_summary"):
            ev["t"] = round(float(self.schedule.get("sim_time", self._last_t)), 3)
        else:
            ev["t"] = round(self._last_t, 3)
        ev["t_exact"] = False
        return ev

    # -- the corrector ------------------------------------------------------------------
    def reconcile(self, events, decisions_csv):
        """Replace synthesised times with the CSV's authoritative ones, in place.

        `_decisions.csv` carries `t_attack_start` and `t_detect` per event. Called once,
        after the process exits (the CSVs are only complete then — and note that
        `Logger::Close()` never closes _controller_audit.csv / _llm_incident.csv, so they
        flush at process exit rather than at the summary line).

        Returns (events_corrected, max_shift_seconds) for logging; a large shift means the
        synthesiser's assumptions no longer hold and should be investigated, not ignored.
        """
        if not decisions_csv or not os.path.exists(decisions_csv):
            return 0, 0.0

        truth = {}
        with open(decisions_csv, newline="") as fh:
            for row in csv.DictReader(fh):          # never split(',') — quoted fields
                try:
                    eid = int((row.get("event_id") or "").strip())
                    t0 = float((row.get("t_attack_start") or "").strip())
                except (TypeError, ValueError):
                    continue
                if row.get("submitted", "").strip() == "1":
                    truth[eid] = t0

        if not truth:
            return 0, 0.0

        # shift every event of an accusation by the same delta so intra-event ordering holds
        shift = {eid: truth[eid] - self._t_of_event[eid]
                 for eid in truth if eid in self._t_of_event}
        fixed, worst = 0, 0.0
        for ev in events:
            eid = ev.get("event")
            if eid in shift:
                ev["t"] = round(ev["t"] + shift[eid], 3)
                ev["t_exact"] = True
                fixed += 1
                worst = max(worst, abs(shift[eid]))
        for eid, t in truth.items():
            self._t_of_event[eid] = t
        return fixed, round(worst, 3)
