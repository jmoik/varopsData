"""DIVCORE fixtures against DIV(s, v) = 510 s + 33 s v at 1.0x the run's reference.

Usage: analyze_div.py <machine dir>...; s counts quotient rows as fit_calibrations.py does."""
import collections, csv, statistics, sys
for d in sys.argv[1:]:
    ref = float(open(f'{d}/divcore.csv').read().split('Reference_Script_Evaluation_Seconds:')[1].split()[0])
    s = collections.defaultdict(list)
    for r in csv.DictReader(open(f'{d}/divcore.csv.samples.csv')):
        s[r['probe']].append(float(r['ns_per_execution']))
    rate = 40e9 / (ref * 1e9)
    rows = []
    for probe, v in s.items():
        _, aw, bw, seed, op, pattern = probe.split('/')
        aw, bw = int(aw), int(bw)
        steps = max(1, aw - bw + 2)
        charge = 510 * steps + 33 * steps * bw
        rows.append((statistics.median(v) * rate / charge, aw, bw, op, pattern, seed))
    by = collections.defaultdict(list)
    for r in rows: by[r[4]].append(r)
    print(f'== {d}  reference {ref:.3f}s')
    for pattern, rs in sorted(by.items()):
        rs.sort(reverse=True)
        top = ', '.join(f'{r[1]}/{r[2]} {r[3]} {r[0]:.2f}x' for r in rs[:4])
        print(f'  {pattern:11} worst {rs[0][0]:.2f}x of DIV charge; top: {top}')
    norm = {(r[1], r[2], r[3]): r[0] for r in rows if r[4] == 'normalized' and r[5] == '17'}
    pairs = [(r[0] / norm[(r[1], r[2], r[3])], r[1], r[2], r[3]) for r in rows if r[4] == 'add-back' and (r[1], r[2], r[3]) in norm]
    pairs.sort(reverse=True)
    print('  add-back / normalized, largest: ' + ', '.join(f'{a}/{b} {op} {x:.2f}' for x, a, b, op in pairs[:5]))
