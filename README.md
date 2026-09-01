# realtime-ui — live SDVN false-accusation demonstrator

Browser UI that configures and launches a **real** ns-3 simulation, streams its events live
onto the Manhattan map, and replays past runs.

Design spec: [`../new-task/UI_DESIGN.md`](../new-task/UI_DESIGN.md)

## Layout

```
tools/     P0 asset pipeline (stdlib only, run offline)
assets/    generated web assets — committed, see .gitignore for why
backend/   FastAPI: flag whitelist, process runner, stdout parser, WebSocket hub
frontend/  vanilla JS, no build step
tests/     golden-log fixtures + parser/validator tests
```

## P0 — build the assets

```bash
python3 tools/build_assets.py
```

Reads `../project-simulation/sumo-manhattan/{manhattan.net.xml,manhattan.fcd.xml,manhattan.tcl}`
and writes `assets/`. Takes <1 s.

**It verifies the node-id mapping and aborts if it is wrong.** ns-3 vehicle index `i` must equal
SUMO trace `$node_(i)` must equal the i-th vehicle to appear in the FCD — that identity is what
lets the UI draw `[TARGET] attacker=97` as the correct car. Current status:

```
mapping  200/200 nodes match manhattan.tcl (max error 0.000 m)  OK
```

If the SUMO scenario is ever regenerated, re-run this. A failure here is a real problem, not a
nuisance — do not bypass it.

### Assets produced (~1.07 MB gzipped total)

| File | Contents |
|---|---|
| `roads.svg.gz` | 881 edges + 476 junction polygons, grouped into 5 render tiers, stroke width in world metres from lane count |
| `buildings.svg.gz` | 4217 building polygons (optional layer) |
| `pos_x.u16.gz` / `pos_y.u16.gz` | dense `[node][t]` uint16, 0.05 m resolution |
| `heading.u8.gz` | dense `[node][t]` uint8, 1.41° resolution |
| `nodes.json` | per node: SUMO id, vehicle class, insertion time `t0` |
| `manifest.json` | world extent, array layout, class histogram |

Arrays are node-major: `index = node * T + t`, with `N=200`, `T=1200`, `dt=1 s`, `t=0..1199`.
Cells before a vehicle's `t0` are forward-filled with its first real sample (never garbage); the
frontend hides a node while `t < t0`.

### Coordinate frame

Assets stay in **native SUMO metres** — no Y flip in the data. Flip once in the DOM:

```html
<svg viewBox="0 0 3034.96 2506.26">
  <g id="camera">                                        <!-- pan/zoom -->
    <g id="world" transform="translate(0,2506.26) scale(1,-1)">
```

so `topology.cc`'s RSU/controller formulas are used verbatim with no sign juggling. Each sprite
carries `translate(x,y) scale(1,-1) rotate(heading)` — the inner flip cancels the world flip so
badges stay upright, and SUMO's heading convention (0 = north, clockwise) then works directly.
