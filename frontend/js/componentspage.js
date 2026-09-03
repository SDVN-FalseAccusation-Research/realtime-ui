/* Per-component detail: what went in, what came out, what it changed.
 *
 * Every view renders the backend's `notes` verbatim and prominently. Those notes say what
 * a run did NOT capture — the GNN's input features, the LLM's prompt text — and naming the
 * gap is more useful than an empty panel, and far more honest than a plausible-looking one.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const runId = new URLSearchParams(location.search).get('run');
const S = { overview: null, current: null };

const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const n2 = (x) => (x == null ? '—' : Number(x).toFixed(2));
const n3 = (x) => (x == null ? '—' : Number(x).toFixed(3));
const n4 = (x) => (x == null ? '—' : Number(x).toFixed(4));

const notes = (list, cls = '') => !list || !list.length ? '' :
  `<div class="notes">${list.map(t =>
    `<div class="note ${/MISMATCH|WRONG|wrong|NOT recorded|CLASSICAL/.test(t) ? 'bad' : ''} ${cls}">${esc(t)}</div>`
  ).join('')}</div>`;

const tile = (label, value, sub = '', cls = '') =>
  `<div class="kpi ${cls}"><div class="k-label">${esc(label)}</div>
     <div class="k-value">${value}</div><div class="k-sub">${esc(sub)}</div></div>`;

const card = (title, body) =>
  `<section class="card-wide"><h2>${esc(title)}</h2>${body}</section>`;

const table = (cols, rows) =>
  `<div class="tbl-scroll"><table class="runs"><thead><tr>${
    cols.map(c => `<th class="${c[2] || ''}">${esc(c[1])}</th>`).join('')
  }</tr></thead><tbody>${rows}</tbody></table></div>`;

/* ------------------------------------------------------------------- overview --- */
function renderOverview(d) {
  $('view').innerHTML =
    `<div class="comp-grid">` + d.cards.map(c =>
      `<div class="comp-card" data-available="${c.available}" data-go="${c.component}">
         <h3>${esc(c.label)}</h3>
         <div class="role">${esc(c.role)}</div>
         <div class="headline">${esc(c.headline)}</div>
       </div>`).join('') + `</div>` +
    `<p class="kpi-note">Components not enabled for this run are dimmed. Click one to see
      what it received, what it decided, and what it changed.</p>`;
  $('view').querySelectorAll('.comp-card[data-available="true"]').forEach(el => {
    el.onclick = () => go(el.dataset.go);
  });
}

