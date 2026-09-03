#!/usr/bin/env python3
"""Import an existing simulator run directory so the UI can replay it.

    python3 tools/import_run.py <run-dir> [--id NAME] [--label TEXT]
    python3 tools/import_run.py --scan <dir>          # every run under a sweep tree
    python3 tools/import_run.py --scan <dir> --dry-run

WHY
    The UI can only replay runs it launched itself, and all of those are undefended. The
    team's real evidence — sweeps, ablations, the defended campaign — lives in
    `results/realnet/sweep_full_*/` and was invisible. This reads those directories and
    reconstructs the same `events.jsonl` the live pipeline writes, so they show up in the
    History page and replay through the same code path. Nothing about the frontend changes.

WHAT IT DOES NOT DO
    It does not copy the CSVs. `run_config.json` records `source_dir` and the metrics
    endpoint reads from there — one cell in the defended sweep carries a 178 MB
    `_beacons.csv` and duplicating that per import would be absurd.

FIDELITY
    Timings come from the CSV (`t_attack_start` / `t_detect`), so an imported run is exactly
    as accurate as a reconciled live one. What is *not* recoverable is anything the CSVs
    never held: per-witness reporter arcs and RSU-window timing. Those are approximated on
    the live page from the mobility trace. Every imported event is therefore tagged
    `"src": "csv"` and the run is marked `"imported": true`, so the UI can say so plainly
    rather than presenting a reconstruction as captured truth.
"""

import argparse
import glob
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import config          # noqa: E402
import csv_events      # noqa: E402
from parse import Parser   # noqa: E402

SRC = "csv"


# ------------------------------------------------------------------ configuration ----
def recover_config(run_dir):
    """Rebuild the run's flags from whatever the source directory kept.

    Three sources, in order of preference:
      1. `run_config.json` — a UI run being re-imported; already exact.
      2. the sweep's `run_config.txt` one or two levels up — has the full `sim flags:` line.
      3. the cell's own `sim.log` / directory name — last resort.
    """
    cfg, notes = {}, []

    ui = os.path.join(run_dir, "run_config.json")
    if os.path.exists(ui):
        try:
            with open(ui) as fh:
                d = json.load(fh)
            return dict(d.get("effective") or {}), ["run_config.json"]
        except (OSError, ValueError):
            pass

    for up in (run_dir, os.path.dirname(run_dir), os.path.dirname(os.path.dirname(run_dir))):
        p = os.path.join(up, "run_config.txt")
        if not os.path.exists(p):
            continue
        try:
            text = open(p).read()
        except OSError:
            continue
        notes.append(os.path.relpath(p, run_dir))
        m = re.search(r"^sim flags\s*:\s*(.*)$", text, re.M)
        if m:
            for tok in m.group(1).split():
                if not tok.startswith("--"):
                    continue
                k, _, v = tok[2:].partition("=")
                cfg[k] = _coerce(v if _ else "1")
        break

    # The cell directory is named <attack>_p<pct>_s<seed> by run_sweep.sh.
    m = re.match(r"^(?P<a>[a-z_]+)_p(?P<p>\d+)_s(?P<s>\d+)$", os.path.basename(run_dir))
    if m:
        cfg.setdefault("attackType", m.group("a"))
        cfg.setdefault("attackPercent", int(m.group("p")))
        cfg.setdefault("seed", int(m.group("s")))
        notes.append("directory name")

    # Anything still unknown falls back to what the sweep and the sim actually default to.
    cfg.setdefault("numVehicles", 200)
    cfg.setdefault("numRsus", 64)
    cfg.setdefault("numControllers", 4)
    cfg.setdefault("trace", "manhattan")
    return cfg, notes


