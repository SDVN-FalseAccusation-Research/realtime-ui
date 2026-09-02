/* The campaign page: defended vs baseline, 4 attacks x 4 penetrations.
 *
 * FORM CHOICE. The data's job is change over a continuum (penetration 20->80) for two
 * conditions, so each attack gets its own small line chart rather than everything being
 * crushed into one 16-series plot. ASR and FPR are two different measures, so they get two
 * ROWS of small multiples -- never one chart with two y-axes.
 *
 * The two series are conditions, not states, so they take categorical slots 1 and 2 from
 * tokens.css (validated: worst CVD dE 26.8, normal-vision dE 31.8, contrast >=3:1) and NOT
 * the reserved status colours.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const ARMS = ['undefended', 'defended'];
const PCTS = [20, 40, 60, 80];
const pctFmt = (v) => v === null || v === undefined ? '—' : (v * 100).toFixed(1) + '%';

/* ------------------------------------------------------------------ tooltip -- */
const tip = document.createElement('div');
tip.id = 'tip';
document.body.appendChild(tip);
function showTip(e, html) {
  tip.innerHTML = html;
  tip.style.opacity = '1';
  const r = tip.getBoundingClientRect();
  tip.style.left = Math.min(e.clientX + 14, innerWidth - r.width - 10) + 'px';
  tip.style.top = Math.max(e.clientY - r.height - 12, 8) + 'px';
}
const hideTip = () => { tip.style.opacity = '0'; };

/* ------------------------------------------------------------------- charts -- */
const NS = 'http://www.w3.org/2000/svg';
const el = (t, a = {}, p = null) => {
  const n = document.createElementNS(NS, t);
  for (const k in a) n.setAttribute(k, a[k]);
  if (p) p.appendChild(n);
  return n;
};

/** One small multiple: penetration on x, a rate on y, two series. */
function chart(attack, rows, key) {
  const W = 260, H = 150, L = 34, R = 10, T = 10, B = 24;
  const card = document.createElement('div');
  card.className = 'chart-card';
  card.innerHTML = `<div class="title">${attack.replace('_data', '')}</div>`;

  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
                          'aria-label': `${attack} ${key} by penetration` });
  card.appendChild(svg);

  // A rate is 0..1 and the reader must be able to compare ACROSS attacks, so the scale is
  // fixed at 0..1 for every panel. Auto-scaling each panel would make a 4% FPR and a 68%
  // FPR draw the same shape, which is the classic small-multiples lie.
  const x = (p) => L + (PCTS.indexOf(p) / (PCTS.length - 1)) * (W - L - R);
  const y = (v) => T + (1 - v) * (H - T - B);

  const g = el('g', { class: 'grid' }, svg);
  const ax = el('g', { class: 'ax' }, svg);
  for (const v of [0, 0.25, 0.5, 0.75, 1]) {
    el('line', { x1: L, x2: W - R, y1: y(v), y2: y(v) }, g);
    el('text', { x: L - 6, y: y(v) + 3, 'text-anchor': 'end' }, ax).textContent =
      (v * 100).toFixed(0);
  }
  el('line', { x1: L, x2: W - R, y1: y(0), y2: y(0) }, ax);
  for (const p of PCTS) {
    el('text', { x: x(p), y: H - 8, 'text-anchor': 'middle' }, ax).textContent = p + '%';
  }

  for (const arm of ARMS) {
    const pts = PCTS.map(p => {
      const r = rows.find(q => q.attack === attack && q.percent === p);
      const c = r && r[arm];
      return (c && c[key] !== null && c[key] !== undefined) ? { p, v: c[key], c } : null;
    }).filter(Boolean);
    if (!pts.length) continue;
    el('path', { class: `series ${arm}`,
                 d: pts.map((q, i) => `${i ? 'L' : 'M'}${x(q.p)},${y(q.v)}`).join(' ') }, svg);
    for (const q of pts) {
      el('circle', { class: `dot ${arm}`, cx: x(q.p), cy: y(q.v) }, svg);
      const hit = el('circle', { class: 'hit', cx: x(q.p), cy: y(q.v), r: 12 }, svg);
      const n = key === 'asr'
        ? `${q.c.attacks_succeeded} of ${q.c.attacks_submitted} attacks succeeded`
        : `${q.c.genuine_blocked} of ${q.c.genuine_submitted} genuine reports blocked`;
      hit.addEventListener('mousemove', (e) => showTip(e,
        `<b>${attack.replace('_data', '')} · ${q.p}%</b><br>${arm} — ` +
        `<b>${pctFmt(q.v)}</b><br>${n}`));
      hit.addEventListener('mouseleave', hideTip);
    }
  }

  // Legend on every panel: identity must never rest on colour alone.
  card.insertAdjacentHTML('beforeend',
    `<div class="chart-legend">${ARMS.map(a =>
      `<span><i style="background:var(--arm-${a})"></i>${a}</span>`).join('')}</div>`);
  return card;
}

