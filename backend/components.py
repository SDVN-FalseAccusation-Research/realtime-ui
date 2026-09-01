"""Per-component detail: what went in, what came out, what it changed.

One builder per defence component. Each returns

    {"summary": {...}, "rows": [...], "notes": [...], "available": bool}

`notes` is the important field. Several components cannot show everything a reader would
want, because the simulator never recorded it — the GNN's input features and the LLM's
prompt text are built in-process and handed straight to the sidecar. Rather than render an
empty panel or, worse, imply the evidence exists, each builder states the gap in words and
names the flag that would close it. This is the same discipline `pem.py` applies to an
undefined metric: an honest `n/a` with a reason beats a confident zero.

Everything is read on demand, server-side. `_trust_refresh.csv` is 11 MB and
`_reputation.csv` is 153 MB; neither belongs in the event stream or on the wire.
"""

import json
import os

import config
import csv_events as C

NAMES = ["pqc", "zkp", "gnn", "llm", "chain", "reputation", "keymgmt"]

ROLES = {
    "pqc":  ("Post-quantum signatures",
             "Signs and verifies every accusation, report and RSU forward."),
    "zkp":  ("zk-STARK gate",
             "Proves an accusation is well-formed and proximate without revealing the reporter."),
    "gnn":  ("GNN detector",
             "Scores each ZK-clean accusation graph for anomaly and attack variant."),
    "llm":  ("LLM confirmation",
             "Explainable second opinion on GNN-flagged accusations only."),
    "chain": ("Blockchain SC1–SC4",
              "Seals evidence before the controller acts, then audits the outcome against it."),
    "reputation": ("Reputation / TFMD",
                   "Emergent trust Rt → Rec → GLSim → Tr, the thing the attack tries to poison."),
    "keymgmt": ("Key management",
                "Threshold RA via DKG, plus a per-zone logical key hierarchy."),
}

SC_ROLES = [
    ("SC1", "identity registry — credential commitments, nullifier revocation, circuit VK hashes"),
    ("SC2", "reputation, RSU evidence custody, outcome consensus; endorsers recompute w1'/w2'"),
    ("SC3", "controller audit and autonomous failover on three-channel divergence"),
    ("SC4", "RSU custody trust — re-verifies each σ_R, ACTIVE → QUARANTINED → REMOVED"),
]

# The six deterministic blocks llm_sidecar/prompt.py assembles. The text itself is never
# persisted (only its SHA3-512), so this is the structure, not the content.
LLM_SOURCES = [
    "signature definitions + the verdict rule",
    "the accusation under review (accuser, victim, claim, event, time)",
    "the zero-knowledge proof result forwarded from NS-3",
    "GNN output — s_v, flagged, tau_gnn, per-variant scores, pattern",
    "SC2 ledger excerpt — trust, history, blacklist state, read from chain",
    "observed per-vehicle features for victim and accuser",
    "(conditional) identity check — total vs unsigned reporters (σ_R)",
]


def run_paths(run_id):
    """Where this run's CSVs actually live.

    An imported run does not copy them — `run_config.json` points at `source_dir` — so this
    resolves the same way `/api/runs/{id}/metrics` does.
    """
    d = os.path.join(config.RESULTS_UI, run_id)
    meta = {}
    try:
        with open(os.path.join(d, "run_config.json")) as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        pass
    src = meta.get("source_dir")
    if meta.get("imported") and src and os.path.isdir(src):
        return src, meta
    return d, meta


def _cfg(meta, key, default=None):
    return (meta.get("effective") or {}).get(key, default)


def _on(meta, key):
    return str(_cfg(meta, key, 0)) in ("1", "True", "true")


