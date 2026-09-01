/* Tweens, packet flights and transient overlays.
 *
 * Adapted from the gamified UI's fx.js — the quadratic-Bézier packet flight and the
 * central tween list are the parts worth keeping. What changed: it is driven by the
 * PLAYBACK CLOCK rather than wall time, and everything is a no-op when the clock is
 * scrubbing, so replaying a prefix rebuilds state without firing hundreds of animations.
 *
 * Endpoints are given as refs — {v:id} vehicle, {r:id} RSU, {c:id} controller — the same
 * convention the event contract uses on the wire, so a handler passes them straight
 * through.
 */
'use strict';

const Fx = {
  tweens: [],
  enabled: true,          // false while seeking: apply state, skip animation

  /** Called once per frame with the elapsed wall milliseconds. */
  tick(dtMs) {
    if (!this.tweens.length) return;
    const still = [];
    for (const tw of this.tweens) {
      tw.el += dtMs;
      const f = Math.min(1, tw.el / tw.dur);
      tw.fn(tw.ease ? tw.ease(f) : f);
      if (f < 1) still.push(tw);
      else if (tw.done) tw.done();
    }
    this.tweens = still;
  },

  tween(dur, fn, ease, done) {
    if (!this.enabled) { fn(1); if (done) done(); return; }
    this.tweens.push({ dur: Math.max(1, dur), el: 0, fn, ease, done });
  },

  clear() {
    this.tweens.length = 0;
    if (World.layers) {
      World.layers.fx.innerHTML = '';
      World.layers.packets.innerHTML = '';
    }
  },

  /** Resolve a wire ref to world coordinates. */
  pos(ref) {
    if (!ref) return null;
    if (ref.x !== undefined) return ref;
    if (ref.v !== undefined) {
      const v = World.vehicles[ref.v];
      return (v && v.visible) ? { x: v.x, y: v.y } : null;
    }
    if (ref.r !== undefined) return World.rsus[ref.r] || null;
    if (ref.c !== undefined) return World.controllers[ref.c] || null;
    return null;
  },

  /* ------------------------------------------------------------- packet flight -- */
  /** Fly a glyph from -> to along a bowed curve. `kind` selects the CSS class. */
  packet(from, to, kind = 'report', opts = {}) {
    if (!this.enabled) return;
    const a = this.pos(from), b = this.pos(to);
    if (!a || !b) return;

    const arc = opts.arc !== undefined ? opts.arc : 0.16;
    const dx = b.x - a.x, dy = b.y - a.y;
    const cx = (a.x + b.x) / 2 - dy * arc;
    const cy = (a.y + b.y) / 2 + dx * arc;

    const g = el('g', { class: 'pkt pkt-' + kind }, World.layers.packets);
    if (opts.trail !== false) {
      el('path', { class: 'pkt-trail',
                   d: `M${a.x},${a.y} Q${cx},${cy} ${b.x},${b.y}` }, World.layers.packets);
    }
    const dot = el('circle', { class: 'pkt-dot', r: 5 }, g);

    const dur = opts.dur || 900;
    this.tween(dur, (f) => {
      const u = 1 - f;
      const x = u * u * a.x + 2 * u * f * cx + f * f * b.x;
      const y = u * u * a.y + 2 * u * f * cy + f * f * b.y;
      dot.setAttribute('cx', x); dot.setAttribute('cy', y);
      g.setAttribute('opacity', f > 0.88 ? (1 - f) / 0.12 : 1);
    }, null, () => g.remove());
  },

  /** Fan out from one source to many targets, slightly staggered. */
  broadcast(from, targets, kind, opts = {}) {
    if (!this.enabled) return;
    const stagger = opts.stagger || 60;
    targets.forEach((t, i) => {
      setTimeout(() => this.packet(from, t, kind, opts), i * stagger);
    });
  },

  /** Expanding ring — used for verdicts and blockchain commits. */
  ring(ref, kind = 'info', opts = {}) {
    if (!this.enabled) return;
    const p = this.pos(ref);
    if (!p) return;
    const c = el('circle', { class: 'fx-ring fx-' + kind, cx: p.x, cy: p.y, r: 10 },
                 World.layers.fx);
    const to = opts.r || 220;
    this.tween(opts.dur || 900, (f) => {
      c.setAttribute('r', 10 + (to - 10) * f);
      c.setAttribute('opacity', 1 - f);
    }, null, () => c.remove());
  },

  /** Floating label above a node. */
  tag(ref, text, opts = {}) {
    if (!this.enabled) return;
    const p = this.pos(ref);
    if (!p) return;
    const g = el('g', { class: 'fx-tag fx-' + (opts.kind || 'info') }, World.layers.fx);
    const inner = el('g', { transform: `translate(${p.x},${p.y}) scale(${Render.iconScale},${-Render.iconScale})` }, g);
    const w = Math.max(30, text.length * 5.6);
    el('rect', { x: -w / 2, y: -42, width: w, height: 15, rx: 7.5 }, inner);
    el('text', { x: 0, y: -31.5, 'text-anchor': 'middle' }, inner).textContent = text;
    this.tween(opts.hold || 1800, (f) => {
      g.setAttribute('opacity', f < 0.8 ? 1 : (1 - f) / 0.2);
      inner.setAttribute('transform',
        `translate(${p.x},${p.y - f * 18}) scale(${Render.iconScale},${-Render.iconScale})`);
    }, null, () => g.remove());
  },

  /** RSU evaluation window: a ring that drains over `seconds` of DISPLAY time. */
  rsuWindow(rsuId, seconds) {
    const e = World.rsuEl(rsuId);
    if (!e) return;
    const ring = e.querySelector('.rsu-win');
    if (!ring) return;
    e.classList.add('windowing');
    const C = 2 * Math.PI * 15;
    ring.style.strokeDasharray = C;
    if (!this.enabled) { e.classList.remove('windowing'); return; }
    this.tween(Math.max(300, seconds * 1000), (f) => {
      ring.style.strokeDashoffset = C * f;
    }, null, () => { e.classList.remove('windowing'); ring.style.strokeDashoffset = 0; });
  },
};
