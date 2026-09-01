/* The playback clock — the single source of time on this page.
 *
 * The simulator runs ~3.9x faster than real time at demo scale, so it is NOT the thing
 * driving the display. This clock is. It advances simulated seconds against wall time at
 * `rate`, and is CLAMPED to how far the data has arrived (`buffered`), so playback can
 * never outrun the stream — on hitting the ceiling it visibly stalls rather than silently
 * freezing.
 *
 * Deliberately the only clock in the system: the backend does no pacing at all, which is
 * what makes pause / speed / step / backward-scrub pure local state.
 */
'use strict';

const Clock = {
  t: 0,               // current simulated second
  rate: 1,
  paused: true,
  buffered: 0,        // greatest t received so far
  simEnd: 0,          // total simulated duration, once [SCHEDULE] is known
  stalled: false,
  _last: 0,
  _onSeek: [],

  reset(t = 0) { this.t = t; this.buffered = 0; this.stalled = false; },

  play() { this.paused = false; this._last = performance.now(); },
  pause() { this.paused = true; },
  toggle() { this.paused ? this.play() : this.pause(); },

  setRate(r) { this.rate = r; },

  /** Jump anywhere, including backwards. Listeners rebuild world state from the event
   *  prefix (see dispatch.js) rather than trying to reverse animations. */
  seek(t) {
    this.t = Math.max(0, Math.min(this.buffered, t));
    this._onSeek.forEach(fn => fn(this.t));
  },
  onSeek(fn) { this._onSeek.push(fn); },

  step(seconds = 1) { this.pause(); this.seek(this.t + seconds); },

  note(t) { if (t > this.buffered) this.buffered = t; },

  /** Advance. Returns the wall milliseconds elapsed, for the tween engine. */
  advance(now) {
    const dt = Math.min(250, now - (this._last || now));   // clamp after a tab switch
    this._last = now;
    if (this.paused) { this.stalled = false; return dt; }

    if (this.rate === 'max') {
      this.t = this.buffered;
      this.stalled = false;
      return dt;
    }
    const next = this.t + (dt / 1000) * this.rate;
    if (next > this.buffered) {
      this.t = this.buffered;
      this.stalled = true;          // waiting on the simulator
    } else {
      this.t = next;
      this.stalled = false;
    }
    return dt;
  },

  fmt(t = this.t) {
    const s = Math.max(0, Math.floor(t));
    return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  },
};
