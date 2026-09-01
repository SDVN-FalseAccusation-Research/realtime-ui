"""Turn the simulator's stdout into structured events.

PURE and SYNCHRONOUS by design: strings in, dicts out. No I/O, no asyncio, no clock.
That is what lets the whole live pipeline be developed and tested against a recorded log
(`tests/fixtures/sim_standard.log`) without ever launching ns-3.

Events carry no timestamp here — `timeline.py` stamps them, because the simulator does not
print one (see the module note there).

CONTRACT
    p = Parser()
    for line in stdout:            # one line, newline already stripped
        for ev in p.feed(line):    # 0..n event dicts
            ...

RULES THIS PARSER MUST OBEY
  * It must never raise. An unrecognised or malformed line becomes a `log` event and
    parsing continues. This is what makes the 7-line stderr WARN banner harmless, and it
    means a future simulator change degrades the UI instead of killing the run.
  * Never split on ',' or '|' — `[LW]` contains a literal pipe and `ids=[...]` contains
    commas inside brackets.
  * Ids lists are truncated by the simulator at 15 entries (shared_state.h) and end with
    ",...]". The count is always carried separately from the list, and `truncated` is set
    so the caller can backfill from _decisions.csv at run end.
"""

import re

# --- what the simulator prints, in the order the run prints it -------------------------
# Only the shapes we actually consume are listed; everything else falls through to `log`.

RE_ABL = re.compile(
    r"^\[ABL\] lwRules=(-?\d+) \(lwMode=(\d+)\)\s+pqcGate=(\d+)\s+zkpGate=(\d+)\s+"
    r"endorserRecompute=(\d+)")

# main.cc:266 — only printed in the fixed-duration branch
RE_SCHED_PLAN = re.compile(
    r"^\[SCHEDULE\] pct=(\d+)% units=(\d+) victims=(\d+) opportunities=(\d+) "
    r"spacing=([\d.]+)s simTime=([\d.]+)s")
# scenario.cc:112 — always printed when opportunities>0, and printed AFTER any mutation,
# so this is the authoritative source for spacing/simTime.
RE_SCHED_FIRE = re.compile(
    r"^\[SCHEDULE\] (\d+) attack opportunities, spacing=([\d.]+)s, "
    r"last fires at t=([\d.]+)s \(simTime=([\d.]+)s\)")
RE_SCHED_WIDEN = re.compile(r"^\[SCHEDULE\] attackWindow widened to ([\d.]+)s")
RE_SCHED_EXTEND = re.compile(r"^\[SCHEDULE\] simTime auto-extended [\d.]+ -> ([\d.]+)s")
RE_SCHED_GENUINE = re.compile(
    r"^\[SCHEDULE\] (\d+) genuine accusations over (\d+) misbehaver\(s\), step=([\d.]+)s")

RE_ROLES_ATK = re.compile(
    r"^\[ROLES\] attackerSeed=(\d+) pct=(\d+) attackers=(\d+) victims=(\d+) ids=\[([^\]]*)\]")
RE_ROLES_MIS = re.compile(r"^\[ROLES\] misbehavers=(\d+).*?ids=\[([^\]]*)\]")

RE_WARMUP = re.compile(
    r"^\[WARMUP\] evt=(\d+) accuser=(\d+) victim=(\d+) dist=(\d+)\s*\((.*?)\)")
RE_TARGET = re.compile(
    r"^\[TARGET\] evt=(\d+) attacker=(\d+) victim=(\d+) dist=(\d+) vdens=(\d+)\s*\((.*?)\)")
RE_GENUINE = re.compile(
    r"^\[GENUINE\] evt=(\d+) accuser=(\d+) victim=(\d+) dist=(\d+)")
RE_GENUINE_SKIP = re.compile(r"^\[GENUINE\] evt=(\d+) victim=(\d+) SKIPPED \((.*)\)")

# [TRIG] is the GENUINE-accusation trigger probe, NOT the reporters for an attack.
# scenario.cc:311 GenuineTriggerTrips(witness, target) — so `t=` is the TARGET VEHICLE ID,
# not a timestamp. (Misreading this cost a design iteration; the name is a trap.)
RE_TRIG = re.compile(r"^\[TRIG\] w=(\d+) t=(\d+) observedDeficit=([01])")