def _coerce(v):
    if v in ("1", "0") or re.fullmatch(r"-?\d+", v or ""):
        return int(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


# ------------------------------------------------------------------- the importer ----
def build_events(run_dir, cfg, sources, prefix=None):
    """Every converter, merged and ordered by (t, event, type).

    `prefix` selects one run inside a directory that holds several — `results/nodef/`
    keeps all nine attacks side by side as `single_data_*.csv`, `sybil_data_*.csv`, ...
    Without this the importer would silently pick one alphabetically and label it after
    the directory, which is worse than not importing at all.
    """
    def f(kind):
        if prefix:
            p = os.path.join(run_dir, f"{prefix}_{kind}.csv")
            return p if os.path.exists(p) else None
        return csv_events.find(run_dir, kind)
    dec = f("decisions")
    if not dec:
        return None

    events = []
    events += csv_events.accusation_events(dec, SRC)
    events += csv_events.decision_events(dec, SRC)
    for kind, fn in (("gnn_decision", csv_events.gnn_events),
                     ("zkp", csv_events.zkp_events),
                     ("llm_incident", csv_events.llm_incident_events),
                     ("audit", csv_events.chain_events),
                     ("controller_audit", csv_events.controller_events),
                     ("rsu_audit", csv_events.rsu_events),
                     ("stake", csv_events.stake_events),
                     ("lkh", csv_events.lkh_events)):
        p = f(kind)
        if p:
            got = fn(p, SRC)
            if got:
                sources.append(f"{os.path.basename(p)} ({len(got)})")
                events += got

    # Events that reference an accusation inherit its time; the layer CSVs carry no
    # timestamp of their own. A small per-layer offset keeps the pipeline in causal order
    # instead of stacking every stage on one instant.
    t_of = {e["event"]: e["t"] for e in events if e["type"] == "accusation"}
    order = {"zkp": 0.05, "gnn": 0.10, "llm": 0.15}
    for e in events:
        if "t" in e:
            continue
        base = t_of.get(e.get("event"))
        if base is None:
            e["t"] = 0.0
            e["t_exact"] = False
            continue
        e["t"] = round(base + order.get(e.get("layer"), 0.20), 3)

    # stdout, when the source kept it, adds what the CSVs never held: the attacker roster,
    # per-event distance and density, sybil counts. Reuse the live Parser verbatim.
    log = os.path.join(run_dir, "sim.log")
    if os.path.exists(log):
        p = Parser()
        extra = []
        with open(log, errors="replace") as fh:
            for line in fh:
                for ev in p.feed(line):
                    if ev["type"] in ("roles", "ablation", "schedule", "run_end",
                                      "sybil", "colluder", "pqc_reject", "rsu_tamper"):
                        ev["src"] = SRC
                        ev.setdefault("t", 0.0)
                        extra.append(ev)
        if extra:
            sources.append(f"sim.log ({len(extra)})")
            events += extra

    events.sort(key=lambda e: (e.get("t", 0.0), e.get("event", 0),
                               0 if e["type"] == "accusation" else 1))

    topo = {"vehicles": int(cfg.get("numVehicles", 200)),
            "rsus": int(cfg.get("numRsus", 64)),
            "controllers": int(cfg.get("numControllers", 4)),
            "trace": cfg.get("trace", "manhattan")}
    head = {"type": "run_start", "config": cfg, "topology": topo, "t": 0.0,
            "imported": True, "src": SRC}
    tail = {"type": "run_closed", "t": max((e["t"] for e in events), default=0.0),
            "exit_code": 0, "state": "imported", "timing_reconciled": 0,
            "timing_max_shift_s": 0.0, "src": SRC}
    return [head] + events + [tail]


def prefixes(run_dir):
    """Distinct `<prefix>_decisions.csv` stems in a directory — one run each."""
    out = []
    for p in sorted(glob.glob(os.path.join(run_dir, "*_decisions.csv"))):
        out.append(os.path.basename(p)[: -len("_decisions.csv")])
    return out


def import_run(run_dir, run_id=None, label=None, dry=False, prefix=None):
    run_dir = os.path.abspath(run_dir)
    if not os.path.isdir(run_dir):
        return None, f"not a directory: {run_dir}"

    cfg, notes = recover_config(run_dir)
    sources = list(notes)
    events = build_events(run_dir, cfg, sources, prefix)
    if events is None:
        return None, "no *_decisions.csv — not a run directory"

    # The decisions CSV records attack_type and attack_percent per row. Prefer them over
    # anything inferred from a path: a directory name is a guess, this is what actually ran.
    dec_path = (os.path.join(run_dir, f"{prefix}_decisions.csv") if prefix
                else csv_events.find(run_dir, "decisions"))
    first = next(iter(csv_events._rows(dec_path)), {})
    if first.get("attack_type"):
        cfg["attackType"] = first["attack_type"]
        notes.append("attack_type from decisions.csv")
    if first.get("attack_percent"):
        try:
            cfg["attackPercent"] = int(float(first["attack_percent"]))
        except ValueError:
            pass
    if first.get("blockchain_on"):
        cfg["blockchain"] = 1 if first["blockchain_on"] == "1" else 0

    run_id = run_id or _derive_id(run_dir, cfg, prefix)
    n_acc = sum(1 for e in events if e["type"] == "accusation")
    n_dec = sum(1 for e in events if e["type"] == "decision")
    if dry:
        return {"run_id": run_id, "events": len(events), "accusations": n_acc,
                "decisions": n_dec, "sources": sources, "dry": True}, None

    out_dir = os.path.join(config.RESULTS_UI, run_id)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "events.jsonl"), "w") as fh:
        for i, ev in enumerate(events, 1):
            ev["seq"] = i
            fh.write(json.dumps(ev, separators=(",", ":")) + "\n")

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                          time.gmtime(os.path.getmtime(run_dir)))
    meta = {
        "run_id": run_id, "imported": True, "source_dir": run_dir,
        "label": label or os.path.basename(run_dir),
        "started_utc": stamp, "finished_utc": stamp,
        "exit_code": 0, "events": len(events),
        "effective": cfg, "request": {}, "argv": [],
        "import_sources": sources,
        "summary": _summary(dec_path),
    }
    with open(os.path.join(out_dir, "run_config.json"), "w") as fh:
        json.dump(meta, fh, indent=2)

    return {"run_id": run_id, "events": len(events), "accusations": n_acc,
            "decisions": n_dec, "sources": sources, "dir": out_dir}, None


