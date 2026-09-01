/* World geometry: infrastructure placement and the coordinate frame.
 *
 * THE RSU AND CONTROLLER POSITIONS ARE NOT IN ANY FILE. They are computed by the
 * simulator from numRsus / numControllers / map extent, so they are recomputed here with
 * the SAME formulas (topology.cc InstallMobility). Deriving rather than hardcoding is what
 * lets the UI follow a topology change — 56 RSUs today, 64 once the open bug is fixed —
 * with no edit.
 *
 * COORDINATE FRAME. Assets stay in native SUMO metres (y increases north). SVG y grows
 * downward, so the flip happens ONCE in the DOM:
 *
 *     <g id="world" transform="translate(0,H) scale(1,-1)">
 *
 * Everything inside then uses raw world coordinates and the simulator's formulas work
 * verbatim with no sign juggling. Each sprite carries an inner scale(1,-1) to cancel the
 * flip so text and badges stay upright.
 */
'use strict';

const Geo = {
  W: 3034.96, H: 2506.26,

  setWorld(w, h) { this.W = w; this.H = h; },

  /** RSUs: a near-square grid spanning the map (64 -> 8x8, 56 -> 8x7). */
  rsus(n) {
    const cols = Math.ceil(Math.sqrt(n));
    const rows = Math.ceil(n / cols);
    const out = [];
    // Loop to n, not cols*rows: for 56 the grid is 8x7=56 exactly, but e.g. 50 gives
    // 8x7=56 cells and only 50 RSUs actually exist. Drawing the extra 6 would invent
    // infrastructure that is not in the simulation.
    for (let i = 0; i < n; i++) {
      out.push({ id: i,
                 x: this.W * ((i % cols) + 0.5) / cols,
                 y: this.H * (Math.floor(i / cols) + 0.5) / rows });
    }
    return out;
  },

  /** Controllers: a band at 0.40*H spread across the centre third. */
  controllers(n) {
    const out = [];
    for (let i = 0; i < n; i++) {
      const frac = (n <= 1) ? 0.5 : (1 / 3) + (1 / 3) * i / (n - 1);
      out.push({ id: i, x: this.W * frac, y: this.H * 0.40 });
    }
    return out;
  },

  /** zone(r) = floor(r * numControllers / numRsus) — 56/4 gives 14 RSUs per zone. */
  zoneOfRsu(r, numRsus, numControllers) {
    return Math.floor(r * numControllers / numRsus);
  },

  /** Nearest RSU to a point — how the simulator picks a vehicle's serving RSU. */
  nearestRsu(x, y, rsus) {
    let best = null, bd = Infinity;
    for (const r of rsus) {
      const d = (r.x - x) ** 2 + (r.y - y) ** 2;
      if (d < bd) { bd = d; best = r; }
    }
    return best;
  },

  dist(ax, ay, bx, by) { return Math.hypot(ax - bx, ay - by); },
};
