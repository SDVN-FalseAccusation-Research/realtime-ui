#!/usr/bin/env bash
# Bring up everything a DEFENDED run needs, so the UI can launch one.
#
#     tools/demo_stack.sh status      what is up, and what the GNN is actually serving
#     tools/demo_stack.sh up          sidecars + Fabric + bridge   (~2 min, mostly Fabric)
#     tools/demo_stack.sh reset       fresh ledger + bridge — RUN THIS BETWEEN DEFENDED RUNS
#     tools/demo_stack.sh down        bridge + sidecars; Fabric only with FABRIC=1
#
# ONE DEFENDED RUN PER LEDGER. A second run against a ledger that already holds the first
# run's SC1 registrations exits 0, prints a summary, and files ZERO accusations: SC1
# re-registers fine but the zk-STARK membership gate then denies every vehicle
# ("ZKP membership: admitted=0 denied=200"). It looks like a successful run in which
# nothing happened. run_sweep.sh resets the ledger before every cell for this reason.
#
# WHY THIS EXISTS
#   run_sweep.sh owns the bridge on :7545 and stops it on exit, so after any sweep the UI's
#   /api/health reports can.blockchain=false and a defended run cannot be started from the
#   browser at all. Nothing else in the repo brings this stack up.
#
# RUN IT BEFORE THE AUDIENCE IS WATCHING. A Fabric reset is ~78 s measured, and `up` is not
# something to be doing while a projector is on.
#
# The bridge-start logic below is lifted from run_sweep.sh:361 deliberately, comment and
# all: `port_up 7545` can be satisfied by a PREVIOUS bridge that has not released the port
# while our own process loses the bind and exits. That transient has cost seven sweep cells
# across three campaigns. Do not "simplify" it into a single attempt.
set -uo pipefail

if ! docker info >/dev/null 2>&1 && sg docker -c "docker info" >/dev/null 2>&1; then
  exec sg docker -c "$(printf '%q ' "$0" "$@")"
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UI_ROOT="$(dirname "$HERE")"
FYP="$(dirname "$UI_ROOT")"
SIM="${FYP_SIM:-$FYP/project-simulation}"
BCD="${FYP_BCD:-$SIM/blockchain-defence}"
NET="$BCD/network-realnet"
GEN="$NET/generated"
BRIDGE_BIN="$BCD/bridge/bridge.bin"
LOGS="${DEMO_LOGS:-$UI_ROOT/.stack-logs}"

# The model the banked results were produced with. The sidecar's own default is a DIFFERENT
# model at a different threshold and every one of them answers "gnn-v1 (trained)", so this
# assignment is the only thing standing between the demo and silently wrong scores.
GNN_MODEL="${FYP_GNN_MODEL:-gnn_v16_labelfix}"

say() { printf '%s\n' "$*" >&2; }
port_up() { ss -ltn 2>/dev/null | grep -q "127.0.0.1:$1"; }

wait_port() {  # $1 = port, $2 = seconds
  local i; for i in $(seq 1 "$2"); do port_up "$1" && return 0; sleep 1; done; return 1
}

# ------------------------------------------------------------------------- sidecars ---
start_sidecar() {  # $1 = name, $2 = port, $3 = cwd, then the command
  local name="$1" port="$2" cwd="$3"; shift 3
  if port_up "$port"; then say "  $name already up on :$port"; return 0; fi
  [ -d "$cwd" ] || { say "  $name SKIPPED: no such directory $cwd"; return 1; }
  ( cd "$cwd" && nohup "$@" >"$LOGS/$name.log" 2>&1 & echo $! >"$LOGS/$name.pid" )
  if wait_port "$port" 60; then
    say "  $name up on :$port"
  else
    say "  $name FAILED to bind :$port — see $LOGS/$name.log"; return 1
  fi
}

