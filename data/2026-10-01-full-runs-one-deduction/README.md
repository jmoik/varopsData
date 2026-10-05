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
reference. The rounded candidates of the measured families were implemented at gsr `7d0c293b64`.

This model measured BIP 440's READ, WRITE and ARITH in parts: PREP and READ, PRODUCE and NORMALIZE, ARITH and BIT.
Like every model, it measured SHA256, RIPEMD160 and SHA1 (H256, H160, H1) separately. Since gsr `104c1c0dc3` the
implementation charges the merged READ, WRITE and ARITH, and since `fae30159ae` one HASH for the three hash
functions, with READ's flat rounded up from 290 to 300. The fit composes them from the parts
(see [Combining machines](../../METHODOLOGY.md#combining-machines)): READ and WRITE add their parts' fits and
rounded prices, ARITH and HASH take the larger flat and rate of theirs, and a composed price is rounded again.
Every opcode keeps its composition, so none is charged less than before, and each merged price lies above the
envelope of its parts; for HASH, that envelope covers every machine and every hash function.

gsr `31112e7296` changes two compositions: OP_BYTEREV pays WRITE for its result, and OP_EQUALVERIFY, which
produces no value, no longer pays WRITE(8). Only the second lowers a charge. Its complete scripts were re-screened
on the M4 Pro at that commit, with small equal values added (`worst-case/m4-equalverify-screen-31112e7296.csv`,
`bench_varops --case-filter /OP_EQUALVERIFY/ --sample-budget-percent 2 --epochs 1`): the worst, 8- and 32-byte
values, take 0.60x the reference.

gsr `0e999bbe39` charges WRITE only for values an opcode produces: OP_NUMEQUALVERIFY and OP_CHECKSIGVERIFY no
longer pay WRITE(8) for a result they consume, and OP_CHECKLOCKTIMEVERIFY, OP_CHECKSEQUENCEVERIFY and OP_IFDUP no
longer pay WRITE for the operand they leave on the stack. Each opcode's complete scripts were re-screened on the
M4 Pro at that commit, with small equal values added for OP_NUMEQUALVERIFY
(`worst-case/m4-no-write-screens-0e999bbe39/`, `bench_varops --case-filter /<opcode>/ --sample-budget-percent 2
--epochs 1`). The worst take 0.69x the reference (OP_CHECKSIGVERIFY, a signature check), 0.56x
(OP_NUMEQUALVERIFY, 32-byte values), 0.31x (OP_CHECKLOCKTIMEVERIFY and OP_CHECKSEQUENCEVERIFY, 521 bytes) and
0.20x (OP_IFDUP). Other sessions loaded the machine: the reference took 2.37 s instead of 1.59 s, and the
Schnorr baseline was slower by the same factor.

gsr `26ed8d63da` charges OP_TX READ for each scope operand, which it takes as a number; this only raises a
charge.

```
Primitive  Envelope (varops, unrounded)                   Rounded candidate                      Maximum coefficients (unrounded)
F          305.528                                          350                                    305.528
READ       252.794 + 1.02356 × W(n)                         300 + 3 × W(n)                         252.794 + 1.02356 × W(n)
WRITE      892.546 + 7.14225 × n                            1000 + 8 × W(n)                        892.546 + 7.14225 × n
ARITH      183.087 + 2.68554 × W(n)                         200 + 3 × W(n)                         183.087 + 2.68554 × W(n)
MOVE       178.705 + 36.0097 × k                            200 + 37 × k                           178.705 + 36.0097 × k
MULCORE    360.945 + 5.22967 × u + 118.164 × v + 28.2034 × u × v 400 + 6 × u + 120 × v + 29 × u × v     360.945 + 5.22967 × u + 121.724 × v + 28.2034 × u × v
DIVCORE    500.023 × s + 32.5576 × s × v                    510 × s + 33 × s × v                   212.076 + 500.831 × s + 32.5576 × s × v
HASH       133.549 + 39.1941 × H(n)                         300 + 40 × H(n)                        445.978 + 39.4259 × H(n)
SIG        565437                                           500000                                 565437
TWEAK      168023                                           170000                                 168023
SELECT     2381.66 + 260.3 × k                              2400 + 270 × k                         2381.66 + 260.3 × k
Measured in parts:
  PREP     177.439 + 0.000857024 × W(n)                     200 + W(n)
  READ     81.7392 + 1.02339 × W(n)                         90 + 2 × W(n)
  PRODUCE  760.26 + 7.14225 × n                             800 + 8 × W(n)
  NORMALIZE 189.093                                          200
  ARITH    124.082 + 2.68554 × W(n)                         150 + 3 × W(n)
  BIT      183.087 + 1.80421 × W(n)                         200 + 2 × W(n)
  H256     256.703 + 37.2698 × H(n)                         300 + 38 × H(n)
  H160     50.7168 + 39.3379 × H(n)                         60 + 40 × H(n)
  H1       184.146 + 23.6595 × H(n)                         200 + 24 × H(n)
```
