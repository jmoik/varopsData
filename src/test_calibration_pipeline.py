"""Schema/feature regression tests; synthetic timings are not calibration evidence."""
import copy
import hashlib
import json
from collections import defaultdict
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fit_calibrations as calibration
from fit_calibrations import maximum_coefficients


def artifact(model_id=calibration.MODEL_ID):
    shared = ['F/nop/256', 'READ/zero/16', 'ARITH/sub/2/equal/borrow-chain',
              'MOVE/8/empty', 'MULCORE/2/1/ones', 'DIVCORE/9/2/div/normalized',
              'H256/core/32', 'H160/32', 'H1/32', 'SIG/32', 'TWEAK',
              'SELECT/weight-scan/collated/128/131', 'UNROLL/push-520/15296/3999908/36744848']
    if model_id == 'producer-normalize-v1':
        probes = shared + ['PREP/9/spare', 'PRODUCE/grow/9', 'NORMALIZE/scalar/256', 'BIT/invert/16']
        manifest = [dict(probe='PRODUCE/grow/9', kind='produce', items='2', bytes='13', normalize_bytes='0'),
                    dict(probe='NORMALIZE/scalar/256', kind='normalize', items='1', bytes='0', normalize_bytes='2')]
    else:
        probes = shared + ['READ/convert/9', 'WRITE/grow/9', 'WRITE/scalar/256', 'ARITH/invert/16']
        manifest = [dict(probe='WRITE/grow/9', kind='write', items='2', bytes='13', normalize_bytes='0'),
                    dict(probe='WRITE/scalar/256', kind='write', items='1', bytes='2', normalize_bytes='2')]
    rate = 20.0
    return dict(schema='varop-calibration-v1', model_id=model_id,
                normalization=dict(varops_per_nanosecond=rate,
                                   target_fraction_of_local_pre_v2_worst=1.0,
                                   local_pre_v2_worst_seconds=2.0),
                machine=dict(cpu='synthetic'), source_sha256={}, head='synthetic',
                producer_manifest=manifest,
                primitive_samples=[dict(probe=probe, epoch=epoch, ns_per_execution=100 + epoch,
                                        normalized_varops_per_execution=(100 + epoch)*rate)
                                   for probe in probes for epoch in range(3)])


