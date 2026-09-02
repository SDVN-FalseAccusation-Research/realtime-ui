/* Configuration page.
 *
 * Everything here is generated from GET /api/flags — the registry the backend derived
 * from the simulator's own --PrintHelp. That means the form can never offer a flag the
 * binary does not have, and defaults can never drift from it.
 *
 * The page must fit one screen with no scrolling, so the ~120 advanced flags live in a
 * modal rather than expanding the layout.
 */
'use strict';

const $ = (id) => document.getElementById(id);

const ATTACK_META = {
  single_data:      ['Single', 'data'],
  sybil_data:       ['Sybil', 'data'],
  timing_data:      ['Timing', 'data'],
  colluding_data:   ['Colluding', 'data'],
  single_control:   ['Single', 'control'],
  sybil_control:    ['Sybil', 'control'],
  timing_control:   ['Timing', 'control'],
  evidence_control: ['Evidence spoof', 'control'],
  report_tamper_rsu:['RSU tamper', 'custody'],
};

// Which advanced flags belong on which tab. Anything not listed lands on "Other", so a new
// simulator flag still shows up rather than disappearing.
const TABS = {
  Timing:   ['warmupTime','warmupAccusationStart','warmupAccusations','warmupAccusationSpacing',
             'attackSpacing','evalWindow','beaconInterval','trustInterval','simTime','hardSimTime'],
  Trust:    ['eta','beta','zeta','gamma','penalty','blacklistThreshold','initialGlobalTrust',
             'commRange','normalQuality','noiseQuality','tMax','deltaAcc'],
  Attack:   ['sybilCount','colluderCount','timingMinNeighbours','singleMaxNeighbours',
             'maliciousRsuId','maliciousRsuCount','rsuTamperFraction','victimPoolSize',
             'attackEvents','proximityMargin'],
  Genuine:  ['misbehaveModel','misbehavePercent','misbehaveDropProb','genuineTriggerDelta',
             'genuineTriggerWindow','genuineAccusations','genuineMinCreditable'],
  Ablation: ['pqcGate','zkpGate','endorserRecompute','endorserDivergenceOnly','sc3Audit',
             'sc4Audit','vehiclePqc','infraPqc','zkpContextA','zkpContextB','zkpContextCD',
             'gnnBlock','llmBlock','smartContracts','trustContinuity','lkhFlat','lwRules',
             'revocationBatch','evidenceHash'],
  Stake:    ['rho0','deltaR','tauMin','tauCons','etaC','tauCT','etaF','etaTau','timeDelta',
             'peersPerZone','etaRsu','tauRsuQuar','tauRsuRem'],
};

const DEFENCE = [
  ['blockchain',      'Blockchain (SC1–SC4)', 'blockchain', 'needs the bridge + zk-STARK sidecar'],
  ['gnnDetect',       'GNN detection',        'gnn',        'needs the trained GNN sidecar (:7071)'],
  ['llmReason',       'LLM confirmation',     'llm',        'needs the Qwen sidecar (:7072)'],
  ['secureBridge',    'PQ bridge transport',  'blockchain', 'ML-KEM-1024 + AES-256-GCM'],
  ['encryptChannels', 'Encrypted channels',   null,         'zone-key AES on V2V/V2I'],
  ['lwMode',          'Lightweight mode',     null,         'rule-based; no BC/ZKP/GNN/LLM'],
];

const S = { flags: {}, meta: null, health: null, cfg: {}, adv: {}, tab: 'Timing' };

