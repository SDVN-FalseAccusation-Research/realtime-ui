/* Statistics for one run.
 *
 * Reads the run's event log over the same WebSocket the live page uses — no new endpoint,
 * because `events.jsonl` already carries everything: w1/w2, verdicts, layer attribution,
 * per-layer latency, divergence reasons.
 *
 * METRIC DEFINITIONS ARE COPIED FROM run_sweep.sh AND MUST NOT DRIFT:
 *   an ATTACK  = an accusation whose VICTIM WAS HONEST (victim_honest == 1)
 *   ASR        = attacks accepted / attacks submitted
 *   FPR        = genuine reports blocked / genuine reports submitted
 * A genuine report being accepted is correct behaviour, never an attack success. An
 * earlier version of the sweep script counted every acceptance and reported a healthy
 * cell as "55 false accusations ACCEPTED"; that is the mistake this comment exists to
 * prevent repeating.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const S = { events: [], acc: new Map(), dec: [], cfg: {}, rows: [],
            sort: 'event', dir: 'asc', imported: false };

/* --------------------------------------------------------------------- load ---- */
function load(runId) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/runs/${encodeURIComponent(runId)}`);
  ws.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === '_replay_end' || ev.type === 'run_closed') { ws.close(); build(); return; }
    S.events.push(ev);
    if (ev.type === 'run_start') { S.cfg = ev.config || {}; S.imported = !!ev.imported; }
    if (ev.type === 'accusation') S.acc.set(ev.event, ev);
    if (ev.type === 'decision') S.dec.push(ev);
  };
  ws.onerror = () => { $('loading').textContent = 'could not load this run'; };
  ws.onclose = () => { if (!S.rows.length) build(); };
}

/* -------------------------------------------------------------------- build ---- */
function build() {
  if (!S.dec.length) {
    $('loading').textContent = 'This run has no decisions recorded.';
    return;
  }
  $('loading').remove();
  ['kpis', 'sec-layers', 'sec-table', 'sec-chain', 'sec-attack']
    .forEach(id => $(id).classList.remove('hidden'));

  S.rows = S.dec.map(d => {
    const a = S.acc.get(d.event) || {};
    return {
      event: d.event, t: d.t, kind: a.kind || '—',
      accuser: a.accuser ? a.accuser.v : null,
      victim: a.victim ? a.victim.v : null,
      honest: d.victim_honest,
      reports: d.reports.true + d.reports.false,
      rt: d.reports.true, rf: d.reports.false,
      w1: d.w1, w2: d.w2, accepted: d.accepted,
      stopped_by: d.stopped_by || 'none',
      reason: d.divergence_reason || '',
      latency: d.latency_us || {},
    };
  });

  kpis();
  layerChart();
  renderTable();
  chainList();
  attackDetail();
}

/* ---------------------------------------------------------------------- KPIs --- */
function kpis() {
  const real = S.rows.filter(r => r.kind !== 'warmup');
  const atk = real.filter(r => r.honest);
  const gen = real.filter(r => !r.honest);
  const asr = atk.length ? atk.filter(r => r.accepted).length / atk.length : null;
  const fpr = gen.length ? gen.filter(r => !r.accepted).length / gen.length : null;
  const dur = Math.max(...S.rows.map(r => r.t), 0);

  const tile = (label, value, sub, cls = '') =>
    `<div class="kpi ${cls}"><div class="k-label">${label}</div>
       <div class="k-value">${value}</div><div class="k-sub">${sub}</div></div>`;

  const pct = (x) => x === null ? 'n/a' : x.toFixed(4);
  $('kpi-row').innerHTML =
    tile('accusations', real.length, `${S.rows.length - real.length} warm-up excluded`) +
    tile('attacks', atk.length,
         `${atk.filter(r => r.accepted).length} succeeded · ${atk.filter(r => !r.accepted).length} blocked`) +
    tile('ASR', pct(asr), 'attacks accepted ÷ submitted',
         asr === null ? '' : (asr < 0.2 ? 'good' : 'bad')) +
    tile('genuine reports', gen.length,
         `${gen.filter(r => !r.accepted).length} wrongly blocked`) +
    tile('FPR', pct(fpr), 'genuine blocked ÷ submitted',
         fpr === null ? '' : (fpr < 0.2 ? 'good' : 'bad')) +
    tile('simulated', `${dur.toFixed(0)} s`, S.cfg.attackType || '');
}

/* ------------------------------------------------------- layer attribution ----- */
const LAYER_LABEL = {
  pqc: 'PQC signatures', zkp_proximity: 'zk-STARK proximity', zkp: 'zk-STARK',
  gnn: 'GNN', llm: 'LLM', blockchain: 'Blockchain (SC2/SC3)',
  lw_signature: 'LW rule engine', degenerate: 'degenerate',
};

function layerChart() {
  // run_sweep.sh attributes layers over BLOCKED ATTACKS ONLY — non-warmup, victim
  // honest, and not accepted. Using every decision instead inflates the counts and no
  // longer matches the sweep's own report.
  const blocked = S.rows.filter(r => r.kind !== 'warmup' && r.honest && !r.accepted);
  const counts = {};
  blocked.forEach(r => { counts[r.stopped_by] = (counts[r.stopped_by] || 0) + 1; });

  const degen = counts.degenerate || 0;
  delete counts.degenerate;
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map(e => e[1]), degen);
  const defended = entries.reduce((s, e) => s + e[1], 0);

  const row = (k, n) =>
    `<div class="bar-row" data-layer="${k}" title="${LAYER_LABEL[k] || k}: ${n} of ${blocked.length}">
       <span class="b-name">${LAYER_LABEL[k] || k}</span>
       <span class="bar-track"><span class="bar-fill" style="width:${100 * n / max}%"></span></span>
       <span class="b-val">${n} <small>${(100 * n / blocked.length).toFixed(0)}%</small></span>
     </div>`;

  $('layer-chart').innerHTML =
    `<div class="bars">${entries.map(([k, n]) => row(k, n)).join('')}` +
    (degen ? `<div class="sep"></div>${row('degenerate', degen)}` : '') +
    `</div>`;

  $('layer-note').innerHTML =
    `<b>${defended}</b> of ${blocked.length} blocked attacks were stopped by a defence layer.` +
    (degen ? ` The remaining <b>${degen}</b> were <b>degenerate</b> — the event drew no
      witness reports at all, so it was <em>starved of evidence rather than defended</em>.
      It is shown separately and excluded from the defence total on purpose; crediting it
      to a layer would overstate the defence.` : '');
}

/* --------------------------------------------------------------------- table --- */
function passes(r) {
  const q = $('q').value.trim().toLowerCase();
  if (q && !`v${r.accuser} v${r.victim} ${r.stopped_by} ${r.reason}`.toLowerCase().includes(q))
    return false;
  const k = $('f-kind').value;
  if (k && r.kind !== k) return false;
  const o = $('f-out').value;
  if (o === 'acc' && !r.accepted) return false;
  if (o === 'blk' && r.accepted) return false;
  return true;
}

function renderTable() {
  const rows = S.rows.filter(passes).sort((a, b) => {
    const x = a[S.sort], y = b[S.sort];
    const c = (x > y) - (x < y);
    return S.dir === 'asc' ? c : -c;
  });
  $('tbl-count').textContent = `${rows.length} of ${S.rows.length}`;
  $('rows').innerHTML = rows.map(r => `
    <tr>
      <td class="num">${r.event}</td>
      <td class="num">${r.t.toFixed(1)}</td>
      <td><span class="pill ${r.kind}">${r.kind}</span></td>
      <td class="num">V${r.accuser}</td>
      <td class="num">V${r.victim}${r.honest ? '' : ' <small class="muted">(dishonest)</small>'}</td>
      <td class="num">${r.reports} <small class="muted">${r.rt}T/${r.rf}F</small></td>
      <td class="num">${r.w1.toFixed(2)}</td>
      <td class="num">${r.w2.toFixed(2)}</td>
      <td class="${r.accepted ? 'acc' : 'blk'}">${r.accepted ? 'ACCEPTED' : 'blocked'}</td>
      <td>${r.stopped_by === 'none' ? '<span class="muted">—</span>' : r.stopped_by}</td>
      <td class="reason">${r.reason || ''}</td>
    </tr>`).join('');
}

/* ---------------------------------------------------------------- blockchain --- */
function chainList() {
  const tx = S.events.filter(e => e.type === 'chain_tx');
  if (!tx.length) {
    $('chain-list').innerHTML =
      '<p class="muted">No blockchain layer in this run — undefended baseline.</p>';
    return;
  }
  const diverged = tx.filter(t => t.divergence).length;
  $('chain-list').innerHTML =
    `<p class="kpi-note" style="margin-top:0"><b>${tx.length}</b> SC2 submissions ·
      <b>${tx.filter(t => t.endorsed).length}</b> endorsed ·
      <b>${diverged}</b> with divergence ·
      <b>${tx.filter(t => t.rolled_back).length}</b> rolled back</p>` +
    tx.slice(0, 40).map(t => `
      <div class="tx">
        <div class="tx-head"><b>${t.fn} · event ${t.event}</b>
          <span class="mono muted">t=${(t.t || 0).toFixed(1)}s</span></div>
        <div class="tx-line">endorsed ${t.endorsed ? '✓' : '✗'}${
          t.divergence ? ` · divergence: ${t.divergence_reason || 'yes'}` : ''}${
          t.rolled_back ? ' · ROLLED BACK' : ''}</div>
      </div>`).join('');
}

/* ------------------------------------------------------- attack-specific ------- */
function attackDetail() {
  const type = S.cfg.attackType || '';
  $('attack-title').textContent = `Attack detail — ${type || 'unknown'}`;
  const out = [];

  const sybil = S.events.filter(e => e.type === 'sybil');
  if (sybil.length) {
    const total = sybil.reduce((s, e) => s + e.identities, 0);
    out.push(`<div class="kv"><span>sybil identities forged</span>
              <b>${total} over ${sybil.length} events</b></div>`);
  }
  const rsu = S.events.filter(e => e.type === 'rsu_status');
  if (rsu.length) {
    out.push(`<div class="kv"><span>RSU custody actions</span><b>${rsu.length}</b></div>` +
      rsu.slice(0, 8).map(e =>
        `<div class="kv"><span>R${e.rsu.r}</span><b class="${
          e.state === 'REMOVED' ? 'bad' : 'warn'}">${e.state} · trust ${
          (e.trust ?? 0).toFixed(2)}</b></div>`).join(''));
  }
  const fo = S.events.filter(e => e.type === 'controller_failover');
  if (fo.length) {
    out.push(...fo.map(e => `<div class="kv"><span>controller failover</span>
      <b>C${e.controller.c} → C${e.backup.c} (epoch ${e.epoch})${
        e.no_standby ? ' <span class="bad">no standby!</span>' : ''}</b></div>`));
  }
  const stake = S.events.filter(e => e.type === 'stake');
  if (stake.length) {
    const burned = stake.reduce((s, e) => s + e.burned, 0);
    out.push(`<div class="kv"><span>stake burned</span>
              <b>${burned.toFixed(3)} over ${stake.length} filings</b></div>`);
  }
  const inc = S.events.filter(e => e.type === 'llm_incident');
  if (inc.length) {
    out.push(`<div class="kv"><span>LLM-confirmed incidents</span><b>${inc.length}</b></div>`,
      `<div class="kv-note">accusers whose credentials were revoked / isolated after the
       LLM confirmed the GNN's flag</div>`);
  }

  // Per-layer mean latency — only meaningful when a layer actually ran.
  const lat = {};
  S.rows.forEach(r => {
    for (const k in r.latency) {
      if (r.latency[k] > 0) (lat[k] = lat[k] || []).push(r.latency[k]);
    }
  });
  const keys = Object.keys(lat);
  if (keys.length) {
    out.push('<div class="kv" style="margin-top:8px"><span>mean latency per layer</span><b></b></div>');
    keys.forEach(k => {
      const m = lat[k].reduce((a, b) => a + b, 0) / lat[k].length / 1000;
      out.push(`<div class="kv"><span style="padding-left:10px">${
        LAYER_LABEL[k] || k}</span><b class="mono">${m.toFixed(1)} ms
        <small class="muted">n=${lat[k].length}</small></b></div>`);
    });
  }

  $('attack-detail').innerHTML = out.length ? out.join('')
    : '<p class="muted">No attack-specific events recorded for this run.</p>';
}

