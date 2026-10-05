#!/usr/bin/env python3
"""Fit one provisional primitive schedule from multiple calibration JSON files.

Each machine's fixture timings are normalized by its own pre-v2 reference: a full
40-billion-varop budget of fitted work is priced to take TARGET_FRACTION of that
machine's recorded pre-v2 worst case. The pricing rate is derived from the recorded reference time,
whatever normalization the artifact was collected with. Runs whose epoch noise
exceeds the BIP 440 limit are rejected unless explicitly allowed. Inputs
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


CONSTANT = {"F", "SIG", "TWEAK", "NORMALIZE"}
# The measured families of each bench_varops_primitives model. read-write-arith-v1
# times BIP 440's READ, WRITE and ARITH directly. producer-normalize-v1, the model of
# the recorded runs, timed their parts as families of their own: an operand's
# conversion (PREP) apart from its scans (READ), producing a value (PRODUCE) apart
# from converting a numeric result to bytes (NORMALIZE), and word passes without a
# carry chain (BIT) apart from ARITH.
FAMILIES = {
    "read-write-arith-v1": "F READ WRITE ARITH MOVE MULCORE DIVCORE H256 H160 H1 SIG TWEAK SELECT UNROLL".split(),
    "producer-normalize-v1": "F PREP PRODUCE NORMALIZE READ ARITH BIT MOVE MULCORE DIVCORE H256 H160 H1 SIG TWEAK SELECT UNROLL".split(),
}
MODEL_ID = "read-write-arith-v1"
# Measured compositions of existing primitives: compared against their charge, never priced.
CHECKS = {"UNROLL"}
# BIP 440's priced primitives.
PRICED = "F READ WRITE ARITH MOVE MULCORE DIVCORE HASH SIG TWEAK SELECT".split()
# The hash functions, each measured as a family of its own: SHA256, RIPEMD160, SHA1.
HASHES = ("H256", "H160", "H1")
# How a model's measured families compose a priced primitive: READ pays for both
# converting and scanning an operand and WRITE for both converting a numeric
# result and producing it, so their parts add; ARITH covers passes with and
# without a carry chain and HASH every hash function, so each takes the larger
# of its parts. Other priced primitives are measured families of the same name.
COMPOSED = {
    "read-write-arith-v1": {"HASH": ("max", HASHES)},
    "producer-normalize-v1": {"READ": ("sum", ("PREP", "READ")), "WRITE": ("sum", ("PRODUCE", "NORMALIZE")),
                              "ARITH": ("max", ("ARITH", "BIT")), "HASH": ("max", HASHES)},
}


def charged_by(model_id):
    """The priced primitive that charges each measured family of a model."""
    out = {family: family for family in FAMILIES[model_id] if family in PRICED}
    for primitive, (_, parts) in COMPOSED[model_id].items():
        out.update({part: primitive for part in parts})
    return out


def priced_model(model, model_id):
    """One machine's curves of the priced primitives, composed from its measured fits.

    Every composed part is a fixed term and a rate on W(n), or on H(n) for the hashes; a
    constant fit has a zero rate. A larger-of composition takes the larger flat and the
    larger rate, which covers every part at every size."""
    out = {}
    for family in PRICED:
        how, parts = COMPOSED[model_id].get(family, ("sum", (family,)))
        if any(part not in model for part in parts):
            continue
        curves = [model[part] for part in parts]
        combine = sum if how == "sum" else max
        out[family] = tuple(combine(curve[i] for curve in curves) for i in range(len(curves[0])))
    return out


def compose_candidates(candidates, model_id):
    """Prices of the priced primitives composed from rounded prices of the measured
    families, then rounded again: a sum of rounded flats need not be a rounded flat."""
    composed = priced_model({family: tuple(c) for family, c in candidates.items()}, model_id)
    return {family: tuple(rounded_candidate(family, curve)) for family, curve in composed.items()}
BUDGET_VAROPS = 40_000_000_000
# BIP 440 measurement condition: a run whose epoch noise exceeds this is repeated,
# not priced. Load average and reference drift, which earlier runners recorded,
# are not conditions.
MAX_EPOCH_NOISE = 0.015
# Fitting target: a full budget of fitted work takes 0.9x each machine's pre-v2
# reference, a margin for composition effects and machine variation the fixtures
# do not capture. Complete scripts must stay below 1.0x on every machine.
TARGET_FRACTION = 0.9


def pricing_rate(normalization, fraction=TARGET_FRACTION):
    """Varops per nanosecond at which a full budget takes fraction of the recorded reference time.

    Other fractions only check the rate an artifact recorded when it was collected."""
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


def check_bench_sources(machines):
    """Check that every machine ran this repository's benchmarks from one recorded commit.

    None when no artifact records them: runners in the gsr branch built the benchmarks
    from gsr sources, which source_sha256 covers."""
    benches = [machine.get('bench') for machine in machines]
    if not any(benches):
        return None
    if not all(benches) or len({bench['head'] for bench in benches}) != 1:
        raise ValueError('inputs were measured with benchmarks from different varopsData commits')
    return check_source_snapshots(Path(__file__).resolve().parents[1], [
        dict(file=machine['file'], head=bench['head'], source_sha256=bench['source_sha256'])
        for machine, bench in zip(machines, benches)])


def word(n):
    return (n + 7) // 8 * 8


def hash_span(n):
    """Bytes processed by a 64-byte-block hash: message, at least 9 padding bytes, whole blocks."""
    return (n + 72) // 64 * 64


def features(family, x, group):
    if family in {"DIVCORE", "MULCORE"}:
        return x, x * int(group.split("=")[1])
    if family in HASHES + ("HASH",):
        return 1, hash_span(x)
    if family in {"PREP", "NORMALIZE"}:
        return 1, word(x)
    return 1, x


def fixture(row, producer_manifest=None):
    parts = row["probe"].split("/")
    family = parts[0]
    x, group, included = 1, "measurement", True
    y = float(row["ns_per_execution"])
    if family in {"PRODUCE", "NORMALIZE", "WRITE"}:
        record = producer_manifest[row['probe']]
        count = int(record['items'])
        group = '/'.join(parts[1:-1])
        x = int(record['normalize_bytes']) if family == 'NORMALIZE' else int(record['bytes']) / count
        y /= count
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
    elif family == "ARITH" and parts[1] in {"add", "sub"}:
        # ARITH/<add|sub>/<words>/...; the other ARITH and BIT fixtures name bytes.
        x, group = int(parts[2]) * 8, parts[1] + "/" + "/".join(parts[3:])
    elif family in {"READ", "BIT", "ARITH"}:
        x, group = word(int(parts[2])), parts[1]
        if len(parts) > 3:
            group += "/" + parts[3]
    elif family == "MOVE":
        # MOVE/<depth>/...: rolling from depth takes depth + 1 entries off and puts them back.
        x, group = int(parts[1]) + 1, parts[2]
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
        # SELECT/<kind>/<format>/<records>/<units>.
        x = int(parts[4])
        group, included = parts[1] + "/" + parts[2], parts[2] == "collated"
    elif family == "UNROLL":
        # UNROLL/<shape>/<units>/<unrolled bytes>/<charged varops>: time per charged
        # unit against bytes per unit; the charge covers the complete script.
        units = int(parts[2])
        x, group = int(parts[3]) / units, parts[1].split("-")[0]
        y /= units
        return dict(label=row["probe"], family=family, x=x, y=y, c=1, v=x, group=group,
                    included=included, units=units, bytes=int(parts[3]),
                    charged=int(parts[4]))
    elif family != "TWEAK":
        x = int(parts[1])
    c, v = features(family, x, group)
    return dict(label=row["probe"], family=family, x=x, y=y, c=c, v=v,
                group=group, included=included)


def load_calibration(path):
    data = json.loads(path.read_text())
    if data.get("schema") != "varop-calibration-v1":
        raise ValueError(f"{path}: unsupported calibration schema")
    model_id = data.get('model_id')
    if model_id not in FAMILIES:
        raise ValueError(f'{path}: unknown costing model {model_id}')
    manifest = {r['probe']: r for r in data.get('producer_manifest', [])}
    normalization = data["normalization"]
    recorded_rate = float(normalization["varops_per_nanosecond"])
    if int(normalization.get("budget_varops", BUDGET_VAROPS)) != BUDGET_VAROPS:
        raise ValueError(f"{path}: unexpected budget")
    if not math.isclose(recorded_rate, pricing_rate(normalization, 1.0), rel_tol=1e-12):
        raise ValueError(f"{path}: inconsistent normalization")
    rate = pricing_rate(normalization)
    epochs = len({int(row["epoch"]) for row in data["primitive_samples"]})
    machine = str(path)
    observations = defaultdict(list)
    for row in data["primitive_samples"]:
        name = row["probe"]
        if name.startswith("PRODUCER_CHECK/"):
            continue
        if name.split("/", 1)[0] not in FAMILIES[model_id]:
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
    if {p["family"] for p in points} != set(FAMILIES[model_id]):
        raise ValueError(f"{path}: missing primitive family")
    meta = dict(file=str(path), model_id=model_id, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                cpu=data["machine"]["cpu"], epochs=epochs,
                # Earlier runners recorded the commit with their local fit.
                head=data.get("head") or data.get("fitting", {}).get("metadata", {}).get("head"),
                fixture_count=len(points), reference_seconds=data["normalization"]["local_pre_v2_worst_seconds"],
                varops_per_nanosecond=rate, source_sha256=data["source_sha256"], bench=data.get("bench"),
                conditions=data.get("conditions"), epoch_noise=epoch_noise(data["primitive_samples"]))
    return points, meta


def epoch_noise(samples):
    """Median over fixtures of their epochs' relative median absolute deviation.

    None with fewer than three epochs. Word-rounded size aliases repeat a probe
    name within a pass; the benchmark keeps them apart by their order in it.
    Matches the runner's check, and judges runs from before it alike.
    """
    seen = Counter()
    epochs = defaultdict(list)
    for sample in samples:
        key = sample["probe"], sample["epoch"]
        epochs[sample["probe"], seen[key]].append(float(sample["ns_per_execution"]))
        seen[key] += 1
    deviations = []
    for times in epochs.values():
        middle = statistics.median(times)
        if len(times) >= 3 and middle > 0:
            deviations.append(statistics.median(abs(time - middle) for time in times) / middle)
    return statistics.median(deviations) if deviations else None


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


# machine_model's fits in three independent parts, slowest first, so a process
# pool can run every machine's parts at once. They merge in the order below.
REFIT_PARTS = ('MULCORE', 'DIVCORE', 'other')
FIT_PROCESSES = 12


def refit_part(path, part):
    """One part of machine_model's fits from the recorded samples."""
    points, _ = load_calibration(Path(path))
    series = defaultdict(list)
    for point in points:
        if point['included']:
            series[point['family']].append(point)

    if part == 'DIVCORE':
        return {'DIVCORE': tuple(fit_divcore(series['DIVCORE'], 100))}
    if part == 'MULCORE':
        return {'MULCORE': tuple(fit_mulcore(series['MULCORE'], 100))} if series['MULCORE'] else {}
    model = {}
    # SIG is fitted last: its background is the challenge hash at the H256 fit.
    families = [f for f in series if f not in {'MULCORE', 'DIVCORE'} | CHECKS]
    for family in sorted(families, key=lambda f: f == 'SIG'):
        for point in series[family]:
            point['background'] = background(family, point['x'], model)
        mode = 'residual' if family == 'SIG' else 'constant' if family in CONSTANT else 'affine'
        model[family] = tuple(fit(series[family], mode, 100))
    return model


