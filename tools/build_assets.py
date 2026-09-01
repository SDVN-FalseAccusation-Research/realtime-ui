#!/usr/bin/env python3
"""P0 — turn the SUMO scenario into web-ready assets for the real-time UI.

Run once, offline, whenever the SUMO scenario is regenerated:

    python3 tools/build_assets.py

Inputs  (project-simulation/sumo-manhattan/)
    manhattan.net.xml    road network   -> roads.svg
    manhattan.fcd.xml    mobility       -> pos_x.u16 / pos_y.u16 / heading.u8 / nodes.json
    manhattan.tcl        ns-3 trace     -> used ONLY to verify the node-id mapping
    manhattan.poly.xml   buildings      -> buildings.svg   (optional layer)

Outputs (assets/, all gzipped where it helps)

Everything is stdlib. The FCD is 34 MB so it is streamed with iterparse, never DOM-parsed.

--------------------------------------------------------------------------------------
THE ONE ASSUMPTION THIS WHOLE UI RESTS ON
--------------------------------------------------------------------------------------
ns-3 vehicle index i == SUMO trace $node_(i) == the i-th vehicle to appear in the FCD.

`topology.cc::InstallMobility()` says: "The trace references node ids 0..n-1 (the vehicle
nodes, created first)", and traceExporter numbers nodes in order of first appearance.

So when ns-3 prints `[TARGET] attacker=97`, the UI draws node 97 from these assets. If the
mapping were off by one we would highlight the wrong car for an entire demo and nobody
would notice. Therefore verify_mapping() re-derives it and asserts every node's first
position against manhattan.tcl. A mismatch ABORTS the build.
"""

import gzip
import json
import os
import re
import struct
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
UI_ROOT = os.path.dirname(HERE)
SUMO = os.path.normpath(os.path.join(
    UI_ROOT, "..", "project-simulation", "sumo-manhattan"))
OUT = os.path.join(UI_ROOT, "assets")

NET = os.path.join(SUMO, "manhattan.net.xml")
FCD = os.path.join(SUMO, "manhattan.fcd.xml")
TCL = os.path.join(SUMO, "manhattan.tcl")
POLY = os.path.join(SUMO, "manhattan.poly.xml")

# Positions are stored as uint16 at 1/XY_SCALE metre resolution.
# Measured extent is x<=2739 m, y<=2507 m, so *20 (0.05 m) peaks at ~54.8k < 65535.
XY_SCALE = 20
ABSENT = 0xFFFF          # sentinel, never emitted (we forward-fill instead) but reserved

# Road classes collapsed into render tiers, widest/most important last so they paint on top.
TIERS = [
    ("residential", {"highway.residential", "highway.living_street", "highway.service"}),
    ("tertiary",    {"highway.tertiary", "highway.tertiary_link", "highway.unclassified"}),
    ("secondary",   {"highway.secondary", "highway.secondary_link"}),
    ("primary",     {"highway.primary", "highway.primary_link"}),
    ("motorway",    {"highway.motorway", "highway.motorway_link"}),
]
LANE_WIDTH_M = 3.2       # SUMO's default lane width; stroke is drawn in world metres


def log(msg):
    print(msg, flush=True)


def write_gz(name, data: bytes):
    path = os.path.join(OUT, name + ".gz")
    with gzip.open(path, "wb", compresslevel=6) as fh:
        fh.write(data)
    return len(data), os.path.getsize(path)


def write_plain(name, text: str):
    path = os.path.join(OUT, name)
    with open(path, "w") as fh:
        fh.write(text)
    return os.path.getsize(path)


# ----------------------------------------------------------------------------------
# Roads
# ----------------------------------------------------------------------------------
def parse_shape(s):
    """'x,y x,y ...' -> [(x, y), ...]"""
    pts = []
    for tok in s.split():
        x, _, y = tok.partition(",")
        pts.append((float(x), float(y)))
    return pts


