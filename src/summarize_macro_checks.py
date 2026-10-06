"""Summarize macro checks: python3 src/summarize_macro_checks.py <dir>...

Each dir holds bench_varops --case-filter macro and function CSVs (macro.csv,
function.csv), bench_varops_primitives --only UNROLL samples (unroll.csv.samples.csv)
and run.log with the reference line. Prints the worst ratio per case: block cases
against the same run's slowest pre-v2 case, projected to the full budget; UNROLL
fixtures against their charge, at 40 billion varops per reference."""
import collections, csv, statistics, sys

def rows(path):
    return list(csv.DictReader(l for l in open(path) if not l.startswith('#')))

for d in sys.argv[1:]:
    print(f'== {d}')
    for name in ('macro', 'function'):
        rs = [r for r in rows(f'{d}/{name}.csv') if r['Record_Type'] == 'summary']
        pre = [r for r in rs if r['Domain'] == 'pre-gsr-tapscript-v1' and r['Actual_Termination'] in {'OK', 'SCRIPT_ERR_OK', 'No error'}]
        ref = max(pre, key=lambda r: float(r['Wall_Seconds']))
        refs = float(ref['Wall_Seconds'])
        worst = {}
        for r in rs:
            if r['Domain'] == 'pre-gsr-tapscript-v1' or r['Domain'] == 'raw-schnorr':
                continue
            label = '/'.join(r['Name'].split('/')[3:5])
            # A sampled or budget-limited case is projected to the full budget.
            full = r['Full_Varops_Wall_Seconds']
            scale = float(full) / float(r['Wall_Seconds']) if full else 1.0
            ratio, mx = scale * float(r['Wall_Seconds']) / refs, scale * float(r['Wall_Max_Seconds']) / refs
            if label not in worst or ratio > worst[label][0]:
                worst[label] = (ratio, mx, r['Name'], r['Actual_Termination'], r['Saturation'])
        print(f'  {name}: reference {refs:.3f} s ({ref["Name"].split("/")[1]})')
        for label, (ratio, mx, n, term, sat) in sorted(worst.items(), key=lambda x: -x[1][0]):
            print(f'    {label:55} median {ratio:.3f}x  max round {mx:.3f}x  {term} {sat}')
    ref_ns = float(next(l for l in open(f'{d}/run.log') if l.startswith('reference')).split()[1]) * 1e9
    samples = collections.defaultdict(list)
    for r in rows(f'{d}/unroll.csv.samples.csv'):
        # 40 billion varops take the reference time.
        samples[r['probe']].append(float(r['ns_per_execution']) * 40e9 / ref_ns)
    worst = {}
    for probe, v in samples.items():
        if not probe.startswith('UNROLL/'):
            continue
        parts = probe.split('/')
        shape, charged = parts[1], int(parts[-1].split('#')[0])
        ratio, mx = statistics.median(v) / charged, max(v) / charged
        if shape not in worst or ratio > worst[shape][0]:
            worst[shape] = (ratio, mx, probe, len(v))
    print('  UNROLL fixtures (time at 1.0x the reference / charge):')
    for shape, (ratio, mx, probe, n) in sorted(worst.items(), key=lambda x: -x[1][0]):
        print(f'    {shape:12} median {ratio:.3f}x  max epoch {mx:.3f}x  ({n} epochs) {probe}')
