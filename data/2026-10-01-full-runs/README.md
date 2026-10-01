# Full runs on five machines · 2026-10-01

Commit `e60ac7e070` (branch `gsr`), model `producer-normalize-v1`, 8 MB fixture pool, runner defaults:

    python3 dev/varops/primitive-calibration/run_calibration.py --output varop-calibration-<machine>-full.json

That is 5 reference epochs, 7 primitive epochs, 10 ms samples and 100 ms for storage lifetimes. The i7-7700 and
Ryzen 5 3600 ran it through `bitcoinSetup.sh` with the same settings.

| Machine | Compiler | SHA256 | Reference (s) | Epoch noise | Notes |
|---|---|---|---|---|---|
| Apple M1 Pro | Clang 16.0.0 | arm_shani | 2.257 | 0.25% | remote; second run (see below) |
| Intel i5-12500 | GCC 15.2.0 | x86_shani | 2.734 | 0.32% | remote |
| Ryzen 9 9950X (Windows 11) | Clang 23.1.2 (clang-cl) | x86_shani | 1.596 | 0.45% | run by Julian |
| Intel i7-7700 | GCC 15.2.0 | sse4/avx2 (no SHA-NI) | 3.481 | 0.30% | Hetzner, `bitcoinSetup.sh` |
| Ryzen 5 3600 | GCC 15.2.0 | x86_shani | 3.200 | 0.30% | Hetzner, `bitcoinSetup.sh` |

The Apple M4 Pro is not included. Its overnight run repeated all three attempts because the runner's limit was then 1%
(1.13%, 1.10%, 1.12%, at a median load of 4). A second run this morning was stopped during its second attempt
because the Mac was needed. With seven epochs the noise measure reads about twice what it reads with three, so the
limit is now 1.5% in the runner and the fitter.

The M1's first run crashed with `nonpositive fit timing`. A slow single pilot epoch left
`ARITH/add/375000/one-word/borrow-chain`, an O(1) addition on 3 MB operands, at one repetition per epoch.
That is about 13 ns, below the M1's 41.7 ns clock tick, so four of its seven epochs read 0. The second run planned it
normally. The benchmark will raise a fixture's rounds until every recorded epoch spans 50 clock ticks.

`joint-calibration.json` and `fit.log` come from `src/fit_calibrations.py` over the five artifacts in the order above,
with `--source-root` on the gsr repository. A full budget of fitted work is priced at 0.9× each machine's pre-v2
reference.