up_sidecars() {
  say "== sidecars =="
  start_sidecar zkp 7070 "$BCD/zkp-stark" ./target/release/zkp-stark serve
  # GNN_ARTIFACTS is exported for THIS process only; nothing else on the machine sets it.
  ( export GNN_ARTIFACTS="artifacts/$GNN_MODEL"
    start_sidecar gnn 7071 "$BCD/gnn-sidecar" .venv/bin/python -m gnn_sidecar serve 7071 )
  # The LLM sidecar deliberately runs on the GNN sidecar's venv (shared torch install).
  start_sidecar llm 7072 "$BCD/llm-sidecar" ../gnn-sidecar/.venv/bin/python \
      -m llm_sidecar serve 7072
}

# --------------------------------------------------------------------------- Fabric ---
up_fabric() {
  say "== Fabric (down -> up -> deploy) =="
  if ! docker info >/dev/null 2>&1; then
    say "  docker is not usable from this shell."
    say "  If \`docker info\` works elsewhere, this shell predates the docker group —"
    say "  open a fresh one (or \`newgrp docker\`) and re-run."
    return 1
  fi
  ( cd "$NET" && ./down.sh && ./up.sh && ./deploy.sh ) >"$LOGS/fabric.log" 2>&1
  local rc=$?
  # BELT AND BRACES, same as run_sweep.sh:490. deploy.sh exits non-zero when the commit
  # does not land, but an older checkout fails SILENTLY -- and the bridge then binds :7545,
  # logs a perfectly healthy startup, and every endorsement is wrong.
  if grep -q "commit did not land\|ERROR: chaincode commit" "$LOGS/fabric.log" 2>/dev/null; then
    say "  chaincode commit did not land — retrying deploy"
    ( cd "$NET" && ./deploy.sh ) >>"$LOGS/fabric.log" 2>&1; rc=$?
  fi
  if [ $rc -eq 0 ]; then
    # The UI reads this to know the ledger has not yet been consumed by a defended run.
    date -u +%Y-%m-%dT%H:%M:%SZ >"$LOGS/ledger-fresh"
    say "  ledger reset OK"
  else
    rm -f "$LOGS/ledger-fresh"
    say "  ledger reset FAILED — see $LOGS/fabric.log"
  fi
  return $rc
}