def machine_model(path, refits=None):
    """One machine's 100x under-penalty fit in varops, from its recorded samples.

    refits maps each of REFIT_PARTS to its refit_part result, when computed elsewhere.
    """
    if refits is None:
        refits = {part: refit_part(path, part) for part in REFIT_PARTS}
    model = {}
    for part in ('DIVCORE', 'MULCORE', 'other'):
        model.update(refits[part])
    return model


ENVELOPE = ("Envelope: the cheapest curve of each family's form that covers every machine's fitted curve at every "
            "chargeable size. With feature vector phi(x) >= 0 and machine fits theta_m, it minimizes the weighted mean "
            "relative charge sum_x w(x) theta.phi(x) / max_m theta_m.phi(x) over the family's fixtures x (same "
            "path-group and size-decade weights as the fits), subject to theta.phi(x) >= theta_m.phi(x) for every "
            "machine m and every chargeable size x, and theta >= 0. Sizes start at zero except where an operation "
            "cannot be smaller: a hash processes at least one 64-byte block, a division has at least one quotient row "
            "and one divisor limb, and a multiplication's shorter operand is at most as long as the longer one. RIPEMD160 and SHA1 take at most 520 bytes; other sizes are unbounded. Where sizes "
            "start at zero and are unbounded, the envelope equals the coefficientwise maximum. Constant primitives "
            "and SIG use the maximum. A primitive that takes the larger of measured parts (ARITH, HASH) covers every "
            "machine's curve of every part, each over the part's own chargeable sizes, and weighs each fixture "
            "against its own part's curves.")


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


