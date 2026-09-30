# varopsData

Calibration data and analysis for the BIP 440 varops cost model of Tapscript v2 (GSR).

The report is [`0.4.0/joint-calibration.html`](0.4.0/joint-calibration.html). The measurement tool, the
cost model and its method live on the `gsr` branch of [jmoik/bitcoin](https://github.com/jmoik/bitcoin/tree/gsr):
`dev/varops/primitive-calibration/` (`run_calibration.py`, `costing-methodology.md`, `varops-primitives.md`)
and `src/script/varops.h`.

## Contents

| Path | |
|---|---|
| `e3-20260930-ea7a71e20e/` | Current dataset: six machines, 3 epochs, gsr `ea7a71e20e`. Per-machine artifacts, joint fit, fit log and a README with the run details. |
| `fit_calibrations.py` | Fits each machine and takes the envelope of the machine fits as the pricing basis, then rounds it to the candidate schedule. |
| `render_joint_calibration.py` | Builds the report from a joint fit; `restyle_report.py` applies the page style. |
| `test_calibration_pipeline.py` | Tests for fitting, rounding and rendering. |

Datasets are named `e<epochs>-<date>-<gsr commit>`.

## Pipeline

Measure on each machine, in a checkout of the `gsr` branch:

    python3 dev/varops/primitive-calibration/run_calibration.py --reference-epochs 3 --epochs 3 \
        --sample-ms 5 --copy-sample-ms 50 --output varop-calibration-<machine>-3epoch.json

Collect the artifacts in a new dataset folder, then fit and render:

    python3 fit_calibrations.py <dataset>/varop-calibration-*.json --source-root <gsr checkout>
    python3 render_joint_calibration.py <dataset>/joint-calibration.json \
        --output 0.4.0/joint-calibration.html --source-root <gsr checkout>
    python3 -m unittest test_calibration_pipeline

`--source-root` checks the source hashes recorded in each artifact against their Git commits.

## History

Until 2026-04-10 this repository collected `bench_varops` worst-case results for the original BIP 440 cost model,
including runs contributed by others. Those CSV files and plots remain in the Git history (`906848d`).