/* -------------------------------------------------------------------- render -- */
function render(d) {
  $('paired').textContent = `${d.paired_cells} / 16 cells paired`;

  $('warnings').innerHTML = d.warnings.length
    ? '<b>Not comparable:</b> ' + d.warnings.join(' · ')
    : '';

  const c = d.cost || {};
  const P = d.pooled;
  $('hero').innerHTML = `
    <div class="hero-card">
      <div class="lbl">Attack success rate</div>
      <div class="pair"><span class="from">${pctFmt(c.asr_before)}</span>
        <span class="arrow">→</span><span class="to good">${pctFmt(c.asr_after)}</span></div>
      <div class="sub">${P.defended.fa_acc} of ${P.defended.fa_sub} attacks succeeded
        against ${P.undefended.fa_acc} of ${P.undefended.fa_sub} with no defence</div>
    </div>
    <div class="hero-card">
      <div class="lbl">False-positive rate — the cost</div>
      <div class="pair"><span class="from">${pctFmt(c.fpr_before)}</span>
        <span class="arrow">→</span><span class="to bad">${pctFmt(c.fpr_after)}</span></div>
      <div class="sub">${P.defended.gen_blk} of ${P.defended.gen_sub} genuine reports are
        wrongly blocked. The baseline blocks ${P.undefended.gen_blk}.</div>
    </div>
    <div class="hero-card">
      <div class="lbl">Exchange rate</div>
      <div class="pair"><span class="to">${c.exchange_rate ?? '—'}</span></div>
      <div class="sub">attacks stopped per genuine report lost — an estimate: it assumes the
        baseline rate would have held over the defended arm's ${P.defended.fa_sub} attacks</div>
    </div>`;

  for (const [host, key] of [['asr-charts', 'asr'], ['fpr-charts', 'fpr']]) {
    const n = $(host);
    n.innerHTML = '';
    for (const a of ['single_data', 'sybil_data', 'timing_data', 'colluding_data']) {
      n.appendChild(chart(a, d.grid, key));
    }
  }

  const tot = Object.values(d.layers).reduce((s, v) => s + v, 0) || 1;
  $('layers').innerHTML = Object.entries(d.layers).sort((a, b) => b[1] - a[1]).map(([k, v]) =>
    `<div class="layer-row ${k === 'degenerate' ? 'degenerate' : ''}">
       <span class="nm">${k}</span>
       <span class="track"><span class="fill" style="width:${(v / tot * 100).toFixed(1)}%"></span></span>
       <span class="val">${v} · ${(v / tot * 100).toFixed(1)}%</span>
     </div>`).join('');

  // Attacks fired is shown for BOTH arms, because they differ and the reason matters:
  // undefended, successful attacks blacklist their victims and the scheduler then runs out
  // of eligible ones. Showing only the defended count would hide that entirely.
  $('grid').innerHTML =
    `<thead><tr><th>cell</th>
      <th>atk base</th><th>atk def</th><th>skipped base</th>
      <th>ASR base</th><th>ASR def</th>
      <th>gen sub</th><th>FPR base</th><th>FPR def</th><th>simTime</th></tr></thead><tbody>` +
    d.grid.map(r => {
      const D = r.defended, U = r.undefended;
      return `<tr class="${r.paired ? '' : 'unpaired'}">
        <td class="k">${r.attack.replace('_data', '')} p${r.percent}</td>
        <td class="und">${U ? U.attacks_submitted : '—'}</td>
        <td class="def">${D ? D.attacks_submitted : '—'}</td>
        <td title="opportunities skipped: every eligible victim was already blacklisted"
            >${U ? (U.skipped_no_victim || '·') : '—'}</td>
        <td class="und">${U ? pctFmt(U.asr) : '—'}</td>
        <td class="def">${D ? pctFmt(D.asr) : '—'}</td>
        <td>${D ? D.genuine_submitted : '—'}</td>
        <td class="und">${U ? pctFmt(U.fpr) : '—'}</td>
        <td class="def">${D ? pctFmt(D.fpr) : '—'}</td>
        <td>${D && D.sim_time ? D.sim_time + ' s' : '—'}</td></tr>`;
    }).join('') + '</tbody>';

  // A fourth hero card, and it reports the GAP rather than the baseline total. Both arms
  // skip opportunities for arm-independent reasons (misbehavers are legitimately blacklisted
  // in both; eligibility is constrained by neighbour counts). Only the DIFFERENCE is caused
  // by the attack succeeding, so only the difference belongs in a headline.
  const sum = (arm) => d.grid.reduce((s, r) => s + ((r[arm] && r[arm].skipped_no_victim) || 0), 0);
  const uSkip = sum('undefended'), dSkip = sum('defended');
  if (uSkip - dSkip > 0) {
    $('hero').insertAdjacentHTML('beforeend', `
      <div class="hero-card">
        <div class="lbl">Baseline ran out of victims</div>
        <div class="pair"><span class="to bad">+${uSkip - dSkip}</span></div>
        <div class="sub">more opportunities the baseline could not use (${uSkip} vs ${dSkip}) —
          concentrated in colluding and sybil at 80%, where its successes blacklist the whole
          eligible pool. Compare rates between arms, not raw counts.</div>
      </div>`);
  }

  $('notes').innerHTML = d.notes.map(n => `<li>${n}</li>`).join('');
}

fetch('/api/campaign')
  .then(r => r.json())
  .then(render)
  .catch(e => { $('warnings').textContent = 'could not load the campaign: ' + e; });
