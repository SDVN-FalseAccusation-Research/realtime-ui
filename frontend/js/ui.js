/* The 25% sidebar and the attack/mitigation flow ribbon.
 *
 * SIDEBAR — scrolling log, good for detail, poor for sequence.
 * RIBBON  — fixed left-to-right axis, good for sequence, poor for detail.
 * They are deliberately different tools; the ribbon is what makes the pipeline legible
 * from the back of a room.
 */
'use strict';

/* ========================================================================= RIBBON == */
const Ribbon = {
  /* Stage order is the real gate order (live_run_walkthrough.md section 3):
   *   PQC -> SC2 snapshot -> ZKP -> GNN -> LLM -> SC3/SC4 -> reputation
   * Only enabled layers are shown, so an undefended run collapses to
   * ACCUSER -> REPORTERS -> RSU -> CONTROLLER, which is exactly the contrast worth
   * showing next to a defended one. */
  ALL: [
    { id: 'accuser',   label: 'ACCUSER',   group: 'attack' },
    { id: 'reporters', label: 'REPORTERS', group: 'attack' },
    { id: 'rsu',       label: 'RSU',       group: 'attack' },
    { id: 'pqc',       label: 'PQC',       group: 'defence', flag: 'blockchain' },
    { id: 'zkp',       label: 'ZKP',       group: 'defence', flag: 'blockchain' },
    { id: 'gnn',       label: 'GNN',       group: 'defence', flag: 'gnnDetect' },
    { id: 'llm',       label: 'LLM',       group: 'defence', flag: 'llmReason' },
    { id: 'bc',        label: 'CHAIN',     group: 'defence', flag: 'blockchain' },
    { id: 'outcome',   label: 'OUTCOME',   group: 'outcome' },
  ],
  stages: [],
  root: null,

  build(el_, cfg) {
    this.root = el_;
    this.stages = this.ALL.filter(s => !s.flag || Number(cfg[s.flag]) === 1);
    el_.innerHTML = this.stages.map(s =>
      `<div class="stage" data-stage="${s.id}" data-status="idle">
         <div class="s-label">${s.label}</div>
         <div class="s-value">—</div>
       </div>`).join('<div class="s-arrow">›</div>');
  },

  reset() {
    if (!this.root) return;
    this.root.querySelectorAll('.stage').forEach(s => {
      s.dataset.status = 'idle';
      s.querySelector('.s-value').textContent = '—';
    });
  },

  /** status: idle | active | done | blocked | failed | skipped */
  set(id, status, value) {
    if (!this.root) return;
    const s = this.root.querySelector(`[data-stage="${id}"]`);
    if (!s) return;
    s.dataset.status = status;
    if (value !== undefined) s.querySelector('.s-value').textContent = value;
  },

  /** Mark every defence stage after `id` as skipped rather than merely unreached.
   *
   *  This distinction is a teaching point, not pedantry: control-plane attacks fail the
   *  ZK gate, and the GNN is then DELIBERATELY not run (apps.cc gates it on
   *  zkpResult != FAIL). Showing GNN/LLM as "skipped" explains why their scores are zero,
   *  instead of leaving it looking like the detector missed something. */
  skipAfter(id) {
    const i = this.stages.findIndex(s => s.id === id);
    if (i < 0) return;
    for (let j = i + 1; j < this.stages.length; j++) {
      const s = this.stages[j];
      if (s.group === 'defence') this.set(s.id, 'skipped', 'skipped');
    }
  },
};