def envelope_coefficients(family, models, points, parts=None):
    """The ENVELOPE curve of one family: an exact linear program over the family's
    few coefficients, solved by checking every vertex of the feasible region.

    points are one machine's included fixtures of the family (all machines share them).
    parts are the measured families of a primitive that takes the larger of them: the
    curve then covers every machine's curve of each part over that part's sizes."""
    parts = parts or (family,)
    curves = {part: [tuple(model[part]) for model in models] for part in parts}
    if family in CONSTANT:
        return tuple(max(curve[i] for curve in curves[family]) for i in range(len(curves[family][0])))
    k = len(curves[parts[0]][0])
    constraints = {}
    for part in parts:
        corners, rays = feature_domain(part)
        for direction in corners + rays:
            bound = max(sum(t * e for t, e in zip(curve, direction)) for curve in curves[part])
            constraints[direction] = max(constraints.get(direction, bound), bound)
    for i in range(k):
        constraints.setdefault(tuple(int(i == j) for j in range(k)), 0.0)
    constraints = list(constraints.items())
    features_of = ((lambda p: (1, p['c'], mul_rows(p), p['v'])) if k == 4 else
                   (lambda p: (1, p['c'], p['v'])) if k == 3 else (lambda p: (p['c'], p['v'])))
    if len(parts) > 1:
        # Paths of different parts are different groups.
        points = [dict(p, group=f"{p['family']}/{p['group']}") for p in points]
    ws = weights(points)
    gradient = [0.0] * k
    for w, p in zip(ws, points):
        phi = features_of(p)
        own = curves[p['family']] if len(parts) > 1 else curves[family]
        target = max(sum(t * f for t, f in zip(curve, phi)) for curve in own)
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