# ------------------------------------------------------------------------- PQC ------
def pqc(d, meta):
    """Algorithms are DERIVED the way the simulator derives them, not read off a caption.

    params.h defines:
        VehicleClassical() = lwMode || !vehiclePqc      (:953)
        InfraClassical()   = !infraPqc                  (:958)

    Note lwMode alone downgrades the vehicle tier even with vehiclePqc left on — reading
    the flag by itself would get the A13/LW arms wrong.

    UPSTREAM BUG, deliberately surfaced below: the simulator's own end-of-run line at
    main.cc:701 hardcodes the string "(σ_E accuser ECDSA-256 + σ_F RSU FALCON-1024)". It is
    a literal, printed regardless of the flags, so it claims the vehicle tier is classical
    even on a fully post-quantum run. In a project whose central claim is post-quantum
    security that is worth flagging rather than repeating.
    """
    # The signature layer only exists under --blockchain=1: sigma_E / sigma_R / sigma_F are
    # produced and verified on the chain path, and the simulator's own [PQC] line is gated on
    # enableBlockchain. Reporting "FALCON-512" for an undefended baseline would claim
    # protection the run never had.
    if not _on(meta, "blockchain") and not _on(meta, "lwMode"):
        return {"available": False, "summary": {}, "rows": [],
                "notes": ["No signature layer in this run. sigma_E / sigma_R / sigma_F are "
                          "produced on the --blockchain=1 path only, so an undefended "
                          "baseline carries no signatures to verify."]}

    lw = _on(meta, "lwMode")
    veh_pq = str(_cfg(meta, "vehiclePqc", 1)) != "0"
    inf_pq = str(_cfg(meta, "infraPqc", 1)) != "0"
    veh_classical = lw or not veh_pq
    inf_classical = not inf_pq

    veh = "ECDSA-256 (classical)" if veh_classical else "FALCON-512 (NIST L1)"
    inf = "ECDSA-256 (classical)" if inf_classical else "FALCON-1024 (NIST L5)"

    notes = []
    if veh_classical:
        notes.append("Vehicle tier is CLASSICAL here: " +
                     ("--lwMode=1 forces it" if lw else "--vehiclePqc=0 (the A10 arm)") +
                     ". σ_E and σ_R are ECDSA-256, not FALCON-512.")
    if inf_classical:
        notes.append("Infrastructure tier is CLASSICAL: --infraPqc=0 (the A11 arm), so σ_F "
                     "and controller credentials are ECDSA-256. Note the ML-KEM → ECDH half "
                     "of A11 is not implemented — that handshake is shared with the Go "
                     "bridge and cannot be switched from ns-3 alone.")

    rows, checks = [], None
    log = os.path.join(d, "sim.log")
    if os.path.exists(log):
        try:
            with open(log, errors="replace") as fh:
                for line in fh:
                    if line.startswith("[PQC] signature checks:"):
                        checks = line.strip()
                    elif line.startswith("[PQC] accusation signature invalid"):
                        rows.append({"text": line.strip()})
        except OSError:
            pass

    # The caption is a literal (main.cc:701) and does not track the flags. If it disagrees
    # with what actually ran, say so — otherwise a reader trusts the wrong one.
    if checks and not veh_classical and "accuser ECDSA-256" in checks:
        notes.append("The run's own [PQC] line reads 'σ_E accuser ECDSA-256', but that "
                     "caption is a HARDCODED string in main.cc:701 and is printed whatever "
                     "the flags say. This run had vehiclePqc on and lwMode off, so the "
                     "vehicle tier really is FALCON-512. The log line is wrong, not the run.")

    notes.append("Per-signature records are not written to CSV. Only the aggregate "
                 "pass/fail count and individually rejected forgeries are recoverable.")

    return {
        "available": True,
        "summary": {
            "vehicle_tier": veh, "infrastructure_tier": inf,
            "vehicle_classical": veh_classical, "infra_classical": inf_classical,
            "session_keys": "ECDH P-256" if inf_classical else "ML-KEM-1024 (Kyber)",
            "bridge_transport": ("ML-KEM-1024 + AES-256-GCM" if _on(meta, "secureBridge")
                                 else "plaintext (--secureBridge=0)"),
            "evidence_hash": "SHA3-512",
            "checks_line": checks,
            "forgeries_rejected": len(rows),
        },
        "rows": rows[:100], "notes": notes,
    }


