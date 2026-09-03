/* Performance evaluation metrics for one run.
 *
 * A thin presenter over GET /api/runs/{id}/metrics, which calls pem.compute_cell() —
 * the project's own implementation of M1-M12. Nothing is recomputed here on purpose: a
 * second implementation would eventually disagree with the paper, and the paper wins.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const runId = new URLSearchParams(location.search).get('run');
let DATA = null;

// M5_Ldet is NOT SHOWN AT ALL -- not as a tile and not in the table. At ~3.3 s it is
// dominated by the fixed evalWindow the controller waits before deciding, so it reads as a
// latency problem when it is a design constant, and it invites exactly the wrong question.
// The per-stage decomposition below is the honest view of where time actually goes.
// pem.py still computes M5; this page just declines to lead with a number it would have to
// spend the whole answer explaining away.
const HEADLINE = ['M1_MCC', 'M3_FBR', 'M6_APO'];
const DESC = {
  M1_MCC:  ['MCC', 'Matthews correlation — the headline detection quality'],
  M2_ASR:  ['ASR', 'attack success rate, per attack variant'],
  M3_FBR:  ['FBR', 'false blacklist rate'],
  M4_HVTD: ['HVTD', 'honest-vehicle trust degradation'],
  M5_Ldet: ['L_det', 'detection latency (s)'],
  M6_APO:  ['APO', 'aggregate pipeline overhead (ms)'],
  M7_AVCA: ['AVCA', 'attack-variant classification accuracy'],
  M8_AER:  ['AER', 'accuser exhaustion rate'],
  M9_CFCR: ['CFCR', 'controller failover correctness'],
  M10_RTDR:['RTDR', 'RSU tampering detection rate'],
  M11_ZPPR:['ZPPR', 'ZK proof pass rate'],
  M12_GKUE:['GKUE', 'group-key update efficiency'],
};
const LAYERS = [['M6_LPQC_ms', 'PQC', 'pqc'], ['M6_LZKP_ms', 'zk-STARK', 'zkp'],
                ['M6_LGNN_ms', 'GNN', 'gnn'], ['M6_LLLM_ms', 'LLM', 'llm'],
                ['M6_LBC_ms', 'Chain', 'bc']];

const fmt = (m) => m.value === null ? 'n/a' :
  (Math.abs(m.value) >= 100 ? m.value.toFixed(1) : m.value.toFixed(4));

function render() {
  const M = DATA.metrics;
  $('loading').remove();
  $('main').classList.remove('hidden');

  // headline tiles + every per-variant ASR, since M2 is the one that must never be
  // quoted without M1 beside it
  const tiles = [];
  for (const k of HEADLINE) {
    if (!M[k]) continue;
    const [name, sub] = DESC[k];
    const na = M[k].value === null;
    tiles.push(`<div class="kpi ${na ? '' : (k === 'M1_MCC' && M[k].value > 0.7 ? 'good' : '')}">
      <div class="k-label">${name}</div>
      <div class="k-value">${fmt(M[k])}</div>
      <div class="k-sub">${na ? 'not computable' : sub}</div></div>`);
  }
  for (const k of Object.keys(M).filter(k => k.startsWith('M2_ASR'))) {
    const variant = k.slice(7, -1);
    tiles.push(`<div class="kpi ${M[k].value > 0.2 ? 'bad' : 'good'}">
      <div class="k-label">ASR · ${variant}</div>
      <div class="k-value">${fmt(M[k])}</div>
      <div class="k-sub">${M[k].num}/${M[k].den} attacks succeeded</div></div>`);
  }
  $('kpi-row').innerHTML = tiles.join('');

  // latency decomposition — magnitudes across named stages, so horizontal bars again
  const vals = LAYERS.map(([k, label, cls]) => [label, cls, (M[k] || {}).value || 0]);
  const max = Math.max(1, ...vals.map(v => v[2]));
  const total = vals.reduce((s, v) => s + v[2], 0);
  $('lat-chart').innerHTML = `<div class="bars">` + vals.map(([label, cls, v]) =>
    `<div class="bar-row" data-layer="${cls}" title="${label}: ${v.toFixed(1)} ms">
       <span class="b-name">${label}</span>
       <span class="bar-track"><span class="bar-fill" style="width:${100 * v / max}%"></span></span>
       <span class="b-val">${v.toFixed(1)} <small>ms</small></span>
     </div>`).join('') +
    `</div><p class="kpi-note" style="margin-top:8px">total ≈ <b>${total.toFixed(1)} ms</b>
     per accusation</p>`;

  // full table, with the n/a reason shown in full rather than a dash
  $('rows').innerHTML = Object.entries(M)
    .filter(([k]) => !k.startsWith('M6_L') && !k.startsWith('M5_Ldet'))
    .sort((a, b) => (parseInt(a[0].slice(1)) || 99) - (parseInt(b[0].slice(1)) || 99))
    .map(([k, m]) => {
      const base = k.split('[')[0];
      const [name, sub] = DESC[base] || [k, ''];
      const variant = k.includes('[') ? ` <small class="muted">${k.slice(k.indexOf('['))}</small>` : '';
      return `<tr>
        <td><b>${name}</b>${variant}<div class="reason">${sub}</div></td>
        <td class="num ${m.value === null ? 'muted' : ''}">${fmt(m)}</td>
        <td class="num">${m.den === null || m.den === undefined ? '—' : `${m.num}/${m.den}`}</td>
        <td class="reason">${m.na_reason || ''}</td></tr>`;
    }).join('');

  const c = DATA.confusion;
  $('confusion').innerHTML = `
    <div class="kpi-row" style="grid-template-columns:repeat(4,1fr)">
      <div class="kpi"><div class="k-label">true positive</div><div class="k-value">${c.tp}</div>
        <div class="k-sub">attack correctly blocked</div></div>
      <div class="kpi"><div class="k-label">false positive</div><div class="k-value">${c.fp}</div>
        <div class="k-sub">genuine wrongly blocked</div></div>
      <div class="kpi"><div class="k-label">true negative</div><div class="k-value">${c.tn}</div>
        <div class="k-sub">genuine correctly upheld</div></div>
      <div class="kpi"><div class="k-label">false negative</div><div class="k-value">${c.fn}</div>
        <div class="k-sub">attack got through</div></div>
    </div>`;
}

$('export').onclick = () => {
  if (!DATA) return;
  const rows = [['metric', 'value', 'num', 'den', 'na_reason']];
  for (const [k, m] of Object.entries(DATA.metrics))
    rows.push([k, m.value ?? '', m.num ?? '', m.den ?? '', (m.na_reason || '').replace(/"/g, "'")]);
  const csv = rows.map(r => r.map(v => `"${v}"`).join(',')).join('\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
  a.download = `${runId}_metrics.csv`;
  a.click();
};

if (!runId) location.href = '/runs';
$('run-label').textContent = runId;
$('link-stats').href = `/stats?run=${encodeURIComponent(runId)}`;
$('link-live').href = `/live?run=${encodeURIComponent(runId)}`;
$('link-components').href = `/components?run=${encodeURIComponent(runId)}`;

fetch(`/api/runs/${encodeURIComponent(runId)}/metrics`)
  .then(r => r.ok ? r.json() : r.json().then(e => Promise.reject(e.detail || 'failed')))
  .then(d => { DATA = d; render(); })
  .catch(e => { $('loading').innerHTML =
    `<span style="color:var(--danger)">could not compute metrics: ${e}</span>`; });

fetch(`/api/runs/${encodeURIComponent(runId)}`).then(r => r.json()).then(m => {
  if (m.imported) $('imported-chip').innerHTML = '<span class="badge warn">imported</span>';
}).catch(() => {});