def envelope_model(models, series, measured=None, parts=None):
    """ENVELOPE coefficients for every family of the machine models; series holds the
    fixtures of all machines, of which the first machine's are used for weights. A
    family in parts takes the larger of those measured families, whose curves the
    measured machine models hold."""
    parts = parts or {}
    first = next(iter(series.values()))[0]['machine']
    out = {}
    for family in models[0]:
        points = [p for p in series.get(family, []) if p['included'] and p['machine'] == first]
        out[family] = (envelope_coefficients(family, measured, points, parts[family]) if family in parts else
                       envelope_coefficients(family, models, points))
    return out


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
WORD_PRICED = {'PREP', 'PRODUCE', 'READ', 'WRITE', 'ARITH', 'BIT'}


def ceil_to(value, step):
    """Round up to a multiple of step; an exact multiple is kept."""
    return int(step * math.ceil(value / step - 1e-9))


def round_coefficient(value):
    """Up to two significant figures, and at least to a whole varop: the step is at most
    a tenth of the value from 10 up, so rounding adds at most 10% there."""
    if value <= 0:
        return 0
    return ceil_to(value, max(1, 10 ** (math.floor(math.log10(value)) - 1)))


def round_flat(value):
    """Flats round up in coarser steps than rates: to a multiple of 10 below 100 and of 50
    from 100, and never to more than two significant figures. A flat is the intercept of a
    fit, which moves most between runs; coarse steps keep it from changing on every refit."""
    if value <= 0:
        return 0
    return max(round_coefficient(value), ceil_to(value, 10 if value < 100 else 50))


