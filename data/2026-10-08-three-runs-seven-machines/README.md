# Three full runs on seven machines · 2026-10-07 to 2026-10-08

Each of the seven machines ran the full calibration three times, model `read-write-arith-v1`, runner defaults:

    python3 <varopsData>/src/run_calibration.py --output varop-calibration-<machine>-full.json

That is 5 reference epochs, 7 primitive epochs, 10 ms samples and 100 ms for storage lifetimes.

| Run | gsr commit | varopsData benchmarks |
|---|---|---|
| `run1/` | `aceaec55cb` | `af25746` (M4 Pro and 9950X: `53d2d0b`) |
| `run2/` | `1be4628bae` | `f1b546b` (M4 Pro and 9950X: `154903d`) |
| `run3/` | `1be4628bae` | `f1b546b` (M4 Pro and 9950X: `154903d`) |

Between the runs only the prices changed in gsr, and only `bench/bench_varops.cpp` changed among the benchmark
sources: its own copy of the MUL and DIV charges, which sizes and checks complete-script cases and does not enter
any calibration measurement. The fit accepts that file's versions with `--reviewed-bench-difference` and records
them (`bench_check.reviewed_differences`). Macro unrolling labels record the charge of the build that ran them;
the fitter compares fixtures without it, and the report recomputes charges from the current prices.

| Machine | OS | Compiler | SHA256 | Reference (s), runs 1–3 | Epoch noise, runs 1–3 |
|---|---|---|---|---|---|
| Apple M1 Pro | macOS | Clang 16.0.0 | arm_shani | 2.255 / 2.256 / 2.255 | 0.21 / 0.22 / 0.25% |
| Intel i5-12500 | Ubuntu 26.04 | GCC 15.2.0 | x86_shani | 1.865 / 1.892 / 1.792 | 1.30 / 1.28 / 1.35% |
| AMD Ryzen 9 9950X | Windows 11 | Clang 23.1.2 | x86_shani | 1.635 / 1.639 / 1.641 | 0.40 / 0.48 / 0.47% |
| Intel i7-7700 | Ubuntu 26.04 | GCC 15.2.0 | sse4 | 3.482 / 3.481 / 3.481 | 0.27 / 0.29 / 0.28% |
| AMD Ryzen 5 3600 | Ubuntu 26.04 | GCC 15.2.0 | x86_shani | 2.778 / 2.751 / 2.750 | 0.57 / 0.51 / 0.55% |
| Apple M4 Pro | macOS | Clang 17.0.0 | arm_shani | 1.565 / 1.589 / 1.577 | 1.20 / 1.25 / 1.15% |
| AMD Ryzen 7 7700 | Ubuntu 26.04 | GCC 15.2.0 | x86_shani | 1.957 / 1.957 / 1.956 | 0.47 / 0.42 / 0.42% |

The i5-12500 exceeded the 1.5% epoch-noise limit on its first attempt in runs 1 and 2; the runner repeated it and
the second attempt passed. The Windows artifacts record their benchmark sources with CRLF line endings; the fitter
takes them as the same sources.

`joint-calibration.json` and `fit.log` come from `src/fit_calibrations.py` over all 21 artifacts, run by run in the
machine order above:

    python3 src/fit_calibrations.py data/2026-10-08-three-runs-seven-machines/run{1,2,3}/varop-calibration-*.json \
        --source-root <gsr checkout> --reviewed-bench-difference bench/bench_varops.cpp

Every run is a separate fit, so the envelope covers each machine's three runs. The rounded candidates (flats up to
multiples of 50, rates up to whole varops) are implemented at gsr `43e28c2a15`:

    BASE    300
    READ    350 + 2 W(n)
    WRITE   800 + 7 W(n)
    ARITH   200 + 3 W(n)
    MOVE    200 + 23 k
    MUL     700 + 1 W(n) + 18 W(m) + 1 W(n) W(m)
    DIV     1150 + 60 Q(n, m) + 11 W(m) + 1 Q(n, m) W(m),  Q(n, m) = MAX(0, W(n) - W(m))
    HASH    50 + 40 H(n)
    SIG     500000
    SELECT  1550 + 620 k

Against the first run alone (the previous dataset, `2026-10-07-full-runs-seven-machines`), four prices changed.
WRITE's flat follows the M4 Pro's second run (753). MUL's flat follows the i5-12500's third run (666), whose fit
traded W(m) rate for flat. DIV's W(m) rate follows the i5-12500's third run (10.3), and its Q rate falls from 61 to
60, since the envelope over all runs lies at or above every run's curve on DIV's domain with less Q rate. SELECT's
flat follows the M4 Pro's third run (1,522) and its rate the Ryzen 5 3600's second run (619).
