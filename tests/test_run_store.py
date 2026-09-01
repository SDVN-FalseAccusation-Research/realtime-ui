#!/usr/bin/env python3
"""Durability of the event log. Stdlib unittest.

    python3 tests/test_run_store.py

events.jsonl is the ONE durable record — live, resume and replay are all served from it —
so an event that is broadcast but not written is a silent hole. The regression pinned here
is exactly that: rewrite_events() replaces the file while the append handle is still open,
and everything appended afterwards lands in the orphaned inode.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import config                              # noqa: E402
import run_store                           # noqa: E402


class AppendSurvivesRewrite(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._saved = config.RESULTS_UI
        config.RESULTS_UI = self.d

    def tearDown(self):
        config.RESULTS_UI = self._saved
        shutil.rmtree(self.d, ignore_errors=True)

    def _lines(self, store):
        with open(store.events_path) as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def test_events_appended_after_a_rewrite_are_still_written(self):
        """reconcile() rewrites the log, then run_closed is appended. Both must survive."""
        st = run_store.RunStore("t1").open({}, {}, [])
        st.append({"type": "accusation", "t": 1.0})
        st.append({"type": "decision", "t": 2.0})

        st.events[0]["t"] = 9.0            # what reconcile() does
        st.rewrite_events()

        st.append({"type": "run_closed", "t": 3.0})
        st.close(exit_code=0)

        got = self._lines(st)
        self.assertEqual([e["type"] for e in got],
                         ["accusation", "decision", "run_closed"])
        self.assertEqual(got[0]["t"], 9.0, "the correction was not persisted")
        # The count in run_config.json must agree with the file, or the two disagree about
        # what happened -- which is how this went unnoticed.
        with open(os.path.join(st.dir, "run_config.json")) as fh:
            self.assertEqual(json.load(fh)["events"], len(got))

    def test_read_events_sees_everything_after_a_rewrite(self):
        st = run_store.RunStore("t2").open({}, {}, [])
        for i in range(5):
            st.append({"type": "accusation", "event": i})
        st.rewrite_events()
        st.append({"type": "run_closed"})
        st.close(exit_code=0)
        self.assertEqual(len(run_store.read_events("t2")), 6)

    def test_sequence_numbers_stay_monotonic_across_a_rewrite(self):
        st = run_store.RunStore("t3").open({}, {}, [])
        st.append({"type": "a"})
        st.rewrite_events()
        st.append({"type": "b"})
        st.close(exit_code=0)
        self.assertEqual([e["seq"] for e in self._lines(st)], [1, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