def rounded_candidate(family, coeff):
    """Schedule adoption, not refitting: each coefficient rounded up on its own, the flat
    with round_flat and rates with round_coefficient; SIG stays at its fixed allowance."""
    if family == 'SIG':
        return [500000, 0]
    return [round_flat(coeff[0])] + [round_coefficient(v) for v in coeff[1:]]


def terms(*pairs):
    """Sum of coefficient × variable terms as BIP 440 writes them: zero terms are left out,
    and a coefficient of one is not written."""
    shown = [f"{c:.6g}" if not v else v if c == 1 else f"{c:.6g} × {v}" for c, v in pairs if c != 0]
    return " + ".join(shown) or "0"


def formulas(family, coeff, candidate=False):
    """Formula text; candidates are charged on W(n) where the fit is per byte of n."""
    if family in CONSTANT:
        return f"{coeff[0]:.6g}"
    if family == "MULCORE" and len(coeff) == 4:
        return terms((coeff[0], ''), (coeff[1], 'u'), (coeff[2], 'v'), (coeff[3], 'u × v'))
    if family == "MULCORE":
        return terms((coeff[0], ''), (coeff[1], 'u'), (coeff[2], 'u × v'))
    if family == "DIVCORE":
        return terms((coeff[0], ''), (coeff[1], 's'), (coeff[2], 's × v'))
    if candidate and family in WORD_PRICED:
        variable = "W(n)"
    else:
        variable = ("W(n)" if family in {"PREP", "READ", "ARITH", "BIT", "NORMALIZE"} else "k" if family in {"MOVE", "SELECT"}
                    else "H(n)" if family in HASHES + ("HASH",) else "n")
    return terms((coeff[0], ''), (coeff[1], variable))


def fit_all(series, penalty):
    fits = {}
    order = [f for f in dict.fromkeys(FAMILIES["producer-normalize-v1"] + FAMILIES[MODEL_ID])
             if f in series and f not in CHECKS]
    for family in [f for f in order if f != 'SIG'] + [f for f in ('SIG',) if f in order]:
        points = [p for p in series[family] if p["included"]]
        for point in points:
            point["background"] = background(family, point["x"], fits)
        mode = "residual" if family == "SIG" else "constant" if family in CONSTANT else "affine"
        fits[family] = (fit_divcore(points, penalty) if family == "DIVCORE" else
                        fit_mulcore(points, penalty) if family == "MULCORE" else
                        fit(points, mode, penalty))
    return fits


# METHODOLOGY.md quality gate, checked within every size decade of every path.
QUALITY_GATE = dict(max_rms_factor=1.10, within_factor=1.25, min_within_share=0.95)


def candidate_charge(family, point, candidates):
    """The rounded candidate's charge for one fixture, in the units varops.h charges it."""
    coeff = candidates[family]
    if family == "SIG":
        a, b = candidates["HASH"]
        return coeff[0] + a + b * hash_span(64 + point["x"])
    if family in CONSTANT:
        return coeff[0]
    if family in {"DIVCORE", "MULCORE"}:
        return predict(family, point, coeff, {})
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


