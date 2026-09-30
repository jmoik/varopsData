# Recovered pre-v2 reference samples

The artifacts in the parent directory record only the reference summary rows: `bench_varops` leaves
`Domain` empty on its per-round sample rows, and the exporter kept rows by `Domain`. This directory adds
the raw `reference.csv` of each run, recovered from the machines' work directories on 2026-09-30, and a
corrected export. The artifacts themselves are unchanged.

| Machine | Recovered from | CSV matches artifact `reference.csv_sha256` |
|---|---|---|
| M4 Pro | calibration checkout, `dev/varops/primitive-calibration/calibration-intermediate/reference.csv` | yes |
| M1 Pro | calibration checkout, same path | yes |
| i5-12500 | calibration checkout, same path | yes |
| i7-7700 | results directory, `varop-calibration-i77700-3epoch-work/reference.csv` | yes |
| Ryzen 5 3600 | results directory, `varop-calibration-ryzen53600-3epoch-work/reference.csv` | yes |
| Ryzen 9 9950X | not recovered yet (Windows) | — |

`<machine>-reference.json` is the corrected export: `run_calibration.reference_rows` keeping the per-round
samples by case name, with provenance. It reproduces each artifact's reference time and worst case exactly.
Regenerate with `python3 export_references.py <gsr checkout>` (gsr 4e7b02bd8e or later).

Per-round spread of the worst reference case (3 rounds, one run each):

| Machine | Worst case | Min–max (s) | Spread |
|---|---|---|---|
| M4 Pro (loaded) | OP_RIPEMD160 | 1.702–1.945 | 14.3% |
| M1 Pro | OP_RIPEMD160 | 2.253–2.259 | 0.3% |
| i5-12500 | OP_CHECKSIGADD | 2.735–2.739 | 0.2% |
| i7-7700 | OP_HASH256 | 3.482–3.485 | 0.1% |
| Ryzen 5 3600 | OP_CHECKSIGADD | 3.198–3.199 | 0.0% |

Within a run the reference is stable on quiet machines, while between runs it moved by −10% (Ryzen 5) and
−13% (9950X). A single reference pass per run cannot detect that; later runs measure it before and after
the primitives.