/* ------------------------------------------------------------------ helpers ---- */
function estimate() {
  const n = +S.cfg.numVehicles || 200;
  const pct = +S.cfg.attackPercent || 0;
  const rounds = +S.cfg.attackRounds || 1;
  const window = +S.cfg.attackWindow || 300;
  const warmup = +S.cfg.warmupTime || 60;

  const attackers = Math.round(n * pct / 100);
  const opportunities = Math.max(0, attackers * rounds);
  // the simulator enforces a 3.5 s floor between events and widens the window to keep it
  const spacing = opportunities ? Math.max(3.5, window / opportunities) : 0;
  const effWindow = Math.max(window, spacing * opportunities);
  const simTime = warmup + effWindow + 5;
  const warmupAcc = +S.adv.warmupAccusations || 3;

  // WALL CLOCK. A defended run is not the same machine as an undefended one, and using one
  // number for both was wrong by 2-3x.
  //
  //   undefended  365 s of simulated time took 94.4 s wall at 200 vehicles -> /3.9
  //   defended    every accusation makes a bridge round trip through Fabric, so cost tracks
  //               the ACCUSATION COUNT as much as the clock. Least-squares fit over five
  //               UI-LAUNCHED runs (smoke p2, demo p9, single p60, sybil p80, colluding p80):
  //                   wall = 126.4 + 0.1851*simTime + 2.219*opportunities
  //
  // TREAT THIS AS +/-15%, NOT the 7.6% the in-sample fit reports. A four-point version of
  // this model predicted a held-out colluding-p80 run at 676 s against an actual 596 s --
  // 13.4% out. The reason is visible in the data: sybil p80 and colluding p80 have
  // IDENTICAL simTime (865) and opportunities (160) and still differ by 97 s, 16.3%, purely
  // by attack type -- sybil spawns 160 identity events and 150 stake burns, colluding 480
  // colluder events and 25 burns. No model using only these two inputs can do better than
  // that spread, so the honest reading is a planning band, not a figure.
  //
  // FITTED ON UI RUNS, NOT SWEEP CELLS, and that distinction is the whole accuracy story.
  // An earlier fit used run_sweep.sh's cells, which pass --misbehaveModel=1
  // --misbehavePercent=10 and so carry genuine-report traffic the UI never generates: the
  // p60 sweep cell logged 190 events where the equivalent UI run logged 107. That model
  // over-predicted real UI runs by 22.4% (p60) and 13.5% (p80). Refitting on the population
  // actually being predicted brought both under 5%.
  //
  // All four have blockchain+GNN+LLM on; blockchain alone is faster, so this over-estimates
  // that rarer configuration rather than under-estimating it.
  const defended = !!S.cfg.blockchain;
  const wall = (defended ? 126.4 + 0.1851 * simTime + 2.219 * opportunities
                         : simTime / 3.9) * (n / 200);
  return { attackers, opportunities, simTime, wall, defended,
           accusations: opportunities + warmupAcc, spacing };
}

function fmtMin(s) {
  return s < 90 ? `${Math.round(s)} s` : `${(s / 60).toFixed(1)} min`;
}

function collect() {
  const out = { ...S.adv, ...S.cfg };
  // UI-only keys never reach the API
  delete out._preset;
  return out;
}

function renderCommand() {
  const c = collect();
  const parts = ['sdvn_false_accusation_realnet'];
  Object.keys(c).sort().forEach(k => {
    if (k === 'trace') return;
    parts.push(`--${k}=${typeof c[k] === 'boolean' ? (c[k] ? 1 : 0) : c[k]}`);
  });
  if (c.trace === 'manhattan') parts.push('--traceFile=../sumo-manhattan/manhattan.tcl');
  parts.push('--debugLogs=1', '--liteLogs=1', '--csvPrefix=results/ui/<run-id>/run');
  $('cmd').textContent = parts.join(' \\\n  ');
}

function renderEstimate() {
  const e = estimate();
  $('est-acc').textContent = e.accusations;
  $('est-sim').textContent = `${Math.round(e.simTime)} s`;
  $('est-wall').textContent = `~${fmtMin(e.wall)}`;
  $('est-disp').textContent = fmtMin(e.simTime);
  $('pct-n').textContent = e.attackers;

  const w = [];
  if (S.cfg.trace === 'manhattan' && S.cfg.numVehicles > 200)
    w.push('The Manhattan trace has only 200 vehicles — the rest would sit at the origin.');
  if (S.cfg.numRsus >= 60 && S.cfg.numVehicles >= 190)
    w.push('≥60 RSUs with ~200 vehicles currently crashes the simulator (open bug TASK 5b). ' +
           'Use 56.');
  if (S.cfg.trace === 'manhattan' && (+S.adv.warmupAccusationStart || 35) < 30)
    w.push('Vehicles are still being inserted before t≈42 s — start accusations after 30 s.');
  if (e.simTime > 1199)
    w.push(`Simulated time ${Math.round(e.simTime)} s exceeds the trace (1199 s); ` +
           'vehicles would freeze at their last waypoint.');
  if (e.spacing && e.spacing > window_guard())
    w.push('Attack window widened by the simulator to keep ≥3.5 s between events.');
  $('warnings').innerHTML = w.map(t => `<div class="warnbox">${t}</div>`).join('');
}
function window_guard() { return (+S.cfg.attackWindow || 300) / Math.max(1, estimate().opportunities); }

