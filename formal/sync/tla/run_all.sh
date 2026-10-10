#!/bin/sh
# Re-run every check behind ../README.md. Each line prints either
# "No error has been found" or the violated invariant; traces land in
# out/<NAME>.json (render with ./trace.py out/<NAME>.json).
# Args of run_one.sh: INV MaxSteps InitNeverFetched EnableAbort EnableRemote EnableIgnore EnableDev
cd "$(dirname "$0")"
for inv in I1_NoDoubleCreate I2_DeleteOnlyUserRemoved I2b_DeleteNeedsForce \
           I3_IgnoredUntouched I4_BranchIsolation I5_PullKeepsLocalWork \
           I6_PushThenDiffClean I8_ManifestMatchesDisk I9_AbortLeavesNoUnrecordedWrites \
           I11_NoLostUpdate I11b_NoSilentResurrect I12_PullResolvesRemoteModified; do
  echo "== $inv (depth 4, all actors)"; ./run_one.sh $inv 4
done
echo "== I7 (never-fetched initial entry)"; TAG=nf ./run_one.sh I7_NeverFetchedNotDeleted 4 TRUE
# single-branch (production only) variants: separate the root causes that do
# not need a dev branch from the promote/cross-branch ones
for inv in I2_DeleteOnlyUserRemoved I6_PushThenDiffClean I8_ManifestMatchesDisk; do
  echo "== $inv (production only)"; TAG=prod ./run_one.sh $inv 4 FALSE TRUE TRUE TRUE FALSE
done
echo "== I1 (production only, depth 5)"; TAG=prod ./run_one.sh I1_NoDoubleCreate 5 FALSE TRUE TRUE TRUE FALSE
echo "== I3 (production only, depth 5)"; TAG=prod ./run_one.sh I3_IgnoredUntouched 5 FALSE TRUE TRUE TRUE FALSE