# ------------------------------------------------------------------------- ZKP ------
def zkp(d, meta):
    rows = C._rows(C.find(d, "zkp"))
    used = [r for r in rows if (r.get("zkp_result") or "NA") != "NA"]
    if not used:
        return {"available": False, "summary": {}, "rows": [],
                "notes": ["No ZK proofs in this run — the layer needs --blockchain=1."]}

    p = sum(1 for r in used if r["zkp_result"] == "PASS")
    f = len(used) - p
    vgeo = sum(1 for r in used if C._b(r, "v_geo"))
    out = []
    for r in used:
        out.append({
            "event": C._i(r, "event_id", -1), "warmup": C._b(r, "is_warmup"),
            "result": r["zkp_result"],
            "proximity": C._b(r, "proximity"), "sequence": C._b(r, "sequence"),
            "accusation": C._b(r, "accusation"),
            "reports_proved": C._i(r, "reports_proved"),
            "reports_dropped": C._i(r, "reports_dropped"),
            "v_geo": C._b(r, "v_geo"),
            "true_in_range": C._b(r, "zkp_true_in_range"),
            "latency_us": C._f(r, "l_cd_us"),
        })

    notes = ["Context B is three sub-proofs: a proximity range proof with the coordinates "
             "hidden, C1 (evidence hash matches the anchored one) and C3 (timestamp within "
             "delta). C2 — sequence monotonicity — is checked in the Go bridge, not in the AIR.",
             "Contexts C/D prove a reporter's trust exceeds gamma without revealing it."]
    if vgeo:
        notes.append(f"{vgeo} event(s) ran with v_geo=1 — the A5 substitution, where the real "
                     f"Context B circuit is replaced by a same-zone geometric check. Those "
                     f"rows are a weaker gate and should not be read as STARK verdicts.")
    return {"available": True,
            "summary": {"proofs": len(used), "pass": p, "fail": f,
                        "reports_proved": sum(r["reports_proved"] for r in out),
                        "reports_dropped": sum(r["reports_dropped"] for r in out),
                        "v_geo_rows": vgeo,
                        "mean_latency_us": round(
                            sum(r["latency_us"] for r in out) / len(out), 1)},
            "rows": out, "notes": notes}


# ------------------------------------------------------------------------- GNN ------
def _gnn_manifest():
    """The deployed model's card. The sidecar chooses its artifact via $GNN_ARTIFACTS and
    DEFAULTS TO v12 — a different threshold entirely — so the page cross-checks the hash
    written on every CSV row instead of trusting a directory name."""
    base = os.path.join(config.BCD_DIR, "gnn-sidecar", "artifacts")
    for name in ("gnn_v16_labelfix",):
        p = os.path.join(base, name, "manifest.json")
        if os.path.exists(p):
            try:
                with open(p) as fh:
                    m = json.load(fh)
                m["_dir"] = name
                return m
            except (OSError, ValueError):
                pass
    return None


