# Varops primitives

Frozen calibration model **producer-normalize-v1**; coefficients are not finalized. It is the only implemented schedule; no build option is needed. **NEW** means a separately modeled component absent from BIP440 before fixed execution costs. Existing classes remain marked **No** when renamed or refined; this does not imply their old coefficients remain sufficient.

Degree is the maximum polynomial degree in the specified size/count features: 0 constant, 1 linear (possibly with an intercept), 2 quadratic. `W(n)` rounds bytes up to a multiple of eight; `u,v` are limb counts.

| Primitive | Short description | Degree | NEW |
| --- | --- | --- | --- |
| `BASE` | Common evaluator overhead per executed opcode; anchored by BASE-only groups (NOP, upgradable NOPs, CODESEPARATOR, ELSE, inactive flat/nested IF/ENDIF), with skipped NOPs as an unfitted diagnostic. | 0 | Yes |
| `PREPARE(n)` | Numeric operand preparation: fixed setup plus traversal of `W(n)`. | 1 | Yes |
| `NORMALIZE` | Numeric result materialization only; allocation, insertion and release belong to WRITE. Flat: conversion hands the buffer over in place. | 0 | Yes |
| `WRITE(n)` | Complete value creation/insertion/release lifetime; replaces COPY and RELEASE. | 1 | No |
| `READ(n)` | Scan/compare bytes; COMPARING, COMPARINGZERO and LENGTHCONV. | 1 | No |
| `ARITH(n)` | Word pass with a carry or borrow chain (add/subtract): each word waits for the previous one; ARITH. | 1 | No |
| `BIT(n)` | Word pass without a chain (bitwise logic, shifts): words are independent and vectorize; OTHER. Byte reversal is measured but priced by the covenant opcode BIP. | 1 | No |
| `MOVE(k)` | Move `k` stack entries without copying payloads; ROLL. | 1 | No |
| `MUL(u,v)` (raw data label `MULCORE`) | Complete multiplication: `a + b*u + c*v + d*u*v` over `u >= v` limbs (one schoolbook row per shorter-operand limb), including internal scratch storage; the product is produced separately. | 2 | Yes |
| `DIV(s,v)` | Complete prepared DIV/MOD, including normalization and scratch storage: constant + coefficient × `s` (per-row quotient estimate and correction) + coefficient × `s*v`. | 2 | Yes |
| `SHA256(n)` | SHA256 initialization, compression of each 64-byte block and finalization: `a + b*H(n)`; refines HASH. | 1 | No |
| `RIPEMD160(n)` | RIPEMD160 initialization, compression of each 64-byte block and finalization: `a + b*H(n)`; refines HASH. | 1 | No |
| `SHA1(n)` | SHA1 initialization, compression of each 64-byte block and finalization: `a + b*H(n)`; refines HASH. | 1 | No |
| `SIG` | Signature verification and fixed transaction-message preparation; SIGCHECK. The `SIG/n` sweep verifies messages up to 4 MB (OP_CHECKSIGFROMSTACK) to check that the challenge hash stays within `SHA256(64+n)`. | 0 | No |
| `TWEAK` | Public-key tweak operation. | 0 | Yes |
| `OP_TX_SELECT(k)` (raw data label `SELECT`) | OP_TX selector setup plus `k` charged units: planned values and records scanned for aggregate fields. Fitted on collated output for each unit kind: witness items, weight scan, amount scan, outputs and input fields. | 1 | Yes |
| `MACRO_UNROLL` (raw data label `UNROLL`) | Macro unrolling per charged unit (substituted instruction or visited reference), measured against unrolled bytes per unit on inactive NOP, push and reference-chain bodies. Currently charged at BASE per unit, with no byte term. | 1 | Yes |

## Collection protocol

