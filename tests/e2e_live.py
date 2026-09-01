#!/usr/bin/env python3
"""P1 exit criterion: spawn a real simulation and stream it, end to end.

    # terminal 1
    cd backend && ../.venv/bin/python -m uvicorn app:app --port 8000
    # terminal 2
    .venv/bin/python tests/e2e_live.py

Unlike tests/test_parse.py and tests/test_validate.py (which are pure and run in
milliseconds) this launches ns-3 and takes ~90 s. It is the check that the pieces are
wired together, not that any one of them is correct.

Verifies, in order:
    V2  a malicious config is rejected before anything is spawned
    V3  a run starts and events arrive over the WebSocket while it is still running
    V4  the accusation count on the socket matches the simulator's own summary
    V5  the finished run replays byte-identically, and resumes from an arbitrary seq
        (this is what makes P4 replay nearly free)
    --  pem.compute_cell() reads the run directory with no adaptation
"""

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".venv", "lib", "python3.12", "site-packages"))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import websockets                       # noqa: E402
import config                           # noqa: E402

BASE = os.environ.get("UI_BASE", "http://127.0.0.1:8000")
WS = BASE.replace("http://", "ws://")

# Smoke preset — the shortest run that still exercises warmup + attack + reconciliation.
CFG = {"numVehicles": 200, "numRsus": 56, "numControllers": 4, "trace": "manhattan",
       "attackType": "single_data", "attackPercent": 2, "attackRounds": 1,
       "attackWindow": 120, "warmupTime": 60, "warmupAccusationStart": 35, "seed": 1}

ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{('  — ' + detail) if detail else ''}")


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


async def stream(run_id, from_seq=1, until_closed=True):
    events = []
    url = f"{WS}/ws/runs/{run_id}?from_seq={from_seq}"
    async with websockets.connect(url, max_size=None, ping_interval=None) as ws:
        while True:
            ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=240))
            if ev["type"] == "_replay_end":
                break
            events.append(ev)
            if until_closed and ev["type"] == "run_closed":
                break
    return events


def main():
    print("V2  injection is rejected before any process is spawned")
    code, body = post("/api/runs", {**CFG, "attackType": "x; rm -rf ~"})
    check("HTTP 400 naming the field", code == 400 and body.get("field") == "attackType",
          body.get("error", "")[:60])

    print("\nV3  a real run streams live")
    t0 = time.time()
    code, body = post("/api/runs", CFG)
    check("run accepted", code == 200, body.get("run_id", body))
    if code != 200:
        return 1
    run_id = body["run_id"]

    events = asyncio.run(stream(run_id))
    wall = time.time() - t0
    kinds = {}
    for e in events:
        kinds[e["type"]] = kinds.get(e["type"], 0) + 1
    check("events received", len(events) > 10, f"{len(events)} events in {wall:.0f}s")
    check("run_start first", events[0]["type"] == "run_start")

    closed = [e for e in events if e["type"] == "run_closed"]
    check("run exited cleanly", closed and closed[0]["exit_code"] == 0)
    check("timings reconciled from CSV", closed and closed[0]["timing_reconciled"] > 0,
          f"shift {closed[0]['timing_max_shift_s']}s" if closed else "")

    print("\nV4  the stream agrees with the simulator's own summary")
    summary = [e for e in events if e["type"] == "run_end"]
    acc = [e for e in events if e["type"] == "accusation"]
    attacks = [e for e in acc if e["kind"] == "attack"]
    check("summary line present", bool(summary))
    if summary:
        check("attack count == submitted",
              len(attacks) == summary[0]["submitted"],
              f"{len(attacks)} streamed vs {summary[0]['submitted']} submitted")

    print("\nV5  replay and resume")
    full = asyncio.run(stream(run_id, until_closed=False))
    again = asyncio.run(stream(run_id, until_closed=False))
    check("replay is byte-identical",
          json.dumps(full, sort_keys=True) == json.dumps(again, sort_keys=True),
          f"{len(full)} events")
    mid = max(2, len(full) // 2)
    part = asyncio.run(stream(run_id, from_seq=mid, until_closed=False))
    check("resume from seq matches the tail",
          part == [e for e in full if e["seq"] >= mid], f"from seq={mid}")
    racc = [e for e in full if e["type"] == "accusation"]
    check("all accusations marked t_exact", all(e["t_exact"] for e in racc),
          f"t = {[e['t'] for e in racc]}")

    print("\n--  the project's own metrics code reads the run directory unmodified")
    sys.path.insert(0, config.BCD_DIR)
    import pem
    out = pem.compute_cell(os.path.join(config.RESULTS_UI, run_id))
    check("pem.compute_cell() returns metrics", out is not None)
    if out:
        res, _cm = out
        print(f"       M2_ASR[single] = {res.get('M2_ASR[single]')}   "
              f"M4_HVTD = {res.get('M4_HVTD')}")

    print(f"\n{'ALL CHECKS PASSED' if ok else 'FAILURES ABOVE'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
