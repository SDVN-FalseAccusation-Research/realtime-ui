#!/usr/bin/env python3
"""Parser + timeline tests. Stdlib unittest — no pytest, no dependencies.

    python3 tests/test_parse.py

The fixtures are real recorded runs (see tests/fixtures/README.md), so these tests pin the
parser against the simulator's actual output rather than against an idea of it.
"""

import csv
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

from parse import Parser, _ids            # noqa: E402
from timeline import Timeline             # noqa: E402

FIX = os.path.join(HERE, "fixtures")
STANDARD_LOG = os.path.join(FIX, "sim_standard.log")
STANDARD_CSV = os.path.join(FIX, "standard_decisions.csv")

# the exact configuration that produced sim_standard.log
STANDARD_CFG = {"warmupTime": 60, "warmupAccusationStart": 35, "attackWindow": 300,
                "attackRounds": 1, "attackPercent": 5}


def run_fixture(path=STANDARD_LOG, cfg=None):
    p, tl = Parser(), Timeline(config=cfg or STANDARD_CFG)
    with open(path) as fh:
        events = [tl.stamp(ev) for line in fh for ev in p.feed(line)]
    return p, tl, events


class TestIdList(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(_ids("97,56,152"), ([97, 56, 152], False))

    def test_truncated(self):
        """The simulator caps ids at 15 and appends ',...' — that must be detected."""
        ids, trunc = _ids("1,2,3,...")
        self.assertEqual(ids, [1, 2, 3])
        self.assertTrue(trunc)

    def test_empty(self):
        self.assertEqual(_ids(""), ([], False))


class TestParserNeverRaises(unittest.TestCase):
    def test_garbage_becomes_log(self):
        p = Parser()
        for junk in ("", "   ", "not a tagged line", "[TARGET] malformed evt=",
                     "[LW] event=1 BLOCKED score=x | radio=2", "\x00\xff binary"):
            for ev in p.feed(junk):
                self.assertIn("type", ev)

    def test_multiline_stderr_banner_is_harmless(self):
        """The 7-line WARN banner must not corrupt parsing — each line is just a log."""
        p = Parser()
        banner = ["", "*" * 69, "** WARN: 3 node key registration(s) FAILED on SC1.",
                  "**", "*" * 69, "", ""]
        out = [ev for ln in banner for ev in p.feed(ln, stream="stderr")]
        self.assertTrue(all(e["type"] == "log" for e in out))

    def test_stderr_error_surfaces_immediately(self):
        p = Parser()
        out = p.feed("ERROR: blockchain not ready within 180s", stream="stderr")
        self.assertEqual(out[0]["type"], "run_error")


class TestStandardFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p, cls.tl, cls.events = run_fixture()

    def test_every_line_is_recognised(self):
        """No `log` events: the parser understands 100% of a real run's stdout."""
        unknown = [e for e in self.events if e["type"] == "log"]
        self.assertEqual(unknown, [], f"{len(unknown)} unrecognised line(s)")

    def test_run_reached_the_end(self):
        self.assertEqual(self.p.phase, "ended")
        end = [e for e in self.events if e["type"] == "run_end"]
        self.assertEqual(len(end), 1)
        self.assertEqual(end[0]["submitted"], 10)
        self.assertEqual(end[0]["accepted"], 8)

    def test_accusation_counts(self):
        acc = [e for e in self.events if e["type"] == "accusation"]
        self.assertEqual(len(acc), 13)                       # 3 warmup + 10 attack
        self.assertEqual(sum(1 for e in acc if e["kind"] == "warmup"), 3)
        self.assertEqual(sum(1 for e in acc if e["kind"] == "attack"), 10)

    def test_roles_and_truncation(self):
        """10 attackers fit under the cap; 20 misbehavers do not and MUST be flagged."""
        self.assertEqual(self.p.roles["attacker_count"], 10)
        self.assertFalse(self.p.roles["attackers_truncated"])
        self.assertEqual(len(self.p.roles["attackers"]), 10)

        self.assertEqual(self.p.roles["misbehaver_count"], 20)
        self.assertTrue(self.p.roles["misbehavers_truncated"])
        self.assertEqual(len(self.p.roles["misbehavers"]), 15)   # simulator's own cap

    def test_schedule_is_authoritative_line(self):
        self.assertEqual(self.p.schedule["spacing"], 30.0)
        self.assertEqual(self.p.schedule["sim_time"], 365.0)
        self.assertEqual(self.p.schedule["last_fire"], 330.0)

    def test_trig_is_aggregated_not_exploded(self):
        """[TRIG] is the genuine-trigger probe (t=target id, NOT a timestamp)."""
        probes = [e for e in self.events if e["type"] == "trigger_probe"]
        self.assertEqual(len(probes), 151)
        self.assertTrue(all("witness" in e and "target" in e for e in probes))

    def test_in_range_flag(self):
        """One attack in this run is FALLBACK-far; it must not be marked in-range."""
        far = [e for e in self.events
               if e["type"] == "accusation" and not e["in_range"]]
        self.assertEqual(len(far), 1)
        self.assertEqual(far[0]["event"], 8)


class TestTimelineAgainstGroundTruth(unittest.TestCase):
    """The load-bearing test.

    The simulator prints no timestamp, so `t` is synthesised from the announced schedule.
    This checks it against `t_attack_start` in _decisions.csv, which is authoritative.
    """

    def test_synthesised_time_matches_csv(self):
        _p, _tl, events = run_fixture()
        truth = {}
        with open(STANDARD_CSV, newline="") as fh:
            for r in csv.DictReader(fh):
                if r["submitted"].strip() == "1":
                    truth[int(r["event_id"])] = float(r["t_attack_start"])

        self.assertTrue(truth, "fixture CSV had no submitted rows")
        worst = 0.0
        for e in events:
            if e["type"] != "accusation":
                continue
            self.assertIn(e["event"], truth)
            worst = max(worst, abs(e["t"] - truth[e["event"]]))
        self.assertLessEqual(worst, 0.05, f"max timing error {worst:.3f}s exceeds 50 ms")

    def test_reconcile_marks_events_exact(self):
        _p, tl, events = run_fixture()
        fixed, worst = tl.reconcile(events, STANDARD_CSV)
        self.assertEqual(fixed, 13)
        self.assertEqual(worst, 0.0)
        self.assertTrue(all(e["t_exact"] for e in events
                            if e["type"] == "accusation"))

    def test_events_are_monotonic_in_time(self):
        """The playback cursor is a single forward index, so t must never go backwards
        within a kind, and accusations overall must be ordered."""
        _p, _tl, events = run_fixture()
        acc = [e["t"] for e in events if e["type"] == "accusation"]
        self.assertEqual(acc, sorted(acc))


class TestDeterminism(unittest.TestCase):
    def test_parsing_is_byte_stable(self):
        """Same input -> identical events. Required for replay to be trustworthy."""
        import json
        a = json.dumps(run_fixture()[2], sort_keys=True)
        b = json.dumps(run_fixture()[2], sort_keys=True)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