/* ---------------------------------------------------------------------- boot --- */
const runId = new URLSearchParams(location.search).get('run');
if (!runId) location.href = '/runs';
$('run-label').textContent = runId;
$('link-live').href = `/live?run=${encodeURIComponent(runId)}`;
$('link-metrics').href = `/metrics?run=${encodeURIComponent(runId)}`;

['q', 'f-kind', 'f-out'].forEach(id => {
  $(id).oninput = renderTable; $(id).onchange = renderTable;
});
document.querySelectorAll('th[data-sort]').forEach(th => {
  th.onclick = () => {
    const k = th.dataset.sort;
    S.dir = (S.sort === k && S.dir === 'asc') ? 'desc' : 'asc';
    S.sort = k;
    document.querySelectorAll('th[data-sort]').forEach(o => o.removeAttribute('data-dir'));
    th.setAttribute('data-dir', S.dir);
    renderTable();
  };
});

fetch(`/api/runs/${encodeURIComponent(runId)}`).then(r => r.json()).then(meta => {
  if (meta.imported) {
    $('imported-chip').innerHTML =
      '<span class="badge warn" title="Reconstructed from this run\'s CSVs — timings are ' +
      'exact, but per-witness detail the CSVs never held is approximated">imported</span>';
  }
}).catch(() => {});

load(runId);