/* -------------------------------------------------------------------- render --- */
function renderAttacks() {
  $('attack-grid').innerHTML = S.meta.attack_types.map(a => {
    const [label, plane] = ATTACK_META[a] || [a, ''];
    return `<button data-atk="${a}" aria-pressed="${a === S.cfg.attackType}">
              <span class="plane">${plane}</span>${label}</button>`;
  }).join('');
  $('attack-grid').querySelectorAll('button').forEach(b => {
    b.onclick = () => { S.cfg.attackType = b.dataset.atk; $('atk-val').textContent = b.dataset.atk;
                        renderAttacks(); refresh(); };
  });
}

function renderDefence() {
  const can = (S.health && S.health.can) || {};
  // The note depends on which layers THIS config asks for, so it is re-derived whenever a
  // toggle changes, not only when health is polled.
  renderStackNote(S.health || {});
  $('defence-toggles').innerHTML = DEFENCE.map(([flag, name, dep, why]) => {
    const avail = dep ? !!can[dep] : true;
    const on = !!S.cfg[flag];
    return `<label class="toggle" data-available="${avail}">
      <input type="checkbox" data-flag="${flag}" ${on ? 'checked' : ''}
             ${avail ? '' : 'disabled'}>
      <span class="name">${name}</span>
      <span class="why">${avail ? why : 'unavailable — ' + why}</span>
    </label>`;
  }).join('');
  $('defence-toggles').querySelectorAll('input').forEach(i => {
    i.onchange = () => {
      S.cfg[i.dataset.flag] = i.checked ? 1 : 0;
      // llmReason is gated by gnnDetect, which is gated by blockchain — mirror that here
      // rather than letting the simulator silently ignore the flag.
      if (i.dataset.flag === 'gnnDetect' && i.checked) S.cfg.blockchain = 1;
      if (i.dataset.flag === 'llmReason' && i.checked) { S.cfg.gnnDetect = 1; S.cfg.blockchain = 1; }
      renderDefence(); refresh();
    };
  });
}

function renderPresets() {
  $('presets').innerHTML = Object.entries(S.meta.presets).map(([k, p]) =>
    `<button data-preset="${k}" title="${p._desc}"
             aria-pressed="${S.cfg._preset === k}">${p._label}</button>`).join('');
  $('presets').querySelectorAll('button').forEach(b => {
    b.onclick = () => applyPreset(b.dataset.preset);
  });
}

function applyPreset(key) {
  const p = { ...S.meta.presets[key] };
  delete p._label; delete p._desc;
  // A preset may ask for defence layers (the demo one does). Drop any whose service is not
  // up rather than sending the flag anyway: the simulator would accept it and quietly
  // degrade, which is the one failure mode that looks like a successful run.
  const can = (S.health && S.health.can) || {};
  for (const [flag, dep] of [['blockchain','blockchain'], ['secureBridge','blockchain'],
                             ['gnnDetect','gnn'], ['llmReason','llm']]) {
    if (p[flag] && !can[dep]) delete p[flag];
  }
  Object.assign(S.cfg, p);
  S.cfg._preset = key;
  ['numVehicles','numRsus','numControllers','attackWindow','attackRounds','seed',
   'attackPercent'].forEach(k => { if ($(k) && S.cfg[k] !== undefined) $(k).value = S.cfg[k]; });
  S.adv.warmupTime = p.warmupTime;
  S.adv.warmupAccusationStart = p.warmupAccusationStart;
  $('pct-val').textContent = S.cfg.attackPercent;
  $('atk-val').textContent = S.cfg.attackType;
  renderAttacks(); renderPresets(); renderTrace(); refresh();
}

function renderTrace() {
  $('trace-manhattan').setAttribute('aria-pressed', S.cfg.trace === 'manhattan');
  $('trace-synthetic').setAttribute('aria-pressed', S.cfg.trace === 'synthetic');
}