RE_SYBIL = re.compile(
    r"^\[SYBIL\] event=(\d+) accuser=(\d+) victim=(\d+) identities=(\d+)")
RE_COLLUDE = re.compile(
    r"^\[COLLUDE-SEND\] evt=(\d+) colluder=(\d+) zone=(\d+) dist=(\d+)")
RE_RSU_ATTACK = re.compile(
    r"^\[RSU-ATTACK\] rsu=(\d+) flipped (\d+)/(\d+) reports event=(\d+)")
RE_CTMF = re.compile(
    r"^\[CTMF\] controller=(\d+) REVOKED trust=([\d.-]+) zone=(\d+) -> backup=(\d+) "
    r"epoch=(\d+)")
RE_RSU_SC4 = re.compile(
    r"^\[RSU-SC4\] serving rsu=(\d+) trust=([\d.-]+) (QUARANTINED|REMOVED) event=(\d+)")
RE_PQC_FORGED = re.compile(
    r"^\[PQC\] accusation signature invalid \(forged\) accuser=(\d+) event=(\d+)")
RE_LW_BLOCK = re.compile(r"^\[LW\] event=(\d+) BLOCKED score=([\d.]+)")

# end-of-run block
RE_PQC_SUM = re.compile(r"^\[PQC\] signature checks: pass=(\d+) fail=(\d+)")
RE_KEYMGMT = re.compile(
    r"^\[KEYMGMT\] DKG t=(\d+)/n=(\d+)\s+LKH re-key events=(\d+) nodes=(\d+)")
RE_CUSTODY = re.compile(
    r"^\[RSU-SC4\] custody: quarantined=(\d+) removed=(\d+) of (\d+) RSUs")
RE_SUMMARY = re.compile(r"^attackType=(\S+) ")

RE_BRIDGE_WAIT = re.compile(r"^Waiting up to (\d+)s for the blockchain")
RE_BRIDGE_OK = re.compile(r"^Blockchain ready")
RE_ZKP_MEMBER = re.compile(r"^ZKP membership: admitted=(\d+) denied=(\d+)")
RE_DKG = re.compile(r"^DKG: RA key split t=(\d+) of n=(\d+)")


def _ids(blob):
    """'97,56,152' or '97,56,...' -> ([97, 56, 152], truncated?)

    The simulator caps the list at 15 and appends ',...' — never treat the printed list as
    the complete set.
    """
    truncated = False
    out = []
    for tok in blob.split(","):
        tok = tok.strip()
        if tok in ("...", ""):
            truncated = truncated or tok == "..."
            continue
        try:
            out.append(int(tok))
        except ValueError:
            truncated = True
    return out, truncated


