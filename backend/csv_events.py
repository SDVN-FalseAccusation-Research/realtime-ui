"""Convert the simulator's CSV output into UI events.

ONE implementation, two callers:
  * `runner.py` — at the end of a live run, to emit per-accusation verdicts (the simulator
    prints no verdict line at all; `accepted`, `w1/w2` and the layer attribution exist only
    in _decisions.csv).
  * `tools/import_run.py` — to reconstruct a whole run from a directory of CSVs, so the UI
    can replay sweeps and past evidence it did not launch itself.

Keeping them in one place matters: a second implementation would drift, and the two paths
would disagree about the same run.

Stdlib only. Every reader is defensive — a sweep directory may be mid-flight, truncated, or
missing files entirely, and a partial import is far more useful than an exception.

CSV RULES THAT BITE
  * Always `csv.DictReader`, never `split(',')` — `divergence_reason` is quoted and contains
    commas (`victim_mismatch(rsu=3,ctrl=1)`).
  * Values may be space-padded in some cells and not others; strip everything.
  * An empty file is meaningful, not an error. `_lkh.csv` with only a header is the evidence
    that no re-key happened, which is what `pem.py` needs to report M12 as n/a.
"""

import csv
import os

# Timing comes from the CSV, so imported events are as accurate as a reconciled live run.
T_START = "t_attack_start"
T_DETECT = "t_detect"


def _rows(path):
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, newline="") as fh:
            return [{(k or "").strip(): (v or "").strip() for k, v in r.items()}
                    for r in csv.DictReader(fh)]
    except OSError:
        return []


def _feed(path, rows=None):
    """Rows to convert: either a caller-supplied batch, or the whole file.

    `rows` is what makes the live tailer possible — backend/tailer.py parses the bytes that
    have just been appended to a CSV and hands them straight to these same converters, so the
    live path and tools/import_run.py can never disagree about the same run.
    """
    return _rows(path) if rows is None else rows


def find(run_dir, kind):
    """`<anything>_<kind>.csv` inside run_dir — the same glob contract pem.py uses."""
    import glob
    hits = sorted(glob.glob(os.path.join(run_dir, f"*_{kind}.csv")))
    return hits[0] if hits else None


def _f(row, key, default=0.0):
    try:
        return float(row.get(key) or "")
    except (TypeError, ValueError):
        return default


def _i(row, key, default=0):
    try:
        return int(float(row.get(key) or ""))
    except (TypeError, ValueError):
        return default


def _b(row, key):
    return (row.get(key) or "").strip() == "1"


# ---------------------------------------------------------------------- decisions ----
def decision_events(path, src=None, rows=None):
    """_decisions.csv -> one `decision` event per submitted accusation.

    Stamped with `t_detect`, which is when the controller actually ruled — so the verdict
    lands after the accusation on the timeline rather than on top of it.
    """
    out = []
    for row in _feed(path, rows):
        if not _b(row, "submitted"):
            continue
        eid = _i(row, "event_id", -1)
        if eid < 0:
            continue
        ev = {
            "type": "decision", "event": eid,
            "t": round(_f(row, T_DETECT) or _f(row, T_START), 3), "t_exact": True,
            "accepted": _b(row, "accepted"),
            "victim_honest": _b(row, "victim_honest"),
            "w1": _f(row, "w1"), "w2": _f(row, "w2"),
            "controller": {"c": _i(row, "controller_id")},
            "rsu": {"r": _i(row, "serving_rsu")},
            "reports": {"true": _i(row, "rsu_true"), "false": _i(row, "rsu_false"),
                        "trusted_true": _i(row, "trusted_true"),
                        "trusted_false": _i(row, "trusted_false")},
            "stopped_by": row.get("stopped_by_layer") or None,
            "divergence": _b(row, "divergence"),
            "blockchain_blocked": _b(row, "blockchain_blocked"),
            "divergence_reason": row.get("divergence_reason", ""),
            "blacklisted": _b(row, "blacklisted"),
            "trust_after": _f(row, "trust_after"),
            "latency_us": {k: _f(row, f"l_{k}_us")
                           for k in ("pqc", "zkp", "gnn", "llm", "bc")},
        }
        if src:
            ev["src"] = src
        out.append(ev)
    return out


