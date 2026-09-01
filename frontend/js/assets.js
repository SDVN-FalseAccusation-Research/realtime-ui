/* Load the P0 assets and expose vehicle poses over simulated time.
 *
 * The arrays are served pre-gzipped with Content-Encoding, so fetch() hands back raw
 * bytes that go straight into typed arrays — no parsing, no client-side gunzip. Layout is
 * node-major: index = node * T + t.
 *
 * Positions are NOT streamed from the simulator. The whole trace is known in advance, so
 * the browser interpolates locally against the playback clock and only *events* come over
 * the WebSocket. That keeps the live channel tiny and makes scrubbing possible.
 */
'use strict';

const Assets = {
  manifest: null, nodes: null, roads: null, buildings: null,
  x: null, y: null, heading: null,
  N: 0, T: 0, scale: 20, dt: 1,

  async load(onProgress = () => {}) {
    onProgress('manifest');
    this.manifest = await (await fetch('/assets/manifest.json')).json();
    const m = this.manifest.mobility;
    this.N = m.N; this.T = m.T; this.scale = m.xy_scale; this.dt = m.dt;
    Geo.setWorld(this.manifest.world.W, this.manifest.world.H);

    onProgress('nodes');
    this.nodes = await (await fetch('/assets/nodes.json')).json();

    onProgress('roads');
    this.roads = await (await fetch('/assets/roads.svg')).text();
    // Buildings are optional; a missing layer must not stop the map from loading.
    try {
      if (this.manifest.layers && this.manifest.layers.buildings) {
        this.buildings = await (await fetch('/assets/buildings.svg')).text();
      }
    } catch (e) { this.buildings = null; }

    onProgress('positions');
    const [bx, by, bh] = await Promise.all([
      fetch('/assets/pos_x.u16').then(r => r.arrayBuffer()),
      fetch('/assets/pos_y.u16').then(r => r.arrayBuffer()),
      fetch('/assets/heading.u8').then(r => r.arrayBuffer()),
    ]);
    this.x = new Uint16Array(bx);
    this.y = new Uint16Array(by);
    this.heading = new Uint8Array(bh);

    const want = this.N * this.T;
    if (this.x.length !== want || this.heading.length !== want) {
      throw new Error(`asset size mismatch: expected ${want} cells, got ` +
                      `${this.x.length}/${this.heading.length}. Re-run tools/build_assets.py`);
    }
    onProgress('ready');
    return this;
  },

  /** Insertion time of a vehicle; it must not be drawn before this. */
  t0(i) { return this.nodes.vehicles[i] ? this.nodes.vehicles[i].t0 : 0; },
  klass(i) { return this.nodes.vehicles[i] ? this.nodes.vehicles[i].class : 'passenger'; },

  /** Pose at an arbitrary (fractional) simulated second, linearly interpolated.
   *  Returns null while the vehicle has not yet been inserted. */
  pose(i, t) {
    if (i < 0 || i >= this.N) return null;
    if (t < this.t0(i)) return null;

    const T = this.T;
    const f = Math.max(0, Math.min(T - 1, t / this.dt));
    const i0 = Math.floor(f);
    const i1 = Math.min(T - 1, i0 + 1);
    const a = f - i0;
    const b0 = i * T + i0, b1 = i * T + i1;
    const s = this.scale;

    const x = (this.x[b0] + (this.x[b1] - this.x[b0]) * a) / s;
    const y = (this.y[b0] + (this.y[b1] - this.y[b0]) * a) / s;

    // Heading is an angle: interpolate the SHORT way round or a car crossing north
    // spins 359 degrees.
    const h0 = this.heading[b0] * 360 / 256;
    const h1 = this.heading[b1] * 360 / 256;
    let d = ((h1 - h0 + 540) % 360) - 180;
    return { x, y, heading: (h0 + d * a + 360) % 360 };
  },

  /** Vehicles within `range` metres of a point at time t.
   *
   *  Used to draw witness/reporter arcs. The simulator does NOT print reporter identities
   *  anywhere in stdout (checked: no cout site emits them), so these are the geometrically
   *  correct in-range neighbours rather than the exact set the simulator polled. The UI
   *  labels them as such — never as ground truth.
   */
  neighbours(i, t, range, limit = 24) {
    const me = this.pose(i, t);
    if (!me) return [];
    const out = [];
    for (let j = 0; j < this.N; j++) {
      if (j === i) continue;
      const p = this.pose(j, t);
      if (!p) continue;
      const d = Math.hypot(p.x - me.x, p.y - me.y);
      if (d <= range) out.push({ id: j, d });
    }
    out.sort((a, b) => a.d - b.d);
    return out.slice(0, limit);
  },
};