def charge_coverage(series, schedule, machine_keys, machine_names, charged=None):
    """Included fixtures measured above the charge of one schedule.

    charged maps a measured family to the priced primitive that charges its fixtures
    (charged_by); by default each family is its own. ratio is measured / charged, with
    measurements normalized so that a full budget takes TARGET_FRACTION of the
    machine's pre-v2 reference; above 1, a full budget of that fixture alone would
    take longer than that. A primitive fixture above its charge is a diagnostic
    finding; only complete scripts establish a limit violation."""
    above, checked = [], 0
    for family, points in series.items():
        primitive = (charged or {}).get(family, family)
        if primitive not in schedule:
            continue
        names = dict(zip(machine_keys, machine_names))
        for p in points:
            if not p["included"] or p["machine"] not in names:
                continue
            checked += 1
            charge = candidate_charge(primitive, p, schedule)
            if p["y"] > charge:
                above.append(dict(machine=names[p["machine"]], family=family, primitive=primitive,
                                  fixture=p["label"], measured_varops=p["y"], charged_varops=charge,
                                  ratio=p["y"] / charge))
    above.sort(key=lambda item: -item["ratio"])
    return dict(checked=checked, above_charge=above)


def lifetime_checks(path, model, machine):
    """Held-out lifetimes (PRODUCER_CHECK) measured above one machine's composed fit.

    model holds the machine's priced curves (priced_model). A numeric lifetime
    produces its bytes, which is a WRITE, then reads them as a number and writes the
    result at the word span of its bytes; other lifetimes are their WRITE events.
    ratio is measured / composed; the lifetimes are never fitted."""
    data = json.loads(Path(path).read_text())
    rate = pricing_rate(data["normalization"])
    observations = defaultdict(list)
    for sample in data["primitive_samples"]:
        if sample["probe"].startswith("PRODUCER_CHECK/"):
            observations[sample["probe"]].append(float(sample["ns_per_execution"]))

    def charge(family, count, span):
        return model[family][0] * count + model[family][1] * span

    above, checked = [], 0
    for row in data.get("producer_manifest", []):
        if not row["probe"].startswith("PRODUCER_CHECK/"):
            continue
        count, size, numeric = (int(row[k]) for k in ("items", "bytes", "normalize_bytes"))
        composed = charge("WRITE", count, size)
        if row["kind"] == "numeric":
            span = word(numeric)
            composed += charge("READ", 1, span) + charge("WRITE", 1, span)
        measured = statistics.median(observations[row["probe"]]) * rate
        checked += 1
        if measured > composed:
            above.append(dict(machine=machine, fixture=row["probe"], measured_varops=measured,
                              composed_varops=composed, ratio=measured / composed))
    return checked, above


def failed_conditions(machines):
    """Runs whose epoch noise exceeds the BIP 440 limit; such a run is repeated, not priced."""
    return [meta for meta in machines if (meta.get("epoch_noise") or 0) > MAX_EPOCH_NOISE]


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
    lifetimes = diag["held_out_lifetimes"]
    worst = max(lifetimes["above_fit"], key=lambda item: item["ratio"], default=None)
    print(f"Held-out lifetimes against each machine's fit: {len(lifetimes['above_fit'])} of {lifetimes['checked']} "
          f"above the composed fit" + (f"; worst {worst['ratio']:.2f}x: {worst['fixture']} on {worst['machine']}" if worst else ""))
    for meta in machines:
        noise = meta.get("epoch_noise")
        print(f"Conditions: {meta['label']}: " + ("fewer than three epochs, epoch noise not measured" if noise is None else
              f"epoch noise {100 * noise:.2f}%" + (f" exceeds {100 * MAX_EPOCH_NOISE:g}%" if noise > MAX_EPOCH_NOISE else "")))


