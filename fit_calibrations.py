#!/usr/bin/env python3
"""Fit one provisional primitive schedule from multiple calibration JSON files.

Each machine's fixture timings are normalized by its own pre-v2 reference: a full
40-billion-varop budget of fitted work is priced to take that machine's recorded
pre-v2 worst case. The pricing rate is derived from the recorded reference time,
whatever normalization the artifact was collected with. Runs whose recorded
measurement conditions failed are rejected unless explicitly allowed. Inputs
remain unchanged. This is a descriptive fit; complete scripts on every machine
decide whether a schedule holds.
"""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import tempfile


ORDER = "F PREP OUTPUT COPY RELEASE READ ARITH BIT MOVE MUL DIVCORE H256 H160 H1 SIG TWEAK SELECT DECODE".split()
CONSTANT = {"F", "SIG", "TWEAK", "NORMALIZE"}
FIT_ORDER = [name for name in ORDER if name != "SIG"] + ["SIG"]
PRODUCER_ORDER = "F PREP PRODUCE NORMALIZE READ ARITH BIT MOVE MUL DIVCORE H256 H160 H1 SIG TWEAK SELECT UNROLL".split()
# Measured compositions of existing primitives: compared against their charge, never priced.
CHECKS = {"UNROLL"}
MODEL_ID = "producer-normalize-v1"
BUDGET_VAROPS = 40_000_000_000


def pricing_rate(normalization, fraction=1.0):
    """Varops per nanosecond at which a full budget takes the recorded reference time.

    fraction only checks artifacts that recorded their rate for a fraction of it."""
    return BUDGET_VAROPS / (float(normalization["local_pre_v2_worst_seconds"]) * 1e9 * fraction)


def check_source_snapshots(root, machines):
    """Check recorded sources against their recorded commit, never today's worktree."""
    unmatched = []
    checked = 0
    for machine in machines:
        head = machine['head']
        if len(head) != 40 or any(c not in '0123456789abcdef' for c in head):
            raise ValueError('source verification requires a full recorded commit hash')
        for name, digest in machine['source_sha256'].items():
            name = name.replace('\\', '/')
            raw = subprocess.check_output(
                ['git', '-C', str(root), 'show', f'{head}:{name}'])
            lf = raw.replace(b'\r\n', b'\n')
            acceptable = {hashlib.sha256(content).hexdigest()
                          for content in (lf, lf.replace(b'\n', b'\r\n'))}
            checked += 1
            if digest not in acceptable:
                unmatched.append(dict(machine=machine['file'], path=name, sha256=digest))
    return dict(unmatched=unmatched, checked=checked,
                commits=sorted({m['head'] for m in machines}),
                note='Compared with each recorded Git commit, accepting LF or CRLF. '
                     'Unmatched hashes require the original source bytes for review.')


def word(n):
    return (n + 7) // 8 * 8


def hash_span(n):
    """Bytes processed by a 64-byte-block hash: message, at least 9 padding bytes, whole blocks."""
    return (n + 72) // 64 * 64


def features(family, x, group):
    if family in {"DIVCORE", "MULCORE"}:
        return x, x * int(group.split("=")[1])
    if family in {"H256", "H160", "H1"}:
        return 1, hash_span(x)
    if family in {"PREP", "OUTPUT", "NORMALIZE"}:
        return 1, word(x)
    return 1, x


def fixture(row, producer_manifest=None):
    parts = row["probe"].split("/")
    family = parts[0]
    x, group, included = 1, "measurement", True
    y = float(row["ns_per_execution"])
    if family in {"PRODUCE", "NORMALIZE"}:
        record = producer_manifest[row['probe']]
        count = int(record['items'])
        group = '/'.join(parts[1:-1])
        x = int(record['normalize_bytes']) if family == 'NORMALIZE' else int(record['bytes']) / count
        y /= count
        # Production vectors are word-aligned, so conversion never compacts an
        # offset span; that path is timed as a diagnostic only.
        included = not (family == 'NORMALIZE' and group == 'offset-span')
    elif family == "F":
        # F/<group>/<n>: n charged F-only instructions plus the final OP_1.
        # F/skipped/<n> times n uncharged skipped NOPs as a diagnostic.
        group = parts[1]
        x = int(parts[2]) + (group != "skipped")
        y /= x
        included = group != "skipped"
    elif family == "PREP":
        x, group = int(parts[1]), parts[2]
        included = group == "spare"
    elif family == "OUTPUT":
        if parts[1] == "scalar":
            x, group = (int(parts[2]).bit_length() + 7) // 8, "scalar"
        else:
            x, group = int(parts[1]), "materialized"
    elif family == "ARITH":
        x, group = int(parts[2]) * 8, parts[1] + "/" + "/".join(parts[3:])
    elif family in {"READ", "BIT"}:
        x, group = int(parts[2]), parts[1]
        if family == "READ" or group != "reverse":
            x = word(x)
        if len(parts) > 3:
            group += "/" + parts[3]
    elif family == "MOVE":
        x, group = int(parts[1]), parts[2]
    elif family == "MUL":
        x, group = int(parts[2]), "u=1"
    elif family == "MULCORE":
        # MULCORE/<u>/<v>/<pattern>: u rows of v limbs each.
        x, group = int(parts[1]), f"v={int(parts[2])}"
    elif family == "DIVCORE":
        dividend, divisor = int(parts[1]), int(parts[2])
        # Quotient limbs plus a normalization carry row; one comparison row for a shorter dividend.
        x, group = max(1, dividend - divisor + 2), f"v={divisor}"
    elif family == "H256":
        x, group = int(parts[2]), parts[1]
    elif family == "SELECT":
        # SELECT/<kind>/<format>/<records>/<units>; earlier artifacts lack the unit count.
        x = int(parts[4]) if len(parts) > 4 else int(parts[3]) + 1
        group, included = parts[1] + "/" + parts[2], parts[2] == "collated"
    elif family == "UNROLL":
        # UNROLL/<shape>/<units>/<unrolled bytes>[/<charged varops>]: time per charged
        # unit against bytes per unit; the charge covers the complete script.
        units = int(parts[2])
        x, group = int(parts[3]) / units, parts[1].split("-")[0]
        y /= units
        return dict(label=row["probe"], family=family, x=x, y=y, c=1, v=x, group=group,
                    included=included, units=units, bytes=int(parts[3]),
                    charged=int(parts[4]) if len(parts) > 4 else None)
    elif family == "DECODE":
        x, group = int(parts[2]), parts[1]
    elif family == "COPY":
        x, group = int(parts[2]), parts[1]
        included = group == "isolated"
    elif family == "RELEASE":
        group = parts[1]
        x = int(parts[3]) if group == "preallocated" else word(int(parts[2]))
    elif family != "TWEAK":
        x = int(parts[1])
    if family in {"COPY", "RELEASE"} and x == 0 and group != "preallocated":
        group, included = "empty", False
    c, v = features(family, x, group)
    return dict(label=row["probe"], family=family, x=x, y=y, c=c, v=v,
                group=group, included=included)


