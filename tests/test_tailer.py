#!/usr/bin/env python3
"""Live CSV tailer tests. Stdlib unittest — no pytest, no dependencies.

    python3 tests/test_tailer.py

The two things that can go wrong here are both silent:
  * a row split across two polls is dropped, or read twice;
  * the tailer and _finalise()'s post-exit sweep both announce the same verdict, and every
    number on the statistics page doubles.

So the tests write files the way the simulator does — a byte at a time, mid-row — and then
assert against tools/import_run.py's view of the same directory, which is the reference the
component pages were already verified against.
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import csv_events                          # noqa: E402
from tailer import Tailer, _File, event_key  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
STANDARD_CSV = os.path.join(FIX, "standard_decisions.csv")


class FakeRun:
    """Just enough Run for the tailer: a store directory and the two emit hooks."""

    def __init__(self, d):
        self.store = type("S", (), {"dir": d})()
        self.events = []
        self.errors = []
        self.seen = set()

    async def emit_derived(self, ev):
        key = event_key(ev)
        if key in self.seen:
            return False
        self.seen.add(key)
        self.events.append(ev)
        return True

    async def emit_tailer_error(self, detail):
        self.errors.append(detail)


class TornLines(unittest.TestCase):
    """A row still being written must be left alone until it is complete."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "run_decisions.csv")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_partial_row_is_not_yielded_until_complete(self):
        f = _File(self.p)
        with open(self.p, "w") as fh:
            fh.write("a,b\n")
            fh.flush()
            self.assertEqual(f.new_rows(), [])        # header only, no data rows yet
            fh.write("1,2")                           # no newline: still being written
            fh.flush()
            self.assertEqual(f.new_rows(), [])        # must NOT be parsed yet
            fh.write("\n3,4\n")
            fh.flush()
            self.assertEqual(f.new_rows(),
                             [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}])
            self.assertEqual(f.new_rows(), [])        # and never a second time

    def test_byte_at_a_time_yields_each_row_exactly_once(self):
        """The pathological case: one byte per poll."""
        payload = "a,b\n1,2\n3,4\n5,6\n"
        f = _File(self.p)
        got = []
        with open(self.p, "w") as fh:
            for ch in payload:
                fh.write(ch)
                fh.flush()
                got.extend(f.new_rows())
        self.assertEqual(got, [{"a": "1", "b": "2"},
                               {"a": "3", "b": "4"},
                               {"a": "5", "b": "6"}])

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(_File(os.path.join(self.d, "nope.csv")).new_rows(), [])

    def test_values_are_stripped_like_csv_events_rows(self):
        with open(self.p, "w") as fh:
            fh.write("a, b\n 1 ,2 \n")
        self.assertEqual(_File(self.p).new_rows(), [{"a": "1", "b": "2"}])


class AgainstTheImporter(unittest.TestCase):
    """Tailing a file in chunks must produce exactly what reading it whole produces."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.dst = os.path.join(self.d, "run_decisions.csv")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_incremental_equals_wholesale(self):
        with open(STANDARD_CSV) as fh:
            text = fh.read()
        run = FakeRun(self.d)
        t = Tailer(run)

        # Write in 64-byte chunks, draining after each — rows land mid-write constantly.
        async def drive():
            with open(self.dst, "w") as fh:
                for i in range(0, len(text), 64):
                    fh.write(text[i:i + 64])
                    fh.flush()
                    await t._drain()
            await t._drain()
        asyncio.run(drive())

        live = [e for e in run.events if e["type"] == "decision"]
        whole = csv_events.decision_events(self.dst)
        self.assertTrue(whole, "fixture produced no decisions")
        self.assertEqual(len(live), len(whole))
        self.assertEqual([e["event"] for e in live], [e["event"] for e in whole])
        for a, b in zip(live, whole):
            self.assertEqual(a, b)

    def test_post_exit_sweep_adds_nothing_the_tailer_already_sent(self):
        """V28: the exact double-count the dedupe exists to prevent."""
        shutil.copy(STANDARD_CSV, self.dst)
        run = FakeRun(self.d)
        asyncio.run(Tailer(run)._drain())
        first = len(run.events)
        self.assertTrue(first)

        async def sweep():
            return sum([await run.emit_derived(ev)
                        for ev in csv_events.decision_events(self.dst)])
        self.assertEqual(asyncio.run(sweep()), 0)
        self.assertEqual(len(run.events), first)

    def test_tailer_missing_everything_leaves_the_backstop_to_do_the_work(self):
        """The inverse: if the tailer never ran, _finalise() must still emit them all."""
        shutil.copy(STANDARD_CSV, self.dst)
        run = FakeRun(self.d)

        async def sweep():
            return sum([await run.emit_derived(ev)
                        for ev in csv_events.decision_events(self.dst)])
        self.assertEqual(asyncio.run(sweep()),
                         len(csv_events.decision_events(self.dst)))


class Keys(unittest.TestCase):
    def test_layers_on_one_accusation_do_not_collide(self):
        z = {"type": "layer", "layer": "zkp", "event": 7}
        g = {"type": "layer", "layer": "gnn", "event": 7}
        self.assertNotEqual(event_key(z), event_key(g))
        self.assertEqual(event_key(z), event_key(dict(z, verdict="PASS")))

    def test_rsu_transitions_key_on_the_transition_not_the_event(self):
        # _rsu_audit.csv has a row per event; rsu_events() emits only changes, so the key
        # must be (rsu, state) or a re-read would re-announce the same quarantine.
        a = {"type": "rsu_status", "event": 3, "rsu": {"r": 9}, "state": "QUARANTINED"}
        b = {"type": "rsu_status", "event": 41, "rsu": {"r": 9}, "state": "QUARANTINED"}
        c = {"type": "rsu_status", "event": 41, "rsu": {"r": 9}, "state": "REMOVED"}
        self.assertEqual(event_key(a), event_key(b))
        self.assertNotEqual(event_key(b), event_key(c))


class RsuState(unittest.TestCase):
    def test_seen_state_persists_across_polls(self):
        """rsu_events() dedupes across rows; split over two polls it must still dedupe."""
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "run_rsu_audit.csv")
            hdr = "seed,event_id,is_warmup,serving_rsu,rsu_trust,rsu_tampered,rsu_status\n"
            with open(p, "w") as fh:
                fh.write(hdr)
            run = FakeRun(d)
            t = Tailer(run)
            with open(p, "a") as fh:
                fh.write("1,1,0,9,0.5,1,1\n")
                fh.flush()
                asyncio.run(t._drain())
                fh.write("1,2,0,9,0.5,1,1\n")     # same state again, later event
                fh.flush()
                asyncio.run(t._drain())
            states = [e for e in run.events if e["type"] == "rsu_status"]
            self.assertEqual(len(states), 1, "unchanged status re-announced")
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
