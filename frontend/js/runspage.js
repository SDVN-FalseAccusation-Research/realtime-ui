/* Previous simulations: list, filter, sort, and open a run for replay.
 *
 * Opening a row goes to the SAME live page. A finished run replays from its events.jsonl
 * through the same WebSocket the live run used, so there is no separate replay renderer to
 * build or keep in step — the frontend cannot tell the difference.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const S = { runs: [], sort: 'started', dir: 'desc' };

function imported(r) { return String(r.run_id || '').startsWith('imported-'); }

function defences(r) {
  const on = [];
  if (Number(r.blockchain) === 1) on.push(['bc', 'BC']);
  if (Number(r.gnnDetect) === 1) on.push(['gnn', 'GNN']);
  if (Number(r.llmReason) === 1) on.push(['llm', 'LLM']);
  if (Number(r.lwMode) === 1) on.push(['lw', 'LW']);
  return on.length ? on : [['none', 'none']];
}

function asr(r) {
  const s = r.summary;
  if (!s || !s.submitted) return null;
  return s.successRate !== undefined ? s.successRate : s.accepted / s.submitted;
}

function outcome(r) {
  // `state` before exit_code: -15 means the operator pressed Stop, which is not a crash,
  // and a `degraded` run exits 0 while having filed no accusations at all (dirty ledger).
  if (r.state === 'stopped')  return { txt: 'stopped', cls: 'warn' };
  if (r.state === 'degraded') return { txt: 'degraded — no accusations', cls: 'bad' };
  if (r.exit_code === 0) return { txt: 'completed', cls: 'good' };
  if (r.exit_code === null || r.exit_code === undefined)
    return { txt: 'running / incomplete', cls: 'warn' };
  return { txt: `exit ${r.exit_code}`, cls: 'bad' };
}

function key(r, k) {
  switch (k) {
    case 'topo': return (r.numVehicles || 0) * 1000 + (r.numRsus || 0);
    case 'defence': return defences(r).map(d => d[1]).join();
    case 'accusations': return (r.summary && r.summary.submitted) || -1;
    case 'asr': { const a = asr(r); return a === null ? -1 : a; }
    case 'outcome': return r.exit_code === 0 && r.state !== 'degraded' ? 0 : 1;
    default: return r[k] === undefined || r[k] === null ? '' : r[k];
  }
}

function passes(r) {
  const q = $('q').value.trim().toLowerCase();
  if (q && !`${r.run_id} ${r.attackType} ${r.trace}`.toLowerCase().includes(q)) return false;

  const a = $('f-attack').value;
  if (a && r.attackType !== a) return false;

  const d = $('f-defence').value;
  if (d === 'none') {
    if (defences(r)[0][0] !== 'none') return false;
  } else if (d && Number(r[d]) !== 1) return false;

  const o = $('f-outcome').value;
  // A degraded run exits 0 but is not "ok" — it filed no accusations.
  const good = r.exit_code === 0 && r.state !== 'degraded';
  if (o === 'ok' && !good) return false;
  if (o === 'failed' && good) return false;

  const t = $('f-topo').value;
  if (t === '200' && r.numVehicles !== 200) return false;
  if (t === 'small' && !(r.numVehicles < 200)) return false;

  return true;
}

function render() {
  const rows = S.runs.filter(passes).sort((x, y) => {
    const a = key(x, S.sort), b = key(y, S.sort);
    const c = (a > b) - (a < b);
    return S.dir === 'asc' ? c : -c;
  });

  $('count').textContent = `${rows.length} of ${S.runs.length} runs`;
  $('empty').style.display = rows.length ? 'none' : '';
  $('empty').textContent = S.runs.length
    ? 'No runs match these filters.'
    : 'No runs yet — start one from the configuration page.';

  $('rows').innerHTML = rows.map(r => {
    const a = asr(r);
    const o = outcome(r);
    const when = (r.started || '').replace('T', ' ').replace('Z', '');
    return `<tr data-run="${r.run_id}">
      <td class="mono">${when}</td>
      <td><span class="run-id">${r.run_id}</span>${
        imported(r) ? ' <span class="chip imported" title="reconstructed from this run\'s CSVs">imported</span>' : ''}</td>
      <td>${r.attackType || '—'}</td>
      <td class="num">${r.attackPercent ?? '—'}</td>
      <td class="num">${r.numVehicles || '?'}/${r.numRsus || '?'}/${r.numControllers || '?'}</td>
      <td>${defences(r).map(([c, l]) => `<span class="chip ${c}">${l}</span>`).join('')}</td>
      <td class="num">${(r.summary && r.summary.submitted) ?? '—'}</td>
      <td class="num">${a === null ? '—' :
        `<span class="asr-bar"><i style="width:${Math.round(a * 40)}px"></i>${a.toFixed(3)}</span>`}</td>
      <td class="${o.cls}">${o.txt}</td>
      <td class="row-actions">${r.has_events
        ? '<button data-go="live">Replay</button>' +
          '<button data-go="stats">Stats</button>' +
          '<button data-go="metrics">Metrics</button>' +
          '<button data-go="components">Components</button>'
        : '<span class="muted">no log</span>'}</td>
    </tr>`;
  }).join('');

  $('rows').querySelectorAll('tr').forEach(tr => {
    const go = (page) => { location.href = `/${page}?run=${encodeURIComponent(tr.dataset.run)}`; };
    tr.onclick = (e) => go(e.target.dataset?.go || 'live');
  });
}

async function load() {
  const d = await (await fetch('/api/runs?limit=500')).json();
  S.runs = d.runs || [];
  const types = [...new Set(S.runs.map(r => r.attackType).filter(Boolean))].sort();
  $('f-attack').innerHTML = '<option value="">all attacks</option>' +
    types.map(t => `<option>${t}</option>`).join('');
  render();
}

['q', 'f-attack', 'f-defence', 'f-outcome', 'f-topo'].forEach(id => {
  $(id).oninput = render; $(id).onchange = render;
});
document.querySelectorAll('th[data-sort]').forEach(th => {
  th.onclick = () => {
    const k = th.dataset.sort;
    S.dir = (S.sort === k && S.dir === 'desc') ? 'asc' : 'desc';
    S.sort = k;
    document.querySelectorAll('th[data-sort]').forEach(o => o.removeAttribute('data-dir'));
    th.setAttribute('data-dir', S.dir);
    render();
  };
});
$('refresh').onclick = load;
load();