def gnn(d, meta):
    rows = C._rows(C.find(d, "gnn_decision"))
    if not rows:
        return {"available": False, "summary": {}, "rows": [],
                "notes": ["No GNN decisions — the layer needs --gnnDetect=1 "
                          "(which itself needs --blockchain=1)."]}

    man = _gnn_manifest()
    hashes = {r.get("model_hash") for r in rows if r.get("model_hash")}
    flagged = sum(1 for r in rows if C._b(r, "gnn_flagged"))
    blocked = sum(1 for r in rows if C._b(r, "gnn_blocked"))

    out = [{
        "event": C._i(r, "event_id", -1), "victim": C._i(r, "victim_id"),
        "score": C._f(r, "gnn_score"), "flagged": C._b(r, "gnn_flagged"),
        "blocked": C._b(r, "gnn_blocked"), "pattern": r.get("gnn_pattern") or "",
        "pv": {k: C._f(r, f"gnn_pv_{k}")
               for k in ("single", "sybil", "timing", "colluding")},
        "accepted": C._b(r, "accepted"),
        "model_hash": r.get("model_hash") or "",
    } for r in rows]

    notes = []
    # THE gap: the feature vector the model actually saw was never written to disk.
    graphs = [f for f in os.listdir(d) if f.endswith("_graphs.jsonl")] if os.path.isdir(d) else []
    if not graphs:
        notes.append("The input feature vector for each accusation is NOT recorded in this "
                     "run. The graph is built in-process and sent straight to the sidecar; "
                     "it only reaches disk under --gnnExport=1. So this page can show what "
                     "the model decided, but not what it saw. Re-run with --gnnExport=1 to "
                     "capture <csvPrefix>_graphs.jsonl.")
    if man:
        expected = man.get("model_hash")
        if hashes and expected and hashes != {expected}:
            notes.append(f"MODEL MISMATCH: rows were scored by a model whose hash is not "
                         f"{man['_dir']}'s. The sidecar defaults to gnn_v12_sigmoid_invfreq "
                         f"(tau 0.6795) unless GNN_ARTIFACTS is set, and both report "
                         f"'gnn-v1 (trained)', so this is easy to miss.")
        # The 16-vs-17 disagreement is upstream; report it rather than picking a side.
        notes.append(f"Model card lists {len(man.get('vehicle_features') or [])} vehicle "
                     f"features; llm-sidecar/prompt.py:FEATURE_KEYS lists 17 (it adds "
                     f"'trust') and claims the two must match. They do not — an upstream "
                     f"inconsistency worth resolving.")
    else:
        notes.append("Model manifest not found; the score threshold shown may not be the "
                     "one this run used.")

    summary = {"scored": len(rows), "flagged": flagged, "blocked": blocked,
               "distinct_model_hashes": len(hashes),
               "model_hash": (list(hashes)[0] if len(hashes) == 1 else None)}
    if man:
        summary["model"] = {
            "dir": man.get("_dir"), "tau_gnn": man.get("tau_gnn"),
            "test_mcc": man.get("test_mcc"), "val_mcc": man.get("val_mcc"),
            "test_fpr": man.get("test_fpr"),
            "var_head_accuracy": man.get("var_head_accuracy"),
            "by_attack": man.get("test_mcc_by_attack"),
            "features": man.get("vehicle_features"),
            "hash": man.get("model_hash"),
            "hash_matches": (len(hashes) == 1 and list(hashes)[0] == man.get("model_hash")),
        }
    return {"available": True, "summary": summary, "rows": out, "notes": notes}


# ------------------------------------------------------------------------- LLM ------
def llm(d, meta):
    rows = C._rows(C.find(d, "gnn_decision"))
    rows = [r for r in rows if r.get("llm_verdict")]
    inc = C._rows(C.find(d, "llm_incident"))
    if not rows and not inc:
        return {"available": False, "summary": {}, "rows": [],
                "notes": ["No LLM verdicts — the layer needs --llmReason=1 "
                          "(which needs --gnnDetect=1)."]}

    fa = sum(1 for r in rows if r["llm_verdict"] == "false_accusation")
    leg = sum(1 for r in rows if r["llm_verdict"] == "legitimate")
    esc = sum(1 for r in rows if C._b(r, "llm_escalate"))

    out = [{
        "event": C._i(r, "event_id", -1),
        "verdict": r["llm_verdict"], "confidence": C._f(r, "llm_confidence"),
        "escalate": C._b(r, "llm_escalate"), "blocked": C._b(r, "llm_blocked"),
        "gnn_score": C._f(r, "gnn_score"),
        "prompt_hash": r.get("llm_prompt_hash") or "",
    } for r in rows]

    incidents = [{
        "event": C._i(r, "event_id", -1),
        "accuser": C._i(r, "accuser_id"), "victim": C._i(r, "victim_id"),
        "verdict": r.get("llm_verdict"), "confidence": C._f(r, "llm_confidence"),
        "gnn_score": C._f(r, "gnn_score"), "pattern": r.get("gnn_pattern"),
        "action": r.get("action") or "none",
    } for r in inc]

    notes = [
        "The prompt text is NOT persisted — only its SHA3-512 hash. Assembly is "
        "deterministic (sorted keys, floats rounded), so a prompt could be re-assembled "
        "offline and checked against the stored hash, but only if the run also captured "
        "the graphs (--gnnExport=1).",
        "The gate escalates only when the reply parses AND verdict is false_accusation AND "
        "confidence >= tau_llm (0.8). Every other outcome is a default-reject; the reason "
        "string is produced but not written to CSV.",
        "Variant classification is deliberately the GNN's, not the LLM's — llm_variant is "
        "parsed then discarded, which is why it is empty here.",
    ]
    return {"available": True,
            "summary": {"verdicts": len(rows), "false_accusation": fa, "legitimate": leg,
                        "escalated": esc, "incidents": len(incidents),
                        "tau_llm": 0.8, "sources": LLM_SOURCES},
            "rows": out, "incidents": incidents, "notes": notes}