/* ------------------------------------------------------------------- per view --- */
const VIEWS = {
  pqc(d) {
    const s = d.summary;
    return `<div class="kpi-row">
        ${tile('vehicle tier', s.vehicle_classical ? 'classical' : 'post-quantum',
               s.vehicle_tier, s.vehicle_classical ? 'bad' : 'good')}
        ${tile('infrastructure tier', s.infra_classical ? 'classical' : 'post-quantum',
               s.infrastructure_tier, s.infra_classical ? 'bad' : 'good')}
        ${tile('forgeries rejected', s.forgeries_rejected, 'σ verification failures')}
      </div>` +
      card('What protects what', `<dl class="def-list">
        <dt>σ_E accuser</dt><dd>${esc(s.vehicle_tier)}</dd>
        <dt>σ_R reporter</dt><dd>${esc(s.vehicle_tier)}</dd>
        <dt>σ_F RSU forward</dt><dd>${esc(s.infrastructure_tier)}</dd>
        <dt>session keys</dt><dd>${esc(s.session_keys)}</dd>
        <dt>bridge transport</dt><dd>${esc(s.bridge_transport)}</dd>
        <dt>evidence hash</dt><dd>${esc(s.evidence_hash)}</dd>
      </dl>${s.checks_line ? `<p class="kpi-note" style="margin-top:10px">
        run's own line: <code>${esc(s.checks_line)}</code></p>` : ''}`) +
      notes(d.notes);
  },

  zkp(d) {
    const s = d.summary;
    const rows = d.rows.slice(0, 300).map(r => `<tr>
      <td class="num">${r.event}</td>
      <td class="${r.result === 'PASS' ? 'blk' : 'acc'}">${r.result}</td>
      <td>${r.proximity ? '✓' : '✗'}</td>
      <td>${r.sequence ? '✓' : '✗'}</td>
      <td>${r.accusation ? '✓' : '✗'}</td>
      <td class="num">${r.reports_proved}</td>
      <td class="num">${r.reports_dropped}</td>
      <td>${r.v_geo ? '<span class="warn">V_geo</span>' : ''}</td>
      <td class="num">${(r.latency_us / 1000).toFixed(1)}</td></tr>`).join('');
    return `<div class="kpi-row">
        ${tile('proofs', s.proofs, 'accusations gated')}
        ${tile('pass', s.pass, '', 'good')}
        ${tile('fail', s.fail, 'rejected at the gate', s.fail ? 'bad' : '')}
        ${tile('reports proved', s.reports_proved, `${s.reports_dropped} dropped`)}
        ${tile('mean latency', (s.mean_latency_us / 1000).toFixed(1) + ' ms', 'Contexts C/D')}
      </div>` +
      card('The four contexts', `<dl class="def-list">
        <dt>A · membership</dt><dd>knowledge of the credential behind a committed leaf</dd>
        <dt>B · proximity</dt><dd>(ax−cx)²+(ay−cy)² ≤ r², coordinates hidden</dd>
        <dt>B · C1</dt><dd>H(evidence) equals the anchored hash</dd>
        <dt>B · C3</dt><dd>timestamp within ±delta of the event</dd>
        <dt>C/D · reporter</dt><dd>commitment opening + gamma &lt; trust ≤ 1, trust hidden</dd>
      </dl>`) +
      card('Per accusation', table(
        [[0, '#', 'num'], [0, 'result'], [0, 'proximity'], [0, 'sequence'], [0, 'C1∧C3'],
         [0, 'proved', 'num'], [0, 'dropped', 'num'], [0, ''], [0, 'ms', 'num']], rows)) +
      notes(d.notes);
  },

  gnn(d) {
    const s = d.summary, m = s.model || {};
    const rows = d.rows.slice(0, 300).map(r => {
      const tot = Object.values(r.pv).reduce((a, b) => a + b, 0) || 1;
      const bar = ['single', 'sybil', 'timing', 'colluding']
        .map(k => `<i class="${k}" style="width:${100 * r.pv[k] / tot}%"></i>`).join('');
      return `<tr>
        <td class="num">${r.event}</td><td class="num">V${r.victim}</td>
        <td class="num ${r.flagged ? 'acc' : ''}">${n3(r.score)}</td>
        <td>${r.flagged ? '<span class="bad">FLAGGED</span>' : 'clear'}</td>
        <td>${r.blocked ? '<span class="good">blocked</span>' : ''}</td>
        <td>${esc(r.pattern)}</td>
        <td><span class="pv">${bar}</span></td></tr>`;
    }).join('');
    return `<div class="kpi-row">
        ${tile('scored', s.scored, 'ZK-clean accusations')}
        ${tile('flagged', s.flagged, `threshold τ = ${n4(m.tau_gnn)}`, 'bad')}
        ${tile('blocked', s.blocked, 'GNN overrode the outcome', 'good')}
        ${tile('test MCC', n4(m.test_mcc), `FPR ${n4(m.test_fpr)}`)}
      </div>` +
      card('Model card', `<dl class="def-list">
        <dt>artifact</dt><dd>${esc(m.dir)}</dd>
        <dt>threshold τ</dt><dd>${n4(m.tau_gnn)}</dd>
        <dt>val / test MCC</dt><dd>${n4(m.val_mcc)} / ${n4(m.test_mcc)}</dd>
        <dt>variant accuracy</dt><dd>${n4(m.var_head_accuracy)}</dd>
        <dt>features</dt><dd>${(m.features || []).length}</dd>
        <dt>model hash</dt><dd class="hash">${esc(m.hash)}</dd>
        <dt>rows scored by it</dt><dd>${m.hash_matches
          ? `<span class="ok-tick">✓ all ${s.scored} rows match this model</span>`
          : `<span class="bad">✗ CSV hash differs from the manifest</span>`}</dd>
      </dl>
      <p class="kpi-note">The sidecar picks its artifact from <code>$GNN_ARTIFACTS</code> and
       <b>defaults to gnn_v12_sigmoid_invfreq</b> (τ 0.6795). Both report
       "gnn-v1 (trained)", so the digest alone cannot tell them apart — this hash check can.</p>
      ${m.by_attack ? `<div class="tbl-scroll" style="margin-top:10px">${table(
        [[0, 'variant'], [0, 'MCC', 'num'], [0, 'recall', 'num'], [0, 'n_pos', 'num']],
        Object.entries(m.by_attack).map(([k, v]) =>
          `<tr><td>${esc(k)}</td><td class="num">${n4(v.mcc)}</td>
           <td class="num">${n4(v.recall)}</td><td class="num">${v.n_pos}</td></tr>`).join(''))
      }</div>` : ''}`) +
      card('Per accusation', table(
        [[0, '#', 'num'], [0, 'victim', 'num'], [0, 'score', 'num'], [0, 'verdict'],
         [0, 'action'], [0, 'pattern'], [0, 'variant mix']], rows) +
        `<div class="pv-key"><span class="single">single</span><span class="sybil">sybil</span>
         <span class="timing">timing</span><span class="colluding">colluding</span></div>`) +
      notes(d.notes);
  },

  llm(d) {
    const s = d.summary;
    const rows = d.rows.slice(0, 300).map(r => `<tr>
      <td class="num">${r.event}</td>
      <td class="${r.verdict === 'false_accusation' ? 'acc' : ''}">${esc(r.verdict)}</td>
      <td class="num">${n2(r.confidence)}</td>
      <td>${r.escalate ? '<span class="bad">escalated</span>' : ''}</td>
      <td class="num">${n3(r.gnn_score)}</td>
      <td class="hash">${esc((r.prompt_hash || '').slice(0, 24))}…</td></tr>`).join('');
    const inc = (d.incidents || []).slice(0, 100).map(r => `<tr>
      <td class="num">${r.event}</td><td class="num">V${r.accuser}</td>
      <td class="num">V${r.victim}</td><td class="num">${n3(r.gnn_score)}</td>
      <td class="good">${esc(r.action)}</td></tr>`).join('');
    return `<div class="kpi-row">
        ${tile('verdicts', s.verdicts, 'on GNN-flagged accusations')}
        ${tile('false accusation', s.false_accusation, '', 'bad')}
        ${tile('legitimate', s.legitimate, '', 'good')}
        ${tile('escalated', s.escalated, `confidence ≥ τ ${s.tau_llm}`)}
        ${tile('incidents', s.incidents, 'led to real mitigation')}
      </div>` +
      card('What the prompt is made of', `<ol class="kpi-note" style="line-height:1.9">${
        s.sources.map(x => `<li>${esc(x)}</li>`).join('')}</ol>`) +
      card('Per accusation', table(
        [[0, '#', 'num'], [0, 'verdict'], [0, 'conf', 'num'], [0, 'gate'],
         [0, 'GNN score', 'num'], [0, 'prompt hash']], rows)) +
      (inc ? card('Confirmed incidents and their consequence', table(
        [[0, '#', 'num'], [0, 'accuser', 'num'], [0, 'victim', 'num'],
         [0, 'GNN', 'num'], [0, 'action']], inc)) : '') +
      notes(d.notes);
  },

  chain(d) {
    const s = d.summary;
    const rows = d.rows.slice(0, 300).map(r => `<tr>
      <td class="num">${r.event}</td>
      <td>${r.endorsed ? '<span class="good">✓</span>' : '<span class="bad">✗</span>'}</td>
      <td>${r.divergence ? '<span class="bad">yes</span>' : ''}</td>
      <td class="reason">${esc(r.reason)}</td>
      <td>${r.blocked ? '<span class="good">blocked</span>' : ''}</td>
      <td class="num">${n3(r.controller_trust)}</td></tr>`).join('');
    return `<div class="kpi-row">
        ${tile('SC2 submissions', s.submissions)}
        ${tile('endorsed', s.endorsed, `${s.submissions - s.endorsed} refused`)}
        ${tile('divergence', s.divergence, 'sealed evidence ≠ outcome', s.divergence ? 'bad' : '')}
        ${tile('blocked on chain', s.blocked, '', 'good')}
        ${tile('stake burned', n3(s.stake_burned), `${s.stake_filings} filings`)}
      </div>` +
      card('The four contracts', s.contracts.map(([k, v]) =>
        `<div class="sc-row"><b>${k}</b><span>${esc(v)}</span></div>`).join('')) +
      (s.failovers ? card('SC3 controller failover', table(
        [[0, 'event', 'num'], [0, 'from', 'num'], [0, 'to', 'num'], [0, 'zone', 'num'],
         [0, 'trust', 'num'], [0, 'epoch', 'num']],
        d.failovers.map(f => `<tr><td class="num">${f.event}</td>
          <td class="num">C${f.controller}</td><td class="num">C${f.backup}</td>
          <td class="num">${f.zone}</td><td class="num">${n3(f.trust)}</td>
          <td class="num">${f.epoch}${f.no_standby ? ' <span class="bad">no standby</span>' : ''}</td>
          </tr>`).join(''))) : '') +
      (s.custody_actions ? card('SC4 RSU custody', table(
        [[0, 'RSU', 'num'], [0, 'state'], [0, 'trust', 'num'], [0, 'event', 'num']],
        d.custody.map(c => `<tr><td class="num">R${c.rsu}</td>
          <td class="${c.state === 'REMOVED' ? 'acc' : 'warn'}">${c.state}</td>
          <td class="num">${n3(c.trust)}</td><td class="num">${c.event}</td></tr>`).join(''))) : '') +
      card('Per accusation', table(
        [[0, '#', 'num'], [0, 'endorsed'], [0, 'divergence'], [0, 'reason'],
         [0, 'outcome'], [0, 'ctrl trust', 'num']], rows)) +
      notes(d.notes);
  },

  keymgmt(d) {
    const s = d.summary, l = s.lkh || {};
    return `<div class="kpi-row">
        ${tile('re-key events', l.events || 0)}
        ${tile('messages', l.messages || 0, `mean ${l.mean_per_rekey || 0} per re-key`)}
        ${tile('group size', l.mean_group || 0, `log₂ = ${l.log2_group || 0}`)}
        ${tile('mean latency', ((l.mean_latency_us || 0) / 1000).toFixed(2) + ' ms')}
      </div>` +
      card('Re-key cost — the O(m log N) claim, measured', `
        <dl class="def-list">
          <dt>mean messages</dt><dd>${l.mean_per_rekey || 0} per revocation</dd>
          <dt>log₂(group)</dt><dd>${l.log2_group || 0}  ← what a hierarchy should cost</dd>
          <dt>flat would cost</dt><dd>${l.flat_would_be || 0}  ← O(N), one per member</dd>
          <dt>revocation batch</dt><dd>${esc(s.revocation_batch)}</dd>
        </dl>
        ${l.by_kind ? `<div class="tbl-scroll" style="margin-top:10px">${table(
          [[0, 'trigger'], [0, 'count', 'num'], [0, 'mean messages', 'num']],
          Object.entries(l.by_kind).map(([k, v]) =>
            `<tr><td>${esc(k)}</td><td class="num">${v.n}</td>
             <td class="num">${v.mean_c}</td></tr>`).join(''))}</div>` : ''}
        ${s.dkg_line ? `<p class="kpi-note" style="margin-top:10px">
          run's own line: <code>${esc(s.dkg_line)}</code></p>` : ''}`) +
      notes(d.notes);
  },
};

/* ----------------------------------------------------------------------- nav ---- */
function tabs(active) {
  const cards = (S.overview && S.overview.cards) || [];
  $('tabs').innerHTML =
    `<button data-go="" aria-pressed="${!active}">Overview</button>` +
    cards.map(c => `<button data-go="${c.component}" data-available="${c.available}"
      aria-pressed="${active === c.component}" ${c.available ? '' : 'disabled'}
      >${esc(c.label)}</button>`).join('');
  $('tabs').querySelectorAll('button').forEach(b => { b.onclick = () => go(b.dataset.go); });
}

async function go(name) {
  S.current = name || null;
  const u = new URL(location);
  if (name) u.searchParams.set('c', name); else u.searchParams.delete('c');
  history.replaceState({}, '', u);
  tabs(S.current);

  if (!name) { renderOverview(S.overview); return; }
  $('view').innerHTML = '<p class="muted" style="padding:20px">Loading…</p>';
  try {
    const d = await (await fetch(
      `/api/runs/${encodeURIComponent(runId)}/component/${name}`)).json();
    $('view').innerHTML = (VIEWS[name] || (() => '<p class="muted">No view.</p>'))(d);

    // Notes say what this run did NOT capture. Each view appends them last, which buries
    // them under a 300-row table — so lift them to just under the headline numbers, where
    // they are read before the data they qualify.
    const nts = $('view').querySelector('.notes');
    const kpi = $('view').querySelector('.kpi-row');
    if (nts && kpi && kpi.nextSibling !== nts) kpi.after(nts);
  } catch (e) {
    $('view').innerHTML = `<p style="color:var(--danger);padding:20px">${esc(e)}</p>`;
  }
}

/* ---------------------------------------------------------------------- boot ---- */
if (!runId) location.href = '/runs';
$('run-label').textContent = runId;
for (const [id, page] of [['link-stats', 'stats'], ['link-metrics', 'metrics'],
                          ['link-live', 'live']]) {
  $(id).href = `/${page}?run=${encodeURIComponent(runId)}`;
}

fetch(`/api/runs/${encodeURIComponent(runId)}/components`)
  .then(r => r.json())
  .then(d => {
    S.overview = d;
    if (d.imported) $('imported-chip').innerHTML = '<span class="badge warn">imported</span>';
    $('loading').remove();
    go(new URLSearchParams(location.search).get('c') || '');
  })
  .catch(e => { $('loading').innerHTML =
    `<span style="color:var(--danger)">could not load components: ${esc(e)}</span>`; });
