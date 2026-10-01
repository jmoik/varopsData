# 3-epoch runs on six machines · 2026-09-30

Commit `13e9a89dc7` (branch `gsr`), model `producer-normalize-v1`, 8 MB fixture pool.
Since the previous dataset (`ea7a71e20e`), the implementation charges the candidates of that dataset (gsr `c46da31e5a`),
and the `BIT/byterev/n` fixtures time OP_BYTEREV's work after dispatch (pop, reverse, push) instead of the reversal
kernel alone. `bench_varops` had found short OP_BYTEREV values above the 1.0 limit on the i5 (1.19x) and i7 (1.12x)
at `BASE + BIT(W(n))` with BIT's flat at 90; with these fixtures in the BIT fit, its flat is 183 before rounding.

    python3 dev/varops/primitive-calibration/run_calibration.py --reference-epochs 3 --epochs 3 \
        --sample-ms 5 --copy-sample-ms 50 --output varop-calibration-<machine>-3epoch.json

| Machine | Compiler | SHA256 | Reference (s) | Previous (s) | Notes |
|---|---|---|---|---|---|
| Apple M4 Pro | Clang 17.0.0 | arm_shani | 1.586 | 1.715 | local; 1-minute load median 3.1, peak 5.0 (`m4pro-load-3epoch.log`) |
| Apple M1 Pro | Clang 16.0.0 | arm_shani | 2.264 | 2.256 | remote |
| Intel i5-12500 | GCC 15.2.0 | x86_shani | 2.735 | 2.736 | remote |
| Ryzen 9 9950X (Windows 11) | Clang 23.1.2 (clang-cl) | x86_shani | 1.605 | 1.591 | run by Julian |
| Intel i7-7700 | GCC 15.2.0 | sse4/avx2 (no SHA-NI) | 3.482 | 3.483 | Hetzner, `bitcoinSetup.sh` |
| Ryzen 5 3600 | GCC 15.2.0 | x86_shani | 3.201 | 3.199 | Hetzner, `bitcoinSetup.sh` |

`joint-calibration.json` and `fit.log` come from `fit_calibrations.py` over all six artifacts in the order above,
with `--source-root` on the gsr repository. Runs are admitted by their epoch noise, at most 1.5%; these measure
0.13–0.77% (M4 Pro 0.77%, M1 Pro 0.29%). Their median loads of 3.09 and 2.37, which an earlier fitter rejected,
are no longer a condition. The fit prices a full budget at 0.9× each machine's pre-v2 reference; BIT's
implemented `190 + 2 × W(n)` is its candidate.
