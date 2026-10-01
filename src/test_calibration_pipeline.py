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


def artifact():
    probes = ['F/nop/256', 'PREP/9/spare', 'PRODUCE/grow/9', 'NORMALIZE/scalar/256',
              'READ/zero/16', 'ARITH/sub/2/equal/borrow-chain', 'BIT/invert/16',
              'MOVE/8/empty', 'MULCORE/2/1/ones', 'DIVCORE/9/2/div/normalized',
              'H256/core/32', 'H160/32', 'H1/32', 'SIG/32', 'TWEAK',
              'SELECT/weight-scan/collated/128/131', 'UNROLL/push-520/15296/3999904']
    rate = 20.0
    return dict(schema='varop-calibration-v1', model_id=calibration.MODEL_ID,
                normalization=dict(varops_per_nanosecond=rate,
                                   target_fraction_of_local_pre_v2_worst=1.0,
                                   local_pre_v2_worst_seconds=2.0),
                machine=dict(cpu='synthetic'), source_sha256={}, head='synthetic',
                producer_manifest=[dict(probe='PRODUCE/grow/9', kind='produce', items='2', bytes='13', normalize_bytes='0'),
                                   dict(probe='NORMALIZE/scalar/256', kind='normalize', items='1', bytes='0', normalize_bytes='2')],
                primitive_samples=[dict(probe=probe, epoch=epoch, ns_per_execution=100 + epoch,
                                        normalized_varops_per_execution=(100 + epoch)*rate)
                                   for probe in probes for epoch in range(3)])


