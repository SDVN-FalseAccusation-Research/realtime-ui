/* SVG sprite factories.
 *
 * SCALE IS THE WHOLE PROBLEM. The map is 3035 x 2506 metres. A real car is 4.5 m — about
 * one and a half pixels on a projector, i.e. invisible. So icons are drawn at a FIXED
 * SCREEN SIZE and counter-scaled against zoom (Render.setIconScale), which works out to
 * roughly 8-12x exaggeration at the default view. That is standard for traffic
 * visualisation; the alternative is an empty screen.
 *
 * Every node type is also distinct BY SHAPE, because projectors wash out colour:
 *     vehicle    tapered body, pointed at the front
 *     truck      longer, squared off
 *     bus        longest, flat ends
 *     motorcycle small lozenge
 *     RSU        mast with signal arcs
 *     controller shield hexagon
 *
 * TRANSFORM NESTING (see geometry.js for why the world is flipped):
 *     translate(x, y)      world metres
 *       scale(k, -k)       cancels the world flip and applies the icon scale, so the
 *                          local frame is screen-like (y down) and text stays upright
 *         rotate(heading)  SUMO convention: 0 = north, clockwise. Shapes point -y.
 */
'use strict';

const SVGNS = 'http://www.w3.org/2000/svg';

function el(tag, attrs = {}, parent = null) {
  const n = document.createElementNS(SVGNS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(n);
  return n;
}

const Render = {
  iconScale: 2.3,

  setIconScale(k) {
    this.iconScale = k;
    document.documentElement.style.setProperty('--icon-k', k);
  },

  /* ---------------------------------------------------------------- vehicles --- */
  vehicleShape(g, klass) {
    // All shapes point toward -y (north at heading 0) and are centred on the origin.
    if (klass === 'bus') {
      el('rect', { class: 'body', x: -4.5, y: -13, width: 9, height: 26, rx: 1.5 }, g);
      el('rect', { class: 'glass', x: -3, y: -11.5, width: 6, height: 4, rx: 1 }, g);
    } else if (klass === 'truck') {
      el('rect', { class: 'body', x: -4.5, y: -11, width: 9, height: 22, rx: 1.5 }, g);
      el('rect', { class: 'cab', x: -4.5, y: -11, width: 9, height: 7, rx: 1.5 }, g);
    } else if (klass === 'motorcycle') {
      el('ellipse', { class: 'body', cx: 0, cy: 0, rx: 2.4, ry: 6 }, g);
    } else {
      // passenger car: tapered nose so heading reads at a glance
      el('path', { class: 'body',
                   d: 'M0,-9 L4,-4.5 L4,7.5 Q4,9 2.5,9 L-2.5,9 Q-4,9 -4,7.5 L-4,-4.5 Z' }, g);
      el('rect', { class: 'glass', x: -2.6, y: -4, width: 5.2, height: 4.2, rx: 1 }, g);
    }
  },

  vehicle(layer, id, klass) {
    const g = el('g', { class: 'veh', 'data-id': id, 'data-class': klass,
                        'data-state': 'honest' }, layer);
    const s = el('g', { class: 'veh-scale' }, g);
    const body = el('g', { class: 'veh-body' }, s);
    this.vehicleShape(body, klass);

    // comm-range circle, shown only for the focused actors
    el('circle', { class: 'veh-range', cx: 0, cy: 0, r: 300 }, g);
    // highlight ring for accuser/victim
    el('circle', { class: 'veh-ring', cx: 0, cy: 0, r: 16 }, s);

    // badge stays upright (it lives inside veh-scale, which already cancelled the flip)
    const badge = el('g', { class: 'veh-badge' }, s);
    el('text', { class: 'veh-label', x: 0, y: -14, 'text-anchor': 'middle' }, badge)
      .textContent = 'V' + id;
    // trust bar: width is set from the reputation, with a tick at the blacklist threshold
    el('rect', { class: 'rep-track', x: -9, y: 12, width: 18, height: 2.4, rx: 1.2 }, badge);
    el('rect', { class: 'rep-fill', x: -9, y: 12, width: 18 * 0.7, height: 2.4, rx: 1.2 }, badge);
    return g;
  },

  /* ------------------------------------------------------------------- RSUs ---- */
  rsu(layer, id, zone) {
    const g = el('g', { class: 'rsu', 'data-id': id, 'data-zone': zone,
                        'data-state': 'active' }, layer);
    const s = el('g', { class: 'rsu-scale' }, g);
    el('rect', { class: 'mast', x: -1.2, y: -14, width: 2.4, height: 22, rx: 1 }, s);
    el('rect', { class: 'base', x: -7, y: 7, width: 14, height: 4, rx: 1.5 }, s);
    el('path', { class: 'wave w1', d: 'M2,-12 A7,7 0 0 1 2,-4' }, s);
    el('path', { class: 'wave w2', d: 'M4,-14 A11,11 0 0 1 4,-2' }, s);
    // evaluation-window countdown ring
    el('circle', { class: 'rsu-win', cx: 0, cy: -3, r: 15 }, s);
    // "N rpts" pill
    const pill = el('g', { class: 'rsu-buf' }, s);
    // Wide enough for the reconciled "9T/3F" form, not just a bare count.
    el('rect', { x: -19, y: -30, width: 38, height: 11, rx: 5.5 }, pill);
    el('text', { x: 0, y: -22, 'text-anchor': 'middle' }, pill).textContent = '0';
    el('text', { class: 'rsu-label', x: 0, y: 22, 'text-anchor': 'middle' }, s)
      .textContent = 'R' + id;
    return g;
  },

  /* ------------------------------------------------------------ controllers --- */
  controller(layer, id, zone) {
    const g = el('g', { class: 'ctrl', 'data-id': id, 'data-zone': zone,
                        'data-state': 'active' }, layer);
    const s = el('g', { class: 'ctrl-scale' }, g);
    el('path', { class: 'shield',
                 d: 'M0,-18 L15,-9 L15,7 Q15,16 0,20 Q-15,16 -15,7 L-15,-9 Z' }, s);
    el('path', { class: 'tick', d: 'M-6,0 L-2,5 L7,-6' }, s);
    el('text', { class: 'ctrl-label', x: 0, y: 32, 'text-anchor': 'middle' }, s)
      .textContent = 'C' + id;
    return g;
  },

  /** RSU -> controller backhaul, a dotted curve that lights up when it carries traffic. */
  backhaul(layer, a, b) {
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
    const dx = b.x - a.x, dy = b.y - a.y;
    const cx = mx - dy * 0.12, cy = my + dx * 0.12;   // gentle perpendicular bow
    return el('path', { class: 'backhaul',
                        d: `M${a.x},${a.y} Q${cx},${cy} ${b.x},${b.y}` }, layer);
  },
};