Run `python3 dev/varops/primitive-calibration/run_calibration.py` from the same clean, frozen commit on every machine (`python` on Windows). Defaults: 7 primitive epochs, 10 ms per batch, 100 ms for storage lifetimes; median, margin 1. Reference measurement retains 5 rounds, and normalization uses 100% of the local pre-v2 reference. Build uses up to 8 jobs; measurements remain serial. Each epoch is one pass over every fixture, so a fixture's epochs lie a whole pass apart and a slow spell of the machine affects one epoch of many fixtures rather than every epoch of a few; the first pass chooses each fixture's repetitions. Progress is printed between fixtures approximately every 5 seconds.

Keep the existing fixture set, feature basis and 100× underprediction objective unchanged across machines. Preserve raw epochs. Repeat suspicious fixtures in separate processes before changing prices; isolated growth repeats do not replace mixed allocator-history coverage. These collection settings are a practical accuracy/runtime compromise, not a confidence guarantee. Combine only matching model/source/fixture sets; SIG remains fixed at 500,000. Validate any resulting schedule with `bench_varops` before adopting it.

## Composition rules

- WRITE includes eventual release. Charge initial witness values once after immediate-success prescanning; do not charge moves, drops or in-place shrinkage as new producers.
- Numeric results pay `WRITE(W(n)) + NORMALIZE`. MUL prepays full result/scratch production and only NORMALIZE at final output. DIV includes its internal temporary storage. See the [storage ledger](storage-accounting.md).
- Remove standalone `ALLOC`: required allocation and growth belong to the relevant producer or operation, including scratch storage. Never omit or charge them twice.
- Scalar results use the same numeric-result formula. Ordinary LOCK checker overhead remains in BASE; operands still pay preparation/scanning costs.
- Retain PREPARE and NORMALIZE constants rather than inflating BASE for arithmetic. A fitted slope may be zero.
- `H(n) = ((n + 72) / 64) * 64` is the padded message length processed by SHA1, SHA256 and RIPEMD160 (64-byte blocks, at least 9 bytes of padding and length). Each fixture size is fitted at its hash block span.
- Hashes compose: HASH256 uses `SHA256(n) + SHA256(32)`; HASH160 uses `SHA256(n) + RIPEMD160(32)`. Add BASE and any result work not already covered.
- Unrolled instructions in inactive branches pay nothing when reached, so MACRO_UNROLL must cover their substitution, copying and skipping. `bench_varops` evaluates such scripts repeatedly against one shared budget, as inputs of one transaction, because one script unrolls at most 4 MB.
- Final success checking pays PREPARE + READ once per script, with no separate FINAL primitive. Retain complete final-check timings as a composition diagnostic. Fixed-point SCALE is not a primitive.

Measure complete defined operations, including their storage lifetimes, then check compositions through `bench_varops`. Capacity and cache state are measurement conditions, never charge inputs. Prefer simple scaling and conservative coverage over exact fits to every timing fluctuation.

[Methodology](costing-methodology.md) · Previous source audit and measurements: `results/first-five-audit-20260922/` (kept outside the repository)

## Frozen collection and fitting

Run `run_calibration.py` without arguments. It builds a Release tree, collects only these 17 families (SIG stays a 500,000-unit policy price), preserves held-out lifetime checks and exports opcode compositions. Removed primitives remain available only through historical/focused probes.

From the root of a `gsr` checkout, run `python3 dev/varops/primitive-calibration/run_calibration.py` (`python` on Windows). No arguments or environment variables are needed. In the research workspace, the result is `data/varopsData/primitive-calibration/producer-normalize-v1/varop-calibration.json`; in other checkout layouts it is `dev/varops/primitive-calibration/varop-calibration.json`. The runner prints the destination at startup and completion. Preserve each machine's result under a distinct filename before collecting another run; a successful run replaces that machine's previous output. Intermediate measurements stay under `calibration-intermediate/`.