def build_roads():
    """net.xml -> an SVG fragment: junction polygons under class-tiered road polylines.

    Emits ONE path per edge (not per lane) and sets stroke-width in world metres from the
    lane count, so roads scale correctly with zoom and read as a city rather than a mesh.
    Junction polygons are what stop intersections looking like crossing wires.
    """
    tiers = defaultdict(list)     # tier -> [(width_m, points)]
    junctions = []
    classes = Counter()
    fallback = 0
    boundary = None

    for _ev, el in ET.iterparse(NET, events=("end",)):
        if el.tag == "location" and boundary is None:
            boundary = el.get("convBoundary")

        elif el.tag == "edge":
            eid = el.get("id", "")
            if el.get("function") == "internal" or eid.startswith(":"):
                el.clear()
                continue
            lanes = el.findall("lane")
            shape = el.get("shape")
            if not shape and lanes:          # 37 edges carry no edge-level shape
                shape = lanes[0].get("shape")
                fallback += 1
            if shape:
                etype = el.get("type", "")
                classes[etype] += 1
                tier = next((t for t, s in TIERS if etype in s), "residential")
                width = max(1, len(lanes)) * LANE_WIDTH_M
                tiers[tier].append((width, parse_shape(shape)))
            el.clear()

        elif el.tag == "junction":
            if el.get("function") != "internal":
                shape = el.get("shape")
                if shape:
                    pts = parse_shape(shape)
                    if len(pts) >= 3:
                        junctions.append(pts)
            el.clear()

    def poly(points):
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)

    parts = ['<g id="roads">']
    parts.append('<g class="junctions">')
    for pts in junctions:
        parts.append(f'<polygon points="{poly(pts)}"/>')
    parts.append("</g>")

    total_pts = sum(len(p) for p in junctions)
    for tier, _cls in TIERS:                      # paint order: minor roads first
        items = tiers.get(tier)
        if not items:
            continue
        parts.append(f'<g class="tier {tier}">')
        for width, pts in items:
            total_pts += len(pts)
            parts.append(f'<polyline stroke-width="{width:.1f}" points="{poly(pts)}"/>')
        parts.append("</g>")
    parts.append("</g>")

    svg = "\n".join(parts)
    raw, gz = write_gz("roads.svg", svg.encode())
    log(f"  roads.svg      {sum(len(v) for v in tiers.values()):5d} edges, "
        f"{len(junctions):4d} junctions, {total_pts:6d} pts   {raw/1024:6.1f} KB -> {gz/1024:5.1f} KB gz")
    if fallback:
        log(f"                 ({fallback} edges used lane[0] shape as fallback)")
    return {"convBoundary": boundary, "edge_classes": dict(classes)}


def build_buildings():
    """poly.xml -> a low-opacity building layer. Optional; skipped if the file is absent."""
    if not os.path.exists(POLY):
        log("  buildings.svg  (skipped — manhattan.poly.xml not found)")
        return False
    parts = ['<g id="buildings">']
    n = pts = 0
    for _ev, el in ET.iterparse(POLY, events=("end",)):
        if el.tag == "poly":
            shape = el.get("shape")
            # Buildings only; SUMO also emits amenity/leisure polys we do not need.
            if shape and "building" in (el.get("type") or ""):
                p = parse_shape(shape)
                if len(p) >= 3:
                    parts.append('<polygon points="%s"/>' %
                                 " ".join(f"{x:.1f},{y:.1f}" for x, y in p))
                    n += 1
                    pts += len(p)
            el.clear()
    parts.append("</g>")
    raw, gz = write_gz("buildings.svg", "\n".join(parts).encode())
    log(f"  buildings.svg  {n:5d} polys, {pts:6d} pts               {raw/1024:6.1f} KB -> {gz/1024:5.1f} KB gz")
    return True


