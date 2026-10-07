# Full runs on seven machines · 2026-10-07

Commit `aceaec55cb` (branch `gsr`), model `read-write-arith-v1`, varopsData benchmarks at `af25746` (the M4 Pro
and the 9950X ran `53d2d0b`, whose benchmark sources are identical), runner defaults:

    python3 <varopsData>/src/run_calibration.py --output varop-calibration-<machine>-full.json

That is 5 reference epochs, 7 primitive epochs, 10 ms samples and 100 ms for storage lifetimes. The i5-12500's
first attempt had 1.52% epoch noise, above the 1.5% limit; the runner repeated it and the second passed.

| Machine | OS | Compiler | SHA256 | Reference (s) | Epoch noise |
|---|---|---|---|---|---|
| Apple M1 Pro | macOS | Clang 16.0.0 | arm_shani | 2.255 | 0.21% |
| Intel i5-12500 | Ubuntu 26.04 | GCC 15.2.0 | x86_shani | 1.865 | 1.30% |
| AMD Ryzen 9 9950X | Windows 11 | Clang 23.1.2 | x86_shani | 1.635 | 0.40% |
| Intel i7-7700 | Ubuntu 26.04 | GCC 15.2.0 | sse4 | 3.482 | 0.27% |
| AMD Ryzen 5 3600 | Ubuntu 26.04 | GCC 15.2.0 | x86_shani | 2.778 | 0.57% |
| Apple M4 Pro | macOS | Clang 17.0.0 | arm_shani | 1.565 | 1.20% |
| AMD Ryzen 7 7700 | Ubuntu 26.04 | GCC 15.2.0 | x86_shani | 1.957 | 0.47% |

The Windows artifact records its benchmark sources with CRLF line endings; the fitter takes them as the same
sources.

`joint-calibration.json` and `fit.log` come from `src/fit_calibrations.py` over the seven artifacts in the order
above, with `--source-root` on the gsr repository. The rounded candidates (flats up to multiples of 50, rates up
to whole varops) are implemented at gsr `1be4628bae`:

    BASE    300
    READ    350 + 2 W(n)
    WRITE   750 + 7 W(n)
    ARITH   200 + 3 W(n)
    MOVE    200 + 23 k
    MUL     650 + 1 W(n) + 18 W(m) + 1 W(n) W(m)
    DIV     1150 + 61 Q(n, m) + 10 W(m) + 1 Q(n, m) W(m),  Q(n, m) = MAX(0, W(n) - W(m))
    HASH    50 + 40 H(n)
    SIG     500000
    SELECT  1350 + 615 k

New in this dataset: MUL and DIV are priced per byte of `W` (their products per unit of `W(n) W(m)`), DIV in terms
of `Q(n, m)` with no constants besides its coefficients, OP_TX SELECT net of its result's WRITE and fitted per
transaction shape (one-byte witness items deserialized into a churned heap included), and HASH priced as its
rounded envelope over all three hash functions.