# ----------------------------------------------------------------------- chain ------
def chain(d, meta):
    audit = C._rows(C.find(d, "audit"))
    ctrl = C._rows(C.find(d, "controller_audit"))
    rsu = C._rows(C.find(d, "rsu_audit"))
    stake = C._rows(C.find(d, "stake"))
    if not audit and not stake:
        return {"available": False, "summary": {}, "rows": [],
                "notes": ["No on-chain activity — the layer needs --blockchain=1."]}

    failovers = [{
        "event": C._i(r, "event_id", -1), "controller": C._i(r, "controller_id"),
        "backup": C._i(r, "backup_controller"), "zone": C._i(r, "controller_zone"),
        "trust": C._f(r, "controller_trust"), "epoch": C._i(r, "region_epoch"),
        "no_standby": C._b(r, "no_standby"),
    } for r in ctrl if C._b(r, "failover")]

    # RSU custody: only the transitions matter, not a row per event
    seen, custody = {}, []
    for r in rsu:
        i, st = C._i(r, "serving_rsu", -1), C._i(r, "rsu_status")
        if i < 0 or seen.get(i) == st:
            continue
        seen[i] = st
        if st:
            custody.append({"rsu": i, "state": {1: "QUARANTINED", 2: "REMOVED"}.get(st, st),
                            "trust": C._f(r, "rsu_trust"), "event": C._i(r, "event_id", -1)})

    burned = sum(C._f(r, "stake_burned") for r in stake)
    return {"available": True,
            "summary": {
                "contracts": SC_ROLES,
                "submissions": len(audit),
                "endorsed": sum(1 for r in audit if C._b(r, "endorsed")),
                "divergence": sum(1 for r in audit if C._b(r, "divergence")),
                "blocked": sum(1 for r in audit if C._b(r, "blockchain_blocked")),
                "rolled_back": sum(1 for r in audit if C._b(r, "rolled_back")),
                "failovers": len(failovers),
                "custody_actions": len(custody),
                "stake_burned": round(burned, 3),
                "stake_filings": sum(1 for r in stake if C._f(r, "stake_burned") > 0),
            },
            "rows": [{
                "event": C._i(r, "event_id", -1),
                "endorsed": C._b(r, "endorsed"), "divergence": C._b(r, "divergence"),
                "reason": r.get("divergence_reason", ""),
                "blocked": C._b(r, "blockchain_blocked"),
                "rolled_back": C._b(r, "rolled_back"),
                "controller_trust": C._f(r, "controller_trust"),
            } for r in audit],
            "failovers": failovers, "custody": custody,
            "notes": ["SC2 seals the RSU snapshot BEFORE the controller acts; SC3 later "
                      "audits the controller's outcome against that sealed evidence. That "
                      "ordering is what defeats control-plane attacks."]}


# ------------------------------------------------------------------ reputation ------
def reputation(d, meta, event_id=None, limit=400):
    path = C.find(d, "trust_refresh")
    if not path:
        return {"available": False, "summary": {}, "rows": [],
                "notes": ["_trust_refresh.csv is absent — this run used --liteLogs=1, "
                          "which skips the three heavy per-pair CSVs."]}
    rows = C.trust_refresh_rows(path, event_id, limit)
    all_rows = C.trust_refresh_rows(path, None, 10 ** 7)
    drops = [r for r in all_rows if r["delta"] < 0]
    return {"available": True,
            "summary": {
                "updates": len(all_rows), "showing": len(rows),
                "decreases": len(drops),
                "mean_delta": round(sum(r["delta"] for r in all_rows) / len(all_rows), 5)
                if all_rows else 0,
                "worst_drop": round(min((r["delta"] for r in all_rows), default=0), 5),
                "chain": "Rt → Rec → GLSim → Tr",
                "params": {k: _cfg(meta, k) for k in
                           ("eta", "beta", "zeta", "gamma", "penalty",
                            "blacklistThreshold", "initialGlobalTrust")},
            },
            "rows": rows,
            "notes": ["Each row is one trust update: the GLSim similarity that drove it and "
                      "the old → new trust it produced. This is the file with an event_id.",
                      "_reputation.csv holds the fuller n×n snapshot but is ~153 MB and "
                      "carries no event_id, so it is not used here."]}