function renderHealth() {
  const h = S.health || {};
  const can = h.can || {};
  const bits = [];
  for (const [k, label] of [['zkp','ZKP'],['gnn','GNN'],['llm','LLM'],['bridge','Bridge']]) {
    // The GNN gets `can.gnn`, not `up`. A sidecar serving the WRONG model answers every
    // probe happily; a green dot there would be the most expensive kind of true statement.
    const ok = k === 'gnn' ? !!can.gnn : !!(h[k] && h[k].up);
    bits.push(`<span class="badge"><span class="dot ${ok ? 'up' : 'down'}"></span>${label}</span>`);
  }
  const d = h.docker && h.docker.up;
  bits.push(`<span class="badge"><span class="dot ${d ? 'up' : 'down'}"></span>Docker</span>`);
  $('health-strip').innerHTML = bits.join(' ');
  renderStackNote(h);
}

/* What is missing, and the one command that fixes it.
 *
 * Keyed on what is AVAILABLE, not on what the current config asks for: an unavailable
 * layer's toggle is rendered disabled, so a note that waited for the user to request the
 * layer could never appear at all. The operator needs this before they choose, not after.
 */
function renderStackNote(h) {
  const el = $('stack-note');
  if (!el) return;
  const can = h.can || {};
  const items = [];

  // ONE DEFENDED RUN PER LEDGER. Raised even when everything is green, because this is the
  // failure that looks like success: the run exits 0, the summary prints, and not a single
  // accusation is filed.
  const led = h.ledger || {};
  if (led.state === 'used') {
    items.push(`<b>Ledger already used</b> — ${led.note || 'reset it before a defended run.'}`);
  } else if (led.state === 'unknown' && can.blockchain) {
    items.push(`<b>Ledger state unknown</b> — ${led.note || ''}`);
  }

  if (!can.blockchain) {
    const parts = [];
    if (!(h.bridge && h.bridge.up)) parts.push('the bridge on :7545 is down');
    if (!(h.zkp && h.zkp.up)) parts.push('the zk-STARK sidecar on :7070 is down');
    items.push(`<b>No defended run available</b> — ${parts.join(' and ')}. ` +
               'Blockchain, ZKP, GNN and LLM all sit on the <code>--blockchain=1</code> path.');
  }
  if (!can.gnn) {
    const g = h.gnn || {};
    items.push(g.model_note
      ? `<b>GNN model mismatch</b> — ${g.model_note}.`
      : '<b>GNN unavailable</b> — :7071 is not serving a trained model.');
  }
  if (!can.llm) items.push('<b>LLM unavailable</b> — the sidecar on :7072 is down.');
  if (h.docker && !h.docker.up) {
    items.push(h.docker.reason === 'no_permission'
      ? `<b>Docker not permitted</b> — ${h.docker.note}`
      : '<b>Docker is down</b> — Fabric cannot be reset, so the ledger cannot be made fresh.');
  }

  if (!items.length) { el.hidden = true; return; }
  el.hidden = false;
  const cmd = (h.ledger && h.ledger.state === 'used' && (h.can || {}).blockchain)
    ? 'realtime-ui/tools/demo_stack.sh reset'      // the stack is up; only the ledger is stale
    : 'realtime-ui/tools/demo_stack.sh up';
  el.innerHTML = `<ul>${items.map(i => `<li>${i}</li>`).join('')}</ul><code>${cmd}</code>`;
}

/* ------------------------------------------------------------- advanced modal -- */
function renderTabs() {
  const names = [...Object.keys(TABS), 'Other'];
  $('modal-tabs').innerHTML = names.map(n =>
    `<button data-tab="${n}" aria-pressed="${n === S.tab}">${n}</button>`).join('');
  $('modal-tabs').querySelectorAll('button').forEach(b => {
    b.onclick = () => { S.tab = b.dataset.tab; renderTabs(); renderFlagGrid(); };
  });
}

function flagsForTab(tab) {
  const listed = new Set(Object.values(TABS).flat());
  const all = Object.keys(S.flags);
  const shown = new Set([...DEFENCE.map(d => d[0]),
                         'numVehicles','numRsus','numControllers','attackType',
                         'attackPercent','attackWindow','attackRounds','seed']);
  if (tab === 'Other') return all.filter(f => !listed.has(f) && !shown.has(f)).sort();
  return (TABS[tab] || []).filter(f => f in S.flags);
}

