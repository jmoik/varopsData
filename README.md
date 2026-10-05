# varopsData

How the costs of Tapleaf 0xC2 operations are measured and priced. [BIP 440](https://github.com/jmoik/bips/blob/gsr-full/bip-0440.mediawiki) gives every script a varops budget and prices each operation from a few cost primitives; this repository holds the measurements on several machines, the fitting code and the [report](report/joint-calibration.html) (download to view). The benchmarks are on the `gsr` branch of [jmoik/bitcoin](https://github.com/jmoik/bitcoin/tree/gsr).

**The requirement:** on every machine, a full block of the slowest Tapleaf 0xC2 scripts must take less time than the slowest block of existing scripts on that machine.

## Method

1. **Reference.** On each machine, time a panel of existing (Tapleaf 0xC0) script workloads, such as 80,000 signature checks or repeated hashing of 520-byte elements. The slowest one, `T_pre`, is the reference.
2. **Primitives.** An operation's cost is a fixed `BASE` plus a few shared primitives: reading operands, writing values, passes over words, moving stack entries, multiplication, division, hashing and signature checks. Each has a simple formula in the operand sizes, declared before measuring.
3. **Measure.** Time every primitive on prepared operands over a grid of sizes (dense near zero, at word and block boundaries, then up to the 4 MB element limit) in 7 passes, and take the median. A run whose passes disagree by more than 1.5% is repeated.
4. **Fit.** For each machine, fit each formula to its measurements, penalizing under-prediction 100 times more than over-prediction, so that a full budget of fitted work takes 0.9 × `T_pre`. A quality gate checks the fit at every size decade.
5. **Combine.** Take the cheapest formula that covers every machine's fit at every size.
6. **Round** up: rates to two significant figures, flat costs to multiples of 10 or 50. Signature checks stay at 500,000, matching today's signature allowance.
7. **Check.** Run complete scripts for every operation, operand shape and boundary, plus searches for the worst ones, and compare each machine's slowest against its `T_pre`. A schedule is accepted only when every machine stays below 1.0.

Formulas stay simple: a cheap path may be over-charged, and a formula changes only when complete scripts exceed the requirement. Machines are admitted by fixed criteria (release builds of supported platforms, idle, full settings), never by their results.

The details of every step, each primitive's measurements and the acceptance statistics are in [METHODOLOGY.md](METHODOLOGY.md). [BIP 440's Derivation of Costs](https://github.com/jmoik/bips/blob/gsr-full/bip-0440.mediawiki#derivation-of-costs) summarizes them.

## Machines

The current dataset, [`data/2026-10-01-full-runs-one-deduction`](data/2026-10-01-full-runs-one-deduction/), has full runs on six machines. It measured READ, WRITE and ARITH in two parts each, from which the fit composes them; the current benchmarks measure them directly. SHA256, RIPEMD160 and SHA1 are measured separately and share one price, HASH. A non-Apple ARM64 machine and a low-end home-node device are still needed.

| Machine | OS | Reference `T_pre` | Slowest existing workload |
|---|---|---|---|
| Apple M1 Pro | macOS | 2.26 s | repeated RIPEMD160 |
| Intel i5-12500 | Linux | 2.73 s | signature checks |
| AMD Ryzen 9 9950X | Windows | 1.59 s | signature checks |
| Intel i7-7700 | Linux | 3.48 s | repeated HASH256 (no SHA-NI) |
| AMD Ryzen 5 3600 | Linux | 3.20 s | signature checks |
| Apple M4 Pro | macOS | 1.59 s | repeated RIPEMD160 |

## Reproduce

On each machine, from a checkout of the `gsr` branch, with this repository checked out beside it (45–60 minutes):

    python3 <varopsData>/src/run_calibration.py --output varop-calibration-<machine>-full.json

It builds the benchmarks in `bench/` inside the gsr checkout with Core's own flags (see `bench/README.md`).

Put the artifacts in a new folder under `data/`, then fit, render and test (Python 3, standard library only):

    python3 src/fit_calibrations.py data/<dataset>/varop-calibration-*.json --source-root <gsr checkout>
    python3 src/render_joint_calibration.py data/<dataset>/joint-calibration.json \
        --output report/joint-calibration.html --source-root <gsr checkout>
    python3 -m unittest discover -s src

## Contents

| Path | |
|---|---|
| `data/2026-10-01-full-runs-one-deduction/` | Current dataset: full runs on six machines after the one-deduction-per-opcode change; the prices come from its fit. |
| `data/2026-10-01-full-runs/` | Full runs on five machines (no M4 Pro), before that change. |
| `report/joint-calibration.html` | The report, rendered from the current dataset. |
| `bench/` | The benchmarks, `bench_varops` (complete scripts) and `bench_varops_primitives` (cost primitives), built into a gsr checkout. |
| `src/` | The calibration runner (`run_calibration.py`), fitting (`fit_calibrations.py`), the report (`render_joint_calibration.py`, `restyle_report.py`) and tests. |
| `METHODOLOGY.md` | The full method. |

Earlier datasets, such as the 3-epoch runs of 2026-09-30, remain in the Git history (last in `0f5c9fa`). Each folder in `data/` holds one artifact per machine with every raw sample, the joint fit, the fit log, a source audit and a README with the run details.