def load_calibration(path):
    data = json.loads(path.read_text())
    if data.get("schema") != "varop-calibration-v1":
        raise ValueError(f"{path}: unsupported calibration schema")
    model_id = data.get('model_id', 'legacy-primitives')
    if model_id != MODEL_ID:
        raise ValueError(f'{path}: unknown costing model {model_id}')
    # Artifacts record their primitive families; MULCORE replaced the MUL row kernel.
    order = data.get("primitive_order") or (PRODUCER_ORDER if model_id == MODEL_ID else ORDER)
    manifest = {r['probe']: r for r in data.get('producer_manifest', [])}
    normalization = data["normalization"]
    recorded_rate = float(normalization["varops_per_nanosecond"])
    # Earlier runners recorded their rate for a fraction of the reference.
    recorded_fraction = float(normalization.get("target_fraction_of_local_pre_v2_worst", 1.0))
    if int(normalization.get("budget_varops", BUDGET_VAROPS)) != BUDGET_VAROPS:
        raise ValueError(f"{path}: unexpected budget")
    if not math.isclose(recorded_rate, pricing_rate(normalization, recorded_fraction), rel_tol=1e-12):
        raise ValueError(f"{path}: inconsistent normalization")
    rate = pricing_rate(normalization)
    epochs = int(data["fitting"]["metadata"]["epochs"])
    machine = str(path)
    observations = defaultdict(list)
    for row in data["primitive_samples"]:
        name = row["probe"]
        if name.startswith(("COPY_CONTROL/", "ZERO/", "SIGHASH/", "PRODUCER_CHECK/")):
            continue
        # H256 prices Core's SHA256; libsecp's internal tagged hashing is not an H256 input.
        # BIT/reverse timed the superseded byte-wise std::reverse; OP_BYTEREV's word kernel is BIT/byterev.
        if name.startswith(("H256/secp_tagged/", "BIT/reverse/")):
            continue
        # Stack values always carry word-padding capacity, so tight PREP buffers cannot occur.
        if name.startswith("PREP/") and name.endswith("/tight"):
            continue
        # Shortening a value needs an opcode that charges its result's production,
        # so create-and-shrink is two productions (PRODUCE/churn), not one WRITE.
        if name.startswith(("PRODUCE/shrink-empty/", "PRODUCE/shrink-one/")):
            continue
        if name.split("/", 1)[0] not in order:
            raise ValueError(f"{path}: unknown primitive {name}")
        ns = float(row["ns_per_execution"])
        if not math.isfinite(ns) or ns <= 0:
            raise ValueError(f"{path}: invalid timing for {name}")
        if not math.isclose(float(row["normalized_varops_per_execution"]), ns * recorded_rate,
                            rel_tol=1e-10):
            raise ValueError(f"{path}: inconsistent normalized timing for {name}")
        observations[name].append((int(row["epoch"]), ns))
    points = []
    for name, samples in observations.items():
        epoch_counts = Counter(epoch for epoch, _ in samples)
        if set(epoch_counts) != set(range(epochs)) or len(set(epoch_counts.values())) != 1:
            raise ValueError(f"{path}: incomplete epochs for {name}")
        point = fixture({"probe": name, "ns_per_execution": statistics.median(ns for _, ns in samples)}, manifest)
        point["y"] *= rate
        point["machine"] = machine
        points.append(point)
    if {p["family"] for p in points} != set(order):
        raise ValueError(f"{path}: missing primitive family")
    meta = dict(file=str(path), model_id=model_id, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                cpu=data["machine"]["cpu"], epochs=epochs,
                head=data["fitting"]["metadata"].get("head"),
                fixture_count=len(points), reference_seconds=data["normalization"]["local_pre_v2_worst_seconds"],
                varops_per_nanosecond=rate, source_sha256=data["source_sha256"],
                conditions=data.get("conditions"))
    return points, meta


def decade(x):
    return -1 if x == 0 else math.floor(math.log10(x))


def weights(points):
    counts = Counter((p["machine"], p["group"], decade(p["x"])) for p in points)
    bins = Counter((machine, group) for machine, group, _ in counts)
    groups = Counter(machine for machine, _ in bins)
    machines = len(groups)
    return [1 / (machines * groups[p["machine"]] *
                 bins[p["machine"], p["group"]] *
                 counts[p["machine"], p["group"], decade(p["x"])]) for p in points]


