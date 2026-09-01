# Golden fixtures

Real recorded simulator output. They exist so the parser, the timeline synthesiser and the
event contract can be developed and regression-tested **without launching ns-3**.

| File | How it was produced |
|---|---|
| `sim_standard.log` | the Standard demo preset — see command below |
| `sim_standard.stderr` | its stderr (empty on a healthy run) |
| `standard_decisions.csv` | that run's `_decisions.csv` — the **authoritative** timing source |

```bash
cd project-simulation/ns-3.35
export LD_LIBRARY_PATH="$PWD/build/lib:$HOME/.local/lib"
./build/scratch/sdvn_false_accusation_realnet/sdvn_false_accusation_realnet \
  --traceFile=../sumo-manhattan/manhattan.tcl \
  --numVehicles=200 --numRsus=56 --numControllers=4 \
  --warmupTime=60 --warmupAccusationStart=35 \
  --attackType=single_data --attackPercent=5 --attackRounds=1 --attackWindow=300 \
  --debugLogs=1 --liteLogs=1 --csvPrefix=results/ui/fixture_standard/run
```

`--debugLogs=1` is **required** — it gates every per-event line *and* the end-of-run summary.
(`--liteLogs` does not affect stdout at all; it only skips three heavy CSVs.)

## What this fixture happens to cover

* 3 warmup + 10 attack accusations, 151 `[TRIG]` probes, a clean `run_end`.
* **A truncated `[ROLES]` list** — `misbehavers=20` exceeds the simulator's 15-id cap and
  ends `,...]`. That is the case that would otherwise silently under-colour the map.
* **One `FALLBACK-far` attack** (event 8, dist=2021) alongside nine `in-range` ones.

## Still to record

* a 40%-attackPercent run — truncated *attacker* list (the Extended preset has 18)
* a deliberately failing run — stderr `ERROR:` + non-zero exit
