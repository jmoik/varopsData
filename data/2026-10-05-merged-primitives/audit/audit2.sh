#!/usr/bin/env bash
# Usage: audit2.sh <gsr checkout> <out dir> <reference seconds>: contention and ConnectBlock.
set -euo pipefail
G=$1; O=$2; ref=$3; B=$G/build-varops-calibration/bin
mkdir -p "$O"
echo "start $(date -u +%FT%TZ) reference $ref load: $(uptime | sed 's/.*load average[s]*: //')"
"$B/bench_varops_primitives" --connect-block --funding-shape v2 --funding-shape p2a --funding-shape c4 \
    --funding-shape c2-op1 --funding-shape c2-success-stack --funding-shape c2-success-macros --funding-shape v2-stack \
    --pre-v2-seconds "$ref" --epochs 5 > "$O/connect-block.txt" 2> "$O/connect-block.log"
grep CONNECT_BLOCK "$O/connect-block.txt" | sed 's/ weight=[0-9]*//'
"$B/bench_varops_primitives" --contention --pre-v2-seconds "$ref" --epochs 3 > "$O/contention.txt" 2> "$O/contention.log"
grep CONTENTION "$O/contention.txt" | sed 's/ weight=.*consumed=[0-9]*//'
echo "end $(date -u +%FT%TZ)"
echo EXIT=0
