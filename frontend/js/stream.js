/* WebSocket client. Arrival only — it never touches the DOM.
 *
 * Events land in a list kept sorted by (t, seq); dispatch.js walks a cursor through it
 * against the playback clock. Separating arrival from playback is what lets the client
 * buffer far ahead of what is on screen, which it always does because the simulator runs
 * ~3.9x faster than the display.
 *
 * A dropped socket reconnects with ?from_seq=<last+1>. The server replays the gap from
 * events.jsonl before rejoining the live tail, so an interruption is invisible — worth
 * having when the demo is live.
 */
'use strict';

const Stream = {
  events: [],
  lastSeq: 0,
  ws: null,
  runId: null,
  state: 'idle',          // idle | connecting | live | ended | error
  onEvent: () => {},
  onState: () => {},
  _retry: 0,

  connect(runId, { onEvent, onState } = {}) {
    this.runId = runId;
    if (onEvent) this.onEvent = onEvent;
    if (onState) this.onState = onState;
    this._open();
  },

  _open() {
    this._set('connecting');
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/ws/runs/${encodeURIComponent(this.runId)}` +
                `?from_seq=${this.lastSeq + 1}`;
    const ws = new WebSocket(url);
    this.ws = ws;

    ws.onopen = () => { this._retry = 0; this._set('live'); };

    ws.onmessage = (m) => {
      let ev;
      try { ev = JSON.parse(m.data); } catch { return; }

      if (ev.type === '_replay_end') { this._set('ended'); return; }

      if (ev.seq) {
        if (ev.seq <= this.lastSeq) return;         // duplicate after a resume
        this.lastSeq = ev.seq;
      }
      this._insert(ev);
      Clock.note(ev.t || 0);
      this.onEvent(ev);
      if (ev.type === 'run_closed') this._set('ended');
    };

    ws.onclose = () => {
      if (this.state === 'ended') return;
      // exponential backoff, capped; resume picks up exactly where we stopped
      const wait = Math.min(8000, 400 * Math.pow(2, this._retry++));
      this._set('connecting');
      setTimeout(() => this._open(), wait);
    };

    ws.onerror = () => { /* onclose handles recovery */ };
  },

  /** Keep the list ordered by (t, seq). The server emits in causal order, so this is an
   *  append in the overwhelming majority of cases. */
  _insert(ev) {
    const n = this.events.length;
    if (!n || this._key(ev) >= this._key(this.events[n - 1])) { this.events.push(ev); return; }
    let lo = 0, hi = n;
    const k = this._key(ev);
    while (lo < hi) { const mid = (lo + hi) >> 1;
                      if (this._key(this.events[mid]) <= k) lo = mid + 1; else hi = mid; }
    this.events.splice(lo, 0, ev);
  },
  _key(e) { return (e.t || 0) * 1e6 + (e.seq || 0); },

  _set(s) { this.state = s; this.onState(s); },

  close() { this.state = 'ended'; if (this.ws) this.ws.close(); },
};
