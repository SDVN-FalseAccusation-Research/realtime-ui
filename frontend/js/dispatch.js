/* The playback cursor and the event handler table.
 *
 * One integer walks the sorted event list against the playback clock:
 *
 *     while (cursor < events.length && events[cursor].t <= Clock.t) apply(events[cursor++])
 *
 * THE INVARIANT that makes scrubbing work: every handler takes (ev, animate). With
 * animate=false it only sets state — no tweens, no timers, no dependence on a previous
 * animation having finished. So any point in the run can be reached by resetting the world
 * and re-applying the whole prefix silently. Break that and backward scrub breaks with it.
 *
 * Unknown event types are ignored rather than throwing, which is what lets P8 add
 * `layer` and `chain_tx` events later without touching this file.
 */
'use strict';

/** Full-width, above the map: the one place a run-level failure cannot hide. */
function banner(html, cls = '') {
  const el = document.getElementById('run-banner');
  if (!el) return;
  el.className = cls;
  el.innerHTML = html;
  el.hidden = false;
}

const Dispatch = {
  follow: false,          // camera follows each accusation; see the `follow` checkbox
  cursor: 0,
  cfg: {},
  roles: { attackers: [], misbehavers: [] },
  counts: { total: 0, accepted: 0, blocked: 0 },
  current: null,          // the accusation being shown
  layers: {},             // event id -> { zkp|gnn|llm|... : event }  (imported/P8 runs)
  pipeline: [],
  ledger: [],
  seen: new Set(),        // decision event ids already applied (back-fill guard)
  seenAcc: new Set(),     // accusation ids already applied -- see the handler

  reset() {
    this.cursor = 0;
    this.counts = { total: 0, accepted: 0, blocked: 0 };
    this.current = null;
    this.layers = {};
    this.pipeline = [];
    this.ledger = [];
    this.seen.clear();
    this.seenAcc.clear();
    Sidebar.clearHistory();
    Ribbon.reset();
    Sidebar.event(null);
    World.resetRoles(this.roles.attackers, this.roles.misbehavers);
  },

  /** An event was spliced into Stream.events at `i`. Keep the cursor on the same EVENT.
   *
   * The cursor is an array index, so a splice below it shifts every later element right and
   * the index silently comes to mean a different, already-applied event. Measured before
   * this existed: 20 late verdicts advanced the cursor 2926 -> 2946, re-applied 3
   * accusations, and destroyed 20 of 22 in-flight packets — because re-applying an
   * accusation calls World.clearEventMarks(). On screen that was the whole map blinking
   * several times a second, and `counts.total` over-reporting (45 -> 48).
   *
   * It only ever happened on a LIVE run: a finished run's events arrive already ordered, so
   * every replay looked perfect, which is exactly why this shipped.
   */
  noteInsert(i) {
    if (i < this.cursor) this.cursor++;
  },

  /** Advance to the clock. Called every frame. */
  pump() {
    const evs = Stream.events;
    let n = 0;
    while (this.cursor < evs.length && (evs[this.cursor].t || 0) <= Clock.t) {
      this.apply(evs[this.cursor++], true);
      if (++n > 400) break;           // never block a frame on a burst
    }
  },

  /** Re-apply everything up to t with animation off. Used on seek, including backwards. */
  rebuild(t) {
    Fx.enabled = false;
    Fx.clear();
    this.reset();
    const evs = Stream.events;
    while (this.cursor < evs.length && (evs[this.cursor].t || 0) <= t) {
      this.apply(evs[this.cursor++], false);
    }
    Fx.enabled = true;
  },

  /** An event that arrives BEHIND the cursor (late verdicts from the CSV) is applied at
   *  once rather than dropped — otherwise the first accusations would never show one. */
  backfill(ev) {
    if ((ev.t || 0) <= Clock.t) this.apply(ev, false);
  },

  apply(ev, animate) {
    const h = this.handlers[ev.type];
    if (h) h.call(this, ev, animate);
  },

  /* ------------------------------------------------------------------ handlers -- */
  handlers: {
    run_start(ev) {
      this.cfg = ev.config || {};
      World.buildTopology(ev.topology);
      Ribbon.build(document.getElementById('ribbon'), this.cfg);
      Sidebar.status(this.cfg, this.counts, Clock.fmt(0));
    },

    schedule(ev) {
      if (ev.sim_time) Clock.simEnd = ev.sim_time;
    },

    roles(ev) {
      if (ev.role === 'attackers') {
        this.roles.attackers = ev.ids;
        this.roles.truncated = ev.truncated;
        ev.ids.forEach(i => World.setState(i, 'attacker'));
        if (ev.truncated) {
          // The simulator caps the printed list at 15. Say so rather than quietly
          // colouring a subset — at the Extended preset (18 attackers) this matters.
          Sidebar.sys(`<div class="warn-line">attacker list truncated by the simulator: ` +
                      `showing ${ev.ids.length} of ${ev.count}</div>`);
        }
      } else if (ev.role === 'misbehavers') {
        this.roles.misbehavers = ev.ids;
        ev.ids.forEach(i => { if (World.getState(i) === 'honest') World.setState(i, 'misbehaver'); });
      }
    },

    accusation(ev, animate) {
      // APPLY EACH ACCUSATION ONCE. This handler is destructive — clearEventMarks() empties
      // the fx and packets layers and strips every mark — so replaying one mid-flight wipes
      // the animation of whatever is currently on screen and double-counts it. noteInsert()
      // is what stops the cursor drifting onto an applied event in the first place; this is
      // the belt to that pair of braces, because the failure is silent and looks like a
      // rendering glitch rather than a logic error. reset() clears the set, so rebuild()
      // and backward scrub still replay everything.
      if (ev.event !== undefined) {
        if (this.seenAcc.has(ev.event)) return;
        this.seenAcc.add(ev.event);
      }

      // close out the previous event's highlighting
      World.clearEventMarks();
      Ribbon.reset();
      this.pipeline = [];
      this.current = ev;
      this.counts.total++;

      const a = ev.accuser.v, v = ev.victim.v;
      World.mark(a, 'accuser'); World.mark(v, 'victim'); World.showRange(a, true);
      Sidebar.event(ev);

      Ribbon.set('accuser', 'done', 'V' + a);

      // Serving RSU: nearest to the accuser, which is how the simulator chooses it.
      const ap = Assets.pose(a, ev.t);
      const rsu = ap ? Geo.nearestRsu(ap.x, ap.y, World.rsus) : null;
      if (rsu) {
        World.rsuEl(rsu.id).classList.add('serving');
        Sidebar.setEventField('rsu', 'R' + rsu.id);
        Ribbon.set('rsu', 'active', 'R' + rsu.id);
        const zone = Geo.zoneOfRsu(rsu.id, World.topology.rsus, World.topology.controllers);
        Sidebar.setEventField('ctrl', 'C' + zone);
        ev._rsu = rsu.id; ev._zone = zone;
      }

      // Witnesses: geometric neighbours of the accuser. The simulator never prints
      // reporter identities, so these are in-range vehicles, labelled as such in the UI.
      const range = Number(this.cfg.commRange) || 300;
      const wits = ap ? Assets.neighbours(a, ev.t, range, 12) : [];
      ev._wits = wits.map(w => w.id);
      Sidebar.setEventField('wit', `${wits.length} in ${range} m`);
      Ribbon.set('reporters', 'done', `${wits.length} in range`);

      if (animate) {
        // Only when the presenter asks for it. Zooming the camera on every accusation
        // means the audience never sees the map and the presenter cannot hold a view.
        if (Dispatch.follow) World.focusOn([ap, rsu].filter(Boolean));
        Fx.tag({ v: a }, 'accuses V' + v, { kind: 'attacker' });
        Fx.ring({ v: v }, 'victim', { r: 260 });
        wits.forEach(w => World.mark(w.id, 'reporter'));
        if (rsu) {
          Fx.packet({ v: a }, { r: rsu.id }, 'accuse', { dur: 800, arc: 0.18 });
          // 1. the witnesses observe the victim...
          Fx.broadcast({ v: v }, wits.map(w => ({ v: w.id })), 'observe',
                       { dur: 600, arc: 0.1, stagger: 45 });
          Fx.rsuWindow(rsu.id, Number(this.cfg.evalWindow) || 3);

          // 2. ...then FILE what they saw to the serving RSU, which is the step the
          // defence actually acts on and the one the map never showed. The buffer counts
          // up as each report lands, so the RSU visibly fills before it decides.
          World.setRsuBuffer(rsu.id, 0);
          let landed = 0;
          Fx.collect(wits.map(w => ({ v: w.id })), { r: rsu.id }, 'report',
                     { dur: 700, arc: 0.14, stagger: 55, delay: 620,
                       onArrive: () => World.setRsuBuffer(rsu.id, ++landed) });
        }
      } else {
        wits.forEach(w => World.mark(w.id, 'reporter'));
        if (rsu) World.setRsuBuffer(rsu.id, wits.length);
      }
    },

    decision(ev, animate) {
      if (this.seen.has(ev.event)) return;
      this.seen.add(ev.event);

      const cur = this.current;
      const accepted = ev.accepted;
      if (accepted) this.counts.accepted++; else this.counts.blocked++;

      // Only refresh the detail panel if this verdict belongs to the event on screen;
      // a back-filled verdict for an earlier event must not overwrite the current one.
      if (cur && cur.event === ev.event) {
        Sidebar.setEventField('reports',
          `${ev.reports.true + ev.reports.false} · ` +
          `<span class="good">${ev.reports.true}T</span> / ` +
          `<span class="bad">${ev.reports.false}F</span>`);
        Sidebar.verdict(accepted, ev.stopped_by);
        Ribbon.set('rsu', 'done', `${ev.reports.true + ev.reports.false} rpts`);
        // The buffer was counting geometrically-inferred reporters; _decisions.csv knows
        // how many reports the RSU actually took and how they split. Correct it.
        if (cur._rsu != null) {
          World.setRsuBuffer(cur._rsu, ev.reports.true + ev.reports.false,
                             { t: ev.reports.true, f: ev.reports.false });
        }
        Ribbon.set('outcome', accepted ? 'failed' : 'blocked',
                   accepted ? 'ACCEPTED' : 'BLOCKED');

        // Per-layer rows. Latencies are real (from the CSV); a zero means the layer did
        // not run for this event, which for the GNN on a control-plane attack is correct
        // behaviour rather than a miss.
        const L = ev.latency_us || {};
        const rows = [];
        const seen = this.layers[ev.event] || {};
        const add = (key, name, on) => {
          if (!on) return;
          const us = L[key] || 0;
          const lay = seen[key];
          // A real verdict beats inferring "it ran" from a non-zero latency. Where the
          // layer genuinely did not run — the GNN on a control-plane attack, which fails
          // the ZK gate first — both agree, and the row says so rather than showing 0 ms.
          const verdict = lay
            ? (lay.score !== undefined ? `${lay.verdict} ${lay.score.toFixed(3)}` : lay.verdict)
            : null;
          const ran = !!lay || us > 0;
          rows.push({
            name,
            status: !ran ? 'skipped'
                         : (lay && (lay.verdict === 'FAIL' || lay.verdict === 'FLAGGED'
                                    || lay.verdict === 'false_accusation') ? 'bad' : 'ran'),
            value: !ran ? 'not run'
                        : `${verdict ? verdict + ' · ' : ''}${(us / 1000).toFixed(1)} ms`,
          });
          if (!lay) Ribbon.set(key, ran ? 'done' : 'skipped',
                               ran ? `${(us / 1000).toFixed(1)} ms` : 'skipped');
        };
        const bc = Number(this.cfg.blockchain) === 1;
        add('pqc', 'PQC signatures', bc);
        add('zkp', 'zk-STARK', bc);
        add('gnn', 'GNN', Number(this.cfg.gnnDetect) === 1);
        add('llm', 'LLM', Number(this.cfg.llmReason) === 1);
        add('bc', 'Chain (SC2/SC3)', bc);
        rows.push({ name: 'w1 vs w2', status: accepted ? 'bad' : 'good',
                    value: `${ev.w1.toFixed(2)} / ${ev.w2.toFixed(2)}` });
        Sidebar.pipeline(rows);

        if (ev.stopped_by && ev.stopped_by !== 'none') {
          // pass what actually ran, so a credited-but-not-final layer is not mislabelled
          const ran = {};
          for (const k of ['pqc', 'zkp', 'gnn', 'llm', 'bc'])
            ran[k] = !!seen[k] || (L[k] || 0) > 0;
          Ribbon.skipAfter(ev.stopped_by, ran);
        }

        if (bc) {
          this.ledger.unshift({
            title: `SC2.SubmitOutcome · event ${ev.event}`,
            time: `t=${ev.t.toFixed(1)}s`,
            lines: [
              `w1' ${ev.w1.toFixed(2)}  w2' ${ev.w2.toFixed(2)}`,
              `endorsed ${ev.blockchain_blocked ? '✗' : '✓'}` +
                (ev.divergence ? `  divergence: ${ev.divergence_reason || 'yes'}` : ''),
              `reports ${ev.reports.true + ev.reports.false} · σ_R re-verified`,
            ],
          });
          Sidebar.ledger(this.ledger.slice(0, 12));
        }

        if (animate) {
          const ref = ev.controller && World.controllers[ev.controller.c]
            ? { c: ev.controller.c } : (cur ? { v: cur.victim.v } : null);
          if (ref) {
            Fx.ring(ref, accepted ? 'danger' : 'blocked', { r: 300, dur: 900 });
            Fx.tag(ref, accepted ? 'ACCEPTED' : 'BLOCKED',
                   { kind: accepted ? 'danger' : 'blocked' });
          }
          if (cur && cur._rsu !== undefined && ev.controller)
            Fx.packet({ r: cur._rsu }, { c: ev.controller.c }, 'bundle',
                      { dur: 700, arc: 0.1 });
        }
      }

      if (ev.blacklisted && cur) World.setState(cur.victim.v, 'blacklisted');
      if (ev.trust_after !== undefined && cur) World.setTrust(cur.victim.v, ev.trust_after);

      Sidebar.pushHistory({
        event: ev.event,
        accuser: cur && cur.event === ev.event ? cur.accuser.v : '?',
        victim: cur && cur.event === ev.event ? cur.victim.v : '?',
        accepted, by: ev.stopped_by,
      });
      Sidebar.status(this.cfg, this.counts, Clock.fmt());
    },

    /* `layer`, `chain_tx`, `llm_incident` and `stake` come from a run's own CSVs (via the
       importer) and will come live once --liveStream exists. They carry the real verdicts,
       which are far more informative than inferring "ran" from a non-zero latency. */
    layer(ev, animate) {
        const slot = this.layers[ev.event] || (this.layers[ev.event] = {});
        slot[ev.layer] = ev;

        const label = {
          zkp: () => ev.verdict + (ev.v_geo ? ' (V_geo)' : ''),
          gnn: () => (ev.score !== undefined ? ev.score.toFixed(3) : ev.verdict),
          llm: () => ev.verdict === 'false_accusation' ? 'false accusation'
                                                       : (ev.verdict || '—'),
        }[ev.layer];

        const bad = ev.verdict === 'FAIL' || ev.verdict === 'FLAGGED'
                    || ev.verdict === 'false_accusation';
        Ribbon.set(ev.layer, bad ? 'blocked' : 'done', label ? label() : ev.verdict);

        if (animate && this.current && this.current.event === ev.event) {
          const tag = { zkp: 'ZKP ' + ev.verdict,
                        gnn: 'GNN ' + (ev.score !== undefined ? ev.score.toFixed(2) : ''),
                        llm: 'LLM ' + (ev.verdict || '') }[ev.layer];
          if (tag) Fx.tag({ v: this.current.victim.v }, tag,
                          { kind: bad ? 'blocked' : 'info' });
        }
    },

    chain_tx(ev, animate) {
      this.ledger.unshift({
        title: `${ev.fn} · event ${ev.event}`,
        time: `t=${(ev.t || 0).toFixed(1)}s`,
        lines: [
          `endorsed ${ev.endorsed ? '✓' : '✗'}` +
            (ev.divergence ? `  divergence: ${ev.divergence_reason || 'yes'}` : ''),
          ev.rolled_back ? 'outcome ROLLED BACK on chain' : 'outcome committed',
          `controller trust ${(ev.controller_trust ?? 0).toFixed(3)}` +
            (ev.failover ? '  → FAILOVER' : ''),
        ],
      });
      Sidebar.ledger(this.ledger.slice(0, 12));
      Ribbon.set('bc', ev.blocked ? 'blocked' : 'done',
                 ev.blocked ? 'BLOCKED' : (ev.endorsed ? 'endorsed' : 'not endorsed'));
      if (animate && ev.blocked) Fx.ring({ c: 0 }, 'blocked', { r: 260 });
    },

    llm_incident(ev, animate) {
      // Only escalations that actually led to mitigation reach this file, so `action` is a
      // real consequence (credential_revoked|isolated), not just an opinion.
      if (animate) Fx.tag({ v: ev.accuser.v }, ev.action.replace('|', ' + '),
                          { kind: 'blocked', hold: 2400 });
      World.setState(ev.accuser.v, 'blacklisted');
    },

    stake(ev, animate) {
      if (animate && ev.burned > 0)
        Fx.tag({ v: ev.accuser.v }, `stake −${ev.burned.toFixed(2)}`, { kind: 'warn' });
    },

    rsu_status(ev, animate) {
      const state = ev.state === 'REMOVED' ? 'removed' : 'quarantined';
      World.setRsuState(ev.rsu.r, state);
      if (animate) Fx.tag({ r: ev.rsu.r }, ev.state, { kind: 'warn' });
    },

    controller_failover(ev, animate) {
      World.setCtrlState(ev.controller.c, 'revoked');
      if (ev.backup) World.setCtrlState(ev.backup.c, 'active');
      if (animate) {
        Fx.tag({ c: ev.controller.c }, 'REVOKED', { kind: 'danger' });
        Fx.ring({ c: ev.controller.c }, 'danger', { r: 340 });
      }
    },

    pqc_reject(ev, animate) {
      if (animate) Fx.tag({ v: ev.accuser.v }, 'σ invalid', { kind: 'blocked' });
      Ribbon.set('pqc', 'blocked', 'forged');
    },

    sybil(ev, animate) {
      if (animate) Fx.tag({ v: ev.accuser.v }, `${ev.identities} sybil ids`, { kind: 'attacker' });
    },

    colluder(ev, animate) {
      World.mark(ev.colluder.v, 'reporter');
      if (animate && this.current)
        Fx.packet({ v: ev.colluder.v }, { v: this.current.victim.v }, 'accuse',
                  { dur: 700, arc: 0.2 });
    },

    rsu_tamper(ev, animate) {
      if (animate) Fx.tag({ r: ev.rsu.r }, `flipped ${ev.flipped}/${ev.total}`, { kind: 'danger' });
    },

    run_end(ev) {
      Sidebar.sys(`<div class="kv"><span>simulator summary</span><b>` +
                  `${ev.submitted} submitted · ${ev.accepted} accepted · ` +
                  `ASR ${(ev.successRate ?? 0).toFixed(3)}</b></div>`);
    },

    run_closed(ev) {
      // `state` before exit_code: a run the operator stopped exits -15 and is not a
      // failure, and a `degraded` run exits 0 while having filed no accusations at all.
      const st = ev.state || (ev.exit_code === 0 ? 'finished' : 'failed');
      if (st === 'failed') {
        banner(`<b>Simulator failed</b> — exited ${ev.exit_code}. ` +
               `Nothing below this point is a result.`);
      } else if (st === 'stopped') {
        banner('<b>Stopped</b> — this run was ended by hand, so it is incomplete.', 'warn');
      }
      Sidebar.sys((st === 'finished' ? '' : `<div class="err-line">run ${st}</div>`) +
                  `<div class="kv"><span>timings</span><b>` +
                  `${ev.timing_reconciled} reconciled from CSV</b></div>`);
    },

    /* THE FAILURE THAT LOOKS LIKE SUCCESS. A defended run on an already-used ledger exits
       0 and files zero accusations, because SC1 re-registers and the zk-STARK membership
       gate then denies every vehicle. On a projector that reads as a perfect defence. */
    run_degraded(ev) {
      banner(`<b>Degraded run — no accusations were filed.</b> ${ev.detail || ''}`);
    },

    tailer_error(ev) {
      banner('<b>Live verdict stream failed</b> — ' + (ev.detail || '') +
             '. Verdicts will still appear, but only once the run finishes.', 'warn');
    },

    // Informational: what the tailer missed and the post-exit sweep had to supply.
    late_decisions(ev) {
      Sidebar.sys(`<div class="kv"><span>late verdicts</span><b>${ev.count}</b></div>`);
    },

    run_error(ev) {
      Sidebar.sys(`<div class="err-line">${ev.text}</div>`);
    },
  },
};