def accusation_events(path, src=None, rows=None):
    """_decisions.csv -> one `accusation` event per submitted row.

    Used only by the importer; a live run gets these from stdout, where they carry extra
    detail (distance, victim density, in-range) that the CSV does not record.
    """
    out = []
    for row in _feed(path, rows):
        if not _b(row, "submitted"):
            continue
        eid = _i(row, "event_id", -1)
        if eid < 0:
            continue
        warm = _b(row, "is_warmup")
        # The CSV has no genuine/attack flag, but victim_honest carries it: an accusation
        # against a *dishonest* node is the genuine (negative-class) report.
        kind = "warmup" if warm else ("attack" if _b(row, "victim_honest") else "genuine")
        ev = {"type": "accusation", "kind": kind, "event": eid,
              "t": round(_f(row, T_START), 3), "t_exact": True,
              "accuser": {"v": _i(row, "attacker_id")},
              "victim": {"v": _i(row, "victim_id")},
              "dist": 0, "in_range": True,
              "zone": _i(row, "controller_id")}
        if src:
            ev["src"] = src
        out.append(ev)
    return out


# ------------------------------------------------------------------- layer events ----
def gnn_events(path, src=None, rows=None):
    """_gnn_decision.csv -> a `layer` event for the GNN (and the LLM verdict it carries)."""
    out = []
    for row in _feed(path, rows):
        eid = _i(row, "event_id", -1)
        if eid < 0:
            continue
        score = _f(row, "gnn_score")
        out.append({
            "type": "layer", "layer": "gnn", "event": eid, "t_exact": True,
            "verdict": "FLAGGED" if _b(row, "gnn_flagged") else "clear",
            "score": score, "pattern": row.get("gnn_pattern") or None,
            "per_variant": {k: _f(row, f"gnn_pv_{k}")
                            for k in ("single", "sybil", "timing", "colluding")},
            "blocked": _b(row, "gnn_blocked"),
            "model_hash": row.get("model_hash") or None,
            **({"src": src} if src else {}),
        })
        # The LLM verdict is carried on the same row; emit it separately so the ribbon and
        # the pipeline can treat the two layers independently.
        if row.get("llm_verdict"):
            out.append({
                "type": "layer", "layer": "llm", "event": eid, "t_exact": True,
                "verdict": row["llm_verdict"],
                "confidence": _f(row, "llm_confidence"),
                "escalate": _b(row, "llm_escalate"),
                "blocked": _b(row, "llm_blocked"),
                "prompt_hash": row.get("llm_prompt_hash") or None,
                **({"src": src} if src else {}),
            })
    return out


def zkp_events(path, src=None, rows=None):
    """_zkp.csv -> a `layer` event for the zk-STARK gate."""
    out = []
    for row in _feed(path, rows):
        eid = _i(row, "event_id", -1)
        res = row.get("zkp_result") or "NA"
        if eid < 0 or res == "NA":
            continue
        out.append({
            "type": "layer", "layer": "zkp", "event": eid, "t_exact": True,
            "verdict": res,
            "proximity": _b(row, "proximity"), "sequence": _b(row, "sequence"),
            "accusation": _b(row, "accusation"),
            "reports_dropped": _i(row, "reports_dropped"),
            "reports_proved": _i(row, "reports_proved"),
            # v_geo=1 means the weaker geometric substitute ran, not the real Context B —
            # worth surfacing rather than presenting both as the same check.
            "v_geo": _b(row, "v_geo"),
            "latency_us": _f(row, "l_cd_us"),
            **({"src": src} if src else {}),
        })
    return out


def llm_incident_events(path, src=None, rows=None):
    """_llm_incident.csv -> escalations that actually led to mitigation.

    One row per escalated + confirmed + mitigated false accusation, so `action` here is the
    real consequence (`credential_revoked|isolated`), not just a verdict.
    """
    out = []
    for row in _feed(path, rows):
        eid = _i(row, "event_id", -1)
        if eid < 0:
            continue
        out.append({
            "type": "llm_incident", "event": eid, "t_exact": True,
            "accuser": {"v": _i(row, "accuser_id")},
            "victim": {"v": _i(row, "victim_id")},
            "verdict": row.get("llm_verdict"), "confidence": _f(row, "llm_confidence"),
            "gnn_score": _f(row, "gnn_score"), "pattern": row.get("gnn_pattern"),
            "action": row.get("action") or "none",
            **({"src": src} if src else {}),
        })
    return out


