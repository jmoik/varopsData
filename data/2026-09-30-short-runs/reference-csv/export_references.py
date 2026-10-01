"""Corrected reference exports for recovered reference.csv files, with provenance.

    python3 export_references.py <gsr checkout>

Uses run_calibration.reference_rows from the given checkout, which keeps the per-round
samples by case name.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

D = Path(__file__).resolve().parents[1]
# Where each run left its reference.csv, relative to that machine's calibration checkout
# or results directory.
SOURCES = {
    'm4pro': 'M4 Pro calibration checkout: dev/varops/primitive-calibration/calibration-intermediate/reference.csv',
    'm1pro': 'M1 Pro calibration checkout: dev/varops/primitive-calibration/calibration-intermediate/reference.csv',
    'intel12500': 'i5-12500 calibration checkout: dev/varops/primitive-calibration/calibration-intermediate/reference.csv',
    'i77700': 'i7-7700 results directory: varop-calibration-i77700-3epoch-work/reference.csv',
    'ryzen53600': 'Ryzen 5 3600 results directory: varop-calibration-ryzen53600-3epoch-work/reference.csv',
}


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    gsr = Path(sys.argv[1]).resolve()
    runner = gsr / 'dev/varops/primitive-calibration'
    sys.path.insert(0, str(runner))
    import run_calibration as rc
    commit = subprocess.check_output(['git', '-C', str(gsr), 'rev-parse', '--short=10', 'HEAD'], text=True).strip()
    for machine, source in SOURCES.items():
        csv_path = D / 'reference-csv' / f'{machine}-reference.csv'
        artifact = json.loads((D / f'varop-calibration-{machine}-3epoch.json').read_text())
        digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
        assert digest == artifact['reference']['csv_sha256'], machine
        reference = rc.reference_rows(csv_path)
        assert reference['seconds'] == artifact['reference']['seconds'], machine
        assert reference['worst_case'] == artifact['reference']['worst_case'], machine
        export = dict(
            schema='varop-reference-export-v1',
            provenance=dict(
                artifact=f'varop-calibration-{machine}-3epoch.json',
                recovered_from=source, recovered='2026-09-30',
                csv=csv_path.name, csv_sha256=digest,
                matches_artifact_csv_sha256=True,
                exporter=f'run_calibration.reference_rows at gsr {commit}, keeping per-round samples by case name',
                reason='The artifact export kept only summary rows: per-round rows leave Domain empty and were filtered out.'),
            reference=reference)
        out = D / 'reference-csv' / f'{machine}-reference.json'
        out.write_text(json.dumps(export, indent=2) + '\n')
        samples = sum(r['Record_Type'] == 'sample' for r in reference['raw_rows'])
        print(f'{machine:12s} {reference["seconds"]:.4f} s, {len(reference["raw_rows"])} rows ({samples} samples) -> {out.name}')


if __name__ == '__main__':
    main()
