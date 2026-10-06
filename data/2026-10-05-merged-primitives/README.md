# Merged primitives · 2026-10-05

Model `read-write-arith-v1`: the eleven primitives of BIP 440 (READ, WRITE and ARITH merged from the earlier
producer and normalize parts). Commit `26ed8d63da` (branch `gsr`), varopsData `ac31136`, runner defaults through
`bitcoinSetup.sh`: 5 reference epochs, 7 primitive epochs, 10 ms samples and 100 ms for storage lifetimes.

| Machine | Compiler | SHA256 | Reference (s) | Epoch noise | Notes |
|---|---|---|---|---|---|
| Intel i7-7700 | GCC 15.2.0 | sse4 | 3.485 | 0.25% | Hetzner, `bitcoinSetup.sh` |

Later gsr commits change no measured primitive: `0b644f0761` changes only which opcodes pay MOVE and how many
entries they count, and OP_TX scope operands only in what they accept.

## Macro unrolling checks

Reusable Macros charges `BASE` per substituted instruction and visited reference plus `WRITE` of the unrolled
script. [`macro-checks/`](macro-checks/) checks that charge on three machines at gsr `8998dcca1c` (M4 Pro:
`57c7f97940`, which differs only in a test-vector file) with varopsData `c663622`:

    bench_varops --case-filter macro --epochs 5 --file macro.csv
    bench_varops --case-filter function --epochs 5 --file function.csv
    bench_varops_primitives --only UNROLL --pre-v2-seconds <slowest pre-v2 case of macro.csv> --epochs 7 --out unroll.csv
    python3 src/summarize_macro_checks.py macro-checks/<machine>...

The UNROLL fixtures unroll inactive NOP, push, reference-chain and fan-out bodies (body i references body i-1
twice, 2^depth - 1 references per call); they are timed against their charge. The block cases spend the full
budget on unrolling, in one script or repeated as the inputs of one transaction (`macro-unroll-*-repeated`), plus
reference chains, fan-out and macro bodies of costly opcodes (`function-*`); they are timed against the same run's
slowest pre-v2 script. Worst medians ([`summary.txt`](macro-checks/summary.txt)):

| Machine | UNROLL fixture / charge | Macro block case / reference | Function case / reference |
|---|---|---|---|
| Intel i7-7700 | 0.73x (push-1); fan-out 0.36x | 0.62x (1-byte NOP bodies) | 0.71x (HASH256 chain) |
| Apple M1 Pro | 0.84x (push-1); fan-out 0.80x | 0.91x (reference chain, depth 16,384) | 0.71x (RIPEMD160) |
| Apple M4 Pro | 0.81x (push-1); fan-out 0.48x | 0.65x (reference chain, depth 16,384) | 0.70x (RIPEMD160) |

Nothing reaches 1.0x: the unrolling charge covers unrolling per script and across the inputs of a block, and
needs no primitive of its own. The M4 Pro ran under load from other sessions (load 3–4); its single-epoch
maxima reach 0.87x.