# --------------------------------------------------------------------------- bridge ---
start_bridge() {
  say "== bridge :7545 =="
  [ -x "$BRIDGE_BIN" ] || { say "  no bridge binary at $BRIDGE_BIN
  build it with:  ( cd $BCD/bridge && go build -o bridge.bin . )"; return 1; }
  local attempt i pid
  for attempt in 1 2; do
    fuser -k 7545/tcp 2>/dev/null || true
    for i in $(seq 1 10); do port_up 7545 || break; sleep 1; done

    nohup "$BRIDGE_BIN" --listen 127.0.0.1:7545 --secure --channel sdvnrealnet \
      --node-map "$GEN/node-map.json" --crypto "$GEN/organizations" \
      >"$LOGS/bridge.log" 2>&1 &
    pid=$!
    echo "$pid" >"$LOGS/bridge.pid"
    wait_port 7545 40 || true
    if port_up 7545; then
      sleep 4
      if kill -0 "$pid" 2>/dev/null; then say "  bridge up (pid $pid)"; return 0; fi
    fi
    say "  bridge start attempt $attempt failed — retrying"
    kill "$pid" 2>/dev/null || true
    sleep 3
  done
  say "  bridge FAILED — see $LOGS/bridge.log"
  return 1
}

stop_bridge() {
  [ -f "$LOGS/bridge.pid" ] && kill "$(cat "$LOGS/bridge.pid")" 2>/dev/null
  fuser -k 7545/tcp 2>/dev/null || true
  local i; for i in $(seq 1 15); do port_up 7545 || break; sleep 1; done
  rm -f "$LOGS/bridge.pid"
}

# --------------------------------------------------------------------------- status ---
gnn_model_check() {
  # WHICH model, not just "is it trained" — every one of them answers "gnn-v1 (trained)".
  #
  # This only works while NOTHING ELSE is talking to the sidecar: it serves one connection
  # at a time, so once the bridge is attached a digest request is accepted and never
  # answered. That is why `up` calls this after the sidecars and BEFORE start_bridge.
  port_up 7071 || { echo "  gnn     model   n/a (sidecar down)"; return 1; }
  if port_up 7545; then
    # Asking now would hang and leave a socket the sidecar never reads. Say so plainly
    # instead of printing a timeout that reads like a fault.
    echo "  gnn     model   not read (busy with the bridge; checked at startup)"
    return 0
  fi
  python3 - "$BCD/gnn-sidecar/artifacts/$GNN_MODEL/manifest.json" <<'PY'
import json, socket, sys
try:
    s = socket.create_connection(("127.0.0.1", 7071), 3)
    s.sendall(b'{"op":"digest"}\n')
    served = json.loads(s.recv(65536).decode()).get("model_hash")
except Exception as exc:
    print(f"  gnn     model   UNKNOWN ({exc})"); raise SystemExit
try:
    want = json.load(open(sys.argv[1]))
except Exception:
    print("  gnn     model   UNKNOWN (no manifest)"); raise SystemExit
ok = served == want.get("model_hash")
print(f"  gnn     model   {'OK' if ok else 'MISMATCH'} "
      f"(expected {sys.argv[1].split('/')[-2]}, tau={want.get('tau_gnn')})")
if not ok:
    print("          restart it with GNN_ARTIFACTS=artifacts/"
          + sys.argv[1].split('/')[-2])
PY
}

status() {
  local p n
  for n in zkp:7070 gnn:7071 llm:7072 bridge:7545 ui:8000; do
    p="${n#*:}"
    printf '  %-7s :%-5s %s\n' "${n%%:*}" "$p" \
      "$(port_up "$p" && echo up || echo DOWN)"
  done
  gnn_model_check
  if [ -f "$LOGS/ledger-fresh" ]; then
    echo "  ledger          fresh (reset $(cat "$LOGS/ledger-fresh")) — one defended run"
  else
    echo "  ledger          USED or unknown — run '${0##*/} reset' before a defended run"
  fi
  docker info >/dev/null 2>&1 && echo "  docker          up" \
    || echo "  docker          DOWN or not permitted from this shell"
}

# ----------------------------------------------------------------------------- main ---
mkdir -p "$LOGS"
case "${1:-status}" in
  up)
    rc=0
    up_sidecars || rc=1
    # BEFORE Fabric and the bridge: this is the only window in which the sidecar will
    # answer, and a wrong model here invalidates every number the run produces.
    say "== gnn model =="; gnn_model_check
    up_fabric   || rc=1
    if [ $rc -eq 0 ]; then start_bridge || rc=1; else
      say "== bridge SKIPPED: Fabric is not ready =="
    fi
    say ""; say "== status =="; status
    [ $rc -eq 0 ] && say "" && say "ready — /api/health should now report can.blockchain=true"
    exit $rc
    ;;
  down)
    stop_bridge
    for n in zkp gnn llm; do
      [ -f "$LOGS/$n.pid" ] && kill "$(cat "$LOGS/$n.pid")" 2>/dev/null
      rm -f "$LOGS/$n.pid"
    done
    # Fabric is left UP on purpose: the containers take ~78 s to rebuild and are usually
    # wanted again straight away. FABRIC=1 tears them down too.
    [ "${FABRIC:-0}" = 1 ] && ( cd "$NET" && ./down.sh ) >>"$LOGS/fabric.log" 2>&1
    status
    ;;
  reset)
    # Between defended runs: a fresh ledger, and a bridge reconnected to it.
    stop_bridge
    up_fabric || exit 1
    start_bridge || exit 1
    say ""; status
    ;;
  status) status ;;
  *) say "usage: ${0##*/} {up|down|status}"; exit 2 ;;
esac