def _derive_id(run_dir, cfg, prefix=None):
    """Stable and readable: re-importing the same directory overwrites rather than piling
    up duplicates in the History page."""
    base = os.path.basename(run_dir.rstrip("/")) or "run"
    parent = os.path.basename(os.path.dirname(run_dir.rstrip("/")))
    stamp = re.search(r"(\d{8}-\d{6})", parent) or re.search(r"(\d{8}-\d{6})", base)
    name = f"{base}-{prefix}" if prefix and prefix not in base else base
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", name)[:48]
    return f"imported-{stamp.group(1) + '-' if stamp else ''}{safe}"


def _summary(dec_path):
    """Delegates to csv_events.run_summary -- ONE implementation, shared with the live path
    in runner.py, so the two can never drift apart on what counts as an attack."""
    return csv_events.run_summary(dec_path)


# -------------------------------------------------------------------------- CLI -----
def scan(root):
    """Every directory under `root` that contains a *_decisions.csv."""
    out = []
    for dec in glob.glob(os.path.join(root, "**", "*_decisions.csv"), recursive=True):
        d = os.path.dirname(dec)
        if os.path.realpath(d).startswith(os.path.realpath(config.RESULTS_UI)):
            continue                    # never re-import the UI's own tree
        out.append(d)
    return sorted(set(out))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--scan", metavar="DIR")
    ap.add_argument("--id")
    ap.add_argument("--label")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    targets = scan(a.scan) if a.scan else ([a.run_dir] if a.run_dir else [])
    if not targets:
        ap.error("give a run directory or --scan DIR")

    jobs = []
    for d in targets:
        pres = prefixes(d)
        # One decisions file -> one run; several -> one run per prefix.
        jobs += [(d, None)] if len(pres) <= 1 else [(d, p) for p in pres]

    ok = fail = 0
    for d, prefix in jobs:
        res, err = import_run(d, a.id if len(jobs) == 1 else None,
                              a.label, a.dry_run, prefix)
        rel = os.path.relpath(d, config.NS3_ROOT) + (f"  [{prefix}]" if prefix else "")
        if err:
            print(f"  SKIP  {rel}\n          {err}")
            fail += 1
            continue
        ok += 1
        print(f"  {'would import' if a.dry_run else 'imported'}  {res['run_id']}")
        print(f"          from {rel}")
        print(f"          {res['accusations']} accusations, {res['decisions']} decisions, "
              f"{res['events']} events")
        if res["sources"]:
            print(f"          sources: {', '.join(res['sources'])}")

    print(f"\n{ok} imported, {fail} skipped")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
