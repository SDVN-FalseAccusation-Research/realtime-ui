"""FastAPI application: REST + the one WebSocket, and the static frontend.

    uvicorn app:app --host 127.0.0.1 --port 8000        (from backend/)
    or:  ../.venv/bin/python -m uvicorn app:app --port 8000

BINDS TO LOOPBACK ONLY. This server starts processes on the machine; it is a local tool,
not a service. `assert_local()` is called at startup so an accidental --host 0.0.0.0 is a
loud failure rather than a quiet exposure.
"""

import asyncio
import os

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import components
import config
import health
import run_store
from flags import ATTACK_TYPES, REGISTRY
from hub import Client, Hub
from runner import RunManager
from validate import Rejected, build_argv

app = FastAPI(title="SDVN realtime demonstrator", docs_url=None, redoc_url=None)
HUB = Hub()
MANAGER = RunManager(HUB)


def assert_local(host):
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(
            f"refusing to bind {host}: this server spawns processes and must stay on "
            f"loopback")


# ---------------------------------------------------------------------------- REST ----
@app.get("/api/flags")
def get_flags():
    """The whitelist, its defaults, the presets, and the nine attack types.

    The config page is generated from this, so the form can never offer a flag the
    simulator does not have.
    """
    try:
        REGISTRY.load()
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(503, str(exc))
    return {"flags": REGISTRY.as_list(),
            "attack_types": ATTACK_TYPES,
            "presets": config.PRESETS,
            "default_preset": config.DEFAULT_PRESET,
            "traces": sorted(config.TRACES),
            "limits": {"trace_vehicles": config.TRACE_VEHICLES,
                       "trace_end_s": config.TRACE_END_S}}


@app.get("/api/health")
def get_health():
    return health.probe()


@app.get("/api/runs")
def get_runs(limit: int = Query(200, ge=1, le=1000)):
    return {"runs": run_store.list_runs(limit),
            "busy": MANAGER.busy(),
            "current": MANAGER.current.run_id if MANAGER.busy() else None}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    import json
    try:
        d = run_store.run_dir(run_id)
    except Exception:
        raise HTTPException(400, "bad run id")
    cfg = os.path.join(d, "run_config.json")
    if not os.path.exists(cfg):
        raise HTTPException(404, "no such run")
    with open(cfg) as fh:
        meta = json.load(fh)
    run = MANAGER.get(run_id)
    meta["live"] = bool(run and run.alive)
    return meta


