"""The defended-vs-undefended campaign: one grid, one honest comparison.

WHAT THIS ANSWERS
    "Does the defence work, and what does it cost?" ASR falls from ~0.88 to ~0.04. That is
    the headline. But the false-positive rate RISES, and an examiner will find that whether
    or not we show it, so this module computes both from the same rows and puts them side by
    side.

THE PAIRING IS BY NAME, ON PURPOSE
    A defended cell is `sweep-<attack>_p<pct>`; its baseline is `base-<attack>_p<pct>`. Two
    runs are compared only when both exist. That is auditable from the History page -- you
    can open either half and check it -- and it cannot silently pair a cell with a run that
    used different parameters, which is exactly the mistake this file exists to correct.

THE MISTAKE IT CORRECTS
    The original undefended baseline was NOT parameter-matched to the defended sweep:
    window 150 vs 800, encryptChannels 0 vs 1, and so simTime 360 s vs 865 s at p20. Quoting
    "0.8777 -> 0.0412" across that gap compared runs of different length on different
    channels. Re-running the baseline matched (same window, warmup, encryption and tuned
    flags, only --blockchain=0) moved single_data p20 from 0.7333 to 0.6857 and p40 from
    0.6029 to 0.7333 -- material, and in both directions, so it was not a constant offset.
    `param_match` below re-checks simTime per pair at request time rather than trusting that
    the right sweeps were imported.

COUNTING RULE -- COPIED FROM run_sweep.sh:253, NOT REINVENTED
    Over non-warmup, submitted rows only:
        attacks  submitted = victim_honest == 1        succeeded = ... and accepted == 1
        genuine  submitted = victim_honest != 1        blocked   = ... and accepted != 1
        ASR = succeeded / submitted     FPR = blocked / submitted
    Only accusations against an HONEST victim are attacks; a genuine report being accepted
    is correct behaviour, not an attack success. Getting this backwards inverts the headline.
"""

import json
import os
import re

import csv_events as C
import run_store

# `sweep-single_data_p60` / `base-single_data_p60`
_ID = re.compile(r"^(sweep|base)-(?P<attack>[a-z_]+)_p(?P<pct>\d+)$")


def _source_dir(run_id):
    """Where this run's CSVs actually live. An imported run does not copy them -- one sweep
    cell carries a 178 MB _beacons.csv -- so run_config.json records `source_dir`."""
    d = run_store.run_dir(run_id)
    try:
        with open(os.path.join(d, "run_config.json")) as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return d, {}
    src = meta.get("source_dir")
    if meta.get("imported") and src and os.path.isdir(src):
        return src, meta
    return d, meta


def counts(run_id):
    """The six numbers, by run_sweep.sh's rule. Returns None when there is no decisions CSV."""
    d, meta = _source_dir(run_id)
    path = C.find(d, "decisions")
    if not path:
        return None
    fa_sub = fa_acc = gen_sub = gen_blk = starved = 0
    layers = {}
    for row in C._rows(path):
        if C._b(row, "is_warmup"):
            continue
        if not C._b(row, "submitted"):
            # THE ARMS DO NOT ALWAYS GET THE SAME NUMBER OF CHANCES. A blacklisted victim
            # is no longer eligible, so once the pool empties the scheduler skips the rest.
            #
            # Measured across all 16 cells (baseline skips / defended skips):
            #     colluding p80   99 / 0        sybil p80   97 / 0
            #     single p20-p80   5,5,21,41 / 5,5,17,36
            #     timing p20-p80  15,32,61,107 / 15,32,59,95
            #
            # So there are TWO different causes and they must not be conflated. The
            # divergence is concentrated where the baseline's ASR approaches 100% --
            # colluding and sybil at p80 -- and there it is the successful false accusations
            # themselves emptying the pool: the baseline fired 61 and 63 attacks where the
            # defended arm fired all 160. On single and timing both arms skip almost
            # identically, so that portion is arm-INDEPENDENT (misbehavers are legitimately
            # blacklisted in both, and eligibility is further constrained by
            # singleMaxNeighbours / timingMinNeighbours).
            #
            # Consequence either way: rates are comparable between arms, raw counts are NOT.
            if (row.get("skip_reason") or "").strip() == "no_unblacklisted_victim":
                starved += 1
            continue
        honest, accepted = C._b(row, "victim_honest"), C._b(row, "accepted")
        if honest:
            fa_sub += 1
            if accepted:
                fa_acc += 1
            else:
                # Which layer stopped it. `degenerate` means the event drew no witness
                # reports at all -- starved, not defended -- and apps.cc (F10) is explicit
                # that it must NOT be credited to a defence layer.
                lay = (row.get("stopped_by_layer") or "").strip() or "unattributed"
                layers[lay] = layers.get(lay, 0) + 1
        else:
            gen_sub += 1
            if not accepted:
                gen_blk += 1
    return {"attacks_submitted": fa_sub, "attacks_succeeded": fa_acc,
            "genuine_submitted": gen_sub, "genuine_blocked": gen_blk,
            "skipped_no_victim": starved,
            "asr": round(fa_acc / fa_sub, 4) if fa_sub else None,
            "fpr": round(gen_blk / gen_sub, 4) if gen_sub else None,
            "layers": layers, "sim_time": _sim_time(d), "run_id": run_id}


