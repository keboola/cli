#!/bin/sh
# usage: run_one.sh INVARIANT MaxSteps [InitNeverFetched] [EnableAbort] [EnableRemote] [EnableIgnore] [EnableDev] [MaxScaffolds]
# Checks ONE invariant (plus TypeOK) and writes out/<NAME>.{cfg,log,json};
# NAME = <INV> or <INV>_$TAG when the TAG env var is set.
set -e
cd "$(dirname "$0")"
INV=$1; STEPS=$2; NF=${3:-FALSE}; AB=${4:-TRUE}; RE=${5:-TRUE}; IG=${6:-TRUE}; DEV=${7:-TRUE}; SC=${8:-1}
N=$INV${TAG:+_$TAG}
mkdir -p out
cat > out/$N.cfg <<CFG
CONSTANTS
    MaxSteps = $STEPS
    NV = 3
    InitNeverFetched = $NF
    MaxScaffolds = $SC
    EnableAbort = $AB
    EnableRemote = $RE
    EnableIgnore = $IG
    EnableDev = $DEV
SPECIFICATION Spec
CONSTRAINT StepBound
INVARIANTS TypeOK $INV
CFG
cp SyncEngine.tla out/
cd out
java -XX:+UseParallelGC -cp ~/tools/tla/tla2tools.jar tlc2.TLC -workers auto -deadlock \
  -dumpTrace json $N.json -metadir "states_$N" -config $N.cfg SyncEngine.tla > $N.log 2>&1 || true
rm -rf "states_$N"
grep -E "violated|No error|states generated|distinct states|Error:" $N.log | head -5
