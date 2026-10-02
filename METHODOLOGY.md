# Varops calibration: methodology

The complete method behind the BIP 440 cost primitives. [BIP 440's Derivation of Costs](https://github.com/jmoik/bips/blob/gsr-full/bip-0440.mediawiki#derivation-of-costs) summarizes it. The [README](README.md) gives the overview and the commands to reproduce a calibration.

## Primitives

An opcode's charge is `BASE` plus the primitives its formula names (BIP 440 Cost Primitives and Opcode Costs, BIP 441 for the restored opcodes). Each primitive models one kind of work and has a formula in the sizes of its operands, declared before measuring; calibration only sets the coefficients. The prices themselves are in BIP 440, `src/script/varops.h` and the [report](report/joint-calibration.html).

Notation: `n` is a size in bytes, `W(n) = 8 ceil(n / 8)` its span in 64-bit words, `H(n) = 64 floor((n + 72) / 64)` the bytes a 64-byte-block hash processes, `k` a count of stack entries or charged units, `u ≥ v` the 64-bit limbs of the longer and shorter operand, and `s` the quotient rows of a division.

| Primitive | Formula | What it models |
| --- | --- | --- |
| `BASE` | `a` | The work every instruction does: decoding, dispatch, metering and stack-limit checks. |
| `PREPARE(n)` | `a + b W(n)` | Reading one numeric operand into 64-bit words; charged per operand. |
| `WRITE(n)` | `a + b W(n)` | Creating one stack value of `n` bytes: allocating, filling and inserting it, and eventually releasing it. |
| `NORMALIZE` | `a` | Turning a numeric result back into minimal bytes. |
| `READ(n)` | `a + b W(n)` | Scanning bytes without creating a value: comparisons, zero tests and length conversion. |
| `ARITH(n)` | `a + b W(n)` | One pass over the operands' words with a carry between words: addition and subtraction. |
| `BIT(n)` | `a + b W(n)` | One pass over the operands' words without carries: bitwise logic, shifts and OP_BYTEREV's byte reversal. |
| `MOVE(k)` | `a + b k` | Reordering `k` stack entries without copying their contents, as OP_ROLL does. |
| `MUL(u, v)` | `a + b u + c v + d u v` | Schoolbook multiplication, one row per limb of the shorter operand, including scratch space. |
| `DIV(s, v)` | `a + b s + c s v` | Long division or remainder: `s` quotient rows, each working through the `v` limbs of the divisor, including normalization and temporaries. |
| `SHA256(n)` | `a + b H(n)` | SHA256 of an `n`-byte message, over whole 64-byte blocks. |
| `RIPEMD160(n)` | `a + b H(n)` | RIPEMD160 of a message of at most 520 bytes. |
| `SHA1(n)` | `a + b H(n)` | SHA1 of a message of at most 520 bytes. |
| `SIGCHECK` | 500,000, not fitted | One BIP 340 signature check, at today's allowance of one per 50 weight units; its challenge hash is charged as SHA256. |
| `TWEAK` | `a` | One BIP 449 x-only public key tweak (OP_TWEAKADD). |
| `OP_TX_SELECT(k)` | `a + b k` | One OP_TX selection with `k` charged units: each value selected and each record scanned. |

Rates are fitted per byte of `n` and charged per byte of `W(n)` or `H(n)` (see [Rounding](#rounding)). Macro unrolling adds no primitive: it costs `BASE` per substituted instruction and visited reference plus `WRITE` of the unrolled script.

### Composition rules

- **Lifetimes.** WRITE includes eventual release. Initial witness values pay WRITE once after the immediate-success prescan, including empty values. Moves, drops and in-place shrinkage are not new producers; an opcode that shortens a value pays WRITE for its result.
- **Numeric results** pay `WRITE(W(n)) + NORMALIZE`. MUL charges WRITE and PREPARE of its full product span and WRITE of a scratch row before execution, and only NORMALIZE at output. DIV includes its internal temporary storage.
- **Small results.** A count or numeric comparison costs `WRITE(8) + NORMALIZE` whatever its encoded length; a constant or boolean written directly costs `WRITE(8)`.
- **No separate allocation charge.** Required allocation and growth belong to the producing operation, including scratch storage; they are never omitted or charged twice.
- **Hashes compose.** HASH256 is `SHA256(n) + SHA256(32)`, HASH160 is `SHA256(n) + RIPEMD160(32)`, plus BASE and the digest's WRITE.
- **Final result check**: `PREPARE + READ` of the remaining element, once per script.
- **Macros.** Unrolled instructions in inactive branches pay nothing when reached, so the unrolling charge covers their substitution, copying and skipping. `bench_varops` evaluates such scripts repeatedly against one shared budget, as inputs of one transaction, because one script unrolls at most 4 MB.
- Lock checks pay BASE plus their operands' preparation and scans. PREPARE and NORMALIZE keep their own flats rather than inflating BASE. A fitted rate may be zero.
- Capacity and cache state are measurement conditions, never charge inputs.

For each opcode the source path (calls, multiplicities, size features, branches, charge timing, storage ownership) is derived before looking at its timing; `bench_varops --coverage-manifest` exports the formula and the primitives of every opcode, and an independent charge calculator in `bench_varops --verify-costs` must agree with the meter. Agreement shows accounting, not timing coverage: complete-script benchmarks check that.

## Calibration target

**The criterion.** On every measured machine, the slowest feasible full block of complete Tapleaf 0xC2 scripts must take less than 1.0 times that machine's reference `T_pre`, the slowest measured block of Tapleaf 0xC0 scripts. There is no averaging across machines. A reproducible slower workload rejects the schedule; one noisy observation above 1.0 calls for investigation, not a verdict. Finite tests cannot prove the bound for every script or future processor.

**The fit fraction.** Primitive fits are normalized so that a full budget of fitted work takes 0.9 times the machine's reference. The fraction is a margin for composition effects and machine variation that primitive measurements do not capture. It applies only to fitting: validation and the criterion above use 1.0. Only `fit_calibrations.py` applies it (`TARGET_FRACTION`); the runner only measures and records raw nanoseconds and the reference, and every fit is made here from those samples, so artifacts can be refitted with another fraction without remeasuring.

**Scope.** The claim covers script evaluation only: `EvalTapscriptV2` and its final-result check, or `EvalScript` and its clean-stack check for existing versions, including parsing, metering and execution. Transaction and block validation, Taproot commitment checks and signature-cache effects are not timed. Evaluation is serial; parallel block validation and shared-budget contention are outside the claim, and shared-budget accounting is covered by correctness tests.

**The reference.** `T_pre` is the slowest successful workload of the Tapleaf 0xC0 panel that `bench_varops` measures on the same machine and build: 87 complete Tapleaf 0xC0 scripts and 80,000 raw Schnorr verifications. The panel covers signature checks (CHECKSIG, CHECKSIGVERIFY, CHECKSIGADD), repeated hashing of 1- and 520-byte elements (`3DUP` + three hashes), comparisons and arithmetic on 4-byte operands, stack operations, conditionals over 4 MB scripts, pushes and the 1,000-item initial stack. The slowest workload differs by machine: signature checks on some, repeated hashing of 520-byte elements on others (see the [README](README.md#machines)). Calibration runs measure the panel with the candidate build's Tapleaf 0xC0 evaluator. Validation uses a pinned pre-upgrade build for the reference, so a candidate slowdown of existing scripts cannot raise `T_pre` there.

**Two benchmark modes.**

- *Realistic*: execution is limited by both block weight and the 40,000,000,000-varop budget.
- *Full-varops*: a workload is repeated and its time normalized to 40,000,000,000 varops without treating encoded weight as an execution limit. This models script compression; full-varops figures above current encoded-script limits are extrapolations, not valid block constructions.

**What is timed.**

| Level | Timed work | Use |
| --- | --- | --- |
| Primitive | The named operation or evaluator path on prepared inputs (`bench_varops_primitives`). | Fit coefficients. |
| Complete script | The evaluator call plus its final-result check (`bench_varops`). | Test composition; accept or reject a schedule. |
| Script sequence | The sum of the evaluator calls of a feasible multi-script workload, sharing a budget where required. | Cross-script state and budget effects. |

Initial stacks, checkers, transaction context and budgets are prepared outside the timers. Work done inside the evaluator, including unmetered prescanning, is timed.

## Machine selection

Admission criteria are fixed before results are seen; a machine is excluded only for failing them, never for its results.

- **Platform**: an operating system and architecture for which Bitcoin Core publishes release binaries, on physical hardware or a dedicated virtual machine running them natively.
- **Build**: that platform's release toolchain and flags, with automatic SHA256 backend selection. A native build is accepted if every fitted coefficient agrees within 10% with a release-toolchain build on the same machine. The Windows build uses clang-cl and Ninja with a statically linked runtime.
- **Conditions**: an idle machine on its default power profile. See [Run conditions](#run-conditions).
- **Measurements**: full-length settings (the runner's defaults) at the same source revision on every machine, with raw samples published. Shortened runs are exploratory.
- **Coverage**: at least x86_64 Intel, x86_64 AMD, Apple ARM64 and non-Apple ARM64, together spanning Linux, Windows and macOS, and at least one low-end device of the kind commonly used for home nodes.

Normalizing to each machine's own reference means a uniformly slow machine does not raise prices; only disproportionate relative costs move a coefficient.

**Outliers.** If one machine's coefficient exceeds 1.5 times the next-highest, an independent full run on that machine, after a reboot and on a different day, must reproduce it within 15%; otherwise the first run is discarded. A reproduced outlier is diagnosed. If it stems from a correctable implementation cost outside consensus, such as avoidable allocation, the implementation is fixed and every machine is remeasured; otherwise the machine's fit stays in the combination. Reports name the machine whose fit sets the price at each size, and the diagnosis of each outlier. Rounded prices are final only when complete-script benchmarks stay below 1.0 times the reference on every admitted machine.

### Run conditions

Each primitive is timed in several epochs, each a pass over all primitives, and priced at the median of its epochs. **Epoch noise** is the median over measurements of the median absolute deviation of a measurement's epochs from their median, relative to that median. A run whose epoch noise exceeds **1.5%** is repeated, up to three attempts; failed attempts are kept under `calibration-intermediate/failed-<start>-attempt-<n>/` and recorded in the artifact (`conditions.failed_attempts`). `fit_calibrations.py` applies the same limit to every artifact, including ones collected before the check existed, and rejects runs above it unless `--allow-failed-conditions` is given for an exploratory fit.

Undisturbed runs measure 0.03–0.5%. The limit catches a machine disturbed during a run, not one loaded evenly throughout.

## Measurements and sampling

Every measurement starts from a state that a valid script can create. Prepared operands are cycled through a pool of 8,000,000 bytes, BIP 441's total stack limit (`MAX_TAPSCRIPT_V2_TOTAL_STACK_SIZE`); a state larger than the pool is measured alone. The pool size is a chosen measurement setting, not a bound on a script's working set, which also includes retained capacity, temporaries and the rest of the evaluator's memory. A much larger pool adds memory latency to the fixed costs of small operations: a 64 MiB pool inflated the flats of small MUL, DIV and ARITH measurements on the Ryzen 9 9950X. Memory effects beyond the pool are covered by complete-script benchmarks.

The sample grid follows the operation, not its expected use: dense near zero, at word, hash-block and algorithm boundaries and at legal endpoints, then logarithmic up to the size limit, with allocator-transition neighbours. Two-operand operations include equal lengths, unequal lengths in both orders, and one operand fixed while the other varies, with values that exercise carries, normalization, zero operands and the other branches of the implementation. [Measurements per primitive](#measurements-per-primitive) lists each primitive's paths and ranges.

**Epochs and batches.** Each epoch is one pass over every measurement, so a measurement's epochs lie a whole pass apart: a slow spell of the machine hits one epoch of many measurements, which the median discards, instead of every epoch of a few. Passes before the last alternate direction, so no measurement is always timed early or late. The first pass chooses each measurement's repetitions so that an epoch lasts about the batch time (10 ms; 100 ms for storage lifetimes); later passes reuse them. Every batch gets fresh, untimed state, and the produced state is kept alive until after the clock stops. When the pool caps a batch below what the clock resolves, the batch is repeated on fresh state until an epoch spans at least 50 clock ticks (at most 2% quantization error; Apple's tick is 41.7 ns), and a later pass raises the rounds again if an epoch falls short.

**Timer boundaries.** Prepared operands are outside the timer; conversion, allocation, copying, cleanup and stack or result updates performed by the measured process are inside. Correctness checks run outside the timer. Nothing is subtracted: no OP_NOP dispatch, no separately timed destruction. Existing varops prices never determine a measurement's duration or iteration count.

### Measurements per primitive

Each item names what is timed, its paths (the path groups that the fit weights equally) and its range. Unless stated otherwise the benchmark calls the production helper on prepared operands, which are outside the timer.

- **BASE**: Tapleaf 0xC2 evaluation and final-result checking of 256, 1,024, 4,096 and 16,384 instructions that pay only BASE, followed by OP_1, divided by the executed instruction count. Paths: NOPs, upgradable NOPs, CODESEPARATOR, alternating ELSE, and flat and nested IF/ENDIF in an inactive branch. Parsing and prescanning are included; entry and finalization are amortized. Skipped instructions are timed but not fitted, since serialized weight funds them.
- **PREPARE**: production value conversion of 0 to 4,000,000 bytes on values with word-padded capacity, the state in which every stack value is written. Fitted and charged per byte of `W(n)`.
- **WRITE**: complete finite creation, insertion and release cycles, 0 to 4,000,000 bytes, with 100 ms batches. Paths:
  - `stack`: copying a prepared value onto the stack and releasing it;
  - `vector`: building a value in a word-padded buffer, as producers do, then inserting and releasing it;
  - `zero`: zero-initialized values;
  - `grow`: a value grown once to its result's capacity, as OP_CAT does (both values are counted);
  - `churn`: a large temporary source (about 4 MB) and a small result copied out of it, as OP_SUBSTR or a shortening opcode does (both are counted);
  - `fresh-pages`: from 16 KiB, every value on freshly mapped pages, so each lifetime pays page faults, kernel zeroing and unmapping; this bounds allocators that return large blocks to the operating system.

  Sizes include allocator-transition neighbours (±1, 8 and 16 bytes around 65,536 and other thresholds). Immutable source data is prepared outside the timer; mutable allocations and their destruction are inside. Shortening a value takes an opcode that pays for its result, so creating and shortening a value is two writes; `churn` measures that pair and no single-write shrink is fitted.
- **NORMALIZE**: production numeric-to-byte conversion of prepared aligned spans up to the element size limit (`aligned`) and of scalar results (`scalar`). The returned bytes are retained until after timing; allocation, stack insertion and destruction are outside. The time does not grow with the result's length, because conversion hands the buffer over in place, so NORMALIZE is fitted as a flat.
- **READ**: zero tests on all-zero spans (`zero`), word comparisons of equal spans (`compare`), byte comparisons of equal values as OP_EQUAL performs them (`equal-bytes`) and normalization of zero-padded values (`trim`), from 1 byte to 4,000,000 bytes, forcing full scans.
- **ARITH**: `val64::Add` and `val64::Subtract` (`add`, `sub`) on fresh prepared operands of 1 to 500,000 words, with an equal-length or one-word second operand and full carry and borrow chains. Prepared input and destination storage are outside the timer.
- **BIT**: invert and XOR kernels (`invert`, `xor`), up and down shifts by 1, 7 and 63 bits (`up`, `down`), OP_UPSHIFT's shift of the operand's words after a 64 KiB zero prefix (`upshift`), and OP_BYTEREV's complete work after dispatch: pop, word-wise reversal and push (`byterev`), across sizes. Repeated mutation must not turn a measurement into a cheaper all-zero path.
- **MOVE**: production stack rotation at depths 1, 2, 8, 32, 128, 1,024, 8,192 and 32,767, over empty and nonempty entries; payload bytes are not copied.
- **MUL**: complete prepared multiplications with shorter-operand widths of 1 to 16,384 limbs and longer-operand widths up to the 4,000,000-byte element limit, on all-ones operands (maximal carry chains) and random operands. The zeroed product buffer is created before timing, because the product's WRITE pays for it; internal scratch storage is inside the timer. The per-row term `c v` is each row's call overhead: one schoolbook row per limb of the shorter operand.
- **DIV**: complete prepared division and modulo (DIV and MOD) across divisor widths of 1 to 1,024 limbs, operand ratios and normalization patterns (`normalized`, `top-one`), and dividends up to the element size limit for 1-, 2- and 64-limb divisors. `s = max(1, u − v + 2)` quotient rows and `v` divisor limbs over limbs without trailing zero bytes, as BIP 441 specifies them, not measured loop counts. The bundled measurement includes normalization and temporary storage, so overlapping multiplication or storage work is not added again. The `b s` term covers per-row quotient estimation and correction; measurements with many rows at small `v` identify it.
- **SHA256**: complete Core SHA256 of 0 to 4,000,000 bytes with the automatically selected backend, writing into a prepared digest buffer. Each size is fitted at its block span `H(n)`.
- **RIPEMD160**, **SHA1**: complete calls across the permitted 520-byte direct-input range, including 32-byte second-pass inputs; the digest buffer is prepared outside the timer.
- **SIGCHECK**: uncached production Schnorr verification of valid signatures over messages of 0 to 4,000,000 bytes (OP_CHECKSIGFROMSTACK), checking that the challenge hash stays within `SHA256(64 + n)`. Not fitted for the price, which stays 500,000.
- **TWEAK**: one complete fixed-size production public-key tweak.
- **OP_TX_SELECT**: selector setup plus `k` charged units, on collated output, per unit kind: witness items (`empty_items`), weight scan, amount scan, outputs and input fields. Measured on real transactions.

**Held-out checks**, measured but not fitted:

- **Macro unrolling**: unrolling of inactive NOP, push and reference-chain bodies, against unrolled bytes per unit. The charge is BASE per substituted instruction or visited reference, plus WRITE of the unrolled script when the script declares macros; the measurements check it rather than price it.
- **Lifetime checks**: numeric lifetimes from source to result with tight and spare capacity (`numeric`) and retained values under stack pressure (`retained`). They check that WRITE, PREPARE and NORMALIZE compose to cover complete lifetimes.

## Fitting

**Conversion.** Each measurement's median time is converted to varops at `40,000,000,000 / (0.9 × T_pre)` per nanosecond, so a full budget of fitted work takes 0.9 times the reference on each machine (`pricing_rate` in `src/fit_calibrations.py`).

**Objective.** Each machine is fitted independently, in the primitive's declared feature basis and with nonnegative coefficients. The fit minimizes the weighted squared logarithmic error

    sum_i w_i * p_i * ln(predicted_i / measured_i)^2,   p_i = 100 if predicted_i < measured_i, else 1

Relative error treats small and large operations alike; the 100× penalty keeps the fit at the upper edge of its measurements. A fit is not required to sit above every point, and no safety multiplier enters it.

**Weights.** Weights follow the grid, not expected usage. Within a machine, each path group of a primitive counts equally, each size decade within a group counts equally, and the measurements of a decade share its weight: `w = 1 / (groups × decades in the group × measurements in the decade)`. No operation size is favored by a judgment about how scripts will use it.

**Quality gate.** For every machine, path group and size decade, the fit's multiplicative RMS error must be at most 1.10, and at least 95% of its predictions must lie within a factor of 1.25 of the measurements. Failing bins are reported with one-sided figures: the RMS and maximum factor above the fit (under-prediction) and the maximum factor below it. They are not corrected by reweighting or by excluding measurements.

**Coverage of the charge.** The report lists every included measurement above its rounded charge, as `ratio = measured / charged`; above 1, a full budget of that operation alone would take longer than 0.9 times the reference. A measurement above its charge is a diagnostic finding; only complete scripts establish a limit violation.

**Held-out checks.** Lifetime checks (numeric results with tight and spare capacity, retained values) and macro unrolling are measured but not fitted; they check that the composed charges cover complete lifetimes. `fit_calibrations.py` compares each lifetime with its machine's composed fit and lists those above it (`held_out_lifetimes` in the joint fit's diagnostics).

## Simplicity and revising the basis

The model aims for simple formulas that can be reviewed and reimplemented, not for exact costing. Margin comes from the combination across machines, the underprediction penalty, the fit fraction and upward rounding, so a formula need not follow every path of an implementation. An over-charge of a cheap or rare path is accepted when removing it would add a term or a special case. A shortfall in one primitive is settled by complete-script benchmarks, not by a new term: a formula changes only if complete scripts exceed 1.0 times the reference, and then by the smallest change that suffices, such as a constant.

A decade or path that fails the quality gate is reported, together with every measurement above its rounded charge. A failure that only over-charges may be accepted under the rule above. A measurement above its charge is investigated with complete-script benchmarks, which establish whether it is a resource-limit problem; the primitive definitions stay unchanged meanwhile.

When a formula must change, the basis is revised only with a causal explanation from the implementation: the new term must correspond to work the implementation does and be identifiable on the existing grid. Every machine is then refitted in the revised basis, because combining fits in different bases would charge the same work twice. Consensus charges must be deterministic functions of specified semantics and script-visible operands, results or transaction context; buffer capacity, cache state, backend choice and actual loop or allocation counts are measurement conditions, never charge inputs.

Two examples:

- **MUL** makes one row call per limb of the shorter operand, each with a fixed overhead. A basis of `a + b u + c u v` can charge that overhead only per limb of the longer operand or per limb product, so it over-charges both products with a one-limb operand and wide products, across the whole size range. The per-row term `v` matches the loop structure.
- **DIV** charges a dividend shorter than the divisor one full row, although the implementation only compares lengths. That over-charge is accepted to keep one formula for every operand size.

## Combining machines

The machine fits are combined into their **envelope**: the cheapest curve of the same form that covers every machine's fitted curve at every chargeable size. With feature vector `phi(x) ≥ 0` and machine fits `theta_m`, the combined coefficients `theta ≥ 0` minimize the weighted mean relative charge

    sum_x w(x) * theta . phi(x) / max_m theta_m . phi(x)

over the primitive's measurements, with the fitting weights, subject to `theta . phi(x) ≥ theta_m . phi(x)` for every machine `m` and every chargeable size `x`. The chargeable feature vectors are nonnegative combinations of a few corners and directions, so the constraints are checked there, and the linear program is solved exactly at the vertices of its feasible region (`envelope_coefficients`).

| Primitive | Chargeable domain |
| --- | --- |
| DIV | at least one quotient row and one divisor limb: corner `(1, 1, 1)` of `(1, s, s v)`, directions along `s` and `s v` |
| MUL | shorter operand at most as long as the longer: corner `(1, 0, 0, 0)` of `(1, u, v, u v)`, directions `u`, `u + v`, `u v` |
| SHA256 | at least one 64-byte block, unbounded |
| RIPEMD160, SHA1 | one block up to `H(520)`: the 520-byte direct-input limit |
| Others | from zero, unbounded |

Where sizes start at zero and are unbounded, the envelope equals the coefficientwise maximum; it is lower only where the domain is restricted. Constant primitives use the maximum. The envelope covers every machine's fitted curve, not every measurement. Adding a machine adds constraints, so the envelope never falls below any admitted machine's curve. It is not the coefficientwise maximum in general, which over-charges where machines differ in flat and rate.

## Rounding

After combining, round each coefficient upward on its own (`rounded_candidate`):

- **Rates** to two significant figures, and at least to a whole varop. The prices then carry no more precision than a sample of machines supports, while rounding adds at most 10% to a rate of 10 or more. Rates below 10 can rise by more, because a whole varop is the smallest step.
- **Flats** more coarsely: to a multiple of 10 below 100 and of 50 from 100, but never to more than two significant figures (so 100 from 1,000). A flat is the intercept of a fit, which moves more between calibration runs than the rates do; the coarse steps keep it from changing on every refit, at up to 50% more for a value just above 100.

Zero and values already at a step stay unchanged. Coverage of the machines comes from the envelope, not from rounding. Do not refit after rounding: retain the unrounded fits and report the rounding uplift, especially for small rates. For example, a combined MUL fit of `360.9 + 5.23 u + 118.2 v + 28.20 u v` becomes `400 + 6 u + 120 v + 29 u v`, RIPEMD160's flat of `50.7` becomes `60`, and TWEAK's `168,023` becomes `170,000`.

Rates are fitted per byte of `n` and charged per byte of the padded span the operation processes: `W(n)` for PREPARE, WRITE, READ, ARITH and BIT, `H(n)` for the hashes, and per counted item elsewhere. Since `W(n) ≥ n`, this never charges less than the fit. Every rate is a whole number of varops, so PREPARE's rate, fitted far below one varop per byte, is charged well above its fit on large operands.

**SIGCHECK** is exempt from fitting and rounding: it stays 500,000, matching the existing 50-weight-unit signature allowance. Where the reference is itself signature-bound, verification fitted at 1.0 times the reference approaches 500,000, so signature-heavy scripts run at about 1.0 times the reference rather than 0.9; the budget then admits as many signatures as existing scripts. The benchmarks time uncached production Schnorr verification on valid signatures and separate the modeled challenge-hash contribution; the fitted residual does not replace the policy price. Transaction-message construction is not measured; the fixed allowance covers it by design.

Upward rounding is not a guarantee of timing coverage: rounded compositions are checked with complete scripts on every machine, and their overflow bounds verified.

## Complete-script checks

Primitive benchmarks do not replace complete-script benchmarks. The decisive check is the measured script-evaluation time of whole, feasible workloads under the actual metered evaluator, against the same run's reference.

**The corpus.** `bench_varops` builds stack-neutral sequences repeated to the varops budget or the script-size limit, for every opcode, operand shape and boundary that its generators declare, in realistic and full-varops form. It includes preloaded witness operands, restoration, reusable bodies (macros), OP_TX, short-operand repetition, costly state lifetimes, rejection at operation boundaries, and low-varops work dominated by decoding, inactive regions or prescanning. Each family states its limiting resource; a case need not approach both the weight and the varops limit. Sequences of distinct scripts in one persistent process complement fresh-process runs, so allocator retention is not reset away. `bench_varops --verify-costs` checks every generated script's charge against an independent formula and the exact budget, without timing.

**Searches.** A primitive grid can miss a feature region, so two searches complement the corpus:

- `bench_varops --shape-search --reference-seconds T_pre` samples operand sizes and byte shapes per opcode, hill-climbs the highest projected full-budget time per consumed varop, and confirms the leaders over five epochs.
- `bench_varops --program-search` (experimental, not part of the runner) extends the search to multi-opcode programs in the style of PerfFuzz.

The corpus and the shape search project sampled work to the full budget (`wall × (40e9 − initial) / (consumed − initial)`). For cheap, small-operand sequences that projection is an upper bound: neither a direct script, limited by weight, nor a macro, which charges BASE per substituted instruction, can fill the budget with them.

**Separate measurements from projections.** Reports keep measured evaluator time, predicted composed time and full-varops extrapolation apart. A full-budget projection is decisive only with a feasible way to repeat the same work and state. Do not predict a loop by adding separately timed one-opcode evaluator calls; sum the proposed charges and test the total against measured complete loops. Restoration is explicit: `DUP SHA256 DROP` costs all three operations.

### Validation

A price schedule is validated in three steps on every machine. Only the last can accept it.

1. **Find slow cases.** A quick, noisy run of every complete-script case and the shape search: `bench_varops --sample-budget-percent 2 --epochs 1 --file screen.csv`. A case is **flagged** when its measured or projected full-budget time exceeds 1.0 times that run's slowest Tapleaf 0xC0 case. This step never passes or fails a schedule, and a flagged case is never dropped for being inconvenient.
2. **Re-measure them.** Every flagged case is timed again with longer samples and more rounds: `bench_varops --confirm screen.csv --sample-budget-percent 10 --epochs 7 --file confirm.csv`. Each round runs the flagged Tapleaf 0xC2 cases and the three slowest Tapleaf 0xC0 cases in random order and compares each case with the slowest reference of the same round. A case is `below-limit` when every round is at most 1.0, `straddles-limit` when the median is at most 1.0 but some round is above, and `above-limit` when the median is above 1.0. Every sample records CPU time, page faults and involuntary context switches, so a slow round can be traced to the process or the system. `straddles-limit` cases go into the final measurement; an `above-limit` case is investigated (instrumentation, counts, omitted work) before any repricing. A projection resting on one repetition per sample, such as a large prepared pool, is re-measured at a larger `--sample-budget-percent`.
3. **Final measurement.** The re-measured cases and the slowest cases of each family are timed in a fixed campaign, whose outcome decides acceptance.

Before the final measurement, fix the machines, the workloads, the cost model, the evaluator's memory policy and the reference build; exploratory cases and earlier fit checks do not count. One independent run is a fresh-process session timing the declared evaluator call or script sequence, with its inputs prepared outside the timer; sustained-operation tests instead run the same sequence of distinct feasible scripts in one process and sum their evaluator times. Candidate and reference sessions are interleaved in random order. The number of sessions, at least 30 per workload, is set from pilot variability beforehand, and the campaign does not stop when a desired result appears. A session is excluded only by a rule declared in advance, never for being slow. If drift or correlation between sessions makes them dependent, the comparison is unresolved.

For each workload, `T` is the median of its session times. On each machine, `T_pre` is the maximum `T` over the Tapleaf 0xC0 workloads, and `R = T_candidate / T_pre`. Form exact binomial (order-statistic) lower and upper bounds of every median. With `K` such medians across all machines, allocate `0.05 / (2K)` error probability to each tail, for at least 95% simultaneous coverage by the Bonferroni inequality. With bounds `L` and `U`:

    L_pre = max(reference L); U_pre = max(reference U)
    R interval = [candidate L / U_pre, candidate U / L_pre]

A schedule passes only if every candidate interval's upper end is at most 1; a lower end above 1 shows it fails; otherwise the result is unresolved. Any observed ratio above 1 is investigated even before its interval resolves, and every interval and unresolved case is reported. Repeated campaigns for the same candidate need an error budget declared in advance. The result covers the measured workloads and machines, not undiscovered scripts, other machines or later implementation changes.

## Status

### Current prices

The implementation (gsr `7d0c293b64`) prices every primitive from the six-machine full-run envelope of [`2026-10-01-full-runs-one-deduction`](data/2026-10-01-full-runs-one-deduction/); each dataset's README records what changed since the one before. Earlier datasets remain in the Git history.

### Open items

- Publish the BIP 440 update with these prices; the published draft still lists earlier ones.
- To do before finalizing: add a non-Apple ARM64 machine and a low-end home-node device; run the complete realistic and full-varops suite and its validation on every admitted machine; finalize the WRITE price with complete storage-lifetime and script benchmarks, since buffer-growth measurements depend strongly on allocation history; set and test a separate peak-memory bound.