def _sim_time(d):
    """From the run's own [SCHEDULE] line. Used to PROVE the two arms are comparable rather
    than assume it -- a length mismatch is precisely what made the first baseline invalid."""
    log = os.path.join(d, "sim.log")
    try:
        with open(log, errors="replace") as fh:
            for line in fh:
                m = re.search(r"simTime=(\d+)s", line)
                if m:
                    return int(m.group(1))
    except OSError:
        pass
    return None


def _rate(num, den):
    return round(num / den, 4) if den else None


def build():
    """The grid, the pooled totals, and the parameter check."""
    have = {r["run_id"] for r in run_store.list_runs(1000)}
    cells, warnings = {}, []

    for rid in sorted(have):
        m = _ID.match(rid)
        if not m:
            continue
        arm = "defended" if rid.startswith("sweep-") else "undefended"
        key = (m.group("attack"), int(m.group("pct")))
        got = counts(rid)
        if got:
            cells.setdefault(key, {})[arm] = got

    grid, pooled = [], {a: dict(fa_sub=0, fa_acc=0, gen_sub=0, gen_blk=0)
                        for a in ("defended", "undefended")}
    layer_totals = {}

    for (attack, pct) in sorted(cells):
        pair = cells[(attack, pct)]
        row = {"attack": attack, "percent": pct}
        for arm in ("defended", "undefended"):
            c = pair.get(arm)
            row[arm] = c
            if not c:
                continue
            p = pooled[arm]
            p["fa_sub"] += c["attacks_submitted"]
            p["fa_acc"] += c["attacks_succeeded"]
            p["gen_sub"] += c["genuine_submitted"]
            p["gen_blk"] += c["genuine_blocked"]
            if arm == "defended":
                for k, v in c["layers"].items():
                    layer_totals[k] = layer_totals.get(k, 0) + v
        # Comparable only if both arms ran the same amount of simulated time.
        d, u = pair.get("defended"), pair.get("undefended")
        row["paired"] = bool(d and u)
        if d and u and d["sim_time"] and u["sim_time"] and d["sim_time"] != u["sim_time"]:
            row["param_match"] = False
            warnings.append(f"{attack} p{pct}: simTime {d['sim_time']}s defended vs "
                            f"{u['sim_time']}s undefended — not comparable")
        else:
            row["param_match"] = True
        grid.append(row)

    for arm, p in pooled.items():
        p["asr"] = _rate(p["fa_acc"], p["fa_sub"])
        p["fpr"] = _rate(p["gen_blk"], p["gen_sub"])

    d, u = pooled["defended"], pooled["undefended"]
    cost = None
    if d["asr"] is not None and u["asr"] is not None:
        cost = {
            "asr_before": u["asr"], "asr_after": d["asr"],
            "fpr_before": u["fpr"], "fpr_after": d["fpr"],
            # Read as: for each genuine report the defence newly blocks, how many attacks
            # did it stop? A ratio well above 1 is the argument that the trade is worth it.
            "attacks_stopped": u["asr"] * d["fa_sub"] - d["fa_acc"] if d["fa_sub"] else None,
            "genuine_lost": (d["fpr"] - u["fpr"]) * d["gen_sub"]
                            if (d["fpr"] is not None and u["fpr"] is not None) else None,
        }
        if cost["genuine_lost"]:
            cost["exchange_rate"] = round(cost["attacks_stopped"] / cost["genuine_lost"], 2)

    return {"grid": grid, "pooled": pooled, "cost": cost,
            "layers": layer_totals, "warnings": warnings,
            "paired_cells": sum(1 for r in grid if r["paired"]),
            "notes": [
                "Only accusations against an HONEST victim count as attacks. A genuine "
                "report being accepted is correct behaviour, not an attack success.",
                "Counting rule is run_sweep.sh's (non-warmup, submitted rows only), so "
                "these numbers can be checked against the sweep's own report.txt.",
                "`degenerate` in the layer breakdown means the event drew no witness "
                "reports at all — starved rather than defended, and deliberately not "
                "credited to any defence layer.",
                "The two arms do not always get the same number of chances. A blacklisted "
                "victim is no longer eligible, so the pool can empty. The arms DIVERGE only "
                "where the baseline's ASR approaches 100% — colluding and sybil at p80, "
                "where the baseline skipped 99 and 97 opportunities and fired 61 and 63 "
                "attacks against the defended arm's full 160. On single and timing both "
                "arms skip almost identically (e.g. timing p80: 107 vs 95), so that portion "
                "is arm-independent — misbehavers are legitimately blacklisted in both, and "
                "singleMaxNeighbours / timingMinNeighbours constrain eligibility further. "
                "Compare the RATES between arms, never the raw counts.",
            ]}
