/* The SVG scene: layers, camera, node registry, and the per-frame pose update.
 *
 * State changes are made by setting ONE data-* attribute and letting CSS colour it, so an
 * event handler never touches geometry or style directly. That keeps the dispatch table
 * declarative and makes the whole world reconstructible by replaying events — which is
 * what allows scrubbing backwards.
 */
'use strict';

const World = {
  svg: null, camera: null, world: null, layers: {},
  _cam: null,             // in-flight follow move; see tickCamera()
  vehicles: [], rsus: [], controllers: [],
  topology: { vehicles: 0, rsus: 0, controllers: 0 },
  view: { x: 0, y: 0, k: 1 },

  init(svgEl) {
    this.svg = svgEl;
    const W = Geo.W, H = Geo.H;
    svgEl.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svgEl.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    svgEl.innerHTML = '';

    this.camera = el('g', { id: 'camera' }, svgEl);
    // ONE flip, here. Everything inside uses raw world metres (see geometry.js).
    this.world = el('g', { id: 'world', transform: `translate(0,${H}) scale(1,-1)` },
                    this.camera);

    for (const name of ['buildings', 'roads', 'backhaul', 'range', 'infra', 'fx',
                        'vehicles', 'packets', 'overlay']) {
      this.layers[name] = el('g', { class: 'layer', id: 'layer-' + name }, this.world);
    }
    this._wireCamera();
    return this;
  },

  /* ------------------------------------------------------------------- static -- */
  drawRoads(svgFragment) {
    this.layers.roads.innerHTML = svgFragment;
  },

  buildTopology(topo) {
    this.topology = topo;
    const { vehicles: nv, rsus: nr, controllers: nc } = topo;

    this.layers.infra.innerHTML = '';
    this.layers.vehicles.innerHTML = '';
    this.layers.backhaul.innerHTML = '';

    this.rsus = Geo.rsus(nr);
    this.controllers = Geo.controllers(nc);

    // backhaul first so it sits under the infrastructure
    for (const r of this.rsus) {
      const z = Geo.zoneOfRsu(r.id, nr, nc);
      const c = this.controllers[Math.min(z, this.controllers.length - 1)];
      if (c) r.line = Render.backhaul(this.layers.backhaul, r, c);
      r.el = Render.rsu(this.layers.infra, r.id, z);
      r.el.setAttribute('transform', `translate(${r.x},${r.y})`);
    }
    for (const c of this.controllers) {
      c.el = Render.controller(this.layers.infra, c.id, c.id);
      c.el.setAttribute('transform', `translate(${c.x},${c.y})`);
    }

    this.vehicles = [];
    for (let i = 0; i < nv; i++) {
      const g = Render.vehicle(this.layers.vehicles, i, Assets.klass(i));
      g.style.display = 'none';       // hidden until its insertion time
      this.vehicles.push({ id: i, el: g, visible: false });
    }
    this.applyIconScale();
  },

  /* -------------------------------------------------------------- per frame ---- */
  /** Move every vehicle to its pose at simulated time t. The only hot loop. */
  update(t) {
    const k = Render.iconScale;
    for (let i = 0; i < this.vehicles.length; i++) {
      const v = this.vehicles[i];
      const p = Assets.pose(i, t);
      if (!p) {
        if (v.visible) { v.el.style.display = 'none'; v.visible = false; }
        continue;
      }
      if (!v.visible) { v.el.style.display = ''; v.visible = true; }
      v.el.setAttribute('transform', `translate(${p.x.toFixed(1)},${p.y.toFixed(1)})`);
      // scale(k,-k) cancels the world flip; rotate() then matches SUMO's convention
      v.el.firstChild.setAttribute('transform', `scale(${k},${-k})`);
      v.el.firstChild.firstChild.setAttribute('transform', `rotate(${p.heading.toFixed(0)})`);
      v.x = p.x; v.y = p.y;
    }
  },

  applyIconScale() {
    const k = Render.iconScale;
    for (const r of this.rsus)
      r.el.firstChild.setAttribute('transform', `scale(${k},${-k})`);
    for (const c of this.controllers)
      c.el.firstChild.setAttribute('transform', `scale(${k},${-k})`);
  },

  /* ----------------------------------------------------------- state setters --- */
  vehicleEl(i) { return this.vehicles[i] ? this.vehicles[i].el : null; },
  rsuEl(i) { return this.rsus[i] ? this.rsus[i].el : null; },
  ctrlEl(i) { return this.controllers[i] ? this.controllers[i].el : null; },

  setState(i, state) { const e = this.vehicleEl(i); if (e) e.setAttribute('data-state', state); },
  getState(i) { const e = this.vehicleEl(i); return e ? e.getAttribute('data-state') : null; },

  mark(i, flag, on = true) {
    const e = this.vehicleEl(i);
    if (e) e.classList.toggle('mark-' + flag, !!on);
  },

  setTrust(i, trust) {
    const e = this.vehicleEl(i);
    if (!e) return;
    const fill = e.querySelector('.rep-fill');
    if (fill) fill.setAttribute('width', (18 * Math.max(0, Math.min(1, trust))).toFixed(2));
  },

  showRange(i, on) { this.mark(i, 'range', on); },

  setRsuState(i, state) { const e = this.rsuEl(i); if (e) e.setAttribute('data-state', state); },
  /** The RSU's report buffer pill.
   *
   * `split` is the AUTHORITATIVE true/false breakdown from _decisions.csv, supplied once
   * the verdict arrives. Until then the count is the number of report packets seen to land,
   * which is derived from geometry — the simulator never emits reporter identities. The two
   * numbers can differ, and when they do the CSV wins.
   */
  setRsuBuffer(i, n, split = null) {
    const e = this.rsuEl(i);
    if (!e) return;
    const t = e.querySelector('.rsu-buf text');
    if (t) t.textContent = split ? `${split.t}T/${split.f}F` : n;
    e.classList.toggle('has-buf', (split ? split.t + split.f : n) > 0);
    e.classList.toggle('buf-final', !!split);
  },
  setCtrlState(i, state) { const e = this.ctrlEl(i); if (e) e.setAttribute('data-state', state); },

  /** Clear all transient per-event highlighting. Called before each new accusation and
   *  on seek, so the world can be rebuilt from a replayed prefix. */
  clearEventMarks() {
    this.layers.fx.innerHTML = '';
    this.layers.packets.innerHTML = '';
    for (const v of this.vehicles) {
      v.el.classList.remove('mark-accuser', 'mark-victim', 'mark-reporter', 'mark-range');
    }
    for (const r of this.rsus) { r.el.classList.remove('serving'); this.setRsuBuffer(r.id, 0); }
    for (const c of this.controllers) c.el.classList.remove('deciding');
  },

  /** Full reset used on seek: every vehicle back to its baseline role. */
  resetRoles(attackers = [], misbehavers = []) {
    for (const v of this.vehicles) v.el.setAttribute('data-state', 'honest');
    for (const i of attackers) this.setState(i, 'attacker');
    for (const i of misbehavers) this.setState(i, 'misbehaver');
    for (const r of this.rsus) this.setRsuState(r.id, 'active');
    for (const c of this.controllers) this.setCtrlState(c.id, 'active');
    this.clearEventMarks();
  },

  /* ----------------------------------------------------------------- camera ---- */
  _applyCamera() {
    const { x, y, k } = this.view;
    this.camera.setAttribute('transform', `translate(${x},${y}) scale(${k})`);
    // Counter-scale the icons so they keep a constant SCREEN size as we zoom.
    Render.setIconScale(2.3 / Math.sqrt(k));
    this.applyIconScale();
  },

  _wireCamera() {
    const svg = this.svg;
    let drag = null;
    // Touching the camera by hand cancels any follow move in flight — the presenter's
    // input must win, not fight a tween.
    svg.addEventListener('mousedown', (e) => { this._cam = null;
                                               drag = { x: e.clientX, y: e.clientY,
                                                        vx: this.view.x, vy: this.view.y }; });
    window.addEventListener('mouseup', () => { drag = null; });
    window.addEventListener('mousemove', (e) => {
      if (!drag) return;
      const r = svg.getBoundingClientRect();
      const s = Geo.W / r.width;                 // screen px -> world units
      this.view.x = drag.vx + (e.clientX - drag.x) * s;
      this.view.y = drag.vy + (e.clientY - drag.y) * s;
      this._applyCamera();
    });
    svg.addEventListener('wheel', (e) => {
      e.preventDefault();
      this._cam = null;
      const r = svg.getBoundingClientRect();
      const s = Geo.W / r.width;
      const mx = (e.clientX - r.left) * s, my = (e.clientY - r.top) * s;
      const f = e.deltaY < 0 ? 1.15 : 1 / 1.15;
      const k = Math.max(1, Math.min(14, this.view.k * f));
      const eff = k / this.view.k;
      // keep the point under the cursor fixed
      this.view.x = mx - (mx - this.view.x) * eff;
      this.view.y = my - (my - this.view.y) * eff;
      this.view.k = k;
      this._applyCamera();
    }, { passive: false });
  },

  resetView() { this._cam = null; this.view = { x: 0, y: 0, k: 1 }; this._applyCamera(); },

  /* ----------------------------------------------------------- follow camera --- */
  /* focusOn USED TO SNAP, and that was the second cause of the "everything blinks"
   * report. Measured on an imported p80 run at 1x: each accusation moved the camera 6492
   * world units — more than twice the width of the 3035 m map — and changed zoom by 1.73x,
   * between two consecutive frames. Nothing is animating; the scene is simply different,
   * which the eye reads as a flash. At 5x or on a dense run it fires about once a second.
   *
   * Three things fix it, in order of how much they help:
   *   1. DON'T MOVE IF THE ACTION IS ALREADY ON SCREEN. Most accusations at k=1 need no
   *      camera move at all, and a move that was never needed is pure flash.
   *   2. KEEP THE ZOOM unless the target genuinely does not fit. Zoom changes are the most
   *      disorienting part, and re-scaling every icon with them is the most expensive.
   *   3. TWEEN what is left, so a required move reads as a pan rather than a cut.
   */

  /** Is this world point comfortably inside the current view? `m` is a screen-space margin
   *  so a target near the edge still triggers a move. */
  _onScreen(px, py, m = 140) {
    const sx = this.view.x + px * this.view.k;
    const sy = this.view.y + (Geo.H - py) * this.view.k;
    return sx >= m && sx <= Geo.W - m && sy >= m && sy <= Geo.H - m;
  },

  /** Ease the camera to (cx,cy) at zoom k. Advanced by tickCamera() from the frame loop.
   *
   * DURATION SCALES WITH DISTANCE. A fixed duration gives a short hop and a cross-map haul
   * the same time, so the haul still moves in visible jerks — measured at 1028 px in one
   * frame with a flat 620 ms. Tying time to distance keeps the per-frame delta roughly
   * constant no matter how far the action jumped. */
  _flyTo(cx, cy, k, ms = 0) {
    // The camera wraps the flipped world, so it works in screen-space where y grows
    // downward: a world point (cx,cy) sits at (cx, H-cy) before the camera applies.
    // Forgetting the flip here parks the view on the mirror image of the action.
    const to = { k, x: Geo.W / 2 - cx * k, y: Geo.H / 2 - (Geo.H - cy) * k };
    if (!ms) {
      const d = Math.abs(to.x - this.view.x) + Math.abs(to.y - this.view.y);
      ms = Math.max(380, Math.min(1100, 380 + d * 0.09));
    }
    this._cam = { from: { ...this.view }, to, t0: performance.now(), ms };
  },

  /** Called every frame. Cheap no-op when no move is in flight. */
  tickCamera(now) {
    const c = this._cam;
    if (!c) return;
    const p = Math.min(1, (now - c.t0) / c.ms);
    const e = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;   // easeInOutQuad
    this.view.k = c.from.k + (c.to.k - c.from.k) * e;
    this.view.x = c.from.x + (c.to.x - c.from.x) * e;
    this.view.y = c.from.y + (c.to.y - c.from.y) * e;
    this._applyCamera();
    if (p >= 1) this._cam = null;
  },

  /** Frame a set of world points — used to follow the current accusation. */
  focusOn(points, pad = 420) {
    if (!points.length) return;
    const xs = points.map(p => p.x), ys = points.map(p => p.y);
    const minx = Math.min(...xs) - pad, maxx = Math.max(...xs) + pad;
    const miny = Math.min(...ys) - pad, maxy = Math.max(...ys) + pad;

    // (1) already watching it? then there is nothing to show that is not already shown.
    if (points.every(q => this._onScreen(q.x, q.y))) return;

    // (2) keep the current zoom if the target fits in it; only tighten when it does not.
    const fit = Math.max(1, Math.min(6, Math.min(Geo.W / (maxx - minx),
                                                 Geo.H / (maxy - miny))));
    const k = this.view.k <= fit ? this.view.k : fit;

    // (3) tween the rest.
    this._flyTo((minx + maxx) / 2, (miny + maxy) / 2, k);
  },
};