def minimize(fn, lo, hi):
    center = min((fn(lo + (hi - lo) * i / 80), lo + (hi - lo) * i / 80)
                 for i in range(81))[1]
    left, right = max(lo, center - (hi - lo) / 80), min(hi, center + (hi - lo) / 80)
    for _ in range(70):
        a, b = left + (right - left) / 3, right - (right - left) / 3
        if fn(a) < fn(b):
            right = b
        else:
            left = a
    return (left + right) / 2


def log_loss(predictions, points, ws, penalty):
    if min(predictions) <= 0:
        return math.inf
    return sum(w * (penalty if predicted < p["y"] else 1) *
               math.log(predicted / p["y"]) ** 2
               for w, predicted, p in zip(ws, predictions, points))


def optimal_scale(shape, points, ws, penalty):
    targets = [math.log(p["y"] / feature) for p, feature in zip(points, shape)]
    lo, hi = min(targets), max(targets)
    for _ in range(60):
        mid = (lo + hi) / 2
        gradient = sum(w * (penalty if mid < target else 1) * (mid - target)
                       for w, target in zip(ws, targets))
        if gradient < 0:
            lo = mid
        else:
            hi = mid
    return math.exp((lo + hi) / 2)


def fit(points, mode, penalty):
    ws = weights(points)
    loss = lambda pred: log_loss(pred, points, ws, penalty)
    if mode == "residual":
        def objective(q):
            return loss([p["background"] + math.exp(q) for p in points])
        q = minimize(objective, -30, math.log(max(p["y"] for p in points) * 10))
        a = math.exp(q)
        if loss([p["background"] for p in points]) <= objective(q):
            a = 0
        return a, 0
    if mode == "constant":
        if penalty == 1:
            return math.exp(sum(w * math.log(p["y"]) for w, p in zip(ws, points))), 0
        return optimal_scale([1] * len(points), points, ws, penalty), 0
    scale = max(p["v"] / p["c"] for p in points) or 1
    def at(q):
        ratio = math.exp(q) / scale
        shape = [p["c"] + ratio * p["v"] for p in points]
        a = (math.exp(sum(w * math.log(p["y"] / value)
                          for w, p, value in zip(ws, points, shape)))
             if penalty == 1 else optimal_scale(shape, points, ws, penalty))
        return loss([a * value for value in shape]), a, a * ratio
    q = minimize(lambda value: at(value)[0], -35, 35)
    candidates = [at(q)]
    a = (math.exp(sum(w * math.log(p["y"] / p["c"]) for w, p in zip(ws, points)))
         if penalty == 1 else optimal_scale([p["c"] for p in points], points, ws, penalty))
    candidates.append((loss([a * p["c"] for p in points]), a, 0))
    if all(p["v"] > 0 for p in points):
        b = (math.exp(sum(w * math.log(p["y"] / p["v"]) for w, p in zip(ws, points)))
             if penalty == 1 else optimal_scale([p["v"] for p in points], points, ws, penalty))
        candidates.append((loss([b * p["v"] for p in points]), 0, b))
    return min(candidates)[1:]


def fit_divcore(points, penalty):
    """Fit fixed setup + quotient rows + row-by-divisor-limb cells."""
    return fit_terms(points, penalty, [(1, p["c"], p["v"]) for p in points])


def fit_mulcore(points, penalty):
    """Fit fixed setup + longer-operand limbs + one row per shorter-operand limb + cells.

    The product makes one row call per limb of the shorter operand, whose fixed
    overhead the cells alone would spread over every cell."""
    rows = [(1, p["c"], mul_rows(p), p["v"]) for p in points]
    ws = weights(points)
    def loss(coeff):
        return log_loss([sum(a * x for a, x in zip(coeff, row)) for row in rows], points, ws, penalty)
    # Coordinate descent stalls where the fixed and per-row terms trade off; refine jointly.
    return refine(loss, fit_terms(points, penalty, rows))


def refine(loss, start, iterations=4000):
    """Nelder-Mead over nonnegative coefficients from `start`; returns the better of the two."""
    def value(x):
        return loss([max(0.0, a) for a in x])
    k = len(start)
    simplex = [list(start)] + [[a + (0.1 * abs(a) or 1.0) * (i == j) for j, a in enumerate(start)] for i in range(k)]
    scores = [value(x) for x in simplex]
    for _ in range(iterations):
        order = sorted(range(k + 1), key=scores.__getitem__)
        simplex, scores = [simplex[i] for i in order], [scores[i] for i in order]
        if scores[-1] - scores[0] <= 1e-15 * max(1.0, abs(scores[0])):
            break
        centroid = [sum(x[j] for x in simplex[:-1]) / k for j in range(k)]
        worst = simplex[-1]
        reflected = [c + (c - w) for c, w in zip(centroid, worst)]
        r = value(reflected)
        if r < scores[0]:
            expanded = [c + 2 * (c - w) for c, w in zip(centroid, worst)]
            e = value(expanded)
            simplex[-1], scores[-1] = (expanded, e) if e < r else (reflected, r)
        elif r < scores[-2]:
            simplex[-1], scores[-1] = reflected, r
        else:
            contracted = [c + 0.5 * (w - c) for c, w in zip(centroid, worst)]
            q = value(contracted)
            if q < scores[-1]:
                simplex[-1], scores[-1] = contracted, q
            else:
                best = simplex[0]
                simplex = [best] + [[b + 0.5 * (a - b) for a, b in zip(x, best)] for x in simplex[1:]]
                scores = [scores[0]] + [value(x) for x in simplex[1:]]
    best = [max(0.0, a) for a in simplex[min(range(k + 1), key=scores.__getitem__)]]
    return tuple(best) if loss(best) < loss(start) else tuple(start)