# ----------------------------------------------------------------------------------
# Positions
# ----------------------------------------------------------------------------------
def build_positions():
    """fcd.xml -> dense [node][t] arrays of x, y and heading, plus nodes.json.

    Node ids are assigned in order of first appearance, which is exactly what
    traceExporter does when it writes manhattan.tcl. verify_mapping() proves it.

    Vehicle presence is a pure prefix (every vehicle, once inserted, is present to the
    end), so a dense rectangle is both simplest and barely wasteful. Cells before a
    vehicle's insertion are forward-filled with its first real sample so the arrays never
    contain garbage; the frontend hides a node while t < t0[i].
    """
    order = {}          # sumo id -> node index
    meta = {}           # node index -> {sumo, class, first_t, first_xy}
    frames = []         # [t][node] -> (x, y, angle)
    times = []

    for _ev, el in ET.iterparse(FCD, events=("end",)):
        if el.tag != "timestep":
            continue
        t = float(el.get("time"))
        row = {}
        for v in el.findall("vehicle"):
            sid = v.get("id")
            if sid not in order:
                idx = len(order)
                order[sid] = idx
                meta[idx] = {"sumo": sid, "class": v.get("type", "passenger"),
                             "first_t": t}
            x = float(v.get("x")); y = float(v.get("y"))
            row[order[sid]] = (x, y, float(v.get("angle", 0.0)))
            if "first_xy" not in meta[order[sid]]:
                meta[order[sid]]["first_xy"] = (x, y)
        frames.append(row)
        times.append(t)
        el.clear()

    N = len(order)
    T = len(frames)
    log(f"  fcd            {N} vehicles x {T} timesteps "
        f"({sum(len(f) for f in frames)} records, {100*sum(len(f) for f in frames)/(N*T):.1f}% dense)")

    xs = bytearray(N * T * 2)
    ys = bytearray(N * T * 2)
    hs = bytearray(N * T)
    maxx = maxy = 0.0

    for i in range(N):
        first = meta[i]["first_xy"]
        last = (first[0], first[1], 0.0)
        for ti in range(T):
            cur = frames[ti].get(i)
            if cur is None:
                # before insertion (or, defensively, a gap): hold the last known pose
                x, y, a = last
            else:
                x, y, a = cur
                last = cur
            maxx = max(maxx, x); maxy = max(maxy, y)
            off = i * T + ti
            struct.pack_into("<H", xs, off * 2, max(0, min(0xFFFE, round(x * XY_SCALE))))
            struct.pack_into("<H", ys, off * 2, max(0, min(0xFFFE, round(y * XY_SCALE))))
            hs[off] = round(a * 256.0 / 360.0) % 256

    if maxx * XY_SCALE >= 0xFFFE or maxy * XY_SCALE >= 0xFFFE:
        sys.exit(f"FATAL: coordinate overflow at scale {XY_SCALE} "
                 f"(max {maxx:.1f},{maxy:.1f} m). Lower XY_SCALE.")

    rx, gx = write_gz("pos_x.u16", bytes(xs))
    ry, gy = write_gz("pos_y.u16", bytes(ys))
    rh, gh = write_gz("heading.u8", bytes(hs))
    log(f"  pos_x.u16      {rx/1024:6.1f} KB -> {gx/1024:5.1f} KB gz")
    log(f"  pos_y.u16      {ry/1024:6.1f} KB -> {gy/1024:5.1f} KB gz")
    log(f"  heading.u8     {rh/1024:6.1f} KB -> {gh/1024:5.1f} KB gz")

    # _fx/_fy are carried for verify_mapping() and stripped before nodes.json is written,
    # so the 34 MB FCD is parsed exactly once.
    vehicles = [{"i": i, "sumo": meta[i]["sumo"], "class": meta[i]["class"],
                 "t0": meta[i]["first_t"],
                 "_fx": meta[i]["first_xy"][0], "_fy": meta[i]["first_xy"][1]}
                for i in range(N)]
    return {"N": N, "T": T, "dt": (times[1] - times[0]) if T > 1 else 1.0,
            "t_start": times[0], "t_end": times[-1],
            "xy_scale": XY_SCALE, "vehicles": vehicles,
            "classes": dict(Counter(v["class"] for v in vehicles))}