def chain_events(path, src=None, rows=None):
    """_audit.csv -> a `chain_tx` per on-chain SC2 outcome submission."""
    out = []
    for row in _feed(path, rows):
        eid = _i(row, "event_id", -1)
        if eid < 0:
            continue
        out.append({
            "type": "chain_tx", "event": eid, "t_exact": True,
            "fn": "SC2.SubmitOutcome",
            "endorsed": _b(row, "endorsed"),
            "divergence": _b(row, "divergence"),
            "divergence_reason": row.get("divergence_reason", ""),
            "blocked": _b(row, "blockchain_blocked"),
            "rolled_back": _b(row, "rolled_back"),
            "controller_trust": _f(row, "controller_trust"),
            "failover": _b(row, "failover"),
            **({"src": src} if src else {}),
        })
    return out


def controller_events(path, src=None, rows=None):
    """_controller_audit.csv -> `controller_failover` on the rows where one happened."""
    out = []
    for row in _feed(path, rows):
        if not _b(row, "failover"):
            continue
        eid = _i(row, "event_id", -1)
        out.append({
            "type": "controller_failover", "event": eid, "t_exact": True,
            "controller": {"c": _i(row, "controller_id")},
            "backup": {"c": _i(row, "backup_controller")},
            "zone": _i(row, "controller_zone"),
            "trust": _f(row, "controller_trust"),
            "epoch": _i(row, "region_epoch"),
            "no_standby": _b(row, "no_standby"),
            **({"src": src} if src else {}),
        })
    return out


def rsu_events(path, src=None, rows=None, seen=None):
    """_rsu_audit.csv -> `rsu_status` only where the status CHANGES.

    The file has a row per event; emitting all of them would flood the stream with
    unchanged ACTIVE rows. 0=ACTIVE 1=QUARANTINED 2=REMOVED.
    """
    names = {0: "ACTIVE", 1: "QUARANTINED", 2: "REMOVED"}
    seen = {} if seen is None else seen
    out = []
    for row in _feed(path, rows):
        r = _i(row, "serving_rsu", -1)
        if r < 0:
            continue
        st = _i(row, "rsu_status")
        if seen.get(r) == st:
            continue
        seen[r] = st
        if st == 0:
            continue                       # the baseline, not a transition worth showing
        out.append({
            "type": "rsu_status", "event": _i(row, "event_id", -1), "t_exact": True,
            "rsu": {"r": r}, "state": names.get(st, str(st)),
            "trust": _f(row, "rsu_trust"), "tampered": _i(row, "rsu_tampered"),
            **({"src": src} if src else {}),
        })
    return out


def stake_events(path, src=None, rows=None):
    """_stake.csv -> a `stake` event where something was actually burned."""
    out = []
    for row in _feed(path, rows):
        burned = _f(row, "stake_burned")
        if burned <= 0:
            continue
        out.append({
            "type": "stake", "event": _i(row, "event_id", -1), "t_exact": True,
            "accuser": {"v": _i(row, "accuser_id")},
            "locked": _f(row, "stake_locked"), "burned": burned,
            "outcome": row.get("stake_outcome"),
            "history": _i(row, "false_acc_history"),
            "filings_remaining": _i(row, "filings_remaining"),
            **({"src": src} if src else {}),
        })
    return out


def lkh_events(path, src=None, rows=None):
    """_lkh.csv -> `keymgmt` re-key records (written in bulk at end of run)."""
    out = []
    for row in _feed(path, rows):
        out.append({
            "type": "keymgmt", "phase": "rekey", "t_exact": True,
            "kind": row.get("kind"), "zone": _i(row, "zone"),
            "m_e": _i(row, "m_e"), "n_e": _i(row, "n_e"),
            "c_actual": _i(row, "c_actual"),
            "latency_us": _f(row, "l_rekey_us"),
            **({"src": src} if src else {}),
        })
    return out


