# 3-epoch runs on six machines · 2026-09-30

Commit `60bc3ca40a43f5c1a8c5e68f043f2e3622483e58` (branch `gsr`), model `producer-normalize-v1`.
These are the first runs whose prepared-state pool is limited to the 8 MB script stack limit
(`MAX_TAPSCRIPT_V2_TOTAL_STACK_SIZE`); earlier runs used 64 MiB. `gsr` later moved to `e5465e2273`,
which changes only `costing-methodology.md`. The sources and scripts are identical.

    python3 dev/varops/primitive-calibration/run_calibration.py --reference-epochs 3 --epochs 3 \
        --sample-ms 5 --copy-sample-ms 50 --output varop-calibration-<machine>-3epoch.json

| Machine | Compiler | SHA256 | Reference (s) | Notes |
|---|---|---|---|---|
| Apple M4 Pro | Clang 17.0.0 | arm_shani | 1.678 | local; heavily loaded by other sessions (1-minute load median 9.4, peak 24.9; `m4pro-load-3epoch.log`) |
| Apple M1 Pro | Clang 16.0.0 | arm_shani | 2.295 | remote |
| Intel i5-12500 | GCC 15.2.0 | x86_shani | 2.736 | remote |
| Ryzen 9 9950X (Windows 11) | Clang 23.1.2 (clang-cl) | x86_shani | 1.828 | run by Julian |
| Intel i7-7700 | GCC 15.2.0 | sse4/avx2 (no SHA-NI) | 3.484 | Hetzner, `bitcoinSetup.sh` |
| Ryzen 5 3600 | GCC 15.2.0 | x86_shani | 3.551 | Hetzner, `bitcoinSetup.sh` |

`joint-calibration.json` and `fit.log` come from `fit_calibrations.py` over all six artifacts in the order above,
with `--source-root` on the gsr repository. They use the four-term MUL basis and the two-significant-figure rounding of BIP 440
Appendix A (refit 2026-09-30), which superseded an earlier three-term MUL fit. Every candidate is implemented in `varops.h` at gsr `ea7a71e20e` (PREPARE's rate per 64-bit word).

## Pool change

With the 64 MiB pool the 9950X timed operands already evicted to DRAM. In ns per call at 64 MiB, 8 MiB and 1 MiB
pools (focused `--div-only` / `--mul-only` runs):

| Fixture | 64 MiB | 8 MiB | 1 MiB |
|---|---|---|---|
| MUL 1×1 | 38.3 | 14.2 | 6.7 |
| DIV 1/1 | 36.5 | 5.8 | 5.2 |
| DIV 16/16 | 83.8 | 33.8 | 29.0 |
| MUL 64×64 and larger | same | same | same |

The M4, M1 and i5 barely changed (small MUL and DIV within about ±10%). The joint envelope compared with
`../e5-20260930-b077c36f44` (64 MiB pool):

| Coefficient | 64 MiB pool | 8 MB pool |
|---|---|---|
| MULCORE flat | 2551 | 496 |
| DIVCORE flat | 1109 | 0 |
| ARITH flat | 670 | 112 |
| READ flat | 151 | 74 |
| PREP flat | 366 | 176 |
| SELECT | 3455 + 643 k | 2269 + 180 k |

## Remaining issues

Against the slowest machine at each fixture:
- Small MUL (charge under 10k varops): median over-charge 1.11×, at most 1.27×. Small DIV: median 1.38×.
- DIV shortcut paths are over-charged. A dividend shorter than the divisor costs about 105 varops (just a
  comparison) but is charged one step plus v divisor cells, for example 33,692 at 1023/1024 limbs (320×).
  1/1 is charged 2.3× its cost.
- 20 fixtures are under-charged by more than the 0.9 margin:
  - 19 are equal-length DIV MOD cases (64/64, 128/128, 1024/1024 limbs) on the i5 and i7, at 1.12–1.14×.
    MOD's remainder shift back grows with the divisor length, and the model has no term for it at two steps.
  - 1 is MULCORE/262144/32/random on the loaded M4, at 1.29×. Recheck it on a quiet machine.