class CalibrationPipelineTests(unittest.TestCase):
    def test_maximum_coefficients(self):
        a = {'PRODUCE': (100, 6), 'DIVCORE': (10, 0, 1, 4)}
        b = {'PRODUCE': (200, 3), 'DIVCORE': (20, 0, 0, 2)}
        combined = maximum_coefficients([a, b])
        self.assertEqual(combined, {'PRODUCE': (200, 6), 'DIVCORE': (20, 0, 1, 4)})
        self.assertEqual(maximum_coefficients([a, b, a]), combined)
        for n in (0, 1, 100, 4000000):
            self.assertGreaterEqual(combined['PRODUCE'][0] + combined['PRODUCE'][1]*n,
                                    max(m['PRODUCE'][0] + m['PRODUCE'][1]*n for m in (a, b)))
        with self.assertRaisesRegex(ValueError, 'matching feature bases'):
            maximum_coefficients([a, {'PRODUCE': (100, 6)}])

    def test_envelope(self):
        def points(family, sizes):
            return [dict(machine='m', group='g', x=x, c=c, v=v, included=True) for x, c, v in sizes]
        grid = [(q, q, q * v, f'v={v}') for q in (0, 1, 2, 10, 100, 1000) for v in (1, 2, 4, 64, 1024)]
        # A large flat on one machine and a larger divisor-word rate on another: at one
        # divisor word the envelope covers the sum, so its flat may drop below the maximum.
        a = {'DIVCORE': (3000, 400, 100, 40), 'PRODUCE': (2000, 4), 'H1': (300, 20)}
        b = {'DIVCORE': (100, 570, 1000, 43), 'PRODUCE': (500, 6), 'H1': (20, 25)}
        maximum = maximum_coefficients([a, b])
        env = calibration.envelope_coefficients('DIVCORE', [a, b], [dict(machine='m', group=g, x=x, c=c, v=v, included=True) for x, c, v, g in grid])
        self.assertLess(env[0], maximum['DIVCORE'][0])
        def charge(m, q, v):
            return m[0] + m[1] * q + m[2] * v + m[3] * q * v
        for q in (0, 1, 7, 10**6):
            for v in (1, 3, 10**6):
                self.assertGreaterEqual(charge(env, q, v) * (1 + 1e-9), max(charge(m['DIVCORE'], q, v) for m in (a, b)))
        # Sizes from zero, unbounded: covering both needs the largest flat and the largest rate.
        env = calibration.envelope_coefficients('PRODUCE', [a, b], points('PRODUCE', [(n, 1, n) for n in (0, 8, 4096)]))
        self.assertEqual(tuple(round(x, 6) for x in env), (2000, 6))
        # SHA1 is bounded to 520 bytes: one or two hashed blocks up to 576 bytes.
        env = calibration.envelope_coefficients('H1', [a, b], points('H1', [(n, 1, calibration.hash_span(n)) for n in (0, 64, 520)]))
        for n in (0, 55, 56, 520):
            span = calibration.hash_span(n)
            self.assertGreaterEqual((env[0] + env[1] * span) * (1 + 1e-9), max(m['H1'][0] + m['H1'][1] * span for m in (a, b)))
        self.assertLess(env[0], maximum['H1'][0])
        # HASH covers every machine's curve of every hash function, each over its own sizes:
        # SHA256 unbounded, SHA1 to 520 bytes. A higher SHA1 rate need not raise the rate.
        a.update(H256=(450, 37), H160=(240, 39))
        b.update(H256=(250, 38), H160=(50, 40))
        pts = [dict(machine='m', family=f, group='g', x=n, c=1, v=calibration.hash_span(n), included=True)
               for f, sizes in (('H256', (0, 64, 4096, 10**6)), ('H160', (0, 64, 520)), ('H1', (0, 64, 520)))
               for n in sizes]
        env = calibration.envelope_coefficients('HASH', [a, b], pts, calibration.HASHES)
        for part, limit in (('H256', 10**7), ('H160', 520), ('H1', 520)):
            for n in (0, 55, 56, 519, 520, limit):
                span = calibration.hash_span(n)
                self.assertGreaterEqual((env[0] + env[1] * span) * (1 + 1e-9),
                                        max(m[part][0] + m[part][1] * span for m in (a, b)))
        self.assertLess(env[1], 40)

    def test_schedule_rounding(self):
        # Flats round up to a multiple of 50, rates up to a whole varop.
        # Hashes are priced per byte of the block-padded length: 49.0253 per byte is charged as 50.
        self.assertEqual(calibration.rounded_candidate('H256', [3043.03, 49.0253]), [3050, 50])
        # Every rate is a whole number of varops per byte of W(n): PREP's 0.0301 is charged as 1.
        self.assertEqual(calibration.rounded_candidate('PREP', [241.381, .0301]), [250, 1])
        self.assertEqual(calibration.rounded_candidate('PREP', [241.381, 1.3]), [250, 2])
        self.assertEqual(calibration.formulas('PREP', [250, 1], candidate=True), '250 + 1 × W(n)')
        # MUL and DIV are priced per byte of W(n) and W(m) and per unit of W(n) × W(m),
        # each rate a whole varop: 50.9444 per word product is 0.796 per pair of bytes, charged as 1.
        self.assertEqual(calibration.rounded_candidate('DIVCORE', [1280.4, 490.0, 101.9, 50.9444]), [1300, 496, 104, 64])
        self.assertEqual(calibration.formulas('DIVCORE', [1300, 496, 104, 64], candidate=True),
                         '1300 + 62 × Q(n, m) + 13 × W(m) + 1 × Q(n, m) × W(m)')
        self.assertEqual(calibration.rounded_candidate('DIVCORE', [3997.26, 0, 0, 158.884]), [4000, 0, 0, 192])
        self.assertEqual(calibration.rounded_candidate('MULCORE', [338.305, 4.32306, 109.887, 28.2557]), [350, 8, 112, 64])
        self.assertEqual(calibration.formulas('MULCORE', [650, 8, 112, 64], candidate=True),
                         '650 + 1 × W(n) + 14 × W(m) + 1 × W(n) × W(m)')
        # Byte rates are charged per byte of W(n) >= n: 6.40246 per byte of n is charged as 7 × W(n).
        self.assertEqual(calibration.rounded_candidate('PRODUCE', [2130.63, 6.40246]), [2150, 7])
        self.assertEqual(calibration.formulas('PRODUCE', [2150, 7], candidate=True), '2150 + 7 × W(n)')
        self.assertEqual(calibration.rounded_candidate('SELECT', [3494.87, 1643.3]), [3500, 1644])
        self.assertEqual(calibration.rounded_candidate('READ', [81.6, 1.02]), [100, 2])
        self.assertEqual(calibration.rounded_candidate('TWEAK', [168855, 0]), [500000, 0])
        self.assertEqual(calibration.formulas('H256', [192, 39], candidate=True), '192 + 39 × H(n)')
        self.assertEqual(calibration.formulas('HASH', [300, 40], candidate=True), '300 + 40 × H(n)')
        self.assertEqual(calibration.rounded_candidate('NORMALIZE', [187.2, 0]), [200, 0])
        self.assertEqual(calibration.formulas('NORMALIZE', [200, 0], candidate=True), '200')
        # Zero stays zero and exact multiples keep their value.
        self.assertEqual(calibration.rounded_candidate('F', [300, 0]), [300, 0])
        self.assertEqual(calibration.rounded_candidate('DIVCORE', [1000, 120, 8, 64]), [1000, 120, 8, 64])
        self.assertEqual(calibration.rounded_candidate('SIG', [490446, 0]), [500000, 0])
        self.assertEqual([calibration.round_coefficient(v) for v in (0, 0.0012, 9.2, 10, 10.1, 22.8, 60.04, 614.3)],
                         [0, 1, 10, 10, 11, 23, 61, 615])
        self.assertEqual([calibration.round_flat(v) for v in (0, 0.2, 41.4, 50, 53.7, 142.2, 602.465, 1147.94, 1315.57)],
                         [0, 50, 50, 50, 100, 150, 650, 1150, 1350])

    def test_candidate_charge_uses_pricing_units(self):
        c = {'PRODUCE': [680, 7], 'PREP': [180, 1], 'HASH': [280, 38], 'MOVE': [180, 23], 'SIG': [500000, 0],
             'MULCORE': [340, 5, 110, 29], 'F': [310, 0]}
        # WRITE and READ are charged on W(n), hashes on the block span.
        self.assertEqual(calibration.candidate_charge('WRITE', dict(x=9, group='g'), dict(WRITE=[680, 7])), 680 + 7 * 16)
        self.assertEqual(calibration.candidate_charge('PRODUCE', dict(x=9, group='g'), c), 680 + 7 * 16)
        self.assertEqual(calibration.candidate_charge('PREP', dict(x=9, group='spare'), c), 180 + 16)
        self.assertEqual(calibration.candidate_charge('HASH', dict(x=56, group='g'), c), 280 + 38 * 128)
        self.assertEqual(calibration.candidate_charge('MOVE', dict(x=3, group='g'), c), 180 + 69)
        self.assertEqual(calibration.candidate_charge('SIG', dict(x=32, group='g'), c), 500000 + 280 + 38 * 128)
        self.assertEqual(calibration.candidate_charge('F', dict(x=1, group='g'), c), 310)
        mul = dict(x=4, c=4, v=8, group='v=2')
        self.assertEqual(calibration.candidate_charge('MULCORE', mul, c), 340 + 5 * 4 + 110 * 2 + 29 * 8)

    def test_diagnostics_flag_gate_failures_and_undercharges(self):
        # One machine whose fit is exact except one fixture measured at twice its fit.
        pts = [dict(family='MOVE', machine='m', group='g', x=x, c=1, v=x, y=100 + 10 * x, included=True,
                    label=f'MOVE/{x}/g') for x in (1, 2, 3, 4, 5, 6, 7, 8, 9)]
        pts[4]['y'] *= 2
        gate = calibration.quality_gate({'MOVE': pts}, [{'MOVE': (100, 10)}], ['m'], ['M'])
        self.assertEqual((gate['gate_bins'], gate['bins_by_machine'], len(gate['gate_failures'])), (1, {'M': 1}, 1))
        failure = gate['gate_failures'][0]
        self.assertAlmostEqual(failure['max_above_fit'], 2)
        self.assertEqual(failure['max_below_fit'], 1)
        # The one-sided RMS counts the fixture above the fit among all nine.
        self.assertAlmostEqual(failure['rms_above_fit'], 2 ** (1 / 3))
        coverage = calibration.charge_coverage({'MOVE': pts}, {'MOVE': [100, 10]}, ['m'], ['M'])
        self.assertEqual(coverage['checked'], 9)
        self.assertEqual([item['fixture'] for item in coverage['above_charge']], ['MOVE/5/g'])
        self.assertAlmostEqual(coverage['above_charge'][0]['ratio'], 2)
        # A charge at twice the fit covers every fixture.
        coverage = calibration.charge_coverage({'MOVE': pts}, {'MOVE': [200, 20]}, ['m'], ['M'])
        self.assertEqual(coverage['above_charge'], [])
        # Excluded fixtures are neither gated nor flagged.
        pts[4]['included'] = False
        gate = calibration.quality_gate({'MOVE': pts}, [{'MOVE': (100, 10)}], ['m'], ['M'])
        coverage = calibration.charge_coverage({'MOVE': pts}, {'MOVE': [100, 10]}, ['m'], ['M'])
        self.assertEqual((gate['gate_failures'], coverage['above_charge'], coverage['checked']), ([], [], 8))

    def test_failed_conditions_are_not_admitted(self):
        # Only epoch noise decides; a recorded load failure no longer does.
        failed = dict(file='c.json', epoch_noise=0.0151, conditions=None)
        machines = [dict(file='a.json', epoch_noise=None, conditions=None),
                    dict(file='b.json', epoch_noise=0.015,
                         conditions=dict(repeat_required=True, problems=['median one-minute load 3.09 exceeds 1.5'])),
                    failed]
        self.assertEqual(calibration.failed_conditions(machines), [failed])

    def test_epoch_noise(self):
        # Two aliases share a probe name within each pass and are kept apart by their order.
        samples = [dict(probe=probe, epoch=epoch, ns_per_execution=ns)
                   for epoch, times in enumerate([(100, 50), (101, 50), (99, 50), (100, 50), (130, 50)])
                   for probe, ns in (('A', times[0]), ('A', times[1]))]
        samples += [dict(probe='B', epoch=epoch, ns_per_execution=ns) for epoch, ns in enumerate([10, 11, 12, 13, 14])]
        # A: 1% (the 130 outlier is discarded); its alias: 0%; B: 1/12.
        self.assertAlmostEqual(calibration.epoch_noise(samples), 0.01)
        self.assertIsNone(calibration.epoch_noise(samples[:4]))

    def test_source_verification_uses_recorded_commit(self):
        raw = b'original\nsource\n'
        head = 'a' * 40
        machines = [dict(file='windows.json', head=head,
                         source_sha256={'src\\example.cpp': hashlib.sha256(raw.replace(b'\n', b'\r\n')).hexdigest()})]
        with patch.object(calibration.subprocess, 'check_output', return_value=raw) as read:
            result = calibration.check_source_snapshots(Path('/repo'), machines)
        self.assertEqual(result['unmatched'], [])
        read.assert_called_once_with(['git', '-C', '/repo', 'show', head + ':src/example.cpp'])

    def test_source_verification_preserves_real_mismatch(self):
        machines = [dict(file='windows.json', head='a' * 40,
                         source_sha256={'src/example.cpp': hashlib.sha256(b'changed\n').hexdigest()})]
        with patch.object(calibration.subprocess, 'check_output', return_value=b'original\n'):
            result = calibration.check_source_snapshots(Path('/repo'), machines)
        self.assertEqual(len(result['unmatched']), 1)
        self.assertEqual(result['checked'], 1)

    def test_fixture_identity_ignores_unroll_charge(self):
        self.assertEqual(calibration.fixture_identity(dict(family='UNROLL', label='UNROLL/nop-1/2048/1028/729844')),
                         calibration.fixture_identity(dict(family='UNROLL', label='UNROLL/nop-1/2048/1028/625496')))
        self.assertNotEqual(calibration.fixture_identity(dict(family='READ', label='READ/compare/25')),
                            calibration.fixture_identity(dict(family='READ', label='READ/compare/26')))

    def test_bench_sources_match(self):
        raw = b'bench\n'
        bench = dict(head='b' * 40, source_sha256={'bench/bench_varops.cpp': hashlib.sha256(raw).hexdigest()})
        self.assertIsNone(calibration.check_bench_sources([dict(file='old.json')]))
        # Commits that differ only outside the benchmarks are equivalent.
        with patch.object(calibration.subprocess, 'check_output', return_value=raw):
            result = calibration.check_bench_sources([dict(file='a.json', bench=bench),
                                                      dict(file='b.json', bench=dict(bench, head='c' * 40))])
        self.assertEqual((result['unmatched'], result['checked']), ([], 2))
        # A Windows checkout's CRLF line endings are the same sources.
        windows = dict(bench, source_sha256={'bench/bench_varops.cpp': hashlib.sha256(b'bench\r\n').hexdigest()})
        with patch.object(calibration.subprocess, 'check_output', return_value=raw):
            result = calibration.check_bench_sources([dict(file='a.json', bench=bench), dict(file='w.json', bench=windows)])
        self.assertEqual(result['unmatched'], [])
        with self.assertRaises(ValueError), patch.object(calibration.subprocess, 'check_output', return_value=raw):
            calibration.check_bench_sources([dict(file='a.json', bench=bench),
                                             dict(file='b.json', bench=dict(bench, source_sha256={'bench/bench_varops.cpp': '0' * 64}))])
        with self.assertRaises(ValueError):
            calibration.check_bench_sources([dict(file='a.json', bench=bench), dict(file='old.json')])
        # A reviewed file may differ between runs; its versions are recorded.
        other = dict(bench, source_sha256={'bench/bench_varops.cpp': '0' * 64})
        with patch.object(calibration.subprocess, 'check_output', return_value=raw):
            result = calibration.check_bench_sources([dict(file='a.json', bench=bench), dict(file='b.json', bench=other)],
                                                     reviewed=['bench/bench_varops.cpp'])
        self.assertEqual(result['reviewed_differences'],
                         {'bench/bench_varops.cpp': sorted([hashlib.sha256(raw).hexdigest(), '0' * 64])})

    def load(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'synthetic.json'
            path.write_text(json.dumps(data))
            return calibration.load_calibration(path)

    def test_pricing_targets_a_fraction_of_the_reference(self):
        # 40e9 varops over 0.9 × the 2 s reference.
        self.assertEqual(calibration.TARGET_FRACTION, 0.9)
        points, meta = self.load(artifact())
        self.assertAlmostEqual(meta['varops_per_nanosecond'], 40 / (2.0 * 0.9))

    def test_all_families_and_median(self):
        points, meta = self.load(artifact())
        self.assertEqual({p['family'] for p in points}, set(calibration.FAMILIES[calibration.MODEL_ID]))
        self.assertEqual(meta['model_id'], calibration.MODEL_ID)
        by_label = {p['label']: p for p in points}
        # A grown value is two WRITE events: its size is the average per event.
        self.assertEqual(by_label['WRITE/grow/9']['x'], 6.5)
        self.assertAlmostEqual(by_label['WRITE/grow/9']['y'], 1010 / 0.9)
        self.assertEqual((by_label['WRITE/scalar/256']['x'], by_label['WRITE/scalar/256']['group']), (2, 'scalar'))
        self.assertEqual((by_label['READ/convert/9']['x'], by_label['READ/convert/9']['group']), (16, 'convert'))
        self.assertIn('borrow-chain', by_label['ARITH/sub/2/equal/borrow-chain']['group'])
        self.assertEqual((by_label['ARITH/invert/16']['x'], by_label['ARITH/invert/16']['group']), (16, 'invert'))

    def test_recorded_model_families(self):
        # The recorded runs measured READ, WRITE and ARITH in parts.
        points, meta = self.load(artifact('producer-normalize-v1'))
        self.assertEqual({p['family'] for p in points}, set(calibration.FAMILIES['producer-normalize-v1']))
        self.assertEqual(meta['model_id'], 'producer-normalize-v1')
        by_name = {p['family']: p for p in points}
        self.assertEqual(by_name['PRODUCE']['x'], 6.5)
        self.assertAlmostEqual(by_name['PRODUCE']['y'], 1010 / 0.9)
        self.assertEqual(by_name['NORMALIZE']['v'], 8)

    def test_composed_primitives(self):
        # The recorded parts compose BIP 440's READ, WRITE, ARITH and HASH: READ and WRITE
        # add their parts, ARITH takes the larger of ARITH and BIT, HASH the larger of the
        # three hash functions.
        parts = {'F': (350, 0), 'PREP': (200, 1), 'READ': (90, 2), 'PRODUCE': (800, 8), 'NORMALIZE': (200, 0),
                 'ARITH': (150, 3), 'BIT': (200, 2), 'MOVE': (200, 37),
                 'H256': (300, 38), 'H160': (60, 40), 'H1': (200, 24)}
        priced = calibration.priced_model(parts, 'producer-normalize-v1')
        self.assertEqual(priced, {'F': (350, 0), 'READ': (290, 3), 'WRITE': (1000, 8), 'ARITH': (200, 3),
                                  'MOVE': (200, 37), 'HASH': (300, 40)})
        # The composed price is rounded again: 200 + 90 is charged as 300.
        self.assertEqual(calibration.compose_candidates({k: list(v) for k, v in parts.items()}, 'producer-normalize-v1'),
                         dict(priced, READ=(300, 3)))
        # HASH, a larger-of primitive, is priced as its rounded envelope: the seven-machine
        # envelope 41.45 + 39.96 H(n) is charged 50 + 40 H(n).
        self.assertEqual(calibration.rounded_candidate('HASH', [41.4478, 39.9633]), [50, 40])
        # A model that measures READ, WRITE and ARITH directly composes only HASH.
        direct = {'READ': (300, 3), 'WRITE': (1000, 8), 'ARITH': (200, 3),
                  'H256': (300, 38), 'H160': (60, 40), 'H1': (200, 24)}
        self.assertEqual(calibration.priced_model(direct, calibration.MODEL_ID),
                         {'READ': (300, 3), 'WRITE': (1000, 8), 'ARITH': (200, 3), 'HASH': (300, 40)})
        charged = calibration.charged_by('producer-normalize-v1')
        self.assertEqual({part: charged[part] for part in ('PREP', 'READ', 'PRODUCE', 'NORMALIZE', 'ARITH', 'BIT',
                                                           'H256', 'H160', 'H1')},
                         {'PREP': 'READ', 'READ': 'READ', 'PRODUCE': 'WRITE', 'NORMALIZE': 'WRITE',
                          'ARITH': 'ARITH', 'BIT': 'ARITH', 'H256': 'HASH', 'H160': 'HASH', 'H1': 'HASH'})
        # A part's fixture is checked against the price of the primitive that charges it.
        pts = [dict(family='NORMALIZE', machine='m', group='scalar', x=2, c=1, v=8, y=1100, included=True,
                    label='NORMALIZE/scalar/256')]
        coverage = calibration.charge_coverage({'NORMALIZE': pts}, {'WRITE': [1000, 8]}, ['m'], ['M'], charged)
        self.assertEqual(coverage['checked'], 1)
        self.assertEqual(coverage['above_charge'][0]['primitive'], 'WRITE')
        self.assertEqual(coverage['above_charge'][0]['charged_varops'], 1000 + 8 * 8)

    def test_fixed_groups(self):
        fixed = calibration.fixture(dict(probe='F/else/256', ns_per_execution=2570))
        self.assertEqual((fixed['group'], fixed['x'], fixed['y'], fixed['included']), ('else', 257, 10, True))
        skipped = calibration.fixture(dict(probe='F/skipped/256', ns_per_execution=2560))
        self.assertEqual((skipped['x'], skipped['y'], skipped['included']), (256, 10, False))

    def test_divcore_rows(self):
        rows = {}
        for u, v in ((9, 2), (1, 1), (9, 1), (1, 2), (1, 9)):
            point = calibration.fixture(dict(probe=f'DIVCORE/{u}/{v}/17/DIV/top-one', ns_per_execution=1))
            rows[u, v] = (point['x'], point['c'], point['v'])
        # Words by which the dividend exceeds the divisor; none for a shorter dividend.
        self.assertEqual(rows, {(9, 2): (7, 7, 14), (1, 1): (0, 0, 0), (9, 1): (8, 8, 8),
                                (1, 2): (0, 0, 0), (1, 9): (0, 0, 0)})

    def test_lifetime_checks(self):
        # A numeric result of 9 bytes: one WRITE event, then READ and WRITE at its
        # 16-byte word span.
        data = artifact()
        probe = 'PRODUCER_CHECK/numeric/tight/9'
        data['producer_manifest'].append(dict(probe=probe, kind='numeric', items='1', bytes='9', normalize_bytes='9'))
        recorded = data['normalization']['varops_per_nanosecond']
        data['primitive_samples'] += [dict(probe=probe, epoch=epoch, ns_per_execution=20,
                                           normalized_varops_per_execution=20 * recorded) for epoch in range(3)]
        model = {'WRITE': (100, 2), 'READ': (50, 1)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'synthetic.json'
            path.write_text(json.dumps(data))
            checked, above = calibration.lifetime_checks(path, model, 'm')
        self.assertEqual(checked, 1)
        composed = (100 + 2 * 9) + (50 + 16) + (100 + 2 * 16)
        self.assertAlmostEqual(above[0]['composed_varops'], composed)
        self.assertAlmostEqual(above[0]['ratio'], 20 * 40 / (2.0 * 0.9) / composed)

    def test_incomplete_epochs_rejected(self):
        data = artifact()
        data['primitive_samples'].pop()
        with self.assertRaisesRegex(ValueError, 'incomplete epochs'):
            self.load(data)

    def test_wrong_normalization_rejected(self):
        data = artifact()
        data['normalization']['varops_per_nanosecond'] *= 0.9
        with self.assertRaisesRegex(ValueError, 'inconsistent normalization'):
            self.load(data)

    def test_unknown_family_rejected(self):
        data = artifact()
        data['primitive_samples'][0]['probe'] = 'COPY/isolated/8'
        with self.assertRaisesRegex(ValueError, 'unknown primitive'):
            self.load(data)

    def test_unknown_model_rejected(self):
        data = artifact()
        data['model_id'] = 'different-model'
        with self.assertRaisesRegex(ValueError, 'unknown costing model'):
            self.load(data)

    def test_shared_fit_and_policy(self):
        points, _ = self.load(artifact())
        series = defaultdict(list)
        for p in points:
            for machine in ('a', 'b'):
                point = copy.deepcopy(p)
                point['machine'] = machine
                series[p['family']].append(point)
        fits = calibration.fit_all(series, 100)
        self.assertEqual(set(fits), set(calibration.FAMILIES[calibration.MODEL_ID]) - calibration.CHECKS)
        self.assertEqual(len(fits['DIVCORE']), 4)

    def test_divcore_per_row_term(self):
        # Many rows at a small divisor separate the per-row term from the cells.
        points = []
        for u, v in ((1026, 1), (1026, 2), (34, 2), (8, 4), (1032, 8), (80, 16), (1088, 64), (128, 64)):
            point = calibration.fixture(dict(probe=f'DIVCORE/{u}/{v}/17/DIV/top-one', ns_per_execution=1))
            point['y'] = 1000 + 500 * point['c'] + 100 * calibration.mul_rows(point) + 50 * point['v']
            point['machine'] = 'a'
            points.append(point)
        fixed, step, divisor, cell = calibration.fit_divcore(points, 100)
        self.assertAlmostEqual(step, 500, delta=5)
        self.assertAlmostEqual(cell, 50, delta=1)

    def test_divcore_patterns_enveloped(self):
        # A value pattern with a thirteenth of the fixtures and a 1.4x dearer cell sets
        # the machine's cell, which a joint fit pulls toward the cheaper patterns.
        points = []
        for pattern, seeds, cell in (('normalized', 4, 30), ('top-one', 4, 30), ('padded', 4, 30), ('add-back', 1, 42)):
            for u, v in ((1026, 1), (34, 2), (1032, 8), (80, 16), (1088, 64), (128, 64)):
                for seed in range(seeds):
                    point = calibration.fixture(dict(probe=f'DIVCORE/{u}/{v}/{seed}/DIV/{pattern}', ns_per_execution=1))
                    point['y'] = 1000 + 500 * point['c'] + cell * point['v']
                    point['machine'] = 'a'
                    points.append(point)
        joint = calibration.fit_divcore(points, 100)
        curve, patterns = calibration.fit_patterns('DIVCORE', points, 100)
        self.assertEqual(set(patterns), {'DIV/normalized', 'DIV/top-one', 'DIV/padded', 'DIV/add-back'})
        self.assertAlmostEqual(curve[3], 42, delta=0.5)
        self.assertGreater(curve[3], joint[3] + 1)
        self.assertTrue(all(calibration.predict('DIVCORE', p, curve, {}) >= 0.999 * p['y'] for p in points))

    def test_one_pattern_outside_div_and_mul(self):
        # Path groups of other families are different operations, weighted in one fit.
        point = dict(family='WRITE', group='churn', label='WRITE/churn/2000000')
        self.assertEqual(calibration.operand_pattern(point), '')
        self.assertEqual(calibration.operand_pattern(dict(family='MULCORE', group='v=2', label='MULCORE/9/2/ones')), 'ones')
        self.assertEqual(calibration.operand_pattern(dict(family='SELECT', group='outputs/collated',
                                                          label='SELECT/outputs/collated/8/16/80')), 'outputs')

    def test_dearest_operand_values(self):
        # At each size the fit sees the dearer of an operation's value variants, under
        # the operation's group; other operations and sizes are kept as they are.
        def point(group, x, y):
            return dict(family='ARITH', group=group, x=x, y=y, machine='a', label=f'ARITH/{group}/{x}')
        points = [point('add/equal/carry-chain', 8, 300), point('add/equal/borrow-chain', 8, 200),
                  point('add/equal/carry-chain', 64, 400), point('add/equal/borrow-chain', 64, 500),
                  point('up/1', 8, 100), point('up/63', 8, 120), point('invert', 8, 90)]
        kept = {(p['group'], p['x']): p['y'] for p in calibration.dearest_values(points)}
        self.assertEqual(kept, {('add/equal', 8): 300, ('add/equal', 64): 500, ('up', 8): 120, ('invert', 8): 90})
        move = [dict(family='MOVE', group=g, x=9, y=y, machine='a', label='') for g, y in (('empty', 50), ('nonempty', 60))]
        self.assertEqual([(p['group'], p['y']) for p in calibration.dearest_values(move)], [('rotate', 60)])

    def test_mulcore_artifact(self):
        # The frozen model records MULCORE in place of the MUL row kernel.
        data = artifact()
        rate = data['normalization']['varops_per_nanosecond']
        for probe in ('MULCORE/1/1/ones', 'MULCORE/9/2/random'):
            data['primitive_samples'] += [dict(probe=probe, epoch=epoch, ns_per_execution=100 + epoch,
                                               normalized_varops_per_execution=(100 + epoch) * rate)
                                          for epoch in range(3)]
        points, _ = self.load(data)
        self.assertIn('MULCORE', {p['family'] for p in points})
        row = next(p for p in points if p['label'] == 'MULCORE/9/2/random')
        self.assertEqual((row['x'], row['group'], row['c'], row['v']), (9, 'v=2', 9, 18))
        series = defaultdict(list)
        for p in points:
            series[p['family']].append(dict(p, machine='a'))
        fits = calibration.fit_all(series, 100)
        self.assertEqual(len(fits['MULCORE']), 4)

    def test_mulcore_terms(self):
        # Rows over several shorter-operand widths separate the per-row and per-cell terms.
        points = []
        for u, v in ((1, 1), (500, 1), (4000, 1), (64, 16), (4096, 16), (1024, 1024), (8192, 256)):
            point = calibration.fixture(dict(probe=f'MULCORE/{u}/{v}/ones', ns_per_execution=1))
            point['y'] = 1400 + 40 * point['c'] + 42 * point['v']
            point['machine'] = 'a'
            points.append(point)
        fixed, row, _, cell = calibration.fit_divcore(points, 100)
        self.assertAlmostEqual(row, 40, delta=1)
        self.assertAlmostEqual(cell, 42, delta=0.5)

    def test_mulcore_row_term(self):
        # One row call per shorter-operand limb separates from the longer-operand and cell terms.
        points = []
        for u, v in ((1, 1), (500, 1), (4000, 1), (16, 16), (64, 16), (4096, 16), (32, 32),
                     (1024, 1024), (8192, 256), (256, 256), (65536, 4)):
            point = calibration.fixture(dict(probe=f'MULCORE/{u}/{v}/ones', ns_per_execution=1))
            point['y'] = 300 + 2 * point['c'] + 100 * v + 28 * point['v']
            point['machine'] = 'a'
            points.append(point)
        fixed, longer, rows, cell = calibration.fit_mulcore(points, 100)
        self.assertAlmostEqual(rows, 100, delta=5)
        self.assertAlmostEqual(cell, 28, delta=0.5)
        self.assertAlmostEqual(calibration.predict('MULCORE', points[4], (fixed, longer, rows, cell), {}), points[4]['y'],
                               delta=0.02 * points[4]['y'])

    def test_mulcore_envelope_covers_rows(self):
        # The shorter operand is at most as long as the longer one, so a per-row
        # charge can be covered by the longer-operand term.
        models = [{'MULCORE': (100, 0, 50, 10)}, {'MULCORE': (100, 60, 0, 10)}]
        points = [dict(calibration.fixture(dict(probe=f'MULCORE/{u}/{v}/ones', ns_per_execution=1)),
                       machine='a', y=1.0)
                  for u, v in ((1, 1), (8, 2), (64, 64), (4096, 1))]
        envelope = calibration.envelope_coefficients('MULCORE', models, points)
        for u, v in ((0, 0), (1, 0), (1, 1), (7, 3), (64, 64), (4096, 1), (100000, 100000)):
            point = dict(c=u, v=u * v, group=f'v={v}')
            charge = calibration.predict('MULCORE', point, envelope, {})
            for model in models:
                self.assertGreaterEqual(charge + 1e-6, calibration.predict('MULCORE', point, model['MULCORE'], {}))
        self.assertLessEqual(envelope[2], 50)

    def test_word_byte_reversal_is_fitted_as_arith(self):
        # OP_BYTEREV's word kernel is an ARITH path over the word span (BIT in the recorded runs).
        for family in ('ARITH', 'BIT'):
            point = calibration.fixture(dict(probe=f'{family}/byterev/61', ns_per_execution=1))
            self.assertTrue(point['included'])
            self.assertEqual((point['x'], point['group']), (64, 'byterev'))
            self.assertTrue(calibration.fixture(dict(probe=f'{family}/xor/64', ns_per_execution=1))['included'])
        shift = calibration.fixture(dict(probe='ARITH/down/61/7', ns_per_execution=1))
        self.assertEqual((shift['x'], shift['group']), (64, 'down/7'))


    def test_select_and_unroll_units(self):
        # SELECT labels record the charged unit count; noncollated output is a diagnostic.
        point = calibration.fixture(dict(probe='SELECT/inputs/collated/8/64', ns_per_execution=1))
        self.assertEqual((point['x'], point['group'], point['included']), (64, 'inputs/collated', True))
        self.assertFalse(calibration.fixture(dict(probe='SELECT/empty_items/noncollated/8/9', ns_per_execution=1))['included'])
        self.assertFalse(calibration.fixture(dict(probe='SELECT/1b_items_scattered/collated/8/9', ns_per_execution=1))['included'])
        # SELECT is fitted on the time left after the result's WRITE and a scope
        # operand's READ; earlier labels without result bytes are sized from the fixtures.
        self.assertEqual(point['result_bytes'], 55 * 8 + 34)
        items = calibration.fixture(dict(probe='SELECT/1b_items/collated/300/301/603', ns_per_execution=100000))
        self.assertEqual((items['result_bytes'], items['scope_operands']), (603, 1))
        fits = {'WRITE': (800, 6), 'READ': (200, 1)}
        self.assertEqual(calibration.select_overhead(items, fits), 800 + 6 * 603 + 208)
        self.assertEqual(calibration.selection_time(items, fits), 100000 - (800 + 6 * 603 + 208))
        self.assertEqual(calibration.predict('SELECT', items, (1000, 200), fits), 1000 + 200 * 301 + 800 + 6 * 603 + 208)
        candidates = {'SELECT': (1400, 220), 'WRITE': (800, 7), 'READ': (200, 2)}
        self.assertEqual(calibration.candidate_charge('SELECT', items, candidates), 1400 + 220 * 301 + 800 + 7 * 608 + 216)
        # UNROLL is measured per charged unit against unrolled bytes per unit, and
        # checked against BASE and WRITE rather than priced.
        point = calibration.fixture(dict(probe='UNROLL/push-520/200/52300/60000', ns_per_execution=1000))
        self.assertEqual((point['x'], point['y'], point['group']), (261.5, 5, 'push'))
        self.assertEqual((point['units'], point['bytes'], point['charged']), (200, 52300, 60000))
        self.assertNotIn('UNROLL', calibration.fit_all({'UNROLL': [point]}, 100))
        # Rates round up to a whole varop.
        self.assertEqual([calibration.round_coefficient(v) for v in (0, 27.04, 88.2, 283.3, 2234.7, 168514.1, 300)],
                         [0, 28, 89, 284, 2235, 168515, 300])


if __name__ == '__main__':
    unittest.main()