class Parser:
    """Stateful only where the log genuinely requires it."""

    def __init__(self):
        self.phase = "preamble"        # preamble -> running -> ended
        self.schedule = {}             # harvested from the [SCHEDULE] family
        self.roles = {"attackers": [], "misbehavers": [],
                      "attackers_truncated": False, "misbehavers_truncated": False}
        self.summary = None
        self.events_seen = []          # event ids in the order they fired
        # [TRIG] probes accumulate per target vehicle; they are aggregated rather than
        # emitted individually (~15 per candidate victim, 151 in a Standard run).
        self._probes = {}

    # -- helpers ------------------------------------------------------------------------
    @staticmethod
    def _log(line, stream="stdout", level="info"):
        return {"type": "log", "stream": stream, "level": level, "text": line}

    def _accusation(self, kind, evt, accuser, victim, dist, note, vdens=None):
        self.phase = "running"
        self.events_seen.append(evt)
        ev = {"type": "accusation", "kind": kind, "event": evt,
              "accuser": {"v": accuser}, "victim": {"v": victim},
              "dist": dist, "in_range": note.startswith("in-range")}
        if vdens is not None:
            ev["victim_density"] = vdens
        return ev

    # -- the one public method ----------------------------------------------------------
    def feed(self, line, stream="stdout"):
        """One raw line -> list of event dicts. Never raises."""
        try:
            return self._feed(line, stream)
        except Exception as exc:                       # pragma: no cover - safety net
            return [self._log(f"{line}   <parser error: {exc!r}>", stream, "error")]

    def _feed(self, line, stream):
        line = line.rstrip("\n")
        if not line.strip():
            return []

        if stream == "stderr":
            # Any ERROR: must surface immediately — the UI shows a red banner rather than
            # waiting for the process to exit.
            if line.lstrip().startswith("ERROR:"):
                return [{"type": "run_error", "text": line.strip()}]
            return [self._log(line, "stderr", "warn")]

        # ---- accusations (highest value, checked first) -------------------------------
        m = RE_TARGET.match(line)
        if m:
            return [self._accusation("attack", int(m.group(1)), int(m.group(2)),
                                     int(m.group(3)), int(m.group(4)), m.group(6),
                                     vdens=int(m.group(5)))]
        m = RE_WARMUP.match(line)
        if m:
            return [self._accusation("warmup", int(m.group(1)), int(m.group(2)),
                                     int(m.group(3)), int(m.group(4)), m.group(5))]
        m = RE_GENUINE.match(line)
        if m:
            return [self._accusation("genuine", int(m.group(1)), int(m.group(2)),
                                     int(m.group(3)), int(m.group(4)), "in-range")]
        m = RE_GENUINE_SKIP.match(line)
        if m:
            return [{"type": "accusation_skipped", "event": int(m.group(1)),
                     "victim": {"v": int(m.group(2))}, "reason": m.group(3)}]

        # ---- the genuine-trigger probe: aggregate, never emit one-by-one ---------------
        m = RE_TRIG.match(line)
        if m:
            witness, target, trips = int(m.group(1)), int(m.group(2)), m.group(3) == "1"
            slot = self._probes.setdefault(target, {"probed": [], "deficit": []})
            slot["probed"].append(witness)
            if trips:
                slot["deficit"].append(witness)
            return [{"type": "trigger_probe", "target": {"v": target},
                     "witness": {"v": witness}, "deficit": trips,
                     "probed": len(slot["probed"]), "deficit_count": len(slot["deficit"])}]

        # ---- per-event attack detail --------------------------------------------------
        m = RE_SYBIL.match(line)
        if m:
            return [{"type": "sybil", "event": int(m.group(1)),
                     "accuser": {"v": int(m.group(2))}, "victim": {"v": int(m.group(3))},
                     "identities": int(m.group(4))}]
        m = RE_COLLUDE.match(line)
        if m:
            return [{"type": "colluder", "event": int(m.group(1)),
                     "colluder": {"v": int(m.group(2))}, "zone": int(m.group(3)),
                     "dist": int(m.group(4))}]
        m = RE_RSU_ATTACK.match(line)
        if m:
            return [{"type": "rsu_tamper", "rsu": {"r": int(m.group(1))},
                     "flipped": int(m.group(2)), "total": int(m.group(3)),
                     "event": int(m.group(4))}]

        # ---- defence reactions --------------------------------------------------------
        m = RE_CTMF.match(line)
        if m:
            return [{"type": "controller_failover", "controller": {"c": int(m.group(1))},
                     "trust": float(m.group(2)), "zone": int(m.group(3)),
                     "backup": {"c": int(m.group(4))}, "epoch": int(m.group(5)),
                     "no_standby": "NO ELIGIBLE STANDBY" in line}]
        m = RE_RSU_SC4.match(line)
        if m:
            return [{"type": "rsu_status", "rsu": {"r": int(m.group(1))},
                     "trust": float(m.group(2)), "state": m.group(3),
                     "event": int(m.group(4))}]
        m = RE_PQC_FORGED.match(line)
        if m:
            return [{"type": "pqc_reject", "accuser": {"v": int(m.group(1))},
                     "event": int(m.group(2))}]
        m = RE_LW_BLOCK.match(line)
        if m:
            return [{"type": "lw_block", "event": int(m.group(1)),
                     "score": float(m.group(2)), "text": line}]

        # ---- preamble -----------------------------------------------------------------
        m = RE_ROLES_ATK.match(line)
        if m:
            ids, trunc = _ids(m.group(5))
            self.roles.update(attackers=ids, attackers_truncated=trunc,
                              attacker_count=int(m.group(3)),
                              victim_count=int(m.group(4)))
            return [{"type": "roles", "role": "attackers", "ids": ids,
                     "count": int(m.group(3)), "truncated": trunc}]
        m = RE_ROLES_MIS.match(line)
        if m:
            ids, trunc = _ids(m.group(2))
            self.roles.update(misbehavers=ids, misbehavers_truncated=trunc,
                              misbehaver_count=int(m.group(1)))
            return [{"type": "roles", "role": "misbehavers", "ids": ids,
                     "count": int(m.group(1)), "truncated": trunc}]

        m = RE_SCHED_FIRE.match(line)
        if m:
            # authoritative — printed after every mutation
            self.schedule.update(opportunities=int(m.group(1)), spacing=float(m.group(2)),
                                 last_fire=float(m.group(3)), sim_time=float(m.group(4)))
            return [{"type": "schedule", **self.schedule}]
        m = RE_SCHED_PLAN.match(line)
        if m:
            self.schedule.update(attack_percent=int(m.group(1)), units=int(m.group(2)),
                                 victims=int(m.group(3)), opportunities=int(m.group(4)),
                                 spacing=float(m.group(5)), sim_time=float(m.group(6)))
            return [{"type": "schedule", **self.schedule}]
        m = RE_SCHED_GENUINE.match(line)
        if m:
            self.schedule.update(genuine_count=int(m.group(1)),
                                 genuine_step=float(m.group(3)))
            return [{"type": "schedule", **self.schedule}]
        m = RE_SCHED_WIDEN.match(line)
        if m:
            self.schedule["attack_window"] = float(m.group(1))
            return [self._log(line)]
        m = RE_SCHED_EXTEND.match(line)
        if m:
            self.schedule["sim_time"] = float(m.group(1))
            return [self._log(line)]

        m = RE_ABL.match(line)
        if m:
            # first tagged line of every run, unconditional -> "the simulator is alive"
            return [{"type": "ablation", "lw_rules": int(m.group(1)),
                     "lw_mode": int(m.group(2)), "pqc_gate": int(m.group(3)),
                     "zkp_gate": int(m.group(4)), "endorser_recompute": int(m.group(5))}]

        # ---- blockchain bring-up ------------------------------------------------------
        m = RE_BRIDGE_WAIT.match(line)
        if m:
            return [{"type": "bridge", "state": "waiting", "timeout": int(m.group(1))}]
        if RE_BRIDGE_OK.match(line):
            return [{"type": "bridge", "state": "ready"}]
        m = RE_DKG.match(line)
        if m:
            return [{"type": "keymgmt", "phase": "dkg", "t": int(m.group(1)),
                     "n": int(m.group(2))}]
        m = RE_ZKP_MEMBER.match(line)
        if m:
            return [{"type": "zkp_membership", "admitted": int(m.group(1)),
                     "denied": int(m.group(2))}]

        # ---- end of run ---------------------------------------------------------------
        m = RE_PQC_SUM.match(line)
        if m:
            return [{"type": "pqc_summary", "pass": int(m.group(1)),
                     "fail": int(m.group(2))}]
        m = RE_KEYMGMT.match(line)
        if m:
            return [{"type": "keymgmt", "phase": "summary", "t": int(m.group(1)),
                     "n": int(m.group(2)), "rekey_events": int(m.group(3)),
                     "rekey_nodes": int(m.group(4))}]
        m = RE_CUSTODY.match(line)
        if m:
            return [{"type": "custody_summary", "quarantined": int(m.group(1)),
                     "removed": int(m.group(2)), "rsus": int(m.group(3))}]
        if RE_SUMMARY.match(line):
            self.phase = "ended"
            kv = {}
            for tok in line.split():
                k, _, v = tok.partition("=")
                if not _:
                    continue
                try:
                    kv[k] = int(v)
                except ValueError:
                    try:
                        kv[k] = float(v)
                    except ValueError:
                        kv[k] = v
            self.summary = kv
            return [{"type": "run_end", **kv}]

        return [self._log(line, stream)]