/* ======================================================================== SIDEBAR == */
const Sidebar = {
  el: {},
  history: [],

  init() {
    ['run-status', 'cur-event', 'pipeline', 'ledger', 'history', 'sys'].forEach(id => {
      this.el[id] = document.getElementById('sb-' + id);
    });
  },

  status(cfg, counts, clockText) {
    const layers = ['blockchain', 'gnnDetect', 'llmReason', 'lwMode']
      .filter(k => Number(cfg[k]) === 1)
      .map(k => ({ blockchain: 'BC', gnnDetect: 'GNN', llmReason: 'LLM', lwMode: 'LW' })[k]);
    this.el['run-status'].innerHTML = `
      <div class="kv"><span>attack</span><b>${cfg.attackType || '—'}</b></div>
      <div class="kv"><span>penetration</span><b>${cfg.attackPercent ?? '—'}%</b></div>
      <div class="kv"><span>defence</span><b>${layers.length ? layers.join(' + ') : 'none (baseline)'}</b></div>
      <div class="kv"><span>sim clock</span><b class="mono">${clockText}</b></div>
      <div class="kv"><span>accusations</span><b>${counts.total}</b></div>
      <div class="kv"><span>accepted / blocked</span>
        <b><span class="bad">${counts.accepted}</span> / <span class="good">${counts.blocked}</span></b></div>`;
  },

  event(e) {
    if (!e) { this.el['cur-event'].innerHTML = '<p class="muted">Waiting for the first accusation…</p>'; return; }
    const kindLabel = { attack: 'FALSE ACCUSATION', warmup: 'WARM-UP', genuine: 'GENUINE REPORT' }[e.kind] || e.kind;
    this.el['cur-event'].innerHTML = `
      <div class="ev-head">
        <span class="ev-no">EVENT #${e.event}</span>
        <span class="ev-kind ${e.kind}">${kindLabel}</span>
        <span class="mono muted">t=${e.t.toFixed(1)}s</span>
      </div>
      <div class="kv"><span>accuser</span><b class="attacker">V${e.accuser.v}</b></div>
      <div class="kv"><span>victim</span><b class="victim">V${e.victim.v}</b></div>
      <div class="kv"><span>separation</span><b>${e.dist} m ${e.in_range ? '' : '<span class="warn">(out of range)</span>'}</b></div>
      ${e.victim_density !== undefined ? `<div class="kv"><span>victim neighbours</span><b>${e.victim_density}</b></div>` : ''}
      <div class="kv"><span>serving RSU</span><b class="rsu" id="sb-rsu">—</b></div>
      <div class="kv"><span>witnesses</span><b id="sb-wit">—</b></div>
      <div class="kv-note">in-range neighbours, computed from the trace — the simulator
        does not emit reporter identities</div>
      <div class="kv"><span>reports to RSU</span><b id="sb-reports">—</b></div>
      <div class="kv"><span>controller</span><b class="ctrl" id="sb-ctrl">—</b></div>
      <div class="kv verdict" id="sb-verdict"><span>verdict</span><b>pending…</b></div>`;
  },

  setEventField(id, html) {
    const n = document.getElementById('sb-' + id);
    if (n) n.innerHTML = html;
  },

  verdict(accepted, by) {
    const n = document.getElementById('sb-verdict');
    if (!n) return;
    n.innerHTML = accepted
      ? `<span>verdict</span><b class="bad">ACCEPTED — attack succeeded</b>`
      : `<span>verdict</span><b class="good">BLOCKED${by ? ' by ' + by.toUpperCase() : ''}</b>`;
  },

  pipeline(rows) {
    this.el.pipeline.innerHTML = rows.length ? rows.map(r =>
      `<div class="pipe ${r.status}">
         <span class="p-name">${r.name}</span>
         <span class="p-val">${r.value}</span>
       </div>`).join('') : '<p class="muted">No defence layers enabled.</p>';
  },

  ledger(entries) {
    this.el.ledger.innerHTML = entries.length ? entries.map(e =>
      `<div class="tx">
         <div class="tx-head"><b>${e.title}</b><span class="mono muted">${e.time}</span></div>
         ${e.lines.map(l => `<div class="tx-line">${l}</div>`).join('')}
       </div>`).join('') : '<p class="muted">Blockchain layer not enabled.</p>';
  },

  pushHistory(e) {
    this.history.unshift(e);
    this.el.history.innerHTML = this.history.slice(0, 40).map(h =>
      `<div class="hist ${h.accepted ? 'bad' : 'good'}">
         <span class="mono">#${h.event}</span>
         V${h.accuser} → V${h.victim}
         <span class="tag">${h.accepted ? 'ACCEPTED' : 'BLOCKED' + (h.by ? ' · ' + h.by : '')}</span>
       </div>`).join('');
  },

  clearHistory() { this.history = []; this.el.history.innerHTML = ''; },

  sys(html) { this.el.sys.innerHTML = html; },
};