def fit_terms(points, penalty, rows):
    """Coordinate descent over nonnegative coefficients of a fixed term, the leading
    size term, any intermediate terms and a final product term."""
    ws = weights(points)
    k = len(rows[0])
    def loss(coeff):
        return log_loss([sum(a * x for a, x in zip(coeff, row)) for row in rows],
                        points, ws, penalty)
    constant, _ = fit(points, "constant", penalty)
    step, trial = fit(points, "affine", penalty)
    middle = [0] * (k - 3)
    starts = [(constant, 0, *middle, 0), (0, step, *middle, trial), (constant / 3, step / 3, *middle, trial / 3)]
    candidates = []
    for start in starts:
        coeff = list(start)
        for _ in range(100 if penalty > 10 else 20):
            previous = loss(coeff)
            for i in range(k):
                present = [row[i] for row in rows if row[i] > 0]
                upper = math.log(10 * max(p["y"] for p in points) / min(present))
                def objective(q):
                    trial_coeff = coeff.copy()
                    trial_coeff[i] = math.exp(q)
                    return loss(trial_coeff)
                q = minimize(objective, -35, upper)
                trial_coeff = coeff.copy()
                trial_coeff[i] = 0
                coeff[i] = math.exp(q) if objective(q) < loss(trial_coeff) else 0
            if previous - loss(coeff) < 1e-12:
                break
        candidates.append((loss(coeff), tuple(coeff)))
    return min(candidates)[1]


def background(family, x, fits):
    if family == "SIG":
        # The BIP 340 challenge hash covers R || P || msg.
        a, b = fits["H256"]
        return a + b * hash_span(64 + x)
    return 0


def mul_rows(point):
    """Limbs of the shorter MULCORE operand: one row of the schoolbook product each."""
    return int(point["group"].split("=")[1])


def predict(family, point, coeff, fits):
    if family in CONSTANT:
        return coeff[0] + background(family, point["x"], fits)
    if family == "MULCORE" and len(coeff) == 4:
        return coeff[0] + coeff[1] * point["c"] + coeff[2] * mul_rows(point) + coeff[3] * point["v"]
    if family in {"DIVCORE", "MULCORE"}:
        return coeff[0] + coeff[1] * point["c"] + coeff[2] * point["v"]
    return coeff[0] * point["c"] + coeff[1] * point["v"]


def maximum_coefficients(models):
    """Combine fixed independent fits; adding a machine cannot lower a coefficient."""
    first = models[0]
    for model in models:
        if set(model) != set(first) or any(len(model[f]) != len(first[f]) for f in first):
            raise ValueError('Maximum-coefficient curves require matching feature bases')
    return {f: tuple(max(model[f][i] for model in models) for i in range(len(first[f])))
            for f in first}


# machine_model's refits in three independent parts, slowest first, so a process
# pool can run every machine's parts at once. They merge in the order below.
REFIT_PARTS = ('MULCORE', 'DIVCORE', 'other')
FIT_PROCESSES = 12


def refit_part(path, part):
    """One part of machine_model's refits from the recorded samples."""
    points, _ = load_calibration(Path(path))

    def included(family):
        return [p for p in points if p['family'] == family and p['included']]

    if part == 'DIVCORE':
        return {'DIVCORE': tuple(fit_divcore(included('DIVCORE'), 100))}
    if part == 'MULCORE':
        # Recorded MULCORE fits had no per-row term.
        mul = included('MULCORE')
        return {'MULCORE': tuple(fit_mulcore(mul, 100))} if mul else {}
    model = {}
    # Recorded hash fits used message bytes; refit on the padded hash-block span.
    for family in ('H256', 'H160', 'H1'):
        model[family] = tuple(fit(included(family), 'affine', 100))
    # Recorded PRODUCE fits included the shrink fixtures that loading now skips.
    model['PRODUCE'] = tuple(fit(included('PRODUCE'), 'affine', 100))
    # NORMALIZE is flat: conversion hands the buffer over in place, whatever its
    # size. Recorded fits were affine and included the offset-span diagnostic.
    model['NORMALIZE'] = tuple(fit(included('NORMALIZE'), 'constant', 100))
    # Recorded PREP fits were per word; PREP is fitted and priced per byte of W(n).
    model['PREP'] = tuple(fit(included('PREP'), 'affine', 100))
    # Byte reversal belongs to the covenant opcodes; the recorded BIT fit included it.
    model['BIT'] = tuple(fit(included('BIT'), 'affine', 100))
    return model


def machine_model(path, refits=None):
    """Recorded 100x machine fit in varops, with DIVCORE refit from the recorded samples.

    Recorded fits are in nanoseconds and are converted at the pricing rate. Recorded DIVCORE fits use the superseded quotient-step count;
    the same fitter reproduces them exactly from the samples when given that count.
    refits maps each of REFIT_PARTS to its refit_part result, when computed elsewhere.
    """
    data = json.loads(Path(path).read_text())
    rate = pricing_rate(data['normalization'])
    model = {f: tuple(record['under_penalty_100_fit'][k] * rate
                      for k in ('a_ns', 'b_ns', 'c_ns') if k in record['under_penalty_100_fit'])
             for f, record in data['fitting']['fits'].items()}
    if refits is None:
        refits = {part: refit_part(path, part) for part in REFIT_PARTS}
    for part in ('DIVCORE', 'MULCORE', 'other'):
        model.update(refits[part])
    for family in CHECKS:
        model.pop(family, None)
    return model