def run_summary(path):
    """The run's headline counts, computed from _decisions.csv.

    ASR FOLLOWS run_sweep.sh: only accusations against an HONEST victim are attacks. A
    genuine report being accepted is CORRECT BEHAVIOUR, never an attack success.

    This exists because the simulator's own `successRate=` line is wrong whenever genuine
    accusations are enabled. apps.cc:1372 increments falseAccusationsSubmitted guarded only
    on `!rec.isWarmup`, with no victim_honest filter, so every genuine accusation counts as
    an attack attempt and a correctly-accepted one counts as a successful attack. Measured
    on three runs with --misbehaveModel=1: the line reported 0.5345 / 0.3562 / 0.8889 where
    the truth was 0.1724 / 0.0000 / 0.0000 -- the last displaying 89% attack success when
    ZERO attacks succeeded. Runs without genuine traffic are unaffected, which is why this
    hid for so long.

    pem.py:m2_asr already filters this way, so the Metrics page was always right and only
    the History page was wrong. One implementation, two callers (runner.py and
    tools/import_run.py), so they cannot drift apart again.
    """
    rows = [r for r in _rows(path)
            if _b(r, "submitted") and not _b(r, "is_warmup")]
    if not rows:
        return None
    atk = [r for r in rows if _b(r, "victim_honest")]
    acc = sum(1 for r in atk if _b(r, "accepted"))
    return {"submitted": len(atk), "accepted": acc,
            "successRate": (acc / len(atk)) if atk else 0.0,
            "events_total": len(rows)}


# ======================================================================================
# Readers used by the component detail pages (components.py) rather than the event stream.
# These files are either too large for events.jsonl (_trust_refresh.csv is 11 MB) or carry
# no event_id to hang an event off (_lkh.csv), so they are read on demand and aggregated
# server-side instead.
# ======================================================================================

def trust_refresh_rows(path, event_id=None, limit=400):
    """_trust_refresh.csv -> the reputation updates, optionally for one accusation.

    This is the file that answers "what did the reputation layer CHANGE": it carries
    `event_id`, the `glsim` that drove the update, and the `old_tr` -> `new_tr` pair.
    `_reputation.csv` is the fuller n-by-n snapshot but is 153 MB and has NO event_id, so
    it is deliberately not used here.
    """
    out = []
    for row in _rows(path):          # not _feed: this reader is not on the live path
        if event_id is not None and _i(row, "event_id", -1) != event_id:
            continue
        out.append({
            "event": _i(row, "event_id", -1), "t": _f(row, "time"),
            "observer": _i(row, "observer"), "reporter": _i(row, "reporter"),
            "glsim": _f(row, "glsim"),
            "old_tr": _f(row, "old_tr"), "new_tr": _f(row, "new_tr"),
            "delta": round(_f(row, "new_tr") - _f(row, "old_tr"), 6),
            "source": row.get("source", ""),
        })
        if len(out) >= limit:
            break
    return out


def lkh_summary(path):
    """_lkh.csv -> the group-key re-key cost, aggregated.

    Rows are drained once at end-of-run and carry `zone` but NO `event_id`, so they cannot
    be attributed to a specific accusation without inference. The value here is the
    aggregate: `c_actual` (messages actually delivered) against `n_e` (group size) is the
    O(m log N) claim, measured.
    """
    rows = _rows(path)
    if not rows:
        return {"events": 0}
    by_kind, tot_c, tot_n, lat = {}, 0, 0, []
    for r in rows:
        k = r.get("kind") or "?"
        c, n = _i(r, "c_actual"), _i(r, "n_e")
        s = by_kind.setdefault(k, {"n": 0, "c_actual": 0, "n_e": 0})
        s["n"] += 1
        s["c_actual"] += c
        s["n_e"] += n
        tot_c += c
        tot_n += n
        lat.append(_f(r, "l_rekey_us"))
    import math
    # A flat scheme costs O(N) messages per revocation; LKH claims O(m log N). Comparing
    # the measured mean against log2(mean group size) is the whole point of this file.
    mean_n = tot_n / len(rows) if rows else 0
    return {
        "events": len(rows),
        "messages": tot_c,
        "mean_per_rekey": round(tot_c / len(rows), 2),
        "mean_group": round(mean_n, 1),
        "log2_group": round(math.log2(mean_n), 2) if mean_n > 1 else 0,
        "flat_would_be": round(mean_n, 1),
        "mean_latency_us": round(sum(lat) / len(lat), 1) if lat else 0,
        "by_kind": {k: {"n": v["n"],
                        "mean_c": round(v["c_actual"] / v["n"], 2) if v["n"] else 0}
                    for k, v in sorted(by_kind.items())},
    }
