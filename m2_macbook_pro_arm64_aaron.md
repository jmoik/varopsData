Platform: Mac M3
Python: 3.11
Benchmark script: (commit hash)
Total tests: 875

Key observation:
RIPEMD160 takes ~1.63s, very close to Schnorr validation (~1.86s),
yet accounts for only 3.33% of the varops budget.
This indicates a ~30x gap between CPU cost and varops cost.