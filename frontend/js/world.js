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
  setRsuBuffer(i, n) {
    const e = this.rsuEl(i);
    if (!e) return;
    const t = e.querySelector('.rsu-buf text');
    if (t) t.textContent = n;
    e.classList.toggle('has-buf', n > 0);
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
    svg.addEventListener('mousedown', (e) => { drag = { x: e.clientX, y: e.clientY,
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

  resetView() { this.view = { x: 0, y: 0, k: 1 }; this._applyCamera(); },

  /** Frame a set of world points — used to follow the current accusation. */
  focusOn(points, pad = 420) {
    if (!points.length) return;
    const xs = points.map(p => p.x), ys = points.map(p => p.y);
    const minx = Math.min(...xs) - pad, maxx = Math.max(...xs) + pad;
    const miny = Math.min(...ys) - pad, maxy = Math.max(...ys) + pad;
    const k = Math.max(1, Math.min(6, Math.min(Geo.W / (maxx - minx), Geo.H / (maxy - miny))));
    const cx = (minx + maxx) / 2, cy = (miny + maxy) / 2;
    this.view.k = k;
    // The camera wraps the flipped world, so it works in screen-space where y grows
    // downward: a world point (cx,cy) sits at (cx, H-cy) before the camera applies.
    // Forgetting the flip here parks the view on the mirror image of the action.
    this.view.x = Geo.W / 2 - cx * k;
    this.view.y = Geo.H / 2 - (Geo.H - cy) * k;
    this._applyCamera();
  },
};
