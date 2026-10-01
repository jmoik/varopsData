# varopsData

How the costs of Tapscript v2 operations are measured and priced. [BIP 440](https://github.com/jmoik/bips/blob/gsr-full/bip-0440.mediawiki) gives every script a varops budget and prices each operation from a few cost primitives; this repository holds the measurements on several machines, the fitting code and the [report](0.4.0/joint-calibration.html) (download to view). The benchmarks are on the `gsr` branch of [jmoik/bitcoin](https://github.com/jmoik/bitcoin/tree/gsr).

**The requirement:** on every machine, a full block of the slowest Tapscript v2 scripts must take less time than the slowest block of existing scripts on that machine.

## Method

1. **Reference.** On each machine, time a panel of existing (Tapscript v1) script workloads, such as 80,000 signature checks or repeated hashing of 520-byte elements. The slowest one, `T_pre`, is the reference.
2. **Primitives.** An operation's cost is a fixed `BASE` plus a few shared primitives: preparing numbers, writing values, reading, arithmetic, bit operations, moving stack entries, multiplication, division, hashing and signature checks. Each has a simple formula in the operand sizes, declared before measuring.
3. **Measure.** Time every primitive on prepared operands over a grid of sizes (dense near zero, at word and block boundaries, then up to the 4 MB element limit) in 7 passes, and take the median. A run whose passes disagree by more than 1.5% is repeated.
4. **Fit.** For each machine, fit each formula to its measurements, penalizing under-prediction 100 times more than over-prediction, so that a full budget of fitted work takes 0.9 × `T_pre`. A quality gate checks the fit at every size decade.
5. **Combine.** Take the cheapest formula that covers every machine's fit at every size.
6. **Round** up: rates to two significant figures, flat costs to multiples of 10 or 50. Signature checks stay at 500,000, matching today's signature allowance.
7. **Check.** Run complete scripts for every operation, operand shape and boundary, plus searches for the worst ones, and compare each machine's slowest against its `T_pre`. A schedule is accepted only when every machine stays below 1.0.

Formulas stay simple: a cheap path may be over-charged, and a formula changes only when complete scripts exceed the requirement. Machines are admitted by fixed criteria (release builds of supported platforms, idle, full settings), never by their results.

The details of every step, each primitive's fixtures and the acceptance statistics are in [METHODOLOGY.md](METHODOLOGY.md). [BIP 440 Appendix A](https://github.com/jmoik/bips/blob/gsr-full/bip-0440.mediawiki#appendix-a-cost-derivation-methodology) summarizes them.

## Machines

The current dataset, [`e7-20261001-e60ac7e070`](e7-20261001-e60ac7e070/), has full runs on five machines; the Apple M4 Pro is missing from it. A non-Apple ARM64 machine and a low-end home-node device are still needed.

| Machine | OS | Reference `T_pre` | Slowest existing workload |
|---|---|---|---|
| Apple M1 Pro | macOS | 2.26 s | repeated RIPEMD160 |
| Intel i5-12500 | Linux | 2.73 s | signature checks |
| AMD Ryzen 9 9950X | Windows | 1.60 s | signature checks |
| Intel i7-7700 | Linux | 3.48 s | repeated HASH256 (no SHA-NI) |
| AMD Ryzen 5 3600 | Linux | 3.20 s | signature checks |

## Reproduce

On each machine, from a checkout of the `gsr` branch (45–60 minutes):

    python3 dev/varops/primitive-calibration/run_calibration.py --output varop-calibration-<machine>-full.json

Put the artifacts in a dataset folder, then fit and render (Python 3, standard library only):

    python3 fit_calibrations.py <dataset>/varop-calibration-*.json --source-root <gsr checkout>
    python3 render_joint_calibration.py <dataset>/joint-calibration.json \
        --output 0.4.0/joint-calibration.html --source-root <gsr checkout>
    python3 -m unittest test_calibration_pipeline

## Contents

| Path | |
|---|---|
| `e7-20261001-e60ac7e070/` | Current dataset: five machines, 7 epochs. |
| `e3-20260930-13e9a89dc7/`, `e3-20260930-ea7a71e20e/` | Earlier six-machine datasets, from which the implemented prices come. |
| `0.4.0/joint-calibration.html` | The report. |
| `fit_calibrations.py`, `render_joint_calibration.py`, `restyle_report.py`, `test_calibration_pipeline.py` | Fitting, report and tests. |
| `METHODOLOGY.md` | The full method. |

Each dataset folder holds one artifact per machine with every raw sample, the joint fit, the fit log, a source audit and a README with the run details.