# --------------------------------------------------------------------- keymgmt ------
def keymgmt(d, meta):
    lkh = C.lkh_summary(C.find(d, "lkh"))
    dkg = None
    log = os.path.join(d, "sim.log")
    if os.path.exists(log):
        try:
            with open(log, errors="replace") as fh:
                for line in fh:
                    if line.startswith("DKG:") or line.startswith("[KEYMGMT]"):
                        dkg = line.strip() if dkg is None else dkg
        except OSError:
            pass

    notes = [
        "The RA signing key is Shamir-split t-of-n across the controllers, so credential "
        "issue and revoke need ≥t of them.",
        "Honest caveat from keymgmt.h: this is NOT a true threshold signature — ≥t shares "
        "momentarily reconstruct the key. Threshold-BLS / FROST was declared out of scope.",
        "_lkh.csv rows carry a zone but no event_id, so a re-key cannot be attributed to a "
        "specific accusation without inference. The value here is the aggregate cost.",
    ]
    if _on(meta, "lkhFlat"):
        notes.append("This run used --lkhFlat=1: the hierarchy is replaced by a flat "
                     "pre-shared zone key, so re-key cost is O(N) rather than O(m log N). "
                     "That is the A13 arm.")
    return {"available": lkh.get("events", 0) > 0,
            "summary": {"dkg_line": dkg, "lkh": lkh,
                        "revocation_batch": _cfg(meta, "revocationBatch", 1)},
            "rows": [], "notes": notes}


BUILDERS = {"pqc": pqc, "zkp": zkp, "gnn": gnn, "llm": llm,
            "chain": chain, "reputation": reputation, "keymgmt": keymgmt}


def build(run_id, name, **kw):
    if name not in BUILDERS:
        return None
    d, meta = run_paths(run_id)
    if not os.path.isdir(d):
        return None
    out = BUILDERS[name](d, meta, **kw) if name == "reputation" else BUILDERS[name](d, meta)
    label, role = ROLES[name]
    out.update(component=name, label=label, role=role, run_id=run_id)
    return out


def overview(run_id):
    """Which components this run actually used, with a headline number each."""
    d, meta = run_paths(run_id)
    if not os.path.isdir(d):
        return None
    cards = []
    for n in NAMES:
        try:
            r = build(run_id, n)
        except Exception as exc:            # a partial run must not break the whole page
            cards.append({"component": n, "label": ROLES[n][0], "role": ROLES[n][1],
                          "available": False, "headline": f"error: {exc}"})
            continue
        s = r.get("summary", {})
        headline = {
            "pqc": lambda: s.get("vehicle_tier", ""),
            "zkp": lambda: f"{s.get('pass', 0)} pass / {s.get('fail', 0)} fail",
            "gnn": lambda: f"{s.get('flagged', 0)} flagged of {s.get('scored', 0)}",
            "llm": lambda: f"{s.get('false_accusation', 0)} false-accusation verdicts",
            "chain": lambda: f"{s.get('submissions', 0)} submissions · "
                             f"{s.get('divergence', 0)} divergent",
            "reputation": lambda: f"{s.get('updates', 0)} trust updates",
            "keymgmt": lambda: f"{(s.get('lkh') or {}).get('events', 0)} re-key events",
        }[n]
        cards.append({"component": n, "label": ROLES[n][0], "role": ROLES[n][1],
                      "available": r.get("available", False),
                      "headline": headline() if r.get("available") else "not enabled",
                      "notes": len(r.get("notes", []))})
    return {"run_id": run_id, "effective": meta.get("effective", {}),
            "imported": bool(meta.get("imported")), "cards": cards}