ENVELOPE = ("Envelope: the cheapest curve of each family's form that covers every machine's fitted curve at every "
            "chargeable size. With feature vector phi(x) >= 0 and machine fits theta_m, it minimizes the weighted mean "
            "relative charge sum_x w(x) theta.phi(x) / max_m theta_m.phi(x) over the family's fixtures x (same "
            "path-group and size-decade weights as the fits), subject to theta.phi(x) >= theta_m.phi(x) for every "
            "machine m and every chargeable size x, and theta >= 0. Sizes start at zero except where an operation "
            "cannot be smaller: a hash processes at least one 64-byte block, a division has at least one quotient row "
            "and one divisor limb, and a multiplication's shorter operand is at most as long as the longer one. RIPEMD160 and SHA1 take at most 520 bytes; other sizes are unbounded. Where sizes "
            "start at zero and are unbounded, the envelope equals the coefficientwise maximum. Constant primitives "
            "and SIG use the maximum.")


def feature_domain(family):
    """Chargeable sizes in feature space: corners plus nonnegative combinations of
    directions. Covering a linear curve there is equivalent to covering it at the
    corners and not falling behind along the directions."""
    if family == 'DIVCORE':
        # s >= 1 quotient rows, v >= 1 divisor limbs (a zero divisor fails before dividing);
        # features (1, s, s*v) = (1, 1, 1) + (s - 1)(0, 1, 1) + s(v - 1)(0, 0, 1).
        return [(1, 1, 1)], [(0, 1, 1), (0, 0, 1)]
    if family == 'MULCORE':
        # u >= v >= 0 limbs; features (1, u, v, u*v) = (1, 0, 0, 0) + (u - v)(0, 1, 0, 0)
        # + v(0, 1, 1, 0) + u*v(0, 0, 0, 1).
        return [(1, 0, 0, 0)], [(0, 1, 0, 0), (0, 1, 1, 0), (0, 0, 0, 1)]
    if family in {'H160', 'H1'}:
        return [(1, hash_span(0)), (1, hash_span(520))], []
    if family == 'H256':
        return [(1, hash_span(0))], [(0, 1)]
    return [(1, 0)], [(0, 1)]