Use the existing group/size-decade balanced squared-log fit with 100× underprediction penalty, predetermined nonnegative affine terms (BASE/TWEAK constant; DIV fixed + rows + cells). Normalize to `40B / (0.9 · T_pre)`: a full budget of fitted work takes 0.9× each machine's pre-v2 reference, a margin for composition effects and machine variation the fixtures do not capture, and the worst case of a full block's budget in complete scripts must stay below 1.0× the reference on every machine. `fit_calibrations.py` derives this rate from each artifact's recorded `T_pre`, so artifacts collected with another normalization are rescaled rather than edited, and it rejects runs whose epoch noise exceeds 1% unless `--allow-failed-conditions` is given for an exploratory fit. Epoch noise is the median over fixtures of the median absolute deviation of a fixture's epochs from their median, relative to that median; the runner repeats such a run up to three times. The load average is recorded for information only: an idle macOS desktop already reports 1.5–2. Existing implementation coefficients are not automatically repriced. Keep fitted decimals; rounding is a later schedule-adoption step. Do not combine different model IDs, fixture sets or source revisions.

### Combination rule

Keep existing per-machine fits frozen when adding machines. The pricing basis is the envelope of the normalized machine fits: the cheapest curve of each primitive's form that stays at or above every machine's fitted curve at every size the charge applies to ([`fit_calibrations.py`](../fit_calibrations.py) solves it as a linear program). It is not the coefficientwise maximum, which over-charges where machines differ in flat and rate. Adding a machine can move the envelope, but never below any admitted machine's fitted curve. Per-machine underprediction can remain; whole-script checks are still decisive. SIG remains a fixed policy exception.

### Schedule rounding

After combining the independent normalized machine fits into their envelope, round every coefficient, flat or rate, upward to **two significant figures, and at least to a whole varop**. From 10 up this adds at most 10%. Zero and exact multiples stay unchanged. SIG stays **500,000**. Preserve raw decimal coefficients and measurements, report the rounding uplift, and do not refit after rounding. For example, MUL's `327.8 + 9.38 u + 106.8 v + 28.29 u v` becomes `330 + 10 u + 110 v + 29 u v`, and TWEAK's `168,387` becomes `170,000`. Small slopes can increase substantially (e.g. PREPARE's `0.0069 × W(n)/8` becomes `1 × W(n)/8`); show this explicitly. Validate the resulting opcode compositions with `bench_varops` on every machine before finalization. Upward rounding is not an upper-bound guarantee.

The implementation and BIP draft price every primitive from the six-machine (M4 Pro, M1 Pro, i5-12500, Ryzen 9 9950X, i7-7700, Ryzen 5 3600), three-epoch envelope of [`e3-20260930-ea7a71e20e`](../e3-20260930-ea7a71e20e/), measured at commit `ea7a71e20e` with the 8 MB fixture pool, with this rule; PREPARE's rate is priced per 64-bit word. SHA256 is fitted on Core SHA256 only. BIT is the exception: a `bench_varops` screening at `c46da31e5a` found OP_BYTEREV of values up to 33 bytes above 1.0× the reference on the i5-12500 and i7-7700, because its pop and push were outside the BIT fixtures. The `BIT/byterev` fixtures now time OP_BYTEREV's complete work after dispatch, and BIT is priced `190 + 2 × W(n)` from [`e3-20260930-13e9a89dc7/joint-calibration.json`](../e3-20260930-13e9a89dc7/joint-calibration.json) (gsr `962b6a2402`). Historical raw artifacts remain unchanged. WRITE/NORMALIZE is the only implemented storage schedule. OP_TX_SELECT is priced from the same envelope. The macro unrolling charge composes BASE and WRITE; the UNROLL measurements check it rather than price it.

Sampling includes empty/tiny values, word and power-of-two boundaries and known allocator-transition neighbours. ARITH separately measures carry-chain and full-borrow-chain inputs with equal/one-word second operands. Fixture preparation is outside kernel timers. Model-identification measurements are not full-workload confirmation.