@app.post("/api/runs")
async def post_run(body: dict):
    """Validate, allocate a run directory, spawn. Returns before the run finishes."""
    if MANAGER.busy():
        raise HTTPException(409, f"already running {MANAGER.current.run_id}")

    run_id = run_store.new_run_id(body.get("attackType", "run"))
    try:
        argv, effective = build_argv(body, run_id)
    except Rejected as exc:
        # 400 with the offending field named — the config page highlights it
        return JSONResponse(status_code=400,
                            content={"error": exc.reason, "field": exc.field})

    # ONE DEFENDED RUN PER LEDGER. A second run against a consumed ledger exits 0 and
    # files no accusations at all — SC1 re-registers, then the zk-STARK membership gate
    # denies every vehicle. Refusing here is the whole point: the failure is otherwise
    # indistinguishable from a successful run in which the attack simply never fired.
    if effective.get("blockchain"):
        led = health.ledger()
        if led["state"] == "used":
            return JSONResponse(status_code=409,
                                content={"error": led["note"], "field": "blockchain",
                                         "ledger": led})

    store = run_store.RunStore(run_id).open(body, effective, argv)
    try:
        await MANAGER.start(run_id, argv, effective, store)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(503, f"cannot start the simulator: {exc}")

    if effective.get("blockchain"):
        health.consume_ledger(run_id)      # only once it has actually started

    return {"run_id": run_id, "argv": argv, "effective": effective,
            "ws": f"/ws/runs/{run_id}"}


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str):
    run = await MANAGER.stop(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    return {"run_id": run_id, "stopped": True}


@app.get("/api/runs/{run_id}/metrics")
def get_metrics(run_id: str):
    """M1-M12, computed by the project's own pem.py so the UI cannot disagree with the
    paper. Available once the run has finished and its CSVs are complete."""
    import json
    import sys
    sys.path.insert(0, config.BCD_DIR)
    try:
        import pem
    except ImportError as exc:
        raise HTTPException(503, f"pem.py not importable: {exc}")
    d = run_store.run_dir(run_id)
    if not os.path.isdir(d):
        raise HTTPException(404, "no such run")
    # An imported run does not copy its CSVs (one sweep cell carries a 178 MB
    # _beacons.csv), so the metrics are computed from where they actually live.
    try:
        with open(os.path.join(d, "run_config.json")) as fh:
            meta = json.load(fh)
        if meta.get("imported") and os.path.isdir(meta.get("source_dir", "")):
            d = meta["source_dir"]
    except (OSError, ValueError):
        pass
    out = pem.compute_cell(d)
    if out is None:
        raise HTTPException(409, "no decisions.csv yet — has the run finished?")
    res, confusion = out
    return {"run_id": run_id,
            # pem.py returns (tp, TN, FP, fn) -- note the middle two are tn THEN fp,
            # not the conventional tp/fp/tn/fn order (pem.py m1_mcc, final return).
            # Getting this backwards reported 50 false positives where there were 9.
            "confusion": {"tp": confusion[0], "tn": confusion[1],
                          "fp": confusion[2], "fn": confusion[3]},
            "metrics": {k: {"value": m.value, "num": m.num, "den": m.den,
                            "na_reason": m.na_reason}
                        for k, m in res.items()}}


@app.get("/api/runs/{run_id}/components")
def get_components(run_id: str):
    """Which defence components this run actually used, with a headline stat each."""
    out = components.overview(run_id)
    if out is None:
        raise HTTPException(404, "no such run")
    return out


@app.get("/api/runs/{run_id}/component/{name}")
def get_component(run_id: str, name: str,
                  event: int = Query(None), limit: int = Query(400, ge=1, le=5000)):
    """One component's detail: what went in, what came out, what it changed.

    Read server-side from the run's CSVs — `_trust_refresh.csv` is 11 MB and
    `_reputation.csv` 153 MB, so neither is ever shipped whole to a browser.
    """
    if name not in components.NAMES:
        raise HTTPException(404, f"unknown component; expected one of "
                                 f"{', '.join(components.NAMES)}")
    kw = {"event_id": event, "limit": limit} if name == "reputation" else {}
    out = components.build(run_id, name, **kw)
    if out is None:
        raise HTTPException(404, "no such run")
    return out


# ----------------------------------------------------------------------- WebSocket ----
@app.websocket("/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str, from_seq: int = Query(1, ge=1)):
    """The single event channel — identical for live, resume and replay.

    A live run streams from the hub. A finished run replays from events.jsonl. A
    reconnecting client passes ?from_seq=<last+1> and the gap is served from disk before
    it rejoins the tail.
    """
    origin = ws.headers.get("origin")
    if origin and not (origin.startswith("http://127.0.0.1")
                       or origin.startswith("http://localhost")):
        await ws.close(code=1008)
        return

    await ws.accept()
    run = MANAGER.get(run_id)
    live = bool(run and run.alive)

    client = Client(ws, run_id, from_seq)
    if live:
        await HUB.add(client)

    try:
        # 1) backlog from memory (live) or disk (replay / resume)
        backlog = run.store.since(from_seq) if run else run_store.read_events(run_id, from_seq)
        for ev in backlog:
            await ws.send_json(ev)

        if not live:
            await ws.send_json({"type": "_replay_end", "count": len(backlog)})
            return

        # 2) then the live tail, skipping anything the backlog already covered
        last = backlog[-1]["seq"] if backlog else from_seq - 1
        while True:
            ev = await client.queue.get()
            if ev.get("type") == "_eos":
                break
            if ev.get("seq", 0) > last:
                await ws.send_json(ev)
    except WebSocketDisconnect:
        pass
    except (asyncio.CancelledError, RuntimeError):
        pass
    finally:
        await HUB.remove(client)


# --------------------------------------------------------------------------- static ---
_ASSET_TYPES = {".json": "application/json", ".svg": "image/svg+xml",
                ".u16": "application/octet-stream", ".u8": "application/octet-stream"}


@app.get("/assets/{name}")
def asset(name: str):
    """Serve the generated assets, transparently handling the pre-gzipped ones.

    build_assets.py writes `roads.svg.gz`, `pos_x.u16.gz` etc. Serving those with
    `Content-Encoding: gzip` lets the browser inflate them itself, so the frontend can
    `fetch('/assets/pos_x.u16')` and get raw bytes straight into a typed array — no
    DecompressionStream, no client-side gunzip, and the ~1 MB payload stays compressed on
    the wire.
    """
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "bad asset name")
    base = os.path.join(config.ASSETS, name)
    ext = os.path.splitext(name)[1]
    media = _ASSET_TYPES.get(ext, "application/octet-stream")

    if os.path.exists(base):
        return FileResponse(base, media_type=media)
    if os.path.exists(base + ".gz"):
        return FileResponse(base + ".gz", media_type=media,
                            headers={"Content-Encoding": "gzip"})
    raise HTTPException(404, f"no such asset: {name} (run tools/build_assets.py)")


if os.path.isdir(config.FRONTEND):
    app.mount("/static", StaticFiles(directory=config.FRONTEND), name="static")


@app.get("/")
def index():
    p = os.path.join(config.FRONTEND, "index.html")
    if not os.path.exists(p):
        return JSONResponse({"ok": True, "note": "frontend not built yet",
                             "api": ["/api/flags", "/api/health", "/api/runs"]})
    return FileResponse(p)


@app.get("/live")
def live_page():
    p = os.path.join(config.FRONTEND, "live.html")
    if not os.path.exists(p):
        raise HTTPException(404, "live page not built yet")
    return FileResponse(p)


@app.get("/components")
def components_page():
    p = os.path.join(config.FRONTEND, "components.html")
    if not os.path.exists(p):
        raise HTTPException(404, "components page not built yet")
    return FileResponse(p)


@app.get("/metrics")
def metrics_page():
    p = os.path.join(config.FRONTEND, "metrics.html")
    if not os.path.exists(p):
        raise HTTPException(404, "metrics page not built yet")
    return FileResponse(p)


@app.get("/stats")
def stats_page():
    p = os.path.join(config.FRONTEND, "stats.html")
    if not os.path.exists(p):
        raise HTTPException(404, "statistics page not built yet")
    return FileResponse(p)


@app.get("/runs")
def runs_page():
    p = os.path.join(config.FRONTEND, "runs.html")
    if not os.path.exists(p):
        raise HTTPException(404, "history page not built yet")
    return FileResponse(p)
