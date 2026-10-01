# Full runs on six machines, one budget deduction per opcode · 2026-10-01

Commit `fe9a342227` (branch `gsr`), model `producer-normalize-v1`, 8 MB operand pool, runner defaults:

    python3 dev/varops/primitive-calibration/run_calibration.py --output varop-calibration-<machine>-full.json

That is 5 reference epochs, 7 primitive epochs, 10 ms samples and 100 ms for storage lifetimes. The i7-7700 and
Ryzen 5 3600 ran it through `bitcoinSetup.sh` with the same settings. Every run passed on its first attempt.

| Machine | Compiler | SHA256 | Reference (s) | Epoch noise | Notes |
|---|---|---|---|---|---|
| Apple M1 Pro | Clang 16.0.0 | arm_shani | 2.258 | 0.26% | remote |
| Intel i5-12500 | GCC 15.2.0 | x86_shani | 2.734 | 0.30% | remote |
| Ryzen 9 9950X (Windows 11) | Clang 23.1.2 | x86_shani | 1.586 | 0.39% | run by Julian |
| Intel i7-7700 | GCC 15.2.0 | sse4 | 3.481 | 0.36% | Hetzner, `bitcoinSetup.sh` |
| Ryzen 5 3600 | GCC 15.2.0 | x86_shani | 3.202 | 0.30% | Hetzner, `bitcoinSetup.sh` |
| Apple M4 Pro | Clang 17.0.0 | arm_shani | 1.588 | 0.98% | local; load 1.5–5.6 from other sessions |

`fe9a342227` deducts each opcode's charges, BASE included, from the shared budget once. Before it, every opcode
with its own charge made a second atomic deduction after BASE's. F's NOP scripts timed one deduction and the
primitive measurements none, so complete opcodes cost more than BASE plus their primitives on the i5, i7 and 9950X:
OP_BYTEREV of 0-1 bytes reached 1.04x on the 9950X. The primitive measurements are unchanged; on the five machines
of [`2026-10-01-full-runs`](../2026-10-01-full-runs/) every fit except MOVE on the i5-12500 (21 -> 36 varops per
entry) moved within run-to-run noise. The M4 Pro is new in this dataset and sets the BIT, SELECT, PRODUCE and
MOVE flats.

`joint-calibration.json` and `fit.log` come from `src/fit_calibrations.py` over the six artifacts in the order above,
with `--source-root` on the gsr repository. A full budget of fitted work is priced at 0.9x each machine's Tapleaf 0xC0
reference. The rounded candidate is implemented at gsr `7d0c293b64`.

```
Primitive  Envelope (varops, unrounded)                   Rounded candidate                      Maximum coefficients (unrounded)
F          305.528                                          350                                    305.528
PREP       177.439 + 0.000857024 × W(n)                     200 + W(n)                             177.439 + 0.000857024 × W(n)
PRODUCE    760.26 + 7.14225 × n                             800 + 8 × W(n)                         760.26 + 7.14225 × n
NORMALIZE  189.093                                          200                                    189.093
READ       81.7392 + 1.02339 × W(n)                         90 + 2 × W(n)                          81.7392 + 1.02339 × W(n)
ARITH      124.082 + 2.68554 × W(n)                         150 + 3 × W(n)                         124.082 + 2.68554 × W(n)
BIT        183.087 + 1.80421 × W(n)                         200 + 2 × W(n)                         183.087 + 1.80421 × W(n)
MOVE       178.705 + 36.0097 × k                            200 + 37 × k                           178.705 + 36.0097 × k
DIVCORE    500.023 × s + 32.5576 × s × v                    510 × s + 33 × s × v                   212.076 + 500.831 × s + 32.5576 × s × v
H256       256.703 + 37.2698 × H(n)                         300 + 38 × H(n)                        445.978 + 37.2698 × H(n)
H160       50.7168 + 39.3379 × H(n)                         60 + 40 × H(n)                         238.838 + 39.4259 × H(n)
H1         184.146 + 23.6595 × H(n)                         200 + 24 × H(n)                        285.079 + 23.9433 × H(n)
SIG        565437                                           500000                                 565437
TWEAK      168023                                           170000                                 168023
SELECT     2381.66 + 260.3 × k                              2400 + 270 × k                         2381.66 + 260.3 × k
MULCORE    360.945 + 5.22967 × u + 118.164 × v + 28.2034 × u × v 400 + 6 × u + 120 × v + 29 × u × v     360.945 + 5.22967 × u + 121.724 × v + 28.2034 × u × v
```
