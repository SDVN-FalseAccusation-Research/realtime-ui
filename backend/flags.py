"""The simulator's own flag list, parsed from `--PrintHelp`, used as a whitelist.

WHY IT IS DERIVED RATHER THAN HAND-WRITTEN
    The binary is the authority on what flags exist. Deriving the registry from
    `--PrintHelp` means the UI can never drift from the simulator: if a flag is renamed or
    removed, the UI stops offering it on the next restart instead of failing at run time.

    It also means the whitelist is exhaustive by construction. There is no passthrough, no
    `extra_args`, no `--` escape hatch anywhere in this codebase.

FORMAT
    `    --numVehicles:                Number of vehicles [200]`

    One regex handles all 129 program options. The seven lines that do not match are
    ns-3's own meta-options (`--PrintHelp`, `--PrintAttributes=[typeid]`, ...) — they are
    excluded automatically, which is a safety property rather than a gap: they can print
    internals or change global behaviour and no client should reach them.

TYPES are inferred from the default literal, which is unambiguous in practice:
    true|false -> bool (13)   -?\\d+ -> int (49)   -?\\d*\\.\\d+ -> float (38)   else str (29)
"""

import os
import re
import subprocess

import config

# `--name:  help text [default]`
_LINE = re.compile(r"^\s+--([A-Za-z0-9_]+):\s+(.*?)\s*\[(.*)\]\s*$")

# Never offered to a client, whatever --PrintHelp says.
DENY = {
    # ns-3 meta-options: print internals or alter global state
    "PrintHelp", "PrintGroups", "PrintTypeIds", "PrintAttributes", "PrintGlobals",
    "PrintGroup",
    # exits immediately after printing; a client using it would look like a crash
    "selftest",
    # server-controlled: these decide where output lands and what the UI can see
    "csvPrefix", "traceFile", "animFile", "debugLogs", "liteLogs",
}

# Injected by the server on every run; not client-settable.
#   debugLogs=1 gates EVERY per-event line and the end-of-run summary — without it the UI
#               receives almost nothing.
#   liteLogs=1  skips the three ~340 MB per-pair CSVs. It does not affect stdout, and
#               pem.compute_cell() does not read them, so nothing downstream is lost.
FORCED = {"debugLogs": 1, "liteLogs": 1}

# Range limits layered over the derived registry. Anything not listed is bounded only by
# its type. Values here are the simulator's own constraints, not taste.
LIMITS = {
    # the Manhattan trace has exactly TRACE_VEHICLES nodes
    "numVehicles": (1, config.TRACE_VEHICLES),
    "numRsus": (1, 256),
    "numControllers": (1, 32),
    # the simulator itself caps attackPercent at 80 (main.cc kMaxAttackPercent)
    "attackPercent": (0, 80),
    "misbehavePercent": (0, 100),
    "seed": (1, 2 ** 31 - 1),
    "attackerSeed": (1, 2 ** 31 - 1),
    "attackRounds": (1, 100),
    # past the end of the mobility trace every vehicle freezes at its last waypoint
    "simTime": (1, config.TRACE_END_S),
    "attackWindow": (1, config.TRACE_END_S),
    "warmupTime": (0, config.TRACE_END_S),
    "warmupAccusationStart": (0, config.TRACE_END_S),
    "commRange": (10, 2000),
    "beaconInterval": (0.01, 10),
    "evalWindow": (0.1, 60),
}

ATTACK_TYPES = [
    "single_data", "sybil_data", "timing_data", "colluding_data",
    "single_control", "sybil_control", "timing_control", "evidence_control",
    "report_tamper_rsu",
]

# Free-form strings are the highest-risk field, so every string flag we accept is an enum.
ENUMS = {"attackType": ATTACK_TYPES}


class FlagSpec:
    __slots__ = ("name", "type", "default", "help")

    def __init__(self, name, type_, default, help_):
        self.name, self.type, self.default, self.help = name, type_, default, help_

    def as_dict(self):
        d = {"name": self.name, "type": self.type, "default": self.default,
             "help": self.help}
        if self.name in LIMITS:
            d["min"], d["max"] = LIMITS[self.name]
        if self.name in ENUMS:
            d["choices"] = ENUMS[self.name]
        return d


def _infer(default_literal):
    d = default_literal.strip()
    if d in ("true", "false"):
        return "bool", (d == "true")
    if re.fullmatch(r"-?\d+", d):
        return "int", int(d)
    if re.fullmatch(r"-?\d*\.\d+", d):
        return "float", float(d)
    return "str", d


class Registry:
    """Parsed once per binary build; refreshed when the binary's mtime changes."""

    def __init__(self):
        self.specs = {}
        self._mtime = None

    def load(self, binary=None, force=False):
        binary = binary or config.BINARY
        if not os.path.exists(binary):
            raise FileNotFoundError(
                f"simulator binary not found: {binary}\n"
                f"build it with: cd {config.NS3_ROOT} && ./waf build "
                f"--targets=sdvn_false_accusation_realnet")
        mtime = os.path.getmtime(binary)
        if self.specs and not force and mtime == self._mtime:
            return self.specs

        out = subprocess.run(
            [binary, "--PrintHelp"], capture_output=True, text=True,
            env=config.sim_env(), cwd=config.NS3_ROOT, timeout=60).stdout

        specs = {}
        for line in out.splitlines():
            m = _LINE.match(line)
            if not m:
                continue
            name, help_, dflt = m.group(1), m.group(2), m.group(3)
            if name in DENY:
                continue
            t, v = _infer(dflt)
            specs[name] = FlagSpec(name, t, v, help_.strip())

        if len(specs) < 50:
            raise RuntimeError(
                f"--PrintHelp yielded only {len(specs)} flags; the output format "
                f"has probably changed. Refusing to run with a partial whitelist.")

        self.specs, self._mtime = specs, mtime
        return specs

    def __contains__(self, name):
        return name in self.specs

    def __getitem__(self, name):
        return self.specs[name]

    def as_list(self):
        return [s.as_dict() for s in sorted(self.specs.values(), key=lambda s: s.name)]


REGISTRY = Registry()
