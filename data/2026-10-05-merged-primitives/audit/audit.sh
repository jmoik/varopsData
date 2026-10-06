#!/usr/bin/env bash
# Usage: audit.sh <gsr checkout> <out dir>: same-run reference, the DIV/MOD scripts,
# the DIVCORE fixtures (with add-back operands) and the funding shapes.
set -euo pipefail
G=$1; O=$2; B=$G/build-varops-calibration/bin
mkdir -p "$O"
echo "start $(date -u +%FT%TZ) $(git -C "$G" rev-parse HEAD) load: $(uptime | sed 's/.*load average[s]*: //')"
"$B/bench_varops" --case-filter OP_NOP --sample-budget-percent 2 --epochs 5 --silent --file "$O/reference.csv" > "$O/reference.log" 2>&1
ref=$(python3 - "$O/reference.csv" <<'PY'
import csv, sys
rows = [r for r in csv.DictReader(l for l in open(sys.argv[1]) if not l.startswith('#'))
        if r['Record_Type'] == 'summary' and r['Domain'] == 'pre-gsr-tapscript-v1' and r['Actual_Termination'] in {'OK', 'SCRIPT_ERR_OK', 'No error'}]
print(max(float(r['Wall_Seconds']) for r in rows))
PY
)
echo "reference $ref"
"$B/bench_varops_primitives" --funding-block --pre-v2-seconds "$ref" --epochs 5 > "$O/funding-block.txt" 2> "$O/funding-block.log"
grep FUNDING_BLOCK "$O/funding-block.txt" | sed 's/ weight=.*per_signature=[0-9]*//'
"$B/bench_varops" --case-filter divmod --epochs 3 --silent --file "$O/divmod.csv" > "$O/divmod.log" 2>&1
grep -E "pre-v2 worst|projected worst" "$O/divmod.log" || true
"$B/bench_varops_primitives" --only DIVCORE --pre-v2-seconds "$ref" --epochs 5 --out "$O/divcore.csv" 2> "$O/divcore.log"
if "$B/bench_varops_primitives" --help | grep -q -- --contention; then
    "$B/bench_varops_primitives" --contention --pre-v2-seconds "$ref" --epochs 3 > "$O/contention.txt" 2> "$O/contention.log"
    grep CONTENTION "$O/contention.txt" | sed 's/ weight=.*consumed=[0-9]*//'
fi
if "$B/bench_varops_primitives" --help | grep -q -- --connect-block; then
    "$B/bench_varops_primitives" --connect-block --funding-shape v2 --funding-shape p2a --funding-shape c4         --funding-shape c2-op1 --funding-shape c2-success-stack --pre-v2-seconds "$ref" --epochs 5         > "$O/connect-block.txt" 2> "$O/connect-block.log"
    grep CONNECT_BLOCK "$O/connect-block.txt" | sed 's/ weight=[0-9]*//'
fi
echo "end $(date -u +%FT%TZ)"
echo EXIT=0
