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

## Funding block

Every Taproot script-path input that funds the budget, Tapleaf 0xC2 or an unknown leaf version, gets BIP 341's
commitment check outside any budget. `bench_varops_primitives --funding-block` (varopsData `35669eb`, gsr
`c77f8d5361`) builds a block-sized transaction of minimal funding inputs (164 non-witness WU and a 33-byte control
block; 0xc4 with an empty script, or 0xC2 `OP_1`) and one Tapleaf 0xC2 input whose `OP_2DUP OP_CHECKSIGVERIFY`
macro loop spends the whole budget. It validates the transaction as `CheckInputScripts` does, with
`PrecomputedTransactionData`, the transaction budget and a `CScriptCheck` per input, single-threaded, against the
same run's slowest pre-v2 case ([`funding-block/`](funding-block/)):

    bench_varops --case-filter OP_NOP --sample-budget-percent 2 --epochs 5 --file reference.csv
    bench_varops_primitives --funding-block --pre-v2-seconds <slowest pre-v2 case of reference.csv> --epochs 7

| Machine | Funding leaf | Inputs | Signatures | Block / reference | Signature input alone | Per funding input |
|---|---|---|---|---|---|---|
| Intel i7-7700 | 0xc4 | 19,974 | 78,419 | **1.042x** | 0.865x | 30.6 µs |
| Intel i7-7700 | 0xC2 `OP_1` | 19,874 | 78,346 | **1.051x** | 0.874x | 30.7 µs |
| Apple M1 Pro | 0xc4 | 19,974 | 78,419 | **1.146x** (max 1.154x) | 0.944x | 22.7 µs |
| Apple M1 Pro | 0xC2 `OP_1` | 19,874 | 78,346 | **1.154x** (max 1.163x) | 0.952x | 22.9 µs |
| Apple M4 Pro | 0xc4 | 19,974 | 78,419 | 0.889x | 0.731x | 12.3 µs |
| Apple M4 Pro | 0xC2 `OP_1` | 19,874 | 78,346 | 0.907x | 0.746x | 12.6 µs |

One commitment check (tapleaf hash, Merkle root and `CheckTapTweak`) costs 0.84–0.86 of a Schnorr verification:

| Machine | Commitment check | Schnorr verification | Commitment check at 1.0x the reference |
|---|---|---|---|
| Intel i7-7700 | 30.3 µs | 36.2 µs | 348,000 varops (2.05x TWEAK) |
| Apple M1 Pro | 22.4 µs | 26.2 µs | 398,000 varops (2.34x TWEAK) |
| Apple M4 Pro | 12.0 µs | 13.9 µs | 308,000 varops (1.81x TWEAK) |

The block exceeds the reference on the i7-7700 and M1 Pro. A funding input's full time is 31–41 weight units of
budget at 1.0x (M1 Pro: 40.5). If a funding script-path input contributed `(weight − k) × 10,000`, the measured
blocks need k ≥ 12 (i7-7700) and k ≥ 33 (M1 Pro) using the signature loop's own margin; k = 50, one SIGCHECK per
input as in BIP 342's 50 weight units per signature, brings them to 0.83x and 0.92x.

## TWEAK and commitment checks at SIGCHECK

The TWEAK fixture and the OP_TWEAKADD script case tweaked by 1, so libsecp256k1's scalar multiplication walked
one bit. With a hash-sized tweak (varopsData `885f067`), a tweak takes 0.79–0.82 of a signature check, 1.7–2.3
times the fitted TWEAK of 170,000. gsr `d4121fe10f` therefore charges every elliptic-curve operation one SIGCHECK:
OP_TWEAKADD pays `BASE + SIGCHECK + WRITE(32)`, and each funding Taproot script-path input pays one SIGCHECK from
the transaction budget for its commitment check. The funding block's signature input then has about 58,800
signatures instead of 78,400. [`sigcheck/`](sigcheck/) repeats both checks at gsr `f20e116670` (same tree as
`d4121fe10f` except one include) with varopsData `11ada19`:

    bench_varops --case-filter OP_NOP --sample-budget-percent 2 --epochs 5 --file reference.csv
    bench_varops --case-filter tweakadd --epochs 5 --file tweakadd.csv
    bench_varops_primitives --only SIG --pre-v2-seconds <slowest pre-v2 case of reference.csv> --epochs 7 --out sig.csv
    bench_varops_primitives --funding-block --pre-v2-seconds <same> --epochs 7

| Machine | Reference (s) | OP_TWEAKADD script / reference | Funding block 0xc4 / 0xC2 | TWEAK / SIG/32 | Commitment check / SIGCHECK |
|---|---|---|---|---|---|
| Intel i7-7700 | 3.485 | 0.67x | 0.834x / 0.829x | 0.792 | 0.69 |
| Apple M1 Pro | 2.283 | 0.78x | 0.933x / 0.910x (max 0.937x) | 0.799 | 0.79 |
| Apple M4 Pro | 1.584 | 0.60x | 0.711x / 0.713x | 0.821 | 0.61 |

Everything is below 1.0x. The M1 Pro started under load from other sessions (load 7, falling to 3).