def composition_text(composed):
    """How schedule_combination describes a model's composed primitives."""
    if not composed:
        return ""
    sums = [family for family, (how, _) in composed.items() if how == "sum"]
    maxima = [family for family, (how, _) in composed.items() if how == "max"]
    rules = []
    if sums:
        rules.append(f"{' and '.join(sums)} add{'' if len(sums) > 1 else 's'} the parts' prices")
    if maxima:
        rules.append(f"{' and '.join(maxima)} take{'' if len(maxima) > 1 else 's'} the larger flat and the larger rate "
                     f"of {'their' if len(maxima) > 1 else 'its'} parts")
    return (" Primitives measured in parts (composed_from) are priced by composing the parts' rounded prices and "
            "rounding the result again: " + "; ".join(rules) + ". The envelope of a sum covers each machine's composed "
            "curve, that of a larger-of every machine's curve of each part over the part's own sizes; it is compared "
            "with the price.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("calibrations", type=Path, nargs="+", help="Per-machine varop-calibration JSON files")
    parser.add_argument("--output", type=Path, help="Combined JSON (default: beside the first input)")
    parser.add_argument("--implemented", type=Path,
                        help="JSON with the implemented schedule ({source, coefficients}) to check coverage against as well")
    parser.add_argument("--source-root", type=Path, help="Git repository containing the recorded commits for source verification")
    parser.add_argument("--allow-failed-conditions", action="store_true",
                        help="keep runs whose epoch noise exceeds the limit, in an exploratory fit")
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
                         "; ".join(f"{Path(meta['file']).name}: epoch noise {100 * meta['epoch_noise']:.2f}% "
                                   f"exceeds {100 * MAX_EPOCH_NOISE:g}%" for meta in failed))
    if len({meta["head"] for meta in machines}) != 1:
        raise ValueError("inputs were collected from different commits")
    if len({meta['model_id'] for meta in machines}) != 1:
        raise ValueError('cannot combine different costing models; recollect all machines with the frozen candidate')
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
    bench_check = check_bench_sources(machines)
    if bench_check and bench_check["unmatched"]:
        names = ", ".join(sorted({item["path"] for item in bench_check["unmatched"]}))
        raise ValueError(f"benchmark sources differ from their recorded varopsData commit: {names}")
    if any(fixtures != fixture_sets[0] for fixtures in fixture_sets[1:]):
        raise ValueError("inputs have different fixture sets")
    model_id = machines[0]['model_id']
    models = independent_models(paths)
    measured_envelope = envelope_model(models, series)
    charged = charged_by(model_id)
    priced = [priced_model(model, model_id) for model in models]
    priced_series = defaultdict(list)
    for family, points in series.items():
        if family in charged:
            priced_series[charged[family]].extend(points)
    max_coeff = maximum_coefficients(priced)
    composed = COMPOSED[model_id]
    envelope = envelope_model(priced, priced_series, models,
                              {family: members for family, (how, members) in composed.items() if how == "max"})
    order = [family for family in PRICED if family in envelope]
    parts = [part for _, (_, members) in composed.items() for part in members]
    # A composed primitive's price composes its parts' rounded prices, as varops.h
    # merges them, rounded again; its envelope, which covers each machine's composed
    # curve or every machine's curve of each part, is compared with that price.
    part_candidates = {part: rounded_candidate(part, measured_envelope[part]) for part in parts}
    part_maxima = {part: rounded_candidate(part, maximum_coefficients(models)[part]) for part in parts}
    composed_candidates = compose_candidates(part_candidates, model_id)
    composed_maxima = compose_candidates(part_maxima, model_id)
    status = ("exploratory multi-machine fit with unresolved source differences" if unmatched else
              "exploratory multi-machine fit including runs that failed their measurement conditions" if failed else
              "provisional multi-machine fit, not an accepted consensus schedule")
    result = dict(schema="varop-joint-fit-v3", status=status,
                  model_id=model_id,
                  pricing_basis="envelope",
                  schedule_combination="Envelope of machine curves, each fitted independently from the samples recorded on that machine after same-machine normalization (see envelope_combination); DIVCORE rows are trimmed-length quotient rows, fitted as fixed + step + cell. Round after combining; SIG remains fixed at 500000. Coefficientwise maxima are kept for comparison."
                                       + composition_text(composed),
                  envelope_combination=ENVELOPE,
                  method=f"Per-machine median of raw fixture epochs, normalized so that a full 40-billion-varop budget of fitted work takes {TARGET_FRACTION:g}× the recorded local pre-v2 reference (rate derived from the recorded reference time, whatever normalization the artifact was collected with); equal path-group and size-decade weights; weighted squared log error with a 100× underprediction penalty; nonnegative predefined coefficients; no coefficient rounding. SIG diagnostic fits do not replace the fixed 500000 allowance.",
                  machines=machines, source_check=source_check, bench_check=bench_check,
                  schedule_rounding=dict(coefficient="flats to a multiple of 10 below 100 and of 50 from 100, and to no more than two significant figures; rates to two significant figures, and at least to a whole varop", sig_policy=500000,
                                         rule="Ceiling each coefficient independently: flats to a multiple of 10 below 100 and of 50 from 100, never to more than two significant figures; rates (per byte of W(n) or H(n) or per counted item) to two significant figures and at least to a whole varop; preserve zero/exact multiples; no refitting. A price composed from rounded parts is rounded again by the same rule.",
                                         status="Installed as provisional research candidate; source discrepancy and multi-machine script confirmation remain open."),
                  primitives={}, measured_parts={})
    print("Primitive  Envelope (varops, unrounded)                   Rounded candidate                      Maximum coefficients (unrounded)")
    for family in order:
        candidate = (list(composed_candidates[family]) if family in composed else
                     rounded_candidate(family, envelope[family]))
        maximum_candidate = (list(composed_maxima[family]) if family in composed else
                             rounded_candidate(family, max_coeff[family]))
        record = dict(envelope_coefficients=envelope[family],
                      candidate_coefficients=candidate,
                      candidate_formula=formulas(family, candidate, candidate=True),
                      maximum_coefficients=max_coeff[family],
                      maximum_candidate_coefficients=maximum_candidate,
                      notes="SIG is a measured residual; 500000-varop sigops parity is a separate policy decision" if family == "SIG" else "")
        if family in composed:
            record.update(composed_from=list(composed[family][1]), composition=composed[family][0])
        result["primitives"][family] = record
        print(f"{family:<10} {formulas(family, envelope[family]):<48} {formulas(family, candidate, candidate=True):<38} {formulas(family, max_coeff[family])}")
    if parts:
        print("Measured in parts:")
    for part in parts:
        candidate = part_candidates[part]
        result["measured_parts"][part] = dict(primitive=charged[part], envelope_coefficients=measured_envelope[part],
                                              candidate_coefficients=candidate,
                                              candidate_formula=formulas(part, candidate, candidate=True))
        print(f"  {part:<8} {formulas(part, measured_envelope[part]):<48} {formulas(part, candidate, candidate=True)}")
    candidates = {family: record["candidate_coefficients"] for family, record in result["primitives"].items()}
    keys = [str(path) for path in paths]
    for meta in machines:
        meta["label"] = meta["cpu"] if meta["cpu"] not in {None, "", "Unknown"} else Path(meta["file"]).stem
    labels = [meta["label"] for meta in machines]
    schedules = {"candidate": candidates}
    if args.implemented:
        implemented = json.loads(args.implemented.read_text())
        schedules[f"implemented ({implemented['source']})"] = implemented["coefficients"]
    lifetimes = [lifetime_checks(path, model, label) for path, model, label in zip(paths, priced, labels)]
    result["diagnostics"] = dict(
        quality_gate=quality_gate(series, models, keys, labels),
        charge_coverage={name: charge_coverage(series, schedule, keys, labels, charged)
                         for name, schedule in schedules.items()},
        held_out_lifetimes=dict(checked=sum(checked for checked, _ in lifetimes),
                                above_fit=sorted((item for _, above in lifetimes for item in above),
                                                 key=lambda item: -item["ratio"])))
    report_diagnostics(result["diagnostics"], machines)
    if output.exists():
        previous = json.loads(output.read_text())
        old_inputs = [((output.parent / m["file"]).resolve(), m["sha256"]) for m in previous.get("machines", [])]
        new_inputs = [(path, m["sha256"]) for path, m in zip(paths, machines)]
        if previous.get("schema") != result["schema"] or old_inputs != new_inputs[:len(old_inputs)]:
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