class CalibrationPipelineTests(unittest.TestCase):
    def test_maximum_coefficients(self):
        a = {'PRODUCE': (100, 6), 'DIVCORE': (10, 0, 4)}
        b = {'PRODUCE': (200, 3), 'DIVCORE': (20, 0, 2)}
        combined = maximum_coefficients([a, b])
        self.assertEqual(combined, {'PRODUCE': (200, 6), 'DIVCORE': (20, 0, 4)})
        self.assertEqual(maximum_coefficients([a, b, a]), combined)
        for n in (0, 1, 100, 4000000):
            self.assertGreaterEqual(combined['PRODUCE'][0] + combined['PRODUCE'][1]*n,
                                    max(m['PRODUCE'][0] + m['PRODUCE'][1]*n for m in (a, b)))
        with self.assertRaisesRegex(ValueError, 'matching feature bases'):
            maximum_coefficients([a, {'PRODUCE': (100, 6)}])

    def test_envelope(self):
        def points(family, sizes):
            return [dict(machine='m', group='g', x=x, c=c, v=v, included=True) for x, c, v in sizes]
        grid = [(s, s, s * v) for s in (1, 2, 3, 10, 100, 1000) for v in (1, 2, 4, 64, 1024)]
        # A large flat on one machine and a larger row rate on another: at one row and
        # one limb the envelope covers the sum, so its flat may drop below the maximum.
        a = {'DIVCORE': (3000, 400, 40), 'PRODUCE': (2000, 4), 'H1': (300, 20)}
        b = {'DIVCORE': (100, 570, 43), 'PRODUCE': (500, 6), 'H1': (20, 25)}
        maximum = maximum_coefficients([a, b])
        env = calibration.envelope_coefficients('DIVCORE', [a, b], points('DIVCORE', grid))
        self.assertLess(env[0], maximum['DIVCORE'][0])
        for s in (1, 2, 7, 10**6):
            for v in (1, 3, 10**6):
                charge = env[0] + env[1] * s + env[2] * s * v
                self.assertGreaterEqual(charge * (1 + 1e-9), max(m['DIVCORE'][0] + m['DIVCORE'][1] * s + m['DIVCORE'][2] * s * v for m in (a, b)))
        # Sizes from zero, unbounded: covering both needs the largest flat and the largest rate.
        env = calibration.envelope_coefficients('PRODUCE', [a, b], points('PRODUCE', [(n, 1, n) for n in (0, 8, 4096)]))
        self.assertEqual(tuple(round(x, 6) for x in env), (2000, 6))
        # SHA1 is bounded to 520 bytes: one or two hashed blocks up to 576 bytes.
        env = calibration.envelope_coefficients('H1', [a, b], points('H1', [(n, 1, calibration.hash_span(n)) for n in (0, 64, 520)]))
        for n in (0, 55, 56, 520):
            span = calibration.hash_span(n)
            self.assertGreaterEqual((env[0] + env[1] * span) * (1 + 1e-9), max(m['H1'][0] + m['H1'][1] * span for m in (a, b)))
        self.assertLess(env[0], maximum['H1'][0])

    def test_schedule_rounding(self):
        # Hashes are priced per byte of the block-padded length: 49.0253 per byte is charged as 50.
        self.assertEqual(calibration.rounded_candidate('H256', [3043.03, 49.0253]), [3100, 50])
        # Every rate is a whole number of varops per byte of W(n): PREP's 0.0301 is charged as 1.
        self.assertEqual(calibration.rounded_candidate('PREP', [241.381, .0301]), [250, 1])
        self.assertEqual(calibration.rounded_candidate('PREP', [241.381, 1.3]), [250, 2])
        self.assertEqual(calibration.formulas('PREP', [250, 1], candidate=True), '250 + W(n)')
        self.assertEqual(calibration.formulas('DIVCORE', [0, 500, 33], candidate=True), '500 × s + 33 × s × v')
        self.assertEqual(calibration.rounded_candidate('DIVCORE', [3977.26, 0, 158.884]), [4000, 0, 160])
        self.assertEqual(calibration.rounded_candidate('MUL', [186.117, 13.1377]), [190, 14])
        # Byte rates are charged per byte of W(n) >= n: 6.40246 per byte of n is charged as 7 × W(n).
        self.assertEqual(calibration.rounded_candidate('PRODUCE', [2120.63, 6.40246]), [2200, 7])
        self.assertEqual(calibration.formulas('PRODUCE', [2200, 7], candidate=True), '2200 + 7 × W(n)')
        # Rates round to two significant figures; flats to multiples of 10 below 100 and of 50
        # from 100, but never to more than two significant figures.
        self.assertEqual(calibration.rounded_candidate('SELECT', [3454.87, 1643.3]), [3500, 1700])
        self.assertEqual(calibration.rounded_candidate('MULCORE', [338.305, 4.32306, 109.887, 28.2557]), [350, 5, 110, 29])
        self.assertEqual(calibration.rounded_candidate('READ', [80.6, 1.01]), [90, 2])
        self.assertEqual(calibration.rounded_candidate('TWEAK', [168855, 0]), [170000, 0])
        self.assertEqual(calibration.formulas('H256', [192, 39], candidate=True), '192 + 39 × H(n)')
        self.assertEqual(calibration.rounded_candidate('NORMALIZE', [187.2, 0]), [200, 0])
        self.assertEqual(calibration.formulas('NORMALIZE', [200, 0], candidate=True), '200')
        # Zero stays zero and exact multiples keep their value.
        self.assertEqual(calibration.rounded_candidate('F', [300, 0]), [300, 0])
        self.assertEqual(calibration.rounded_candidate('DIVCORE', [1000, 100, 10]), [1000, 100, 10])
        self.assertEqual(calibration.rounded_candidate('SIG', [490446, 0]), [500000, 0])
        # Step boundaries: whole varops below 100, then a tenth of the leading power of ten.
        self.assertEqual([calibration.round_coefficient(v) for v in
                          (0.0012, 9.2, 10, 10.1, 99.1, 100, 100.1, 999.1, 1000, 1000.1, 9950.5, 10000, 10001)],
                         [1, 10, 10, 11, 100, 100, 110, 1000, 1000, 1100, 10000, 10000, 11000])
        self.assertEqual([calibration.round_flat(v) for v in
                          (0, 0.2, 10, 53.7, 99.1, 100, 100.1, 142.2, 950.1, 1000, 1712.1, 9950.5, 168100.2)],
                         [0, 10, 10, 60, 100, 100, 150, 150, 1000, 1000, 1800, 10000, 170000])

    def test_candidate_charge_uses_pricing_units(self):
        c = {'PRODUCE': [680, 7], 'PREP': [180, 1], 'H256': [280, 38], 'MOVE': [180, 23], 'SIG': [500000, 0],
             'MULCORE': [340, 5, 110, 29], 'F': [310, 0]}
        # WRITE and PREPARE are charged on W(n), hashes on the block span.
        self.assertEqual(calibration.candidate_charge('PRODUCE', dict(x=9, group='g'), c), 680 + 7 * 16)
        self.assertEqual(calibration.candidate_charge('PREP', dict(x=9, group='spare'), c), 180 + 16)
        self.assertEqual(calibration.candidate_charge('H256', dict(x=56, group='g'), c), 280 + 38 * 128)
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

    def load(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'synthetic.json'
            path.write_text(json.dumps(data))
            return calibration.load_calibration(path)

    def model(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'synthetic.json'
            path.write_text(json.dumps(data))
            return calibration.machine_model(path)

    def test_pricing_targets_a_fraction_of_the_reference(self):
        # 40e9 varops over 0.9 × the 2 s reference.
        self.assertEqual(calibration.TARGET_FRACTION, 0.9)
        points, meta = self.load(artifact())
        self.assertAlmostEqual(meta['varops_per_nanosecond'], 40 / (2.0 * 0.9))
        # An artifact that recorded its rate for 0.9 of the reference, rather than
        # for the reference itself, prices identically.
        collected = artifact()
        collected['normalization'].update(target_fraction_of_local_pre_v2_worst=0.9,
                                          varops_per_nanosecond=40 / (2.0 * 0.9))
        for row in collected['primitive_samples']:
            row['normalized_varops_per_execution'] = row['ns_per_execution'] * 40 / (2.0 * 0.9)
        recollected, _ = self.load(collected)
        self.assertEqual([p['y'] for p in recollected], [p['y'] for p in points])
        # So do the machine fits, which are fitted from the nanosecond samples.
        self.assertEqual(self.model(collected)['F'], self.model(artifact())['F'])

    def test_all_families_and_median(self):
        points, meta = self.load(artifact())
        self.assertEqual({p['family'] for p in points}, set(calibration.PRODUCER_ORDER))
        self.assertEqual(meta['model_id'], calibration.MODEL_ID)
        by_name = {p['family']: p for p in points}
        self.assertEqual(by_name['PRODUCE']['x'], 6.5)
        self.assertAlmostEqual(by_name['PRODUCE']['y'], 1010 / 0.9)
        self.assertEqual(by_name['NORMALIZE']['v'], 8)
        self.assertIn('borrow-chain', by_name['ARITH']['group'])

    def test_fixed_groups(self):
        fixed = calibration.fixture(dict(probe='F/else/256', ns_per_execution=2570))
        self.assertEqual((fixed['group'], fixed['x'], fixed['y'], fixed['included']), ('else', 257, 10, True))
        skipped = calibration.fixture(dict(probe='F/skipped/256', ns_per_execution=2560))
        self.assertEqual((skipped['x'], skipped['y'], skipped['included']), (256, 10, False))

    def test_normalize_offset_span_is_diagnostic(self):
        manifest = {f'NORMALIZE/{group}/16': dict(items='1', bytes='0', normalize_bytes='16')
                    for group in ('aligned', 'offset-span')}
        aligned = calibration.fixture(dict(probe='NORMALIZE/aligned/16', ns_per_execution=10), manifest)
        offset = calibration.fixture(dict(probe='NORMALIZE/offset-span/16', ns_per_execution=20), manifest)
        self.assertTrue(aligned['included'])
        self.assertFalse(offset['included'])

    def test_divcore_rows(self):
        rows = {}
        for u, v in ((9, 2), (1, 1), (9, 1), (1, 2), (1, 9)):
            point = calibration.fixture(dict(probe=f'DIVCORE/{u}/{v}/17/DIV/top-one', ns_per_execution=1))
            rows[u, v] = (point['x'], point['c'], point['v'])
        self.assertEqual(rows, {(9, 2): (9, 9, 18), (1, 1): (2, 2, 2), (9, 1): (10, 10, 10),
                                (1, 2): (1, 1, 2), (1, 9): (1, 1, 9)})

    def test_lifetime_checks(self):
        # A numeric result of 9 bytes: one production event plus PRODUCE, PREP and
        # NORMALIZE at its 16-byte word span.
        data = artifact()
        probe = 'PRODUCER_CHECK/numeric/tight/9'
        data['producer_manifest'].append(dict(probe=probe, kind='numeric', items='1', bytes='9', normalize_bytes='9'))
        recorded = data['normalization']['varops_per_nanosecond']
        data['primitive_samples'] += [dict(probe=probe, epoch=epoch, ns_per_execution=20,
                                           normalized_varops_per_execution=20 * recorded) for epoch in range(3)]
        model = {'PRODUCE': (100, 2), 'PREP': (50, 1), 'NORMALIZE': (30, 0)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'synthetic.json'
            path.write_text(json.dumps(data))
            checked, above = calibration.lifetime_checks(path, model, 'm')
        self.assertEqual(checked, 1)
        composed = (100 + 2 * 9) + (100 + 2 * 16) + (50 + 16) + 30
        self.assertAlmostEqual(above[0]['composed_varops'], composed)
        self.assertAlmostEqual(above[0]['ratio'], 20 * 40 / (2.0 * 0.9) / composed)

    def test_incomplete_epochs_rejected(self):
        data = artifact()
        data['primitive_samples'].pop()
        with self.assertRaisesRegex(ValueError, 'incomplete epochs'):
            self.load(data)

    def test_wrong_normalization_rejected(self):
        data = artifact()
        data['normalization']['target_fraction_of_local_pre_v2_worst'] = 0.9
        with self.assertRaisesRegex(ValueError, 'inconsistent normalization'):
            self.load(data)

    def test_legacy_family_rejected_in_candidate(self):
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
        self.assertEqual(set(fits), set(calibration.PRODUCER_ORDER) - calibration.CHECKS)
        self.assertEqual(len(fits['DIVCORE']), 3)
        self.assertNotIn('RELEASE', fits)

    def test_divcore_per_row_term(self):
        # Many rows at a small divisor separate the per-row term from the cells.
        points = []
        for u, v in ((1026, 1), (1026, 2), (34, 2), (8, 4), (1032, 8), (80, 16), (1088, 64), (128, 64)):
            point = calibration.fixture(dict(probe=f'DIVCORE/{u}/{v}/17/DIV/top-one', ns_per_execution=1))
            point['y'] = 1000 + 500 * point['c'] + 50 * point['v']
            point['machine'] = 'a'
            points.append(point)
        fixed, step, cell = calibration.fit_divcore(points, 100)
        self.assertAlmostEqual(step, 500, delta=5)
        self.assertAlmostEqual(cell, 50, delta=1)

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
        self.assertNotIn('MUL', fits)

    def test_mulcore_terms(self):
        # Rows over several shorter-operand widths separate the per-row and per-cell terms.
        points = []
        for u, v in ((1, 1), (500, 1), (4000, 1), (64, 16), (4096, 16), (1024, 1024), (8192, 256)):
            point = calibration.fixture(dict(probe=f'MULCORE/{u}/{v}/ones', ns_per_execution=1))
            point['y'] = 1400 + 40 * point['c'] + 42 * point['v']
            point['machine'] = 'a'
            points.append(point)
        fixed, row, cell = calibration.fit_divcore(points, 100)
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

    def test_word_byte_reversal_is_fitted_as_bit(self):
        # OP_BYTEREV's word kernel is a BIT path over the word span; the superseded
        # byte-wise BIT/reverse samples are skipped when an artifact is loaded.
        point = calibration.fixture(dict(probe='BIT/byterev/61', ns_per_execution=1))
        self.assertTrue(point['included'])
        self.assertEqual(point['x'], 64)
        self.assertTrue(calibration.fixture(dict(probe='BIT/xor/64', ns_per_execution=1))['included'])
        data = artifact()
        data['primitive_samples'] += [dict(probe=probe, epoch=epoch, ns_per_execution=100,
                                           normalized_varops_per_execution=2000)
                                      for probe in ('BIT/reverse/64', 'H256/secp_tagged/64') for epoch in range(3)]
        points, _ = self.load(data)
        self.assertTrue(any(p['label'].startswith('BIT/invert/') for p in points))
        self.assertFalse(any(p['label'].startswith(('BIT/reverse/', 'H256/secp_tagged/')) for p in points))


    def test_select_and_unroll_units(self):
        # SELECT labels record the charged unit count; noncollated output is a diagnostic.
        point = calibration.fixture(dict(probe='SELECT/inputs/collated/8/64', ns_per_execution=1))
        self.assertEqual((point['x'], point['group'], point['included']), (64, 'inputs/collated', True))
        self.assertFalse(calibration.fixture(dict(probe='SELECT/empty_items/noncollated/8/9', ns_per_execution=1))['included'])
        # Earlier artifacts: one input plus n witness items.
        self.assertEqual(calibration.fixture(dict(probe='SELECT/empty_items/collated/8', ns_per_execution=1))['x'], 9)
        # UNROLL is measured per charged unit against unrolled bytes per unit, and
        # checked against BASE and WRITE rather than priced.
        point = calibration.fixture(dict(probe='UNROLL/push-520/200/52300', ns_per_execution=1000))
        self.assertEqual((point['x'], point['y'], point['group']), (261.5, 5, 'push'))
        self.assertEqual((point['units'], point['bytes'], point['charged']), (200, 52300, None))
        self.assertEqual(calibration.fixture(dict(probe='UNROLL/nop-1/2048/1028/617500', ns_per_execution=1))['charged'], 617500)
        self.assertNotIn('UNROLL', calibration.fit_all({'UNROLL': [point]}, 100))
        # Coefficients round up to two significant figures, and at least to a whole varop.
        self.assertEqual([calibration.round_coefficient(v) for v in (0, 27.04, 88.2, 283.3, 2234.7, 168514.1, 300)],
                         [0, 28, 89, 290, 2300, 170000, 300])


if __name__ == '__main__':
    unittest.main()
