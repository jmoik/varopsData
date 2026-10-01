# Varops calibration: methodology

The complete method behind the BIP 440 cost primitives, in the order of [BIP 440 Appendix A](https://github.com/jmoik/bips/blob/gsr-full/bip-0440.mediawiki#appendix-a-cost-derivation-methodology), which summarizes it. The [README](README.md) gives the overview and the commands to reproduce a calibration.

## Calibration target

**The criterion.** On every measured machine, the slowest feasible full block of complete Tapleaf 0xC2 scripts must take less than 1.0 times that machine's reference `T_pre`, the slowest measured block of existing-version scripts. There is no averaging across machines. A reproducible slower workload rejects the schedule; one noisy observation above 1.0 calls for investigation, not a verdict. Finite tests cannot prove the bound for every script or future processor.

**The fit fraction.** Primitive fits are normalized so that a full budget of fitted work takes 0.9 times the machine's reference. The fraction is a margin for composition effects and machine variation that primitive fixtures do not capture. It applies only to fitting: screening, confirmation and the criterion above use 1.0. Only `fit_calibrations.py` applies it (`TARGET_FRACTION`); the runner records raw nanoseconds and the reference, so artifacts can be refitted with another fraction without remeasuring.

**Scope.** The claim covers script evaluation only: `EvalTapscriptV2` and its final-result check, or `EvalScript` and its clean-stack check for existing versions, including parsing, metering and execution. Transaction and block validation, Taproot commitment checks and signature-cache effects are not timed. Evaluation is serial; parallel block validation and shared-budget contention are outside the claim, and shared-budget accounting is covered by correctness tests.

**The reference.** `T_pre` is the slowest successful workload of the Tapleaf 0xC0 panel that `bench_varops` measures on the same machine and build: 87 complete Tapleaf 0xC0 scripts and 80,000 raw Schnorr verifications. The panel covers signature checks (CHECKSIG, CHECKSIGVERIFY, CHECKSIGADD), repeated hashing of 1- and 520-byte elements (`3DUP` + three hashes), comparisons and arithmetic on 4-byte operands, stack operations, conditionals over 4 MB scripts, pushes and the 1,000-item initial stack. The runner measures it with `bench_varops --case-filter OP_NOP --sample-budget-percent 2 --epochs 5` (the filter keeps every Tapleaf 0xC0 case and limits Tapleaf 0xC2 timing; the sample limit does not truncate Tapleaf 0xC0 cases). The slowest workload differs by machine. In the [`2026-10-01-full-runs`](data/2026-10-01-full-runs/) dataset it was signature checks on the i5-12500, Ryzen 5 3600 and Ryzen 9 9950X, repeated RIPEMD160 of 520-byte elements on the Apple machines, and repeated HASH256 of 520-byte elements on the i7-7700, which has no SHA-NI. Calibration runs measure the panel with the candidate build's existing-version evaluator. The final gate uses a pinned pre-upgrade build for the reference, so a candidate slowdown of existing scripts cannot raise `T_pre` there.

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

**Machines so far.** Apple M4 Pro and M1 Pro (macOS, Apple Clang), Intel i5-12500 and i7-7700 and AMD Ryzen 5 3600 (Linux, GCC; the i7-7700 has no SHA-NI), and AMD Ryzen 9 9950X (Windows 11, clang-cl). A non-Apple ARM64 machine and a low-end home-node device are still missing. Each dataset's README lists its machines, compilers, SHA256 backends, references and epoch noise.

### Run conditions

Each primitive is timed in several epochs, each a pass over all primitives, and priced at the median of its epochs. **Epoch noise** is the median over fixtures of the median absolute deviation of a fixture's epochs from their median, relative to that median. A run whose epoch noise exceeds **1.5%** is repeated, up to three attempts; failed attempts are kept under `calibration-intermediate/failed-<start>-attempt-<n>/` and recorded in the artifact (`conditions.failed_attempts`). `fit_calibrations.py` applies the same limit to every artifact, including ones collected before the check existed, and rejects runs above it unless `--allow-failed-conditions` is given for an exploratory fit.

Undisturbed runs measure 0.03–0.5%. With seven epochs the measure reads about twice what it reads with three, and a Mac with its usual background tasks measures 1.1–1.2%, hence 1.5% rather than the earlier 1%. The threshold is a provisional screening check: it catches a machine disturbed during a run, not one loaded evenly throughout. The one-minute load average is recorded where the OS reports it, for information only (an idle macOS desktop already reports 1.5–2). A second reference measurement and a reference-drift condition were dropped.

## Fixtures and sampling

Every fixture is a state that a valid script can create. Prepared operands are cycled through a pool of 8,000,000 bytes, BIP 441's total stack limit (`MAX_TAPSCRIPT_V2_TOTAL_STACK_SIZE`); a state larger than the pool is measured alone. The pool size is a chosen measurement setting, not a bound on a script's working set, which also includes retained capacity, temporaries and the rest of the evaluator's memory. A much larger pool adds memory latency to the fixed costs of small operations: a 64 MiB pool inflated the flats of small MUL, DIV and ARITH fixtures on the Ryzen 9 9950X. Memory effects beyond the pool are covered by complete-script benchmarks.

The sample grid follows the operation, not its expected use: dense near zero, at word, hash-block and algorithm boundaries and at legal endpoints, then logarithmic up to the size limit, with allocator-transition neighbours. Two-operand operations include equal lengths, unequal lengths in both orders, and one operand fixed while the other varies, with values that exercise carries, normalization, zero operands and the other branches of the implementation. [Primitives](#primitives) lists each primitive's paths and ranges.

**Epochs and batches.** Each epoch is one pass over every fixture, so a fixture's epochs lie a whole pass apart: a slow spell of the machine hits one epoch of many fixtures, which the median discards, instead of every epoch of a few. Passes before the last alternate direction, so no fixture is always timed early or late. The first pass chooses each fixture's repetitions so that an epoch lasts about the batch time (10 ms; 100 ms for storage lifetimes); later passes reuse them. Every batch gets fresh, untimed state, and the produced state is kept alive until after the clock stops. When the pool caps a batch below what the clock resolves, the batch is repeated on fresh state until an epoch spans at least 50 clock ticks (at most 2% quantization error; Apple's tick is 41.7 ns), and a later pass raises the rounds again if an epoch falls short.

**Timer boundaries.** Prepared operands are outside the timer; conversion, allocation, copying, cleanup and stack or result updates performed by the measured process are inside. Correctness checks run outside the timer. Nothing is subtracted: no OP_NOP dispatch, no separately timed destruction. Existing varops prices never determine a fixture's duration or iteration count.

## Fitting

**Conversion.** Each fixture's median time is converted to varops at `40,000,000,000 / (0.9 × T_pre)` per nanosecond, so a full budget of fitted work takes 0.9 times the reference on each machine (`pricing_rate` in `src/fit_calibrations.py`).

**Objective.** Each machine is fitted independently, in the primitive's declared feature basis and with nonnegative coefficients. The fit minimizes the weighted squared logarithmic error

    sum_i w_i * p_i * ln(predicted_i / measured_i)^2,   p_i = 100 if predicted_i < measured_i, else 1

Relative error treats small and large operations alike; the 100× penalty keeps the fit at the upper edge of its measurements. A fit is not required to sit above every point, and no safety multiplier enters it. The runner records a fit for every primitive. `fit_calibrations.py` refits MUL, DIV, the hashes, WRITE, NORMALIZE, PREPARE and BIT from the recorded samples in their current bases, so older artifacts are fitted like new ones, and uses the recorded fits for the others.

**Weights.** Weights follow the grid, not expected usage. Within a machine, each path group of a primitive counts equally, each size decade within a group counts equally, and the fixtures of a decade share its weight: `w = 1 / (groups × decades in the group × fixtures in the decade)`. No operation size is favored by a judgment about how scripts will use it.

**Quality gate.** For every machine, path group and size decade, the fit's multiplicative RMS error must be at most 1.10, and at least 95% of its predictions must lie within a factor of 1.25 of the measurements. Failing bins are reported with one-sided figures: the RMS and maximum factor above the fit (under-prediction) and the maximum factor below it. They are not corrected by reweighting or by excluding fixtures.

**Coverage of the charge.** The report lists every included fixture measured above its rounded charge, as `ratio = measured / charged`; above 1, a full budget of that fixture alone would take longer than 0.9 times the reference. A fixture above its charge is a diagnostic finding; only complete scripts establish a limit violation.

**Held-out checks.** `PRODUCER_CHECK` lifetime sequences (numeric results with tight and spare capacity, retained values) and the `MACRO_UNROLL` fixtures are measured but not fitted; they check that the composed charges cover complete lifetimes.

## Simplicity and revising the basis

The model aims for simple formulas that can be reviewed and reimplemented, not for exact costing. Margin comes from the combination across machines, the underprediction penalty, the fit fraction and upward rounding, so a formula need not follow every path of an implementation. An over-charge of a cheap or rare path is accepted when removing it would add a term or a special case. A shortfall in one primitive is settled by complete-script benchmarks, not by a new term: a formula changes only if complete scripts exceed 1.0 times the reference, and then by the smallest change that suffices, such as a constant.

A decade or path that fails the quality gate is reported, together with every fixture measured above its rounded charge. A failure that only over-charges may be accepted under the rule above. A fixture above its charge is investigated with complete-script benchmarks, which establish whether it is a resource-limit problem; the primitive definitions stay unchanged meanwhile.

When a formula must change, the basis is revised only with a causal explanation from the implementation: the new term must correspond to work the implementation does and be identifiable on the existing grid. Every machine is then refitted in the revised basis, because combining fits in different bases would charge the same work twice. Consensus charges must be deterministic functions of specified semantics and script-visible operands, results or transaction context; buffer capacity, cache state, backend choice and actual loop or allocation counts are measurement conditions, never charge inputs.

Two examples:

- **MUL** makes one row call per limb of the shorter operand, each with a fixed overhead. A basis of `a + b u + c u v` can charge that overhead only per limb of the longer operand or per limb product, so it over-charges both products with a one-limb operand and wide products, across the whole size range. The per-row term `v` matches the loop structure.
- **DIV** charges a dividend shorter than the divisor one full row, although the implementation only compares lengths. That over-charge is accepted to keep one formula for every operand size.

Earlier findings of this kind: the DIV per-row term was found by search, not by the primitive grid, and OP_BYTEREV of values up to 33 bytes measured above 1.0 on the i5-12500 and i7-7700 because its pop and push were outside the BIT fixtures, which now time its complete work.

## Combining machines

The machine fits are combined into their **envelope**: the cheapest curve of the same form that covers every machine's fitted curve at every chargeable size. With feature vector `phi(x) ≥ 0` and machine fits `theta_m`, the combined coefficients `theta ≥ 0` minimize the weighted mean relative charge

    sum_x w(x) * theta . phi(x) / max_m theta_m . phi(x)

over the primitive's fixtures, with the fitting weights, subject to `theta . phi(x) ≥ theta_m . phi(x)` for every machine `m` and every chargeable size `x`. The chargeable feature vectors are nonnegative combinations of a few corners and directions, so the constraints are checked there, and the linear program is solved exactly at the vertices of its feasible region (`envelope_coefficients`).

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

Zero and values already at a step stay unchanged. Coverage of the machines comes from the envelope, not from rounding. Do not refit after rounding: retain the unrounded fits and report the rounding uplift, especially for small rates. For example, a combined MUL fit of `327.8 + 9.38 u + 106.8 v + 28.29 u v` becomes `350 + 10 u + 110 v + 29 u v`, a flat of `53.7` becomes `60`, and TWEAK's `168,387` becomes `170,000`.

Rates are fitted per byte of `n` and charged per byte of the padded span the operation processes: `W(n)` for WRITE, READ, ARITH and BIT, `H(n)` for the hashes, and per counted item elsewhere. Since `W(n) ≥ n`, this never charges less than the fit. PREPARE's fitted rate is far below one varop per byte, so it is rounded and charged per 64-bit word instead, where a whole varop is a smaller step.

**SIGCHECK** is exempt from fitting and rounding: it stays 500,000, matching the existing 50-weight-unit signature allowance. Where the reference is itself signature-bound, fitted verification approaches 500,000, so signature-heavy scripts run at about 1.0 times the reference; the budget then admits as many signatures as existing scripts. The benchmarks time uncached production Schnorr verification on valid signatures and separate the modeled challenge-hash contribution; the fitted residual does not replace the policy price. Transaction-message construction is not measured; the fixed allowance covers it by design.

Upward rounding is not a guarantee of timing coverage: rounded compositions are checked with complete scripts on every machine, and their overflow bounds verified.

## Complete-script checks

Primitive benchmarks do not replace complete-script benchmarks. The decisive check is the measured script-evaluation time of whole, feasible workloads under the actual metered evaluator, against the same run's reference.

**The corpus.** `bench_varops` builds stack-neutral sequences repeated to the varops budget or the script-size limit, for every opcode, operand shape and boundary that its generators declare, in realistic and full-varops form. It includes preloaded witness operands, restoration, reusable bodies (macros), OP_TX, short-operand repetition, costly state lifetimes, rejection at operation boundaries, and low-varops work dominated by decoding, inactive regions or prescanning. Each family states its limiting resource; a case need not approach both the weight and the varops limit. Sequences of distinct scripts in one persistent process complement fresh-process runs, so allocator retention is not reset away. `bench_varops --verify-costs` checks every generated script's charge against an independent formula and the exact budget, without timing.

**Searches.** A primitive grid can miss a feature region, so two searches complement the corpus:

- `bench_varops --shape-search --reference-seconds T_pre` samples operand sizes and byte shapes per opcode, hill-climbs the highest projected full-budget time per consumed varop, and confirms the leaders over five epochs.
- `bench_varops --program-search --reference-seconds T_pre --search-seconds N --search-corpus DIR` (experimental, not part of the runner) extends it to multi-opcode programs, stack depth and live data size. It is a coverage-guided loop in the style of PerfFuzz: it mutates and splices stack-neutral programs (an initial witness stack, then steps that copy operands or move earlier results, apply one opcode and keep or drop the results) and retains a program when it reaches a new feature (an opcode, a data dependency between two opcodes, an operand size class, a stack depth or a live size) or is slower than that feature's program. Its fitness is the time of a full block of the repeated program that a block can actually hold: a direct script is bounded by the budget and by its weight, and a macro-substituted body pays BASE per substituted instruction plus the unrolled script's write, so cheap bodies are not credited with a full budget. Inactive branches are left to the corpus. The corpus directory keeps each feature's slowest program and is reloaded and re-measured by later runs and other machines. The deadline, the screening budget per sample (`--search-budget`, default 1% of the block) and the confirmation of the leaders bound its runtime.

The corpus and the shape search project sampled work to the full budget (`wall × (40e9 − initial) / (consumed − initial)`). For cheap, small-operand sequences that projection is an upper bound: neither a direct script, limited by weight, nor a macro, which charges BASE per substituted instruction, can fill the budget with them.

**Separate measurements from projections.** Reports keep measured evaluator time, predicted composed time and full-varops extrapolation apart. A full-budget projection is decisive only with a feasible way to repeat the same work and state. Do not predict a loop by adding separately timed one-opcode evaluator calls; sum the proposed charges and test the total against measured complete loops. Restoration is explicit: `DUP SHA256 DROP` costs all three operations.

### Screening and confirmation

Timing evidence has three tiers; only the last accepts a schedule.

1. **Screening** finds candidates cheaply and is expected to be noisy. On each machine run `bench_varops --exclude-experimental --sample-budget-percent 2 --epochs 1 --file screen.csv` and the shape search. A case is **flagged** when its measured or projected full-budget time exceeds 1.0 times that run's slowest Tapleaf 0xC0 case. Screening never passes or fails a schedule, and a flagged case is never dropped for being inconvenient.
2. **Confirmation** re-measures every flagged case with longer samples, more rounds and a same-run reference: `bench_varops --confirm screen.csv --sample-budget-percent 10 --epochs 7 --file confirm.csv`. It reruns exactly the flagged Tapleaf 0xC2 cases together with the screening run's three slowest Tapleaf 0xC0 cases, in randomized order per round, and reports each case's per-round ratio against the slowest reference measured in the same round. Verdicts: `below-limit` (every round ≤ 1.0), `straddles-limit` (median ≤ 1.0 but some round above) and `above-limit` (median > 1.0). Every sample records CPU time, page faults and involuntary context switches, so a slow round can be attributed to the process or the system. `straddles-limit` cases stay in the report and the final panel; `above-limit` requires investigation (instrumentation, counts, omitted work) before any repricing. A projection resting on one repetition per sample, such as a large prepared pool, is confirmed at a larger `--sample-budget-percent`.
3. **Final gate**: the frozen-panel campaign below, with every confirmed-flagged case and the slowest confirmed cases of each family. Its outcome decides acceptance.

### Final gate

Freeze the machine and workload panel, the model, the evaluator memory policy and the reference build before confirmation; exploratory cases and earlier fit checks are not confirmation samples. One independent run is a fresh-process session timing the declared evaluator call or script sequence, with fixtures prepared outside its timer; sustained-operation tests instead run the same declared sequence of distinct feasible scripts in a persistent process and analyze the sum of evaluator times. Candidate and pinned-reference sessions are interleaved in randomized order. The number of sessions is set from pilot variability before confirmation, at least 30 per workload; do not stop when a desired result appears. A technical exclusion must follow a predeclared rule, not a slow time. Check session drift and correlation; if independence is untenable, the comparison remains unresolved.

For each workload, `T` is the median of its session times. On each machine, `T_pre` is the maximum `T` over the frozen Tapleaf 0xC0 panel, and `R = T_candidate / T_pre`. Form exact binomial (order-statistic) lower and upper median bounds for every reference and candidate workload. With `K` such medians across all machines, allocate `0.05 / (2K)` error probability to each tail, for at least 95% simultaneous coverage by the Bonferroni inequality. With bounds `L` and `U`:

    L_pre = max(reference L); U_pre = max(reference U)
    R interval = [candidate L / U_pre, candidate U / L_pre]

A configuration passes only if every candidate interval's upper end is at most 1; a lower end above 1 demonstrates failure; otherwise it is unresolved. Investigate any observed ratio above 1 even before an interval resolves. Report every interval and unresolved case. Repeated campaigns for the same candidate need a predeclared error budget across attempts. The rule covers the frozen finite panel under repeatable session conditions, not undiscovered scripts, machines or implementation changes.

Separately, test evaluator rejection paths: budget exhaustion at operation boundaries, oversized results, late decoding and final-stack failures. Bound evaluator work and temporary memory before rejection, including allowed postcharge work. Preserve raw runs and candidate revisions; if a case fails, fix the measurement or model and obtain a new holdout rather than relabelling the failing case.

## Primitives

An opcode's charge is `BASE` plus the primitives its formula names (BIP 440 Cost Primitives and Opcode Costs, BIP 441 for the re-enabled opcodes). The prices themselves are in BIP 440, `src/script/varops.h` and the [report](report/joint-calibration.html).

Notation: `W(n) = 8 ceil(n / 8)` is the word span of `n` bytes, `H(n) = 64 floor((n + 72) / 64)` the bytes a 64-byte-block hash processes, and `u ≥ v` limb counts. Fixture counts are those of one machine in the [`2026-10-01-full-runs`](data/2026-10-01-full-runs/) dataset; raw labels are the names in the artifacts.

| Primitive | Raw label | Fitted basis | Charged as | Fixtures |
| --- | --- | --- | --- | --- |
| `BASE` | `F` | `a` | flat | 28 |
| `PREPARE(n)` | `PREP` | `a + b W(n)` | flat + per 64-bit word | 221 |
| `WRITE(n)` | `PRODUCE` | `a + b n` | flat + per byte of `W(n)` | 1,311 |
| `NORMALIZE` | `NORMALIZE` | `a` | flat | 251 |
| `READ(n)` | `READ` | `a + b n` | flat + per byte of `W(n)` | 708 |
| `ARITH(n)` | `ARITH` | `a + b n` | flat + per byte of `W(n)` | 1,072 |
| `BIT(n)` | `BIT` | `a + b n` | flat + per byte of `W(n)` | 2,640 |
| `MOVE(k)` | `MOVE` | `a + b k` | flat + per entry | 16 |
| `MUL(u,v)` | `MULCORE` | `a + b u + c v + d u v` | same | 204 |
| `DIV(s,v)` | `DIVCORE` | `a + b s + c s v` | same | 1,192 |
| `SHA256(n)` | `H256` | `a + b H(n)` | same | 221 |
| `RIPEMD160(n)` | `H160` | `a + b H(n)` | same | 130 |
| `SHA1(n)` | `H1` | `a + b H(n)` | same | 130 |
| `SIGCHECK` | `SIG` | not fitted | 500,000 | 10 |
| `TWEAK` | `TWEAK` | `a` | flat | 1 |
| `OP_TX_SELECT(k)` | `SELECT` | `a + b k` | flat + per unit | 62 |
| `MACRO_UNROLL` | `UNROLL` | held-out check | BASE per unit + WRITE | 23 |
| lifetime checks | `PRODUCER_CHECK` | held-out check | — | 519 |

### Measurements

Each item names what is timed, its paths (the path groups that the fit weights equally) and its range. Unless stated otherwise the benchmark calls the production helper on prepared operands, which are outside the timer.

- **BASE**: Tapleaf 0xC2 evaluation and final-result checking of 256, 1,024, 4,096 and 16,384 instructions that pay only BASE, followed by OP_1, divided by the executed instruction count. Paths: NOPs, upgradable NOPs, CODESEPARATOR, alternating ELSE, and flat and nested IF/ENDIF in an inactive branch. Parsing and prescanning are included; entry and finalization are amortized. Skipped instructions (`F/skipped`) are timed as a diagnostic but not fitted, since serialized weight funds them.
- **PREPARE**: production value conversion of 0 to 4,000,000 bytes on values with word-padded capacity, the state in which every stack value is written. Fitted per byte of `W(n)`; its rate is far below one varop per byte, so it is charged per 64-bit word.
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
- **BIT**: invert and XOR kernels (`invert`, `xor`), up and down shifts by 1, 7 and 63 bits (`up`, `down`), OP_UPSHIFT's shift of the operand's words after a 64 KiB zero prefix (`upshift`), and OP_BYTEREV's complete work after dispatch: pop, word-wise reversal and push (`byterev`), across sizes. Repeated mutation must not turn a fixture into a cheaper all-zero path.
- **MOVE**: production stack rotation at depths 1, 2, 8, 32, 128, 1,024, 8,192 and 32,767, over empty and nonempty entries; payload bytes are not copied.
- **MUL**: complete prepared multiplications with shorter-operand widths of 1 to 16,384 limbs and longer-operand widths up to the 4,000,000-byte element limit, on all-ones operands (maximal carry chains) and random operands. The zeroed product buffer is created before timing, because the product's WRITE pays for it; internal scratch storage is inside the timer. The per-row term `c v` is each row's call overhead: one schoolbook row per limb of the shorter operand.
- **DIV**: complete prepared division and modulo (DIV and MOD) across divisor widths of 1 to 1,024 limbs, operand ratios and normalization patterns (`normalized`, `top-one`), and dividends up to the element size limit for 1-, 2- and 64-limb divisors. `s = max(1, u − v + 2)` quotient rows and `v` divisor limbs over limbs without trailing zero bytes, as BIP 441 specifies them, not measured loop counts. The bundled measurement includes normalization and temporary storage, so overlapping multiplication or storage work is not added again. The `b s` term covers per-row quotient estimation and correction; fixtures with many rows at small `v` identify it.
- **SHA256**: complete Core SHA256 of 0 to 4,000,000 bytes with the automatically selected backend, writing into a prepared digest buffer. Each size is fitted at its block span `H(n)`.
- **RIPEMD160**, **SHA1**: complete calls across the permitted 520-byte direct-input range, including 32-byte second-pass inputs; the digest buffer is prepared outside the timer.
- **SIGCHECK**: uncached production Schnorr verification of valid signatures over messages of 0 to 4,000,000 bytes (OP_CHECKSIGFROMSTACK), checking that the challenge hash stays within `SHA256(64 + n)`. Not fitted for the price, which stays 500,000.
- **TWEAK**: one complete fixed-size production public-key tweak.
- **OP_TX_SELECT**: selector setup plus `k` charged units, on collated output, per unit kind: witness items (`empty_items`), weight scan, amount scan, outputs and input fields. Transaction-backed fixtures; OP_TX finalization is postponed.

**Held-out checks**, measured but not fitted:

- `MACRO_UNROLL`: unrolling of inactive NOP, push and reference-chain bodies, against unrolled bytes per unit. The charge is BASE per substituted instruction or visited reference, plus WRITE of the unrolled script when the script declares macros; the measurements check it rather than price it.
- `PRODUCER_CHECK`: numeric lifetimes from source to result with tight and spare capacity (`numeric`) and retained values under stack pressure (`retained`). They check that WRITE, PREPARE and NORMALIZE compose to cover complete lifetimes.

### Composition rules

- **Lifetimes.** WRITE includes eventual release. Initial witness values pay WRITE once after the immediate-success prescan, including empty values. Moves, drops and in-place shrinkage are not new producers; an opcode that shortens a value pays WRITE for its result. See the [storage ledger](#storage-accounting).
- **Numeric results** pay `WRITE(W(n)) + NORMALIZE`. MUL charges WRITE and PREPARE of its full product span and WRITE of a scratch row before execution, and only NORMALIZE at output. DIV includes its internal temporary storage.
- **Small results.** A count or numeric comparison costs `WRITE(8) + NORMALIZE` whatever its encoded length; a constant or boolean written directly costs `WRITE(8)`.
- **No separate allocation charge.** Required allocation and growth belong to the producing operation, including scratch storage; they are never omitted or charged twice.
- **Hashes compose.** HASH256 is `SHA256(n) + SHA256(32)`, HASH160 is `SHA256(n) + RIPEMD160(32)`, plus BASE and the digest's WRITE.
- **Final result check**: `PREPARE + READ` of the remaining element, once per script, with no separate FINAL primitive.
- **Macros.** Unrolled instructions in inactive branches pay nothing when reached, so the unrolling charge covers their substitution, copying and skipping. `bench_varops` evaluates such scripts repeatedly against one shared budget, as inputs of one transaction, because one script unrolls at most 4 MB.
- Lock checks pay BASE plus their operands' preparation and scans. PREPARE and NORMALIZE keep their own flats rather than inflating BASE. A fitted rate may be zero.
- Capacity and cache state are measurement conditions, never charge inputs.

For each opcode the source path (calls, multiplicities, size features, branches, charge timing, storage ownership) is derived before looking at its timing; `bench_varops --coverage-manifest` exports the formula and the primitives of every opcode, and an independent charge calculator in `bench_varops --verify-costs` must agree with the meter. Agreement shows accounting, not timing coverage: complete-script benchmarks check that.

### Storage accounting

A production event is a specified semantic value/result, not each allocator call. Reuse may conservatively pay production again; capacity never enters a charge.

| Path | Production/lifetime accounting | Other work / check |
|---|---|---|
| Initial witness values | WRITE once per item after immediate-success prescan | Empty items included; early success bypass unchanged |
| Literal/scalar push | WRITE(bytes); scalar uses NUMERIC_RESULT(8) | Fixed opcode cost; numeric materialization for scalar |
| DUP/OVER/PICK/TUCK families | WRITE once per new copy | Count conversion or truth check as applicable |
| DROP/NIP/2DROP, final cleanup | No separate release | Lifetime funded by original producer |
| ALTSTACK, SWAP/ROT/ROLL | No new production | MOVE prices ownership/header changes |
| CAT growth | WRITE(full result), even if allocation reused | Inputs were funded independently; growth is not free |
| SUBSTR | WRITE(result) once | Index PREPARE/READ; source/index release already funded |
| LEFT/RIGHT shrink | WRITE(result) once | Pushing may copy the result out of the old buffer; original producer funds releasing it |
| Numeric/bit results, comparisons, MIN/MAX | NUMERIC_RESULT(n) = WRITE(W(n)) + NORMALIZE | PREPARE/kernel work separately; no second RELEASE |
| MUL | WRITE(full result span) + PREPARE(result span) + WRITE(scratch row) before kernel | Row/accumulation kernels exclude these allocations; only NORMALIZE at output |
| DIV/MOD | DIV includes internal scratch and normalization | External result uses NUMERIC_RESULT; do not add kernel scratch again |
| Hash/key result | WRITE(digest/key bytes) | Hash/tweak helper writes prepared output; does not insert on stack |

The numeric output production is a conservative semantic charge even when the input allocation is reused. This is distinct from accidentally charging both an explicit RELEASE and a lifetime-inclusive WRITE. PREPARE tight-capacity growth is checked by finite source-to-result lifetimes, not an allocator-dependent extra formula. Known lifetime-fit underpredictions remain evidence to validate across machines; this ledger alone does not prove timing coverage.

Retained capacity is bounded: `ValtypeStack::push_back(valtype&&)` copies a value whose capacity exceeds twice its word-rounded length into a word-rounded buffer. Every path that can shorten a value pays production of its result (LEFT/RIGHT/SUBSTR, numeric/scalar results, MUL's prepaid span), so the copy is always funded. Creating and then shortening a value is therefore two productions; WRITE is fitted without shrink fixtures, and `PRODUCE/churn` measures the pair. Stack element storage is therefore at most twice the word-rounded logical bytes, plus vector headers and allocator overhead. Every producer (literal pushes, OP_1..OP_16, CAT, SUBSTR, digests, OP_TX, numeric results) reserves `WordPaddedCapacity(n)` before filling a value, so pushing it, or later preparing it as a number, never reallocates; the reserve in `push_back` only guards values that did not. The PRODUCE fixtures build values the same way.

Regression checks: `producer_lifetime_accounting` covers initial values, empty values, moves/drops, shrinkage and multiplication preflight; `producer_composition_boundaries` covers CAT, SUBSTR, ADD carry growth and SUB borrow shrinkage with exact and one-short budgets around byte/word/allocation boundaries; `shortening_splice_costs` covers LEFT/RIGHT; `valtype_stack_move_bounds_retained_capacity` covers the capacity bound. `bench_varops --verify-costs` checks independent formulas and exact budgets across generated scripts.

### Removed primitives

Earlier cost models used other primitives; older datasets and the archive use their names.

| Former | Replaced by |
| --- | --- |
| `COPY`, `RELEASE`, `ALLOC` | WRITE, which times a complete lifetime |
| `OUTPUT`, `SMALL` | NORMALIZE plus WRITE |
| `DIVSTEP` | the `b s` term of the bundled DIV |
| `FINAL` | PREPARE + READ of the final element |
| `DECODE`, `REF` | BASE per decoded instruction and the macro unrolling charge |
| `SIGHASH`, `DISCARD` | SIGCHECK's fixed allowance; no release charge |

## Status

### Current prices

The implementation (gsr `7d0c293b64`) and BIP draft price every primitive, OP_TX_SELECT included, from the six-machine (M1 Pro, i5-12500, Ryzen 9 9950X, i7-7700, Ryzen 5 3600, M4 Pro) full-run envelope of [`2026-10-01-full-runs-one-deduction`](data/2026-10-01-full-runs-one-deduction/), measured with the 8 MB fixture pool and normalized to 0.9 times each reference. SHA256 is fitted on Core SHA256 only. The `BIT/byterev` fixtures time OP_BYTEREV's complete work (pop, reverse, push), since a `bench_varops` screening of an earlier schedule found short OP_BYTEREV values above 1.0 times the reference on the i5-12500 and i7-7700 when only the reversal was measured. Each opcode deducts its charges, BASE included, from the shared budget once, so the deduction F measures is the only one a complete opcode makes; a second deduction per charged opcode had put OP_BYTEREV of 0-1 bytes at 1.04 times the reference on the Ryzen 9 9950X. The previous prices came from the five-machine [`2026-10-01-full-runs`](data/2026-10-01-full-runs/). The 3-epoch datasets of 2026-09-30 (gsr `ea7a71e20e` and `13e9a89dc7`), from which earlier prices came, remain in the Git history (last in `0f5c9fa`). Historical raw artifacts remain unchanged.

### Open items

- To do before finalizing: add a non-Apple ARM64 machine and a low-end home-node device; run the complete realistic and full-varops suite and the confirmation tiers on every admitted machine; finalize the WRITE price with complete storage-lifetime and script benchmarks, since buffer-growth measurements depend strongly on allocation history; set and test a separate peak-memory bound.
- OP_TX and macro finalization are postponed; their fixtures and prices remain experimental.

### History

Realistic-mode measurements of directly encoded workloads under an earlier cost schedule, on x86_64 and ARM64, found the slowest existing-version blocks dominated by Schnorr verification (80,000 signatures) and repeated hashing of 520-byte elements (`3DUP` + HASH256 or RIPEMD160). Under Tapleaf 0xC2 the slowest blocks then also included MUL on 1-byte operands, `2DUP` + LSHIFT on 10 KB elements, HASH256 of 1 KB elements, ROLL at maximum depth, and copying of 100 KB–2 MB elements (TUCK, CAT). Until 2026-04-10 this repository collected those `bench_varops` worst-case results for the original BIP 440 cost model, including runs contributed by others; the CSV files and plots remain in its Git history (`906848d`).
