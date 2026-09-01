"""Turn a client's config dict into an argv list — or reject it.

This is the security boundary of the whole application. A browser form decides what
process runs on this machine, so treat every field as hostile.

THE FIVE RULES
  1. Whitelist, not filter. A name absent from the `--PrintHelp` registry is rejected.
     There is no passthrough and no escape hatch.
  2. Enums for strings. `attackType` is the only free-ish string a client can influence
     and it must be one of nine literals. That is exactly the field an injection attempt
     reaches for.
  3. Numbers are re-serialised from the parsed Python value, never from the submitted
     text. Even if a string slipped through a type check it could not carry a
     metacharacter into argv.
  4. Paths are never accepted. `csvPrefix` is derived from a server-generated run id;
     `traceFile` is chosen by key from config.TRACES.
  5. argv is a list and the process is spawned with shell=False. Nothing is ever
     interpolated into a shell string.

Note that even without rules 1-4, rule 5 alone defeats `attackType="x; rm -rf ~"` — argv
elements are passed to execve() verbatim, so it would arrive as one nonsense argument
rather than a command. The layers are deliberate: rule 5 is the one that must never break,
the others make failures loud instead of silent.
"""

import os
import re

import config
from flags import ENUMS, FORCED, LIMITS, REGISTRY

RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class Rejected(Exception):
    """A config that must not reach the simulator. `.field` names the offender."""

    def __init__(self, field, reason):
        super().__init__(f"{field}: {reason}")
        self.field, self.reason = field, reason


def _coerce(spec, raw):
    """Value -> the declared type, or raise. Booleans become 0/1 for the ns-3 CLI."""
    name, t = spec.name, spec.type

    if t == "bool":
        if isinstance(raw, bool):
            return 1 if raw else 0
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return int(raw)
        if isinstance(raw, str) and raw.strip().lower() in ("true", "false", "0", "1"):
            return 1 if raw.strip().lower() in ("true", "1") else 0
        raise Rejected(name, f"expected a boolean, got {raw!r}")

    if t in ("int", "float"):
        if isinstance(raw, bool):
            raise Rejected(name, "expected a number, got a boolean")
        try:
            val = int(raw) if t == "int" else float(raw)
        except (TypeError, ValueError):
            raise Rejected(name, f"expected {t}, got {raw!r}")
        if val != val or val in (float("inf"), float("-inf")):
            raise Rejected(name, "not a finite number")
        lo, hi = LIMITS.get(name, (None, None))
        if lo is not None and not (lo <= val <= hi):
            raise Rejected(name, f"must be between {lo} and {hi} (got {val})")
        return val

    # strings: enum-only
    if name in ENUMS:
        if raw not in ENUMS[name]:
            raise Rejected(name, f"must be one of {', '.join(ENUMS[name])}")
        return raw
    raise Rejected(name, "free-form string flags are not accepted")


def validate_run_id(run_id):
    """A run id becomes a directory name. Reject anything that is not a plain token, then
    resolve and confirm containment — belt and braces against `..` and symlinks."""
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise Rejected("run_id", "must match [A-Za-z0-9_-]{1,64}")
    path = os.path.realpath(os.path.join(config.RESULTS_UI, run_id))
    root = os.path.realpath(config.RESULTS_UI)
    if not (path == root or path.startswith(root + os.sep)):
        raise Rejected("run_id", "resolves outside the results directory")
    return path


def build_argv(cfg, run_id, binary=None):
    """(client config, server run id) -> (argv list, effective config dict).

    `cfg` is the raw request body. Unknown keys are rejected rather than ignored, so a
    typo'd flag is a visible error instead of a silently-defaulted run.
    """
    REGISTRY.load(binary)
    if not isinstance(cfg, dict):
        raise Rejected("body", "expected an object")

    run_dir = validate_run_id(run_id)
    effective = {}

    # --- the mobility trace is a CHOICE, never a path ---------------------------------
    trace_key = cfg.get("trace", "manhattan")
    if trace_key not in config.TRACES:
        raise Rejected("trace", f"must be one of {', '.join(config.TRACES)}")
    trace_path = config.TRACES[trace_key]

    # --- client-supplied flags ---------------------------------------------------------
    for name, raw in cfg.items():
        if name.startswith("_") or name == "trace":
            continue                                  # UI-only metadata
        if name in FORCED:
            raise Rejected(name, "is set by the server and cannot be overridden")
        if name not in REGISTRY:
            raise Rejected(name, "is not a recognised simulator flag")
        effective[name] = _coerce(REGISTRY[name], raw)

    # --- cross-field checks the type system cannot express -----------------------------
    if trace_key == "manhattan":
        n = effective.get("numVehicles", REGISTRY["numVehicles"].default)
        if n > config.TRACE_VEHICLES:
            raise Rejected("numVehicles",
                           f"the Manhattan trace has only {config.TRACE_VEHICLES} nodes; "
                           f"the rest would sit at the origin")
        # warmup must start after the vehicles are inserted. The last one appears at t=42,
        # not t<=30 as the docs suggest, and only ONE vehicle exists at t=0.
        start = effective.get("warmupAccusationStart",
                              REGISTRY["warmupAccusationStart"].default)
        if start < 30:
            raise Rejected("warmupAccusationStart",
                           "must be >= 30 s with the Manhattan trace — vehicles are still "
                           "being inserted before then (last one at t=42 s)")

    # --- server-controlled flags -------------------------------------------------------
    forced = dict(FORCED)
    forced["csvPrefix"] = os.path.join(run_dir, "run")
    if trace_path:
        forced["traceFile"] = trace_path

    argv = [binary or config.BINARY]
    for name, val in sorted({**effective, **forced}.items()):
        argv.append(f"--{name}={_render(val)}")
    return argv, {**effective, "trace": trace_key}


def _render(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        # repr would give 300.0 for an integral float; the CLI accepts either, but a clean
        # value keeps the "exact command" preview on the config page readable.
        return f"{v:g}"
    return str(v)