function renderFlagGrid() {
  $('flag-grid').innerHTML = flagsForTab(S.tab).map(name => {
    const f = S.flags[name];
    const val = S.adv[name] !== undefined ? S.adv[name] : f.default;
    let input;
    if (f.type === 'bool') {
      input = `<select data-adv="${name}">
                 <option value="1" ${val == 1 ? 'selected' : ''}>on</option>
                 <option value="0" ${val == 1 ? '' : 'selected'}>off</option></select>`;
    } else if (f.choices) {
      input = `<select data-adv="${name}">` + f.choices.map(c =>
        `<option ${c === val ? 'selected' : ''}>${c}</option>`).join('') + '</select>';
    } else {
      const step = f.type === 'float' ? 'any' : '1';
      const rng = (f.min !== undefined) ? `min="${f.min}" max="${f.max}"` : '';
      input = `<input type="number" step="${step}" ${rng} data-adv="${name}" value="${val}">`;
    }
    return `<div class="field">
      <label>${name}<span class="val">${f.default}</span></label>
      ${input}<div class="help">${f.help || ''}</div></div>`;
  }).join('') || '<p style="color:var(--ink-faint)">No flags on this tab.</p>';

  $('flag-grid').querySelectorAll('[data-adv]').forEach(el => {
    el.onchange = () => {
      const n = el.dataset.adv, f = S.flags[n];
      S.adv[n] = (f.type === 'str') ? el.value : Number(el.value);
      refresh();
    };
  });
}

/* --------------------------------------------------------------------- submit -- */
async function run() {
  $('error').innerHTML = '';
  $('run-btn').disabled = true;
  try {
    const res = await fetch('/api/runs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collect()),
    });
    const body = await res.json();
    if (!res.ok) {
      $('error').innerHTML =
        `<div class="errbox"><b>${body.field || 'error'}</b> — ${body.error || body.detail}</div>`;
      const el = $(body.field); if (el) el.classList.add('bad');
      $('run-btn').disabled = false;
      return;
    }
    location.href = `/live?run=${encodeURIComponent(body.run_id)}`;
  } catch (err) {
    $('error').innerHTML = `<div class="errbox">${err}</div>`;
    $('run-btn').disabled = false;
  }
}

function refresh() { renderEstimate(); renderCommand(); }

/* ----------------------------------------------------------------------- boot -- */
async function boot() {
  const [flagsRes, healthRes] = await Promise.all([
    fetch('/api/flags'), fetch('/api/health'),
  ]);
  if (!flagsRes.ok) {
    document.body.innerHTML =
      `<pre style="padding:40px;color:#ff6b6b">${(await flagsRes.json()).detail}</pre>`;
    return;
  }
  S.meta = await flagsRes.json();
  S.health = await healthRes.json();
  S.meta.flags.forEach(f => { S.flags[f.name] = f; });

  $('flag-count').textContent = `${S.meta.flags.length} flags`;
  $('v-max').textContent = `max ${S.meta.limits.trace_vehicles}`;

  applyPreset(S.meta.default_preset);
  renderDefence(); renderHealth(); renderTabs(); renderFlagGrid();

  // wire the simple inputs
  ['numVehicles','numRsus','numControllers','attackWindow','attackRounds','seed']
    .forEach(id => { $(id).oninput = () => { S.cfg[id] = Number($(id).value);
                                             $(id).classList.remove('bad');
                                             S.cfg._preset = null; renderPresets(); refresh(); }; });
  $('attackPercent').oninput = () => {
    S.cfg.attackPercent = Number($('attackPercent').value);
    $('pct-val').textContent = S.cfg.attackPercent;
    S.cfg._preset = null; renderPresets(); refresh();
  };
  $('trace-manhattan').onclick = () => { S.cfg.trace = 'manhattan'; renderTrace(); refresh(); };
  $('trace-synthetic').onclick = () => { S.cfg.trace = 'synthetic'; renderTrace(); refresh(); };

  $('more-btn').onclick = () => $('more-modal').showModal();
  $('modal-close').onclick = () => $('more-modal').close();
  $('reset-tab').onclick = () => {
    flagsForTab(S.tab).forEach(n => delete S.adv[n]);
    renderFlagGrid(); refresh();
  };
  $('run-btn').onclick = run;

  setInterval(async () => {
    try { S.health = await (await fetch('/api/health')).json();
          renderHealth(); renderDefence(); } catch (e) { /* transient */ }
  }, 10000);
}

boot();
