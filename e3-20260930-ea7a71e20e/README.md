# 3-epoch runs on six machines · 2026-09-30

Commit `ea7a71e20e7f6a4d0c1851fc3e5b45f58e062728` (branch `gsr`), model `producer-normalize-v1`, 8 MB fixture pool.
The measured code is the same as in the previous dataset (`60bc3ca40a`); since then only the charges in `varops.h`
and the budget accounting in `bench_varops.cpp` changed, which install the previous candidates.

    python3 dev/varops/primitive-calibration/run_calibration.py --reference-epochs 3 --epochs 3 \
        --sample-ms 5 --copy-sample-ms 50 --output varop-calibration-<machine>-3epoch.json

| Machine | Compiler | SHA256 | Reference (s) | Previous (s) | Notes |
|---|---|---|---|---|---|
| Apple M4 Pro | Clang 17.0.0 | arm_shani | 1.715 | 1.678 | local; 1-minute load median 3.3, peak 7.4 (`m4pro-load-3epoch.log`) |
| Apple M1 Pro | Clang 16.0.0 | arm_shani | 2.256 | 2.295 | remote |
| Intel i5-12500 | GCC 15.2.0 | x86_shani | 2.736 | 2.736 | remote |
| Ryzen 9 9950X (Windows 11) | Clang 23.1.2 (clang-cl) | x86_shani | 1.591 | 1.828 | run by Julian |
| Intel i7-7700 | GCC 15.2.0 | sse4/avx2 (no SHA-NI) | 3.483 | 3.484 | Hetzner, `bitcoinSetup.sh` |
| Ryzen 5 3600 | GCC 15.2.0 | x86_shani | 3.199 | 3.551 | Hetzner, `bitcoinSetup.sh` |

The 9950X and Ryzen 5 references are back at their earlier values (1.59–1.63 s and 3.20 s); the previous dataset's
slower references lowered those machines' normalized costs.

`joint-calibration.json` and `fit.log` come from `fit_calibrations.py` over all six artifacts in the order above,
with `--source-root` on the gsr repository.
