/* Boot and the render loop.
 *
 * One requestAnimationFrame drives everything:
 *     clock advances -> vehicles move -> due events apply -> tweens step -> chrome updates
 *
 * Positions come from the preloaded trace, not the stream, so the map keeps moving even
 * while no events are arriving.
 */
'use strict';

const $ = (id) => document.getElementById(id);

const App = {
  runId: null,

  async boot() {
    const q = new URLSearchParams(location.search);
    this.runId = q.get('run');
    if (!this.runId) { location.href = '/'; return; }
    $('run-label').textContent = this.runId;

    // Deep-link params: ?t=140 jumps straight to a moment, ?rate=5 sets the speed,
    // ?paused=1 holds. Useful for rehearsing a specific accusation on demo day, and it
    // is what makes the page checkable in a headless browser.
    this.seekTo = q.has('t') ? Number(q.get('t')) : null;
    this.startRate = q.get('rate');
    this.startPaused = q.get('paused') === '1';

    try {
      await Assets.load(step => { $('loading').textContent = `loading ${step}…`; });
    } catch (err) {
      $('loading').innerHTML = `<div style="color:var(--danger);max-width:520px">${err}</div>`;
      return;
    }

    World.init($('stage'));
    if (Assets.buildings) World.layers.buildings.innerHTML = Assets.buildings;
    World.drawRoads(Assets.roads);
    Sidebar.init();
    $('loading').remove();

    this.wireControls();

    Stream.connect(this.runId, {
      onEvent: (ev) => {
        // A verdict can arrive behind the cursor (the CSV is only read at process exit,
        // and the first accusations play before that). Apply those immediately instead of
        // letting them fall off the back of the cursor.
        if (Dispatch.cursor > 0 && (ev.t || 0) < Clock.t) Dispatch.backfill(ev);
      },
      onState: (s) => {
        this.setConn(s);
        // A finished run delivers its whole log at once; honour ?t= only after it lands,
        // otherwise there is nothing yet to rebuild from.
        if (s === 'ended' && this.seekTo !== null && !this._sought) {
          this._sought = true;
          Clock.seek(this.seekTo);
          if (this.startPaused) { Clock.pause(); $('play').textContent = '▶'; }
        }
      },
    });

    Clock.onSeek((t) => Dispatch.rebuild(t));

    if (this.startRate) {
      $('rate').value = this.startRate;
      Clock.setRate(this.startRate === 'max' ? 'max' : Number(this.startRate));
    }
    if (this.startPaused) { Clock.pause(); } else { Clock.play(); $('play').textContent = '⏸'; }
    requestAnimationFrame((ts) => this.frame(ts));
  },

  frame(ts) {
    const dt = Clock.advance(ts);
    World.update(Clock.t);
    Dispatch.pump();
    Fx.tick(dt);
    this.chrome();
    requestAnimationFrame((t) => this.frame(t));
  },

  chrome() {
    const end = Clock.simEnd || Math.max(1, Clock.buffered);
    $('clock').textContent =
      `t = ${Clock.t.toFixed(1)} s  ${Clock.fmt()}`;
    $('stall').textContent = Clock.stalled ? 'waiting for simulator…' : '';
    $('buffered').firstElementChild.style.width =
      `${Math.min(100, 100 * Clock.buffered / end)}%`;
    if (!this._scrubbing) $('scrub').value = Math.round(1000 * Clock.t / end);
    Sidebar.status(Dispatch.cfg, Dispatch.counts, Clock.fmt());
  },

  setConn(s) {
    const b = $('conn');
    const cls = { live: 'up', ended: 'up', connecting: '', error: 'down' }[s] || '';
    b.innerHTML = `<span class="dot ${cls}"></span>${s}`;
  },

  wireControls() {
    $('play').onclick = () => {
      Clock.toggle();
      $('play').textContent = Clock.paused ? '▶' : '⏸';
    };
    $('step').onclick = () => { Clock.step(1); $('play').textContent = '▶'; };
    $('rate').onchange = (e) => {
      const v = e.target.value;
      Clock.setRate(v === 'max' ? 'max' : Number(v));
    };
    $('reset-view').onclick = () => World.resetView();

    const scrub = $('scrub');
    scrub.oninput = () => {
      this._scrubbing = true;
      const end = Clock.simEnd || Math.max(1, Clock.buffered);
      Clock.pause();
      $('play').textContent = '▶';
      Clock.seek(end * scrub.value / 1000);
    };
    scrub.onchange = () => { this._scrubbing = false; };

    $('stop-btn').onclick = async () => {
      $('stop-btn').disabled = true;
      try { await fetch(`/api/runs/${encodeURIComponent(this.runId)}`, { method: 'DELETE' }); }
      catch (e) { /* already finished */ }
    };

    document.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
      if (e.code === 'Space') { e.preventDefault(); $('play').click(); }
      if (e.code === 'ArrowRight') Clock.step(1);
      if (e.code === 'ArrowLeft') Clock.step(-1);
    });
  },
};

App.boot();