def solve(rows, rhs):
    """Solve a small square linear system by Gaussian elimination; None if singular."""
    n = len(rows)
    m = [list(map(float, row)) + [float(r)] for row, r in zip(rows, rhs)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(n):
            if r != col:
                factor = m[r][col] / m[col][col]
                m[r] = [a - factor * b for a, b in zip(m[r], m[col])]
    return [m[i][n] / m[i][i] for i in range(n)]


def envelope_coefficients(family, models, points):
    """The ENVELOPE curve of one family: an exact linear program over the family's
    few coefficients, solved by checking every vertex of the feasible region.

    points are one machine's included fixtures of the family (all machines share them)."""
    curves = [tuple(model[family]) for model in models]
    if family in CONSTANT:
        return tuple(max(curve[i] for curve in curves) for i in range(len(curves[0])))
    k = len(curves[0])
    corners, rays = feature_domain(family)
    constraints = {}
    for direction in corners + rays:
        constraints[direction] = max(sum(t * e for t, e in zip(curve, direction)) for curve in curves)
    for i in range(k):
        constraints.setdefault(tuple(int(i == j) for j in range(k)), 0.0)
    constraints = list(constraints.items())
    features_of = ((lambda p: (1, p['c'], mul_rows(p), p['v'])) if k == 4 else
                   (lambda p: (1, p['c'], p['v'])) if k == 3 else (lambda p: (p['c'], p['v'])))
    ws = weights(points)
    gradient = [0.0] * k
    for w, p in zip(ws, points):
        phi = features_of(p)
        target = max(sum(t * f for t, f in zip(curve, phi)) for curve in curves)
        for i in range(k):
            gradient[i] += w * phi[i] / target
    best = None
    for chosen in itertools.combinations(constraints, k):
        theta = solve([e for e, _ in chosen], [r for _, r in chosen])
        if theta is None:
            continue
        if all(sum(t * e for t, e in zip(theta, direction)) >= bound - 1e-9 * max(1.0, abs(bound))
               for direction, bound in constraints):
            value = sum(g * t for g, t in zip(gradient, theta))
            if best is None or value < best[0] - 1e-12:
                best = (value, theta)
    return tuple(max(0.0, t) for t in best[1])


def envelope_model(models, series):
    """ENVELOPE coefficients for every family of the machine models; series holds the
    fixtures of all machines, of which the first machine's are used for weights."""
    first = next(iter(series.values()))[0]['machine']
    return {family: envelope_coefficients(
                family, models,
                [p for p in series.get(family, []) if p['included'] and p['machine'] == first])
            for family in models[0]}


def independent_models(paths):
    """Pricing curves: one fit per machine in a single feature basis per family.

    DIVCORE is fixed + step + cell on every machine. Maxima over mixed bases
    (e.g. adding a fixed + cell fit) would charge the per-row work twice.
    """
    tasks = [(index, part) for part in REFIT_PARTS for index in range(len(paths))]
    refits = [{} for _ in paths]
    with ProcessPoolExecutor(max_workers=min(len(tasks), FIT_PROCESSES, os.cpu_count() or 1)) as pool:
        results = pool.map(refit_part, [paths[index] for index, _ in tasks],
                           [part for _, part in tasks])
        for (index, part), result in zip(tasks, results):
            refits[index][part] = result
    return [machine_model(path, refits[index]) for index, path in enumerate(paths)]


# Size-dependent rates are priced per byte of the padded length the operation processes:
# W(n) (whole 8-byte words) for numeric and byte operations, H(n) (whole 64-byte blocks)
# for the hashes. Fitted byte rates are per byte of n, and W(n) >= n.
WORD_PRICED = {'PREP', 'PRODUCE', 'READ', 'ARITH', 'BIT'}
# PREP's fitted byte rate is far below one varop, so its rate is priced per 64-bit word
# of W(n) instead: one whole varop per byte would charge eight per word.
PER_WORD = {'PREP'}


def ceil_to(value, step):
    """Round up to a multiple of step; an exact multiple is kept."""
    return int(step * math.ceil(value / step - 1e-9))


def round_coefficient(value):
    """Up to two significant figures, and at least to a whole varop: the step is at most
    a tenth of the value from 10 up, so rounding adds at most 10% there."""
    if value <= 0:
        return 0
    return ceil_to(value, max(1, 10 ** (math.floor(math.log10(value)) - 1)))


def rounded_candidate(family, coeff):
    """Schedule adoption, not refitting: each coefficient rounded up on its own with
    round_coefficient; SIG stays at its fixed allowance."""
    if family == 'SIG':
        return [500000, 0]
    if family in PER_WORD:
        coeff = [coeff[0], 8 * coeff[1]]
    return [round_coefficient(v) for v in coeff]


def formulas(family, coeff, candidate=False):
    """Formula text; candidates are charged on W(n) where the fit is per byte of n."""
    if family in CONSTANT:
        return f"{coeff[0]:.6g}"
    if family == "MUL":
        return f"u × ({coeff[0]:.6g} + {coeff[1]:.6g} × v)"
    if family == "MULCORE" and len(coeff) == 4:
        return f"{coeff[0]:.6g} + {coeff[1]:.6g} × u + {coeff[2]:.6g} × v + {coeff[3]:.6g} × u × v"
    if family == "MULCORE":
        return f"{coeff[0]:.6g} + {coeff[1]:.6g} × u + {coeff[2]:.6g} × u × v"
    if family == "DIVCORE":
        if coeff[1] == 0:
            return f"{coeff[0]:.6g} + {coeff[2]:.6g} × s × v"
        return f"{coeff[0]:.6g} + {coeff[1]:.6g} × s + {coeff[2]:.6g} × s × v"
    if candidate and family in PER_WORD:
        variable = "W(n)/8"
    elif candidate and family in WORD_PRICED:
        variable = "W(n)"
    else:
        variable = ("W(n)" if family in {"PREP", "READ", "ARITH", "BIT", "OUTPUT", "NORMALIZE"} else "k" if family in {"MOVE", "SELECT"}
                    else "H(n)" if family in {"H256", "H160", "H1"} else "n")
    return f"{coeff[0]:.6g} + {coeff[1]:.6g} × {variable}"


def fit_all(series, penalty):
    fits = {}
    base = PRODUCER_ORDER if 'PRODUCE' in series else ORDER
    order = [f for f in base + ['MULCORE'] if f in series and f not in CHECKS]
    for family in [f for f in order if f != 'SIG'] + [f for f in ('SIG',) if f in order]:
        points = [p for p in series[family] if p["included"]]
        for point in points:
            point["background"] = background(family, point["x"], fits)
        mode = "residual" if family == "SIG" else "constant" if family in CONSTANT else "affine"
        fits[family] = (fit_divcore(points, penalty) if family == "DIVCORE" else
                        fit_mulcore(points, penalty) if family == "MULCORE" else
                        fit(points, mode, penalty))
    return fits


# BIP 440 Appendix A quality gate, checked within every size decade of every path.
QUALITY_GATE = dict(max_rms_factor=1.10, within_factor=1.25, min_within_share=0.95)


def candidate_charge(family, point, candidates):
    """The rounded candidate's charge for one fixture, in the units varops.h charges it."""
    coeff = candidates[family]
    if family == "SIG":
        a, b = candidates["H256"]
        return coeff[0] + a + b * hash_span(64 + point["x"])
    if family in CONSTANT:
        return coeff[0]
    if family in {"DIVCORE", "MULCORE"}:
        return predict(family, point, coeff, {})
    if family in PER_WORD:
        return coeff[0] + coeff[1] * word(point["x"]) / 8
    if family in WORD_PRICED:
        return coeff[0] + coeff[1] * word(point["x"])
    c, v = features(family, point["x"], point["group"])
    return coeff[0] * c + coeff[1] * v


def quality_gate(series, models, machine_keys, machine_names):
    """Every machine fit against its own included fixtures, per path and size decade."""
    gate_failures, bins = [], Counter()
    for family, points in series.items():
        if family in CHECKS or family not in models[0]:
            continue
        for index, (key, machine) in enumerate(zip(machine_keys, machine_names)):
            groups = defaultdict(list)
            for p in points:
                if p["included"] and p["machine"] == key:
                    groups[p["group"], decade(p["x"])].append(p)
            for (group, size_decade), ps in sorted(groups.items()):
                bins[machine] += 1
                errors = [math.log(predict(family, p, models[index][family], models[index]) / p["y"]) for p in ps]
                rms = math.exp(math.sqrt(sum(e * e for e in errors) / len(errors)))
                within = sum(abs(e) <= math.log(QUALITY_GATE["within_factor"]) for e in errors) / len(errors)
                if rms > QUALITY_GATE["max_rms_factor"] or within < QUALITY_GATE["min_within_share"]:
                    # The one-sided figures count only fixtures above the fit, where it under-predicts.
                    under = [min(e, 0.0) for e in errors]
                    gate_failures.append(dict(machine=machine, family=family, group=group, decade=size_decade,
                                              fixtures=len(ps), rms_factor=rms, within_share=within,
                                              max_above_fit=max(1.0, math.exp(-min(errors))),
                                              max_below_fit=max(1.0, math.exp(max(errors))),
                                              rms_above_fit=math.exp(math.sqrt(sum(e * e for e in under) / len(under)))))
    return dict(quality_gate=QUALITY_GATE, gate_bins=sum(bins.values()), bins_by_machine=dict(bins),
                gate_failures=gate_failures)


def charge_coverage(series, schedule, machine_keys, machine_names):
    """Included fixtures measured above the charge of one schedule.

    ratio is measured / charged, with measurements normalized so that a full budget
    takes the machine's pre-v2 reference; above 1, a full budget of that fixture alone
    would take longer than the reference. A primitive fixture above its charge is a
    diagnostic finding; only complete scripts establish a limit violation."""
    above, checked = [], 0
    for family, points in series.items():
        if family not in schedule:
            continue
        names = dict(zip(machine_keys, machine_names))
        for p in points:
            if not p["included"] or p["machine"] not in names:
                continue
            checked += 1
            charge = candidate_charge(family, p, schedule)
            if p["y"] > charge:
                above.append(dict(machine=names[p["machine"]], family=family, fixture=p["label"],
                                  measured_varops=p["y"], charged_varops=charge, ratio=p["y"] / charge))
    above.sort(key=lambda item: -item["ratio"])
    return dict(checked=checked, above_charge=above)


def failed_conditions(machines):
    """Runs that failed their recorded measurement conditions (BIP 440 admission).

    Such a run is repeated, not priced. Runs from before the runner recorded
    conditions have none and are reported as not recorded."""
    return [meta for meta in machines if (meta.get("conditions") or {}).get("repeat_required")]


def report_diagnostics(diag, machines):
    """Print the quality gate, fixtures above each schedule's charge, and run conditions."""
    gate = diag["quality_gate"]
    print(f"\nQuality gate of the machine fits (RMS factor <= {gate['quality_gate']['max_rms_factor']}, "
          f">= {100 * gate['quality_gate']['min_within_share']:g}% within {gate['quality_gate']['within_factor']}x, "
          f"per machine, path and size decade): {len(gate['gate_failures'])} of {gate['gate_bins']} bins fail, "
          f"{sum(item['max_above_fit'] > gate['quality_gate']['within_factor'] for item in gate['gate_failures'])} "
          f"with a fixture more than {gate['quality_gate']['within_factor']}x above its fit")
    for name, coverage in diag["charge_coverage"].items():
        print(f"Charge coverage, {name} schedule: {len(coverage['above_charge'])} of {coverage['checked']} "
              f"fixtures above the charge")
        worst = {}
        for item in coverage["above_charge"]:
            worst.setdefault(item["family"], item)
        for family, item in worst.items():
            count = sum(other["family"] == family for other in coverage["above_charge"])
            print(f"  {family:<10} {count:4d} fixtures; worst {item['ratio']:.2f}x the charge: "
                  f"{item['fixture']} on {item['machine']}")
    for meta in machines:
        conditions = meta.get("conditions")
        if conditions is None:
            print(f"Conditions: {meta['label']}: not recorded (reference measured once, load not sampled)")
        elif conditions["problems"]:
            print(f"Conditions: {meta['label']}: " + "; ".join(conditions["problems"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("calibrations", type=Path, nargs="+", help="Per-machine varop-calibration JSON files")
    parser.add_argument("--output", type=Path, help="Combined JSON (default: beside the first input)")
    parser.add_argument("--implemented", type=Path,
                        help="JSON with the implemented schedule ({source, coefficients}) to check coverage against as well")
    parser.add_argument("--source-root", type=Path, help="Git repository containing the recorded commits for source verification")
    parser.add_argument("--allow-failed-conditions", action="store_true",
                        help="keep runs whose recorded measurement conditions failed, in an exploratory fit")
    parser.add_argument("--allow-source-mismatch", action="store_true",
                        help="retain and label unexplained source differences in an exploratory fit")
    args = parser.parse_args()
    if len(args.calibrations) < 2:
        parser.error("at least two calibration files are required")
    paths = [p.resolve() for p in args.calibrations]
    if len(set(paths)) != len(paths):
        parser.error("calibration paths must be distinct")
    output = args.output.resolve() if args.output else paths[0].parent / "joint-calibration.json"
    if output in paths:
        raise ValueError(f"refusing to overwrite an input: {output}")
    series = defaultdict(list)
    machines = []
    fixture_sets = []
    for path in paths:
        points, meta = load_calibration(path)
        # Record inputs relative to the joint fit, so a dataset folder can move.
        meta["file"] = Path(os.path.relpath(path, output.parent)).as_posix()
        machines.append(meta)
        fixture_sets.append({p["label"] for p in points})
        for point in points:
            series[point["family"]].append(point)
    failed = failed_conditions(machines)
    if failed and not args.allow_failed_conditions:
        raise ValueError("runs failed their measurement conditions and must be repeated: " +
                         "; ".join(f"{Path(meta['file']).name}: {', '.join(meta['conditions']['problems'])}"
                                   for meta in failed))
    if len({meta["head"] for meta in machines}) != 1:
        raise ValueError("inputs were collected from different commits")
    if len({meta['model_id'] for meta in machines}) != 1:
        raise ValueError('cannot combine different costing models; recollect all machines with the frozen candidate')
    base = PRODUCER_ORDER if machines[0]['model_id'] == MODEL_ID else ORDER
    order = [f for f in base + ['MULCORE'] if f in series and f not in CHECKS]
    if args.allow_source_mismatch and args.source_root is None:
        parser.error("--allow-source-mismatch requires --source-root")
    sources = [{name.replace("\\", "/"): digest for name, digest in meta["source_sha256"].items()}
               for meta in machines]
    if any(set(source) != set(sources[0]) for source in sources[1:]):
        raise ValueError("inputs record different source-file sets")
    unmatched = []
    source_check = dict(unmatched=[], note='Recorded source hashes are identical across machines; not independently verified against Git.')
    if args.source_root:
        source_check = check_source_snapshots(args.source_root.resolve(), machines)
        unmatched = source_check['unmatched']
    elif any(source != sources[0] for source in sources[1:]):
        raise ValueError("source hashes differ; supply --source-root to check LF/CRLF equivalence")
    if unmatched and not args.allow_source_mismatch:
        names = ", ".join(sorted({item["path"] for item in unmatched}))
        raise ValueError(f"source differences remain after LF/CRLF normalization: {names}")
    if any(fixtures != fixture_sets[0] for fixtures in fixture_sets[1:]):
        raise ValueError("inputs have different fixture sets")
    models = independent_models(paths)
    max_coeff = maximum_coefficients(models)
    envelope = envelope_model(models, series)
    status = ("exploratory multi-machine fit with unresolved source differences" if unmatched else
              "exploratory multi-machine fit including runs that failed their measurement conditions" if failed else
              "provisional multi-machine fit, not an accepted consensus schedule")
    result = dict(schema="varop-joint-fit-v2", status=status,
                  model_id=machines[0]['model_id'],
                  pricing_basis="envelope",
                  schedule_combination="Envelope of recorded independently fitted machine curves after same-machine normalization (see envelope_combination); DIVCORE is refit per machine from the recorded samples for trimmed-length quotient rows as fixed + step + cell. Round after combining; SIG remains fixed at 500000. Coefficientwise maxima are kept for comparison.",
                  envelope_combination=ENVELOPE,
                  method="Per-machine median of raw fixture epochs, normalized so that a full 40-billion-varop budget of fitted work takes the recorded local pre-v2 reference (rate derived from the recorded reference time, whatever normalization the artifact was collected with); equal path-group and size-decade weights; weighted squared log error with a 100× underprediction penalty; nonnegative predefined coefficients; no coefficient rounding. SIG diagnostic fits do not replace the fixed 500000 allowance.",
                  machines=machines, source_check=source_check,
                  schedule_rounding=dict(coefficient="up to two significant figures, and at least to a whole varop", sig_policy=500000,
                                         rule="Ceiling each coefficient independently, flats and rates alike (rates per byte of W(n) or H(n) or per counted item), to two significant figures and at least to a whole varop; preserve zero/exact multiples; no refitting.",
                                         status="Installed as provisional research candidate; source discrepancy and multi-machine script confirmation remain open."),
                  primitives={})
    print("Primitive  Envelope (varops, unrounded)                   Rounded candidate                      Maximum coefficients (unrounded)")
    for family in order:
        candidate = rounded_candidate(family, envelope[family])
        result["primitives"][family] = dict(envelope_coefficients=envelope[family],
                                            candidate_coefficients=candidate,
                                            candidate_formula=formulas(family, candidate, candidate=True),
                                            maximum_coefficients=max_coeff[family],
                                            maximum_candidate_coefficients=rounded_candidate(family, max_coeff[family]),
                                            notes=("SIG is a measured residual; 500000-varop sigops parity is a separate policy decision" if family == "SIG" else
                                                   "RELEASE capacity is diagnostic, not a consensus charge input" if family == "RELEASE" else ""))
        print(f"{family:<10} {formulas(family, envelope[family]):<48} {formulas(family, candidate, candidate=True):<38} {formulas(family, max_coeff[family])}")
    candidates = {family: record["candidate_coefficients"] for family, record in result["primitives"].items()}
    keys = [str(path) for path in paths]
    for meta in machines:
        meta["label"] = meta["cpu"] if meta["cpu"] not in {None, "", "Unknown"} else Path(meta["file"]).stem
    labels = [meta["label"] for meta in machines]
    schedules = {"candidate": candidates}
    if args.implemented:
        implemented = json.loads(args.implemented.read_text())
        schedules[f"implemented ({implemented['source']})"] = implemented["coefficients"]
    result["diagnostics"] = dict(
        quality_gate=quality_gate(series, models, keys, labels),
        charge_coverage={name: charge_coverage(series, schedule, keys, labels)
                         for name, schedule in schedules.items()})
    report_diagnostics(result["diagnostics"], machines)
    if output.exists():
        previous = json.loads(output.read_text())
        # Older fits recorded absolute paths; joining keeps those unchanged.
        old_inputs = [((output.parent / m["file"]).resolve(), m["sha256"]) for m in previous.get("machines", [])]
        new_inputs = [(path, m["sha256"]) for path, m in zip(paths, machines)]
        if previous.get("schema") not in {"varop-joint-fit-v1", result["schema"]} or old_inputs != new_inputs[:len(old_inputs)]:
            raise ValueError(f"refusing to overwrite a result from different inputs: {output}")
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                         prefix=".joint-calibration-", suffix=".json",
                                         delete=False) as destination:
            pending = Path(destination.name)
            json.dump(result, destination, indent=2)
            destination.write("\n")
        os.replace(pending, output)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    print(f"Saved {os.path.relpath(output)}")


if __name__ == "__main__":
    main()