# ----------------------------------------------------------------------------------
# The mapping guard
# ----------------------------------------------------------------------------------
def verify_mapping(pos):
    """Assert FCD-appearance-order == $node_(i) in manhattan.tcl, for every node.

    This is the highest-value 30 lines in P0. If the scenario is ever rebuilt and the
    ordering shifts, the build fails here instead of the demo mis-colouring the attacker.
    """
    if not os.path.exists(TCL):
        log("  !! manhattan.tcl absent — MAPPING NOT VERIFIED")
        return False
    # NOTE: the .tcl interleaves each node's init block with that node's movement lines
    # ($node_(0) set X_/Y_/Z_, then all of node 0's "$ns_ at ..." lines, then node 1's
    # init, ...). So scan the WHOLE file — an early break at the first "$ns_" captures
    # only node 0. (That bug made this guard fire on 199 "missing in tcl" nodes.)
    init = {}
    pat = re.compile(r"^\$node_\((\d+)\) set ([XY])_ ([-\d.]+)")
    with open(TCL) as fh:
        for line in fh:
            if not line.startswith("$node_"):
                continue
            m = pat.match(line)
            if m:
                idx, axis, val = int(m.group(1)), m.group(2), float(m.group(3))
                init.setdefault(idx, {})[axis] = val

    bad, worst = [], 0.0
    for v in pos["vehicles"]:
        i = v["i"]
        if i not in init:
            bad.append((i, "missing in tcl"))
            continue
        # first_xy was captured during the FCD pass; recompute from the arrays is overkill
        dx = abs(init[i]["X"] - v["_fx"])
        dy = abs(init[i]["Y"] - v["_fy"])
        worst = max(worst, dx, dy)
        if dx > 0.05 or dy > 0.05:
            bad.append((i, f"tcl=({init[i]['X']},{init[i]['Y']}) fcd=({v['_fx']},{v['_fy']})"))

    if bad:
        log(f"  !! MAPPING MISMATCH on {len(bad)} node(s):")
        for i, why in bad[:10]:
            log(f"       node {i}: {why}")
        sys.exit("FATAL: FCD appearance order does not match $node_(i) in manhattan.tcl.\n"
                 "       manifest.json/nodes.json were NOT written, so the UI cannot load\n"
                 "       these assets. Any .gz already on disk is stale — do not trust it.")
    log(f"  mapping        {len(pos['vehicles'])}/{len(init)} nodes match manhattan.tcl "
        f"(max error {worst:.3f} m)  OK")
    return True


def main():
    os.makedirs(OUT, exist_ok=True)
    for f in (NET, FCD):
        if not os.path.exists(f):
            sys.exit(f"FATAL: missing input {f}\n"
                     f"       regenerate with sumo-manhattan/build_scenario.sh")

    log(f"sources: {SUMO}")
    log(f"output : {OUT}\n")

    log("[1/4] road network")
    net = build_roads()

    log("[2/4] buildings")
    have_buildings = build_buildings()

    log("[3/4] mobility")
    pos = build_positions()

    log("[4/4] verifying the node-id mapping")
    verify_mapping(pos)
    for v in pos["vehicles"]:
        del v["_fx"], v["_fy"]

    bx0, by0, bx1, by1 = [float(s) for s in net["convBoundary"].split(",")]
    manifest = {
        "generated_from": {"net": os.path.basename(NET), "fcd": os.path.basename(FCD)},
        "world": {"W": bx1 - bx0, "H": by1 - by0, "convBoundary": net["convBoundary"]},
        "mobility": {k: pos[k] for k in
                     ("N", "T", "dt", "t_start", "t_end", "xy_scale", "classes")},
        "layers": {"roads": "roads.svg.gz",
                   "buildings": "buildings.svg.gz" if have_buildings else None},
        "arrays": {"x": "pos_x.u16.gz", "y": "pos_y.u16.gz", "heading": "heading.u8.gz",
                   "encoding": "raw", "layout": "node-major: index = node*T + t"},
        "edge_classes": net["edge_classes"],
    }
    write_plain("manifest.json", json.dumps(manifest, indent=2))
    write_plain("nodes.json", json.dumps(
        {"N": pos["N"], "T": pos["T"], "vehicles": pos["vehicles"]}))

    log(f"\ndone. world {manifest['world']['W']:.2f} x {manifest['world']['H']:.2f} m, "
        f"{pos['N']} vehicles, {pos['T']} timesteps")
    log("     " + ", ".join(f"{k}={v}" for k, v in sorted(pos["classes"].items())))


if __name__ == "__main__":
    main()
