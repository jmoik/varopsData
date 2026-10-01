#!/usr/bin/env python3
"""Render a self-contained comparison of per-machine and joint primitive costs."""

import argparse
from collections import defaultdict
import hashlib
import html
import json
import math
import os
import re
from pathlib import Path
import tempfile

from restyle_report import restyle
from fit_calibrations import MAX_EPOCH_NOISE, TARGET_FRACTION, candidate_charge, check_source_snapshots, envelope_model, features, formulas, hash_span, failed_conditions, independent_models, load_calibration, predict, rounded_candidate


# Sections: the BIP 440 primitive categories, then one section per later BIP. A
# primitive or opcode appears under the BIP that introduced it; groups within a
# section are headed only when there is more than one.
BIP440 = "Primitives defined by BIP 440."
TITLE = "Varops multi-machine calibration"

SECTIONS = [
    dict(slug="category-interpreter", title="Interpreter",
         intro=BIP440 + " BIP 441 restores opcodes without adding primitives: each restored opcode is priced entirely from these.",
         groups=[("Interpreter", ("F",))]),
    dict(slug="category-stack", title="Stack and byte processing", intro=BIP440,
         groups=[("Stack and byte processing", ("PRODUCE", "READ", "MOVE"))]),
    dict(slug="category-numeric", title="Numeric and bit operations", intro=BIP440,
         groups=[("Numeric and bit operations", ("PREP", "NORMALIZE", "ARITH", "BIT", "MULCORE", "DIVCORE"))]),
    dict(slug="category-crypto", title="Hashing and signatures", intro=BIP440,
         groups=[("Hashing and signatures", ("H256", "H160", "H1", "SIG"))]),
    dict(slug="section-extended-primitives", title="Extended Primitives · OP_CHECKSIGFROMSTACK, OP_TWEAKADD, OP_BYTEREV",
         intro="Defines one new primitive, TWEAK, for OP_TWEAKADD. OP_CHECKSIGFROMSTACK and OP_BYTEREV are charged from BIP 440 primitives; their checks compare measurements with that composition.",
         groups=[("Opcodes", ("TWEAK", "CSFS", "BYTEREV"))]),
    dict(slug="section-optx", title="OP_TX",
         intro="Defines one new primitive, OP_TX_SELECT, for selecting and encoding transaction fields.",
         groups=[("OP_TX", ("SELECT",))]),
    dict(slug="section-macros", title="Reusable Macros · OP_MACRO, OP_CALLMACRO",
         intro="Adds no primitive. Unrolling is charged as BASE per substituted instruction and visited reference, plus WRITE of the unrolled script once per script with declarations. The check compares complete unrolling with that charge.",
         groups=[("Unrolling", ("UNROLL",))]),
]
# Prices implemented in src/script/varops.h, compared against the joint candidate.
CURRENT_COSTS = {'F': '310', 'PREP': '180 + W(n)', 'PRODUCE': '740 + 8 × W(n)', 'NORMALIZE': '200',
                 'READ': '79 + W(n)', 'ARITH': '120 + 3 × W(n)', 'BIT': '190 + 2 × W(n)', 'MOVE': '180 + 19 × k',
                 'MULCORE': '330 + 10 × u + 110 × v + 29 × u × v', 'DIVCORE': '500 × s + 33 × s × v',
                 'H256': '260 + 38 × H(n)', 'H160': '53 + 40 × H(n)',
                 'H1': '190 + 24 × H(n)', 'SIG': '500000', 'TWEAK': '170000',
                 'SELECT': '2300 + 270 × k'}
BASE, WRITE, SHA256, BIT, SIGCHECK = 310, (740, 8), (260, 38), (190, 2), 500_000
PREPARE, READ = (180, 1), (79, 1)  # per byte of W(n)


def unroll_charge(units, length, base=BASE, write=WRITE, prepare=PREPARE, read=READ, padded=True):
    """Complete charge of an UNROLL fixture, as bench_varops_primitives composes it: the
    unrolling charge, then OP_0, OP_IF, OP_ENDIF, OP_1 and the final check of its result."""
    span = (lambda n: (n + 7) // 8 * 8) if padded else (lambda n: n)
    def write_cost(n):
        return write[0] + write[1] * span(n)
    return (units * base + write_cost(length) + 4 * base + write_cost(0) + write_cost(8) +
            prepare[0] + prepare[1] * 8 + read[0] + read[1] * 8)

# Opcodes charged from existing primitives. Each measured fixture is divided by
# its implemented charge; a ratio above 1 means the charge does not cover it.
CHECKS = {
    'CSFS': dict(
        source='SIG', select=lambda p: True,
        charge=f'{SIGCHECK + SHA256[0]} + {SHA256[1]} × H(64 + n)',
        composition='SIGCHECK + SHA256(64 + n)',
        charged=lambda p: SIGCHECK + SHA256[0] + SHA256[1] * hash_span(64 + p['x']),
        curve=lambda x: SIGCHECK + SHA256[0] + SHA256[1] * hash_span(64 + round(x)),
        xlabel='Message bytes n',
        models='One BIP 340 verification of an <code>n</code>-byte message, as OP_CHECKSIGFROMSTACK performs it. The challenge hash covers <code>R || P || msg</code> and must use the same SHA256 implementation as OP_SHA256, so its message bytes are covered by the SHA256 primitive.',
        fixtures='<code>SIG/&lt;n&gt;</code>: complete verification of a valid signature over an <code>n</code>-byte message, <code>n</code> ∈ {0, 32, 64, 128, 1,024, 8,192, 65,536, 262,144, 1,048,576, 4,000,000}.',
        note='Signature verification uses Core’s SHA256 through libsecp256k1’s compression hook since the 2026-09-28 implementation; measurements collected before then used libsecp256k1’s portable SHA256.'),
    'BYTEREV': dict(
        source='BIT', select=lambda p: p['group'].startswith('byterev'),
        charge='BIT(W(n))',
        charged=lambda p: BIT[0] + BIT[1] * p['x'],
        curve=lambda x: BIT[0] + BIT[1] * x,
        xlabel='Word-padded bytes W(n)',
        models='OP_BYTEREV’s word-wise reversal: byte-swap each 64-bit word and reverse the word order in place. No new value is written.',
        fixtures='<code>BIT/byterev/&lt;n&gt;</code>: the <code>ReverseBytes</code> kernel used by OP_BYTEREV over <code>n</code> bytes, from one word to the 4,000,000-byte element limit.'),
    'UNROLL': dict(
        source='UNROLL', select=lambda p: p.get('charged') is not None,
        charge='BASE × units + WRITE(unrolled length)',
        charged=lambda p: unroll_charge(p['units'], p['bytes']) / p['units'],
        xlabel='Unrolled bytes per charged unit',
        models='Complete evaluation of a script whose macro references all sit in an inactive branch: decoding the references, substituting the bodies, copying their bytes into the unrolled script, and skipping every substituted instruction unexecuted. Units are substituted instructions plus visited references. The charge compared is the complete script’s charge, including its four wrapper opcodes.',
        fixtures='<code>UNROLL/&lt;shape&gt;/&lt;units&gt;/&lt;bytes&gt;/&lt;charge&gt;</code>: bodies of 1, 16 or 256 OP_NOPs; single pushes of 1 byte to 1,000,000 bytes, referenced 16 times or up to the 4,000,000-byte unrolled limit; reference chains 16 and 256 deep that unroll to nothing.'),
}
# Fixtures of priced primitives that may not be in every calibration yet.
FIXTURES = {
    'SELECT': '<code>SELECT/&lt;kind&gt;/&lt;format&gt;/&lt;records&gt;/&lt;k&gt;</code>: empty witness items (collated and noncollated), weight scan, amount scan, outputs, and all fields of every input, each with <code>k</code> charged units. Only collated output is fitted; noncollated output also pays WRITE per value.',
    'TWEAK': '<code>TWEAK</code>: one x-only key tweak with a valid key and tweak.',
}


# What each primitive models (from the BIP 440 primitive table), shown under its heading.
MODELS = {
    'F': 'Common per-instruction work: decoding, dispatch, metering and stack-limit checks.',
    'PREP': 'Take ownership of one numeric operand, pad it to a 64-bit word boundary and set up its word view; charged per operand.',
    'PRODUCE': 'Allocate, fill and insert one new stack value of <code>n</code> bytes, including its eventual release.',
    'NORMALIZE': 'Turn a prepared numeric result back into minimal bytes: remove word padding.',
    'READ': 'Scan bytes without computing a new value: comparisons, zero tests and length conversion.',
    'ARITH': 'One pass over <code>n</code> bytes of words with a carry or borrow between words: addition and subtraction.',
    'BIT': 'One pass over <code>n</code> bytes of words without carries, each result word depending on a fixed number of input words: bitwise logic, shifts and word-wise byte reversal (OP_BYTEREV). These measurements predate the word-wise reversal, so they contain no reversal fixtures.',
    'MOVE': 'Reorder <code>k</code> stack-entry headers without copying their payloads, e.g. OP_ROLL.',
    'MULCORE': 'Schoolbook multiplication of a <code>u</code>-limb operand by a <code>v</code>-limb one, <code>v</code> ≤ <code>u</code>: one row per limb of the shorter operand, each multiplying the longer one, including scratch storage. Fitted per call, per limb of the longer operand, per row and per limb product.',
    'DIVCORE': 'Long division or modulo: <code>s</code> quotient rows, each estimating and correcting against the <code>v</code> divisor limbs, including normalization and temporaries.',
    'H256': 'Core SHA256 of an <code>n</code>-byte message, from initialization to finalization over whole 64-byte blocks.',
    'H160': 'RIPEMD160 of an <code>n</code>-byte message (at most 520 bytes), from initialization to finalization over whole 64-byte blocks.',
    'H1': 'SHA1 of an <code>n</code>-byte message (at most 520 bytes), from initialization to finalization over whole 64-byte blocks.',
    'SIG': 'One BIP340 Schnorr verification, without its challenge hash, which SHA256 charges. The price is fixed at 500,000 by policy; the fitted residual, the verification time less the fitted challenge hash over all measured message sizes, is shown for comparison.',
    'TWEAK': 'One BIP449 x-only public key tweak (OP_TWEAKADD).',
    'SELECT': 'One OP_TX selection with <code>k</code> charged units: every planned value and every record scanned for an aggregate field (witness items, weight and amount scans, outputs, input fields), including planning, collated framing and cleanup.',
}

# Additional explanation shown under a primitive's description.
NOTES = {
    'NORMALIZE': 'Offset spans (hollow marks) are a diagnostic, not fitted. Val64 compacts an offset span only when a vector&#39;s storage is not word-aligned, and <code>operator new</code> always aligns it, so script values never take that path. Conversion hands the buffer over in place, whatever its length, so NORMALIZE is fitted and priced as a flat charge with no byte term.',
    'PRODUCE': 'Since commit eecbd4d4da every producer reserves the word-padded capacity before filling a value, as these fixtures do. At 2dc5b6b0cb, values were instead reallocated when pushed, which made the <code>vector</code> and <code>zero</code> paths up to 58× slower at large sizes and raised the fitted byte rate to 7.7–8.4 on the Apple machines. The shrink-to-empty and shrink-to-one-byte paths are no longer fitted: shortening a value takes an opcode that charges its result&#39;s production, so creating and shortening a value is two productions, which the large-source churn path measures.',
}
# Run conditions that qualify the measurements, shown in the header.
RUN_NOTES = [
    'The Intel Core i7-7700 has no SHA extensions, so SHA256 runs in software (SSE4/AVX2). Its slowest pre-v2 case is a HASH256 script rather than CHECKSIGVERIFY, and SHA256 costs 37 varops per hashed byte on it, against 8–12 on the machines with SHA extensions. It alone sets the SHA256 byte rate.',
    'Prepared operands are cycled through a pool of 8 MB, the stack payload limit of one script. The size is a chosen measurement setting, not a proven bound on a script’s working set, which also includes retained capacity, temporaries and other evaluator memory; complete-script benchmarks cover those. With an earlier 64 MiB pool the flats of READ, ARITH, MUL and DIV on the AMD Ryzen 9 9950X (Windows, clang-cl) were far above the other machines (MUL 2,551, DIV 1,294); with the 8 MB pool they are in line with the others.',
]

def cost_comparison(joint, family):
    """Implemented price and rounded joint candidate of a priced primitive; no candidate before calibration."""
    record = joint['primitives'].get(family)
    if record is None:
        return CURRENT_COSTS[family], None, True
    candidate = formulas(family, record['candidate_coefficients'], candidate=True)
    return CURRENT_COSTS[family], candidate, CURRENT_COSTS[family] == candidate


def check_points(key, series):
    spec = CHECKS[key]
    points = [dict(p) for p in series.get(spec['source'], []) if spec['select'](p)]
    for p in points:
        p['charged'] = spec['charged'](p)
        p['ratio'] = p['y'] / p['charged']
    return points


DISPLAY = {"SELECT": "OP_TX_SELECT", "DECODE": "MACRO_DECODE", "UNROLL": "Macro unrolling",
           "CSFS": "OP_CHECKSIGFROMSTACK", "BYTEREV": "OP_BYTEREV"}
# Published primitive names; raw calibration data keeps the original family labels.
PUBLISHED_NAMES = [(r'\bhashblockspan\(', 'H('), (r'\bF\b', 'BASE'), (r'\bPREP\b', 'PREPARE'), (r'\bPRODUCE\b', 'WRITE'),
                   (r'\bMULCORE\b', 'MUL'), (r'\bDIVCORE\b', 'DIV'), (r'\bH256\b', 'SHA256'),
                   (r'\bH160\b', 'RIPEMD160'), (r'\bH1\b', 'SHA1')]


def publish_names(page):
    """Rename primitives in visible text only; tags, ids, scripts and styles keep raw labels."""
    out, hidden = [], None
    for piece in re.split(r'(<[^>]+>)', page):
        if piece.startswith('<'):
            tag = re.match(r'</?\s*([a-zA-Z]+)', piece)
            name = tag.group(1).lower() if tag else ''
            if name in {'script', 'style'}:
                hidden = None if piece.startswith('</') else name
            out.append(piece)
        elif hidden:
            out.append(piece)
        else:
            for pattern, replacement in PUBLISHED_NAMES:
                piece = re.sub(pattern, replacement, piece)
            out.append(piece)
    return ''.join(out)
COLORS = {"m1": "#2563eb", "m4": "#dc6b18", "ryzen": "#7c3aed", "intel": "#b91c1c", "i7": "#be185d", "r5": "#4a3aa7", "envelope": "#172536", "basis": "#172536",
          "no_step": "#172536", "cells_only": "#b91c1c",
          "prep_constant": "#b91c1c", "prep_current": "#172536"}
WIDTH, HEIGHT = 880, 420
LEFT, RIGHT, TOP, BOTTOM = 82, 846, 24, 344
PRODUCE_MARKERS = {
    'stack': ('#2563eb', 'circle', 'Copy directly onto stack'),
    'vector': ('#dc6b18', 'square', 'Copy buffer, then move onto stack'),
    'zero': ('#087f8c', 'triangle', 'Zero-filled buffer'),
    'grow': ('#70521b', 'plus', 'Grow buffer'),
    'churn': ('#334155', 'cross', 'Large-source churn'),
    'fresh-pages': ('#a16207', 'square', 'Freshly mapped pages'),
}


def produce_marker(group, x, y, color_override=None):
    color, shape, _ = PRODUCE_MARKERS[group]
    color = color_override or color
    if shape == 'circle':
        mark = f'<circle cx="{x}" cy="{y}" r="2.8"/>'
    elif shape == 'square':
        mark = f'<rect x="{x-2.6}" y="{y-2.6}" width="5.2" height="5.2"/>'
    elif shape == 'triangle':
        mark = f'<path d="M{x},{y-3.5}L{x+3.5},{y+3}L{x-3.5},{y+3}Z"/>'
    elif shape == 'down':
        mark = f'<path d="M{x},{y+3.5}L{x+3.5},{y-3}L{x-3.5},{y-3}Z"/>'
    elif shape == 'diamond':
        mark = f'<path d="M{x},{y-3.5}L{x+3.5},{y}L{x},{y+3.5}L{x-3.5},{y}Z"/>'
    elif shape == 'plus':
        mark = f'<path d="M{x-3},{y}H{x+3}M{x},{y-3}V{y+3}" fill="none" stroke-width="1.5"/>'
    else:
        mark = f'<path d="M{x-3},{y-3}L{x+3},{y+3}M{x-3},{y+3}L{x+3},{y-3}" fill="none" stroke-width="1.5"/>'
    return f'<g fill="{color}" stroke="{color}" stroke-width=".6">{mark}</g>'


def esc(value):
    return html.escape(str(value), quote=True)


def short(value):
    if value >= 1_000_000:
        return f"{value / 1_000_000:.3g}M"
    if value >= 1_000:
        return f"{value / 1_000:.3g}k"
    return f"{value:.3g}"


def x_ticks(max_x):
    ticks = [0, 1]
    for exponent in range(0, 9):
        value = 10 ** exponent
        if 1 < value <= max_x:
            ticks.append(value)
    if max_x not in ticks:
        ticks.append(max_x)
    return sorted(set(ticks))


def curve(family, x, group, model):
    c, v = features(family, x, group)
    return predict(family, dict(x=x, c=c, v=v, group=group), model[family], model)


# One name per machine, used everywhere on the page.
MACHINE_NAMES = {'m1': 'Apple M1 Pro', 'm4': 'Apple M4 Pro', 'ryzen': 'AMD Ryzen 9 9950X',
                 'intel': 'Intel Core i5-12500', 'i7': 'Intel Core i7-7700', 'r5': 'AMD Ryzen 5 3600'}
LEGEND_MACHINES = [(key, name, name) for key, name in MACHINE_NAMES.items()]
LEGEND_DESC = [(key, MACHINE_NAMES[key], marker) for key, marker in
               [('m1', 'Blue circles'), ('m4', 'orange squares'), ('ryzen', 'purple diamonds'),
                ('intel', 'red triangles'), ('i7', 'plum inverted triangles'), ('r5', 'indigo left-pointing triangles')]]


def describe(path):
    """Operating system, compiler and SHA256 implementation recorded in an artifact."""
    machine = json.loads(Path(path).read_text())['machine']
    platform = machine['platform']
    system = ('macOS' if platform.startswith('macOS') else
              'Windows 11' if platform.startswith('Windows-11') else
              'Linux' if platform.startswith('Linux') else platform.split('-')[0])
    sha = 'hardware SHA256' if 'shani' in machine['sha256_backend'] else 'software SHA256'
    return f"{system}, {machine['compiler']}, {sha}"


def chart(family, points, models, group=None, id_prefix="", candidates=None):
    """candidates, when given, are the rounded candidate coefficients, drawn dotted over the envelope."""
    shown = [p for p in points if group is None or p["group"] == group]
    if not shown:
        return ""
    display = DISPLAY.get(family, family)
    max_x = max(p["x"] for p in shown)
    groups = sorted({p["group"] for p in shown})
    line_group = group if group is not None else groups[0]
    sample_x = sorted({round(math.expm1(math.log1p(max_x) * i / 159)) for i in range(160)} |
                      {p["x"] for p in shown if p["x"] in {0, 1, max_x}})
    if family == "DIVCORE":
        sample_x = [x for x in sample_x if x >= 1]
    lines = {key: [(x, curve(family, x, line_group, model)) for x in sample_x]
             for key, model in models.items()}
    if candidates is not None:
        lines['candidate'] = [(x, candidate_charge(family, dict(zip(('c', 'v'), features(family, x, line_group)),
                                                                x=x, group=line_group), candidates))
                              for x in sample_x]
    values = [p["y"] for p in shown] + [y for line in lines.values() for _, y in line]
    ymin, ymax = min(values), max(values)
    ymin = max(ymin / 1.3, 0.01)
    ymax *= 1.35
    if ymax <= ymin:
        ymax = ymin * 2

    def xy(x, y):
        px = LEFT + (RIGHT - LEFT) * math.log1p(x) / max(1e-12, math.log1p(max_x))
        py = BOTTOM - (BOTTOM - TOP) * math.log(y / ymin) / math.log(ymax / ymin)
        return px, py

    title = f"{display} — {group}" if group is not None else display
    machine_count = len({p['machine_key'] for p in shown})
    description = ('; '.join(f'{marker}: {label}' for key, label, marker in LEGEND_DESC if any(p['machine_key'] == key for p in shown)) + '. Hollow marks are excluded diagnostics.')
    if family == 'PRODUCE' and machine_count > 1:
        description = 'Machine is encoded by colour and path by marker shape.'
    if 'envelope' in models:
        description += ' Solid black is the envelope, the pricing basis: the cheapest curve of the same form covering every machine fit at every chargeable size, without rounding.'
    if candidates is not None:
        description += ' The dotted line is the rounded candidate, the charge the schedule applies.'
    pieces = [f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="{esc(title)}: {machine_count} machines and {len(models)} fitted cost curves">',
              f'<title>{esc(title)}: normalized varops per measured operation</title>',
              f'<desc>{description}</desc>',
              f'<rect x="{LEFT}" y="{TOP}" width="{RIGHT-LEFT}" height="{BOTTOM-TOP}" fill="none" stroke="#cbd5e1"/>']
    lo_power = math.floor(math.log10(ymin))
    hi_power = math.ceil(math.log10(ymax))
    for exponent in range(lo_power, hi_power + 1):
        value = 10 ** exponent
        if ymin <= value <= ymax:
            _, py = xy(0, value)
            pieces.append(f'<path d="M{LEFT} {py:.2f}H{RIGHT}" stroke="#e2e8f0"/>')
            pieces.append(f'<text x="{LEFT-9}" y="{py+4:.2f}" text-anchor="end" class="tick">{short(value)}</text>')
    last_tick = -100
    for value in x_ticks(max_x):
        px, _ = xy(value, ymin)
        if px - last_tick < 43 and value != max_x:
            continue
        pieces.append(f'<text x="{px:.2f}" y="{BOTTOM+21}" text-anchor="middle" class="tick">{short(value)}</text>')
        last_tick = px
    xlabel = ("Quotient steps s" if family == "DIVCORE" else "Charged units k" if family == "SELECT" else
              "Unrolled bytes per charged unit" if family == "UNROLL" else "Size or item count")
    pieces.append(f'<text x="{(LEFT+RIGHT)/2:.1f}" y="{HEIGHT-12}" text-anchor="middle" class="axis">{xlabel} · log(1+x)</text>')
    ylabel = "Varops per charged unit" if family == "UNROLL" else "Varops per operation"
    pieces.append(f'<text transform="translate(17 {(TOP+BOTTOM)/2:.1f}) rotate(-90)" text-anchor="middle" class="axis">{ylabel} · log scale</text>')
    pieces.append(f'<defs><clipPath id="clip-{id_prefix}{family}-{esc(group or "all")}"><rect x="{LEFT}" y="{TOP}" width="{RIGHT-LEFT}" height="{BOTTOM-TOP}"/></clipPath></defs>')
    pieces.append(f'<g clip-path="url(#clip-{id_prefix}{family}-{esc(group or "all")})">')
    for key in models:
        path = " ".join(f'{"M" if i == 0 else "L"}{xy(x,y)[0]:.2f},{xy(x,y)[1]:.2f}'
                        for i, (x, y) in enumerate(lines[key]))
        dash = (' stroke-dasharray="2 7"' if key == "no_step" else
                ' stroke-dasharray="9 4"' if key == "cells_only" else
                ' stroke-dasharray="6 4"' if key != "envelope" else "")
        pieces.append(f'<path d="{path}" fill="none" stroke="{COLORS[key]}" stroke-width="2.5"{dash}/>')
    if candidates is not None:
        path = " ".join(f'{"M" if i == 0 else "L"}{xy(x,y)[0]:.2f},{xy(x,y)[1]:.2f}'
                        for i, (x, y) in enumerate(lines['candidate']))
        pieces.append(f'<path class="candidate" d="{path}" fill="none" stroke="{COLORS["basis"]}" stroke-width="3"'
                      ' stroke-dasharray="0.1 6" stroke-linecap="round"/>')
    for key in ("m1", "m4", "ryzen", "intel", "i7", "r5"):
        color = COLORS[key]
        for p in shown:
            if p["machine_key"] != key:
                continue
            px, py = xy(p["x"], p["y"])
            color = ("#087f8c" if p["group"] == "aligned" else "#dc6b18") if family == "NORMALIZE" and machine_count == 1 else COLORS[key]
            fill = color if p["included"] else "white"
            if family == "PRODUCE":
                mark = produce_marker(p['group'], round(px, 2), round(py, 2), color if machine_count > 1 else None)
            elif family == "NORMALIZE" and machine_count == 1 and p["group"] == "offset-span":
                mark = f'<path d="M{px:.2f},{py-3.6:.2f}L{px+3.6:.2f},{py+3:.2f}L{px-3.6:.2f},{py+3:.2f}Z" fill="{fill}" stroke="{color}" stroke-width=".8"/>'
            elif (family == "NORMALIZE" and machine_count == 1) or key == "m1":
                mark = f'<circle cx="{px:.2f}" cy="{py:.2f}" r="2.6" fill="{fill}" stroke="{color}" stroke-width=".8"/>'
            elif key == "m4":
                mark = f'<rect x="{px-2.4:.2f}" y="{py-2.4:.2f}" width="4.8" height="4.8" fill="{fill}" stroke="{color}" stroke-width=".8"/>'
            elif key == "intel":
                mark = f'<path d="M{px:.2f},{py-3.6:.2f}L{px+3.6:.2f},{py+3:.2f}L{px-3.6:.2f},{py+3:.2f}Z" fill="{fill}" stroke="{color}" stroke-width=".8"/>'
            elif key == "i7":
                mark = f'<path d="M{px:.2f},{py+3.6:.2f}L{px+3.6:.2f},{py-3:.2f}L{px-3.6:.2f},{py-3:.2f}Z" fill="{fill}" stroke="{color}" stroke-width=".8"/>'
            elif key == "r5":
                mark = f'<path d="M{px-3.6:.2f},{py:.2f}L{px+3:.2f},{py-3.6:.2f}L{px+3:.2f},{py+3.6:.2f}Z" fill="{fill}" stroke="{color}" stroke-width=".8"/>'
            else:
                mark = f'<path d="M{px:.2f},{py-3.4:.2f}L{px+3.4:.2f},{py:.2f}L{px:.2f},{py+3.4:.2f}L{px-3.4:.2f},{py:.2f}Z" fill="{fill}" stroke="{color}" stroke-width=".8"/>'
            pieces.append(f'<g><title>{esc(key)} · {esc(p.get("label", p["group"]))}: {p["y"]:.6g} varops</title>{mark}</g>')
    pieces.append("</g></svg>")
    legend = ''
    if family == 'PRODUCE':
        legend = '<p>Colour identifies the machine; marker shape identifies the execution path. Each machine is fitted over all its paths; the solid line is the envelope of those fits.</p><div class="plot-legend">'
        for key, label in MACHINE_NAMES.items():
            if not any(p['machine_key'] == key for p in shown): continue
            legend += f'<span style="color:{COLORS[key]}">● {label}</span>'
        if 'envelope' in models:
            legend += '<span style="color:#172536">━ Envelope</span>'
        if candidates is not None:
            legend += '<span><i class="plot-line candidate" aria-hidden="true"></i>Rounded candidate</span>'
        legend += '</div><div class="plot-legend">'
        for path, (_, _, label) in PRODUCE_MARKERS.items():
            legend += f'<span><svg class="legend-icon" width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">{produce_marker(path, 7, 7, "#526174")}</svg>{esc(label)}</span>'
        legend += '</div><p class="muted">Complete lifetimes include release. For growth and churn, bytes and time are divided by two production events; x is average bytes per event, not final result size. Hover a mark for its machine and fixture.</p>'
    return legend + "".join(pieces)


def machine_mark(key, px, py):
    color = COLORS[key]
    if key == "m1":
        return f'<circle cx="{px:.2f}" cy="{py:.2f}" r="2.6" fill="{color}" stroke="{color}" stroke-width=".8"/>'
    if key == "m4":
        return f'<rect x="{px-2.4:.2f}" y="{py-2.4:.2f}" width="4.8" height="4.8" fill="{color}" stroke="{color}" stroke-width=".8"/>'
    if key == "intel":
        return f'<path d="M{px:.2f},{py-3.6:.2f}L{px+3.6:.2f},{py+3:.2f}L{px-3.6:.2f},{py+3:.2f}Z" fill="{color}" stroke="{color}" stroke-width=".8"/>'
    if key == "i7":
        return f'<path d="M{px:.2f},{py+3.6:.2f}L{px+3.6:.2f},{py-3:.2f}L{px-3.6:.2f},{py-3:.2f}Z" fill="{color}" stroke="{color}" stroke-width=".8"/>'
    if key == "r5":
        return f'<path d="M{px-3.6:.2f},{py:.2f}L{px+3:.2f},{py-3.6:.2f}L{px+3:.2f},{py+3.6:.2f}Z" fill="{color}" stroke="{color}" stroke-width=".8"/>'
    return f'<path d="M{px:.2f},{py-3.4:.2f}L{px+3.4:.2f},{py:.2f}L{px:.2f},{py+3.4:.2f}L{px-3.4:.2f},{py:.2f}Z" fill="{color}" stroke="{color}" stroke-width=".8"/>'


def check_chart(key, points):
    """Measured varops per fixture against the implemented charge, drawn as a line where it
    depends on the size alone and as a dash per fixture otherwise (macro unrolling)."""
    spec = CHECKS[key]
    title = DISPLAY[key]
    max_x = max(p["x"] for p in points)
    samples = [math.expm1(math.log1p(max_x) * i / 400) for i in range(401)]
    charges = [spec['curve'](x) for x in samples] if 'curve' in spec else [p['charged'] for p in points]
    values = [p["y"] for p in points] + charges
    ymin, ymax = max(min(values) / 1.3, 0.01), max(values) * 1.35

    def xy(x, y):
        px = LEFT + (RIGHT - LEFT) * math.log1p(x) / max(1e-12, math.log1p(max_x))
        py = BOTTOM - (BOTTOM - TOP) * math.log(y / ymin) / math.log(ymax / ymin)
        return px, py

    pieces = [f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="{esc(title)}: measured work and implemented charge for {len(points)} machine fixtures">',
              f'<title>{esc(title)}: measured varops and the implemented charge</title>',
              '<desc>Each mark is one machine fixture. The solid line is the implemented charge; marks above it are not covered.</desc>',
              f'<rect x="{LEFT}" y="{TOP}" width="{RIGHT-LEFT}" height="{BOTTOM-TOP}" fill="none" stroke="#cbd5e1"/>']
    for exponent in range(math.floor(math.log10(ymin)), math.ceil(math.log10(ymax)) + 1):
        value = 10 ** exponent
        if ymin <= value <= ymax:
            _, py = xy(0, value)
            pieces.append(f'<path d="M{LEFT} {py:.2f}H{RIGHT}" stroke="#e2e8f0"/>')
            pieces.append(f'<text x="{LEFT-9}" y="{py+4:.2f}" text-anchor="end" class="tick">{short(value)}</text>')
    last_tick = -100
    for value in x_ticks(round(max_x)):
        px, _ = xy(value, ymin)
        if px - last_tick < 43 and value != round(max_x):
            continue
        pieces.append(f'<text x="{px:.2f}" y="{BOTTOM+21}" text-anchor="middle" class="tick">{short(value)}</text>')
        last_tick = px
    pieces.append(f'<text x="{(LEFT+RIGHT)/2:.1f}" y="{HEIGHT-12}" text-anchor="middle" class="axis">{esc(spec["xlabel"])} · log(1+x)</text>')
    ylabel = "Varops per charged unit" if key == "UNROLL" else "Varops per operation"
    pieces.append(f'<text transform="translate(17 {(TOP+BOTTOM)/2:.1f}) rotate(-90)" text-anchor="middle" class="axis">{ylabel} · log scale</text>')
    if 'curve' in spec:
        path = " ".join(f'{"M" if i == 0 else "L"}{xy(x, y)[0]:.2f},{xy(x, y)[1]:.2f}' for i, (x, y) in enumerate(zip(samples, charges)))
        pieces.append(f'<path d="{path}" fill="none" stroke="{COLORS["basis"]}" stroke-width="2.5"/>')
    else:
        for p in points:
            px, py = xy(p["x"], p["charged"])
            pieces.append(f'<path d="M{px-5:.2f} {py:.2f}H{px+5:.2f}" stroke="{COLORS["basis"]}" stroke-width="2.5"/>')
    for machine in ("m1", "m4", "ryzen", "intel", "i7", "r5"):
        for p in points:
            if p["machine_key"] != machine:
                continue
            px, py = xy(p["x"], p["y"])
            pieces.append(f'<g><title>{esc(machine)} · {esc(p["label"])}: {p["y"]:.6g} varops, {p["ratio"]:.3g} of the charge</title>{machine_mark(machine, px, py)}</g>')
    pieces.append('</svg>')
    return ''.join(pieces)


def reference_spread(artifact):
    """Per-round spread of the worst pre-v2 reference case, from the artifact's summary row."""
    reference = json.loads(Path(artifact).read_text())['reference']
    row = next((row for row in reference['raw_rows'] if row['Record_Type'] == 'summary'
                and row['Name'] == reference['worst_case'] and row['Wall_Min_Seconds']), None)
    if row is None:
        return None
    low, high = float(row['Wall_Min_Seconds']), float(row['Wall_Max_Seconds'])
    return dict(case=row['Opcode'], rounds=int(row['Samples']), low=low, high=high, spread=high / low - 1)


def diagnostics_html(joint, machines, dataset, same_schedule):
    """Quality gate, fixtures above the charge and run conditions, from the joint fit's diagnostics."""
    diag = joint.get('diagnostics')
    if not diag:
        return ''
    names = {machine['meta']['label']: machine['label'] for machine in machines}
    gate = diag['quality_gate']
    limits = gate['quality_gate']
    within = limits['within_factor']
    parts = ['<div id="diagnostics"><h2>Diagnostics</h2>',
             '<p>These checks qualify the candidate; they do not change it. A primitive fixture measured above its charge '
             'is a diagnostic finding. Whether a script can exceed its machine&#39;s pre-v2 reference is established by '
             'complete-script benchmarks (<code>bench_varops</code>), so the primitive definitions stay as they are '
             'while these cases are investigated.</p>']

    for name, coverage in diag['charge_coverage'].items():
        label = ('candidate, identical to the implemented charges' if name == 'candidate' and same_schedule
                 else name)
        above = coverage['above_charge']
        parts.append(f'<h3>Fixtures above the charge · schedule: {esc(label)}</h3>'
                     f'<p>{len(above):,} of {coverage["checked"]:,} included machine-fixture medians of priced primitives '
                     'are above their charge. <strong>Measured ÷ charge</strong> uses the normalization of the fits, a full '
                     f'budget in {TARGET_FRACTION:g}× the machine&#39;s pre-v2 reference time: above 1, a budget spent on the '
                     'fixture alone would take longer than that.</p>')
        worst = {}
        for item in above:
            worst.setdefault(item['family'], item)
        parts.append('<div class="table-wrap"><table><thead><tr><th>Primitive</th><th>Fixtures above charge</th>'
                     '<th>Worst fixture</th><th>Machine</th><th>Measured ÷ charge</th></tr></thead><tbody>')
        for family, item in worst.items():
            count = sum(other['family'] == family for other in above)
            parts.append(f'<tr><td><a href="#{family}">{esc(DISPLAY.get(family, family))}</a></td><td>{count}</td>'
                         f'<td><code>{esc(item["fixture"])}</code></td>'
                         f'<td>{esc(names.get(item["machine"], item["machine"]))}</td>'
                         f'<td>{item["ratio"]:.2f}×</td></tr>')
        parts.append('</tbody></table></div>')
        if above:
            parts.append(f'<details><summary>All {len(above)} fixtures above the charge</summary>'
                         '<div class="table-wrap"><table><thead><tr><th>Fixture</th><th>Machine</th>'
                         '<th>Measured varops</th><th>Charged varops</th><th>Measured ÷ charge</th></tr></thead><tbody>')
            for item in above:
                parts.append(f'<tr><td><code>{esc(item["fixture"])}</code></td>'
                             f'<td>{esc(names.get(item["machine"], item["machine"]))}</td>'
                             f'<td>{item["measured_varops"]:,.0f}</td><td>{item["charged_varops"]:,.0f}</td>'
                             f'<td>{item["ratio"]:.2f}×</td></tr>')
            parts.append('</tbody></table></div></details>')

    failures = gate['gate_failures']
    under = [item for item in failures if item['max_above_fit'] > within]
    below = [item for item in failures if item not in under and item['max_below_fit'] > within]
    one_sided = [item for item in failures if item['rms_above_fit'] > limits['max_rms_factor']]
    parts.append(f'<h3>Quality gate of the machine fits</h3><p>BIP 440 Appendix A asks each machine&#39;s fit to meet, '
                 f'within every path and size decade: an RMS factor of at most {limits["max_rms_factor"]:.2f} and at '
                 f'least {100 * limits["min_within_share"]:g}% of fixtures within {within:g}× of the fit. '
                 f'{len(failures):,} of {gate["gate_bins"]:,} bins fail. The gate counts both directions. In '
                 f'{len(under)} failing bins a fixture lies more than {within:g}× above its own machine&#39;s fit, where '
                 f'the fit under-predicts. Of the other {len(failures) - len(under):,}, {len(below):,} have a fixture more '
                 f'than {within:g}× below the fit, where it over-predicts and the charge errs on the safe side, and '
                 f'{len(failures) - len(under) - len(below)} fail on the RMS factor with every fixture within {within:g}× '
                 f'of the fit. Counting only fixtures above the fit (<em>RMS above the fit</em>), {len(one_sided)} failing '
                 f'bins exceed {limits["max_rms_factor"]:.2f}. Size decade <em>d</em> holds sizes 10<sup><em>d</em></sup> '
                 'to 10<sup><em>d</em>+1</sup> − 1 (decade −1 is size 0).</p>')
    parts.append('<div class="table-wrap"><table><thead><tr><th>Machine</th><th>Bins</th><th>Failing</th>'
                 f'<th>Fixture more than {within:g}× above the fit</th>'
                 f'<th>RMS above the fit over {limits["max_rms_factor"]:.2f}</th></tr></thead><tbody>')
    for machine in machines:
        label = machine['meta']['label']
        failing = [item for item in failures if item['machine'] == label]
        parts.append(f'<tr><td>{esc(machine["label"])}</td><td>{gate["bins_by_machine"][label]}</td>'
                     f'<td>{len(failing)}</td><td>{sum(item in under for item in failing)}</td>'
                     f'<td>{sum(item in one_sided for item in failing)}</td></tr>')
    parts.append('</tbody></table></div>')
    if under:
        parts.append(f'<details><summary>The {len(under)} bins with a fixture more than {within:g}× above the fit'
                     '</summary><div class="table-wrap"><table><thead><tr><th>Primitive</th><th>Path</th><th>Decade</th>'
                     '<th>Machine</th><th>Fixtures</th><th>RMS factor</th><th>RMS above the fit</th><th>Within</th>'
                     '<th>Most above the fit</th>'
                     '</tr></thead><tbody>')
        for item in sorted(under, key=lambda item: -item['max_above_fit']):
            parts.append(f'<tr><td><a href="#{item["family"]}">{esc(DISPLAY.get(item["family"], item["family"]))}</a></td>'
                         f'<td><code>{esc(item["group"])}</code></td><td>{item["decade"]}</td>'
                         f'<td>{esc(names.get(item["machine"], item["machine"]))}</td><td>{item["fixtures"]}</td>'
                         f'<td>{item["rms_factor"]:.2f}</td><td>{item["rms_above_fit"]:.2f}</td>'
                         f'<td>{100 * item["within_share"]:.0f}%</td>'
                         f'<td>{item["max_above_fit"]:.2f}×</td></tr>')
        parts.append('</tbody></table></div></details>')

    parts.append('<h3>Run conditions</h3><p>Each fixture is timed once per pass over all fixtures, each time as the '
                 'average of a batch of repetitions, and priced at the median of these epochs. Epoch noise is the median '
                 'over fixtures of how far a fixture&#39;s epochs typically lie from their median: the median absolute '
                 f'deviation, relative to the median. A run whose epoch noise exceeds {100 * MAX_EPOCH_NOISE:g}% fails its '
                 'conditions: the runner repeats it, and the fitter rejects it unless an exploratory fit is requested. '
                 'The threshold is a provisional screening check, not an accuracy guarantee: a machine loaded evenly '
                 'throughout can pass it. The one-minute load average is shown for information; an idle macOS desktop '
                 'already reports 1.5–2, and Windows reports none. The last column is the spread of the worst reference '
                 'case over its rounds at the start of the run.</p>')
    parts.append('<div class="table-wrap"><table><thead><tr><th>Machine</th><th>Reference (s)</th>'
                 '<th>Epoch noise</th><th>Median load</th><th>Worst reference case: per-round range</th>'
                 '</tr></thead><tbody>')
    for machine in machines:
        meta = machine['meta']
        conditions = meta.get('conditions')
        spread = reference_spread(meta['file'])
        noise = meta['epoch_noise']
        noise = 'not measured' if noise is None else f'{100 * noise:.2f}%' + (' (fails)' if noise > MAX_EPOCH_NOISE else '')
        load = conditions.get('median_one_minute_load') if conditions else None
        load = f'{load:.1f}' if load is not None else ('unavailable' if conditions else 'not recorded')
        rounds = (f'{esc(spread["case"])}, {spread["rounds"]} rounds: {spread["low"]:.3f}–{spread["high"]:.3f} s '
                  f'({100 * spread["spread"]:.1f}%)' if spread else 'not recorded')
        parts.append(f'<tr><td>{esc(machine["label"])}</td><td>{meta["reference_seconds"]:.3f}</td><td>{noise}</td>'
                     f'<td>{load}</td><td>{rounds}</td></tr>')
    parts.append('</tbody></table></div>')
    if (dataset / 'reference-csv').exists():
        parts.append('<p>Recovered per-round reference samples and their provenance are in <code>reference-csv/</code> '
                     'of the dataset.</p>')
    parts.append('</div><!--/diagnostics-->')
    return ''.join(parts)


def render(joint_path, output, source_root=None, title=TITLE):
    joint = json.loads(joint_path.read_text())
    audit_path = joint_path.with_name('source-verification.json')
    joint_digest = hashlib.sha256(joint_path.read_bytes()).hexdigest()
    source_check = joint.get('source_check', {})
    if source_root is not None:
        source_check = check_source_snapshots(source_root, joint['machines'])
        audit_path.write_text(json.dumps(dict(joint_sha256=joint_digest,
                                             source_check=source_check), indent=2) + '\n')
    elif audit_path.exists():
        audit = json.loads(audit_path.read_text())
        if audit['joint_sha256'] != joint_digest:
            raise ValueError('Source audit is stale; rerun with --source-root')
        source_check = audit['source_check']
    for meta in joint['machines']:
        # Inputs are recorded relative to the joint fit (absolute in older fits).
        meta['file'] = str(joint_path.parent / meta['file'])
    if joint.get("schema") != "varop-joint-fit-v2":
        raise ValueError("this comparison requires a v2 joint fit")
    if joint.get("model_id") != "producer-normalize-v1":
        raise ValueError("this report renders the producer-normalize-v1 model only")
    machines = []
    for meta in joint["machines"]:
        source = Path(meta["file"])
        if hashlib.sha256(source.read_bytes()).hexdigest() != meta["sha256"]:
            raise ValueError(f"input changed since the joint fit: {source}")
    machine_models = independent_models([Path(meta["file"]) for meta in joint["machines"]])
    for meta, model in zip(joint["machines"], machine_models):
        source = Path(meta["file"])
        points, loaded = load_calibration(source)
        # Joint fits from before the epoch noise condition do not record it.
        meta.setdefault('epoch_noise', loaded['epoch_noise'])
        identity = (meta['cpu'] + ' ' + source.name).lower()
        key = next((key for key, token in [('m1', 'm1'), ('m4', 'm4'), ('r5', 'ryzen 5 3600'), ('ryzen', 'ryzen'), ('i7', 'i7-7700'), ('intel', 'intel')] if token in identity), None)
        if key is None or any(m['key'] == key for m in machines):
            raise ValueError(f'Unknown or duplicate machine identity: {identity}')
        label = MACHINE_NAMES[key]
        for point in points:
            point["machine_key"] = key
        machines.append(dict(meta=meta, points=points, model=model, key=key, label=label))
    joint_model = {family: tuple(record["envelope_coefficients"])
                   for family, record in joint["primitives"].items()}
    models = {machine["key"]: machine["model"] for machine in machines}
    series = defaultdict(list)
    for machine in machines:
        for point in machine["points"]:
            series[point["family"]].append(point)
    env_model = envelope_model(machine_models, series)
    for family, record in joint['primitives'].items():
        if 'envelope_coefficients' in record and any(
                abs(a - b) > 1e-6 * max(1.0, abs(a)) for a, b in zip(record['envelope_coefficients'], env_model[family])):
            raise ValueError(f'envelope of {family} differs from the joint fit')

    candidates = {family: record['candidate_coefficients'] for family, record in joint['primitives'].items()}

    def family_models(family):
        # Only the envelope: the machine fits are listed in each primitive's table.
        return {'envelope': env_model}
    if output.exists() and not any(known in output.read_text()[:500] for known in (TITLE, title)):
        raise ValueError(f"refusing to overwrite unrelated HTML: {output}")

    parts = ["""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>""" + esc(title) + """</title><style>
:root{color-scheme:light;background:#f4f6f8;color:#172536;font:16px/1.5 system-ui,sans-serif}
body{margin:0}main{max-width:1160px;margin:auto;padding:28px 18px}header,article{background:#fff;border:1px solid #dce2e8;border-radius:10px;padding:22px;margin-bottom:20px}
h1{font-size:28px;margin:0 0 8px}h2{font-size:22px;margin:0 0 10px}h3{font-size:18px;margin:0 0 10px}h4{font-size:18px;margin:0 0 10px}.group-title{font-size:17px;margin:26px 0 10px;color:#415268}.table-section th{background:#e7eefc}p{margin:8px 0 12px}
.muted{color:#526174}.warning{border-left:4px solid #c17712;padding-left:13px}.nav{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0}
.nav a{display:inline-block;padding:7px 12px;border:1px solid #cbd5e1;border-radius:7px;color:#1d4ed8;text-decoration:none;background:white}
.nav a.active{background:#e7eefc;color:#153a83;border-color:#7895d6}.legend{display:flex;flex-wrap:wrap;gap:15px;margin:10px 0 4px;font-size:14px}
.swatch{display:inline-block;width:22px;height:0;border-top:3px solid;vertical-align:middle;margin-right:5px}.dash{border-top-style:dashed}.dot{border-top-style:dotted}.swatch.mark{width:9px;border-top-width:9px;border-radius:50%}
.plot-legend{display:flex;flex-wrap:wrap;gap:7px 17px;margin:8px 0 2px;font-size:14px;color:#415268}.plot-legend span{white-space:nowrap}
.plot-mark{display:inline-block;width:9px;height:9px;vertical-align:middle;margin-right:6px}.plot-mark.m1{background:#2563eb;border-radius:50%}.plot-mark.m4{background:#dc6b18}.plot-mark.ryzen{background:#7c3aed;transform:rotate(45deg)}
.plot-mark.intel{background:#b91c1c;clip-path:polygon(50% 0,100% 100%,0 100%)}.plot-mark.i7{background:#be185d;clip-path:polygon(0 0,100% 0,50% 100%)}.plot-mark.r5{background:#4a3aa7;clip-path:polygon(0 50%,100% 0,100% 100%)}
.plot-line{display:inline-block;width:20px;border-top:2px dashed;vertical-align:middle;margin-right:6px}.plot-line.m1{border-color:#2563eb}.plot-line.m4{border-color:#dc6b18}.plot-line.ryzen{border-color:#7c3aed}.plot-line.intel{border-color:#b91c1c}.plot-line.i7{border-color:#be185d}.plot-line.r5{border-color:#4a3aa7}.plot-line.basis{border-color:#172536;border-top-style:solid}.plot-line.candidate{border-color:#172536;border-top-style:dotted;border-top-width:3px}
.chart{margin:14px 0 20px}.chart > svg{display:block;width:100%;height:auto;max-height:540px}.plot-legend span{display:inline-flex;align-items:center;gap:5px}.plot-legend .legend-icon{display:inline-block;width:14px;height:14px;flex:0 0 14px}.tick{font:12px system-ui;fill:#526174}.axis{font:13px system-ui;fill:#172536}
.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #e2e8f0;vertical-align:top}th{background:#f1f5f9}code{white-space:nowrap;font-size:13px}
.facet-title{font-size:15px;margin:16px 0 3px;color:#415268}article{scroll-margin-top:20px}.category[hidden]{display:none}@media(max-width:650px){main{padding:10px}header,article{padding:13px}h1{font-size:23px}}
</style><main><header><h1>Varops · current reduced calibration model</h1>"""]
    heads = sorted({machine['meta']['head'][:10] for machine in machines if machine['meta'].get('head')})
    commit_chip = (f'commit {heads[0]}' if len(heads) == 1 else 'commits ' + ', '.join(heads)) if heads else 'commit unknown'
    parts.append(f'<p><strong>{esc(joint.get("model_id", ""))} · {commit_chip} · {len(joint_model)} primitives · report updated 2026-09-30.</strong> Current candidate prices are shown first.</p>')
    parts.append('<p><strong>Pricing basis: envelope</strong> (solid black) is the cheapest curve of each primitive&#39;s form that stays at or above every machine&#39;s fitted curve at every size the charge applies to. Formally, with feature vector φ(x) ≥ 0 and machine fits θ<sub>m</sub>, it minimizes the weighted mean relative charge Σ<sub>x</sub> w(x)·θ·φ(x) / max<sub>m</sub> θ<sub>m</sub>·φ(x) over the fixtures, with the fits&#39; path-group and size-decade weights, subject to θ·φ(x) ≥ θ<sub>m</sub>·φ(x) for every machine m and every chargeable size x, and θ ≥ 0. Sizes start at zero except where an operation cannot be smaller: a hash always processes at least one 64-byte block, a division has at least one quotient row and one divisor limb, and a multiplication&#39;s shorter operand is at most as long as the longer one. RIPEMD160 and SHA1 take at most 520 bytes; other sizes are unbounded. Where sizes start at zero and are unbounded, covering every machine at size zero needs the largest flat and covering them at large sizes needs the largest rate, so there the envelope takes the largest flat and the largest rate of any machine; it is lower only for DIV, MUL and the hashes. It is unrounded and is the basis of the rounded implementation prices shown below. It covers the individual fitted curves, not necessarily every measurement. SIG remains a fixed policy charge; its comparison curves are diagnostic.</p>')
    parts.append(f'<p>Fresh {len(machines)}-machine calibration: {sum(len(m["points"]) for m in machines):,} machine-fixture medians; epochs per fixture: {esc(", ".join(str(m["meta"]["epochs"]) for m in machines))}. Each machine is normalized by its own pre-v2 reference: a full 40-billion-varop budget of fitted work is priced to take that reference time. The schedule holds only if complete scripts stay below the reference on every machine. Each machine is fitted independently with equal path-group and size-decade weights and a 100× underprediction penalty. Coefficients remain unrounded; this is not a guaranteed upper bound or whole-script validation.</p>')
    read_rate = joint['primitives']['READ']['envelope_coefficients'][1]
    prep_rate = joint['primitives']['PREP']['envelope_coefficients'][1]
    parts.append(f'<p>The candidate rounds the envelope up, each coefficient on its own. A flat rounds to a multiple of 10 below 100 and of 50 from 100, but never to more than two significant figures: it is the intercept of a fit and moves most between runs, so coarse steps keep it from changing on every refit, at up to 50% more for a flat just above 100. A rate rounds to two significant figures, and at least to a whole varop, which adds at most 10% to any rate of 10 or more. Rates are per byte of the padded length the operation processes, W(n) for PREPARE, WRITE, READ, ARITH and BIT (n rounded up to a multiple of 8 bytes, so W(5) = 8), and H(n) for the hashes (the message plus its padding, rounded up to a multiple of 64 bytes), or per counted item elsewhere. Zero stays zero and exact multiples keep their value. NORMALIZE is a flat charge and SIG remains fixed at 500,000. Every rate is a whole number of varops, so a small fitted rate overcharges large operands most: PREPARE&#39;s {prep_rate:.3g} per byte is charged as 1, READ&#39;s {read_rate:.3g} as {math.ceil(read_rate - 1e-9)}. The table below compares it with the prices implemented in the research implementation and BIP draft; where they differ, the implementation has not been updated. Raw fits remain unrounded; the plots draw the rounded candidate dotted over the unrounded envelope. Whole-script confirmation on every machine remains required.</p>')
    for note in RUN_NOTES:
        parts.append(f'<p class="warning">{esc(note)}</p>')
    unmatched = source_check.get("unmatched", [])
    if unmatched:
        parts.append('<p class="warning">Source verification is incomplete: the M4 benchmark-source discrepancy remains unresolved for this provisional candidate.</p>')
        parts.append(f'<details><summary>Source provenance: {len(unmatched)} file hashes still need review</summary><p>{esc(source_check.get("note", ""))} Resolve these differences before finalizing the schedule.</p><ul>')
        for item in unmatched:
            parts.append(f'<li>{esc(Path(item["machine"]).name)}: <code>{esc(item["path"])}</code></li>')
        parts.append('</ul><p><a href="source-verification.json">Verification record</a></p></details>')
    legend = '<div class="legend">'
    for machine in machines:
        key = machine['key']
        shape = {'m1': 'circles', 'm4': 'squares', 'ryzen': 'diamonds', 'intel': 'triangles', 'i7': 'inverted triangles', 'r5': 'left-pointing triangles'}[key]
        legend += f'<span><span class="swatch mark" style="border-color:{COLORS[key]}"></span>{esc(machine["label"])} ({esc(describe(machine["meta"]["file"]))}) · {shape}</span>'
    parts.append(legend + '<span><span class="swatch" style="border-color:#172536"></span>Envelope · solid (pricing basis)</span><span><span class="swatch dot" style="border-color:#172536"></span>Rounded candidate · dotted</span><span>Hollow marks: excluded diagnostics</span></div>')
    quick = []
    for machine in machines:
        settings = json.loads(Path(machine['meta']['file']).read_text()).get('measurement_settings')
        # Five epochs at full sample lengths is the adopted calibration setting (2026-09-28).
        if settings and (settings['primitive_epochs'] < 5 or settings['reference_epochs'] < 5 or
                         (settings['sample_ms'], settings['copy_sample_ms']) != (10, 100)):
            quick.append(f"{machine['label']}: {settings['primitive_epochs']} epochs, "
                         f"{settings['sample_ms']:g}/{settings['copy_sample_ms']:g} ms samples, "
                         f"{settings['reference_epochs']} reference epoch(s)")
    if quick:
        parts.append('<p class="warning"><strong>Quick runs</strong> (full: at least 5 epochs, 10/100 ms, 5 reference epochs). '
                     'Use these fits to check the pipeline and the direction of changes, not as prices. ' + esc('; '.join(quick)) + '.</p>')
    failed = failed_conditions([machine['meta'] for machine in machines])
    if failed:
        labels = {id(machine['meta']): machine['label'] for machine in machines}
        parts.append('<p class="warning"><strong>Exploratory fit</strong>: these runs failed their measurement conditions '
                     'and must be repeated before pricing: ' + esc('; '.join(
                         f"{labels[id(meta)]} (epoch noise {100 * meta['epoch_noise']:.2f}%)" for meta in failed)) + '.</p>')
    parts.append('<h2 id="comparison">Implemented prices and candidate</h2><div class="table-wrap"><table><thead><tr>'
                 '<th>Primitive or check</th><th>Implemented charge</th><th>Candidate (rounded envelope) or check</th></tr></thead><tbody>')
    for section in SECTIONS:
        parts.append(f'<tr class="table-section"><th colspan="3"><a href="#{section["slug"]}">{esc(section["title"])}</a></th></tr>')
        for _, families in section["groups"]:
            for family in families:
                name = f'<a href="#{family}">{esc(DISPLAY.get(family, family))}</a>'
                if family in CHECKS:
                    points = check_points(family, series)
                    result = (f'Check: up to {max(p["ratio"] for p in points):.3g}× the charge'
                              if points else 'Check awaiting calibration')
                    parts.append(f'<tr><td>{name}</td><td><code>{esc(CHECKS[family]["charge"])}</code></td><td>{esc(result)}</td></tr>')
                    continue
                implemented, candidate, same = cost_comparison(joint, family)
                shown = (f'<code>{esc(candidate)}</code>{"" if same else " <strong>differs</strong>"}'
                         if candidate is not None else 'Awaiting calibration')
                parts.append(f'<tr><td>{name}</td><td><code>{esc(implemented)}</code></td><td>{shown}</td></tr>')
    parts.append('</tbody></table></div>')
    same_schedule = all(cost_comparison(joint, family)[2] for family in CURRENT_COSTS)
    parts.append(diagnostics_html(joint, machines, joint_path.parent, same_schedule))
    parts.append('<nav class="nav" aria-label="Sections">')
    for section in SECTIONS:
        parts.append(f'<a href="#{section["slug"]}">{esc(section["title"].split(" · ")[0])}</a>')
    parts.append('</nav></header>')

    def machine_legend(pts, fits=True, charge=None):
        present = [key for key, _, _ in LEGEND_MACHINES if any(q['machine_key'] == key for q in pts)]
        legend = ''.join(f'<span><i class="plot-mark {key}" aria-hidden="true"></i>{label}</span>'
                         for key, label, _ in LEGEND_MACHINES if key in present)
        if fits:
            legend += '<span><i class="plot-line basis" aria-hidden="true"></i>Envelope</span>'
            legend += '<span><i class="plot-line candidate" aria-hidden="true"></i>Rounded candidate</span>'
        else:
            legend += ('<span><i class="plot-line basis" aria-hidden="true"></i>Implemented charge'
                       + (f' <code>{esc(charge)}</code>' if charge else '') + '</span>')
        return f'<div class="plot-legend" aria-label="Plot legend">{legend}</div>'

    def signature_table(pts):
        """OP_CHECKSIG verifies a signature over the 32-byte transaction digest, the only
        message size Bitcoin signs; other sizes belong to OP_CHECKSIGFROMSTACK."""
        span = hash_span(96)
        charge = SIGCHECK + SHA256[0] + SHA256[1] * span
        out = [f'<p>OP_CHECKSIG verifies a BIP 340 signature over the 32-byte transaction digest, so its challenge hash '
               f'covers 96 bytes: R, P and the digest. It is charged <code>SIGCHECK + SHA256(96)</code> = 500,000 + '
               f'{SHA256[0]} + {SHA256[1]} × {span} = {charge:,} varops. The table compares one such verification, '
               'measured on each machine, with that charge. Other message sizes are verified only by '
               '<a href="#CSFS">OP_CHECKSIGFROMSTACK</a>, whose charge grows with the message.</p>',
               '<div class="table-wrap"><table><thead><tr><th>Machine</th><th>Measured verification (varops)</th>'
               '<th>Charge (varops)</th><th>Measured / charge</th></tr></thead><tbody>']
        for machine in machines:
            own = [p for p in pts if p['machine_key'] == machine['key'] and p['x'] == 32]
            if own:
                y = own[0]['y']
                out.append(f'<tr><td>{esc(machine["label"])}</td><td>{y:,.0f}</td><td>{charge:,}</td><td>{y / charge:.3g}</td></tr>')
        out.append('</tbody></table></div>')
        return out

    def priced_article(family, tag):
        pts = series[family]
        record = joint['primitives'].get(family)
        out = [f'<article id="{family}"><{tag}>{esc(DISPLAY.get(family, family))}</{tag}>']
        if record is None:
            out.append(f'<p><strong>Implemented in varops.h:</strong> <code>{esc(CURRENT_COSTS[family])}</code> varops. '
                       '<strong>Awaiting calibration:</strong> the calibration shown here predates these fixtures.</p>')
            out.append(f'<p class="model"><strong>Models:</strong> {MODELS[family]}</p>')
            out.append(f'<p class="muted"><strong>Fixtures:</strong> {FIXTURES[family]}</p></article>')
            return out
        candidate = formulas(family, record['candidate_coefficients'], candidate=True)
        out.append(f'<p><strong>Rounded implementation candidate:</strong> <code>{esc(candidate)}</code> varops. '
                   f'Implemented in varops.h: <code>{esc(CURRENT_COSTS[family])}</code>. W(n) rounds bytes up to a multiple of eight.</p>')
        envelope = record['envelope_coefficients']
        uplift = ', '.join(f'{a:.6g} → {b:g}' for a, b in zip(envelope, record['candidate_coefficients']))
        out.append(f'<p class="muted">Coefficient rounding (fixed, then variable): {esc(uplift)}. The black curve is the unrounded envelope used for pricing.</p>')
        out.append(f'<p class="model"><strong>Models:</strong> {MODELS[family]}</p>')
        if family in NOTES:
            out.append(f'<p>{NOTES[family]}</p>')
        if family in FIXTURES:
            out.append(f'<p class="muted"><strong>Fixtures:</strong> {FIXTURES[family]}</p>')
        out.append('<details open><summary>Fitted costs</summary>')
        if family == 'SIG':
            out.extend(signature_table(pts))
        elif family != 'PRODUCE':
            out.append(machine_legend(pts))
            if family in {"H256", "DIVCORE", "MULCORE", "NORMALIZE"}:
                for group in sorted({p["group"] for p in pts}):
                    out.append(f'<div class="facet-title">{esc(group)}</div><div class="chart">{chart(family, pts, family_models(family), group, candidates=candidates)}</div>')
            else:
                out.append(f'<div class="chart">{chart(family, pts, family_models(family), candidates=candidates)}</div>')
        else:
            out.append(f'<div class="chart">{chart(family, pts, {"envelope": env_model}, candidates=candidates)}</div>')
        out.append('<div class="table-wrap"><table><thead><tr><th>Machine</th><th>Fitted cost (varops, unrounded)</th></tr></thead><tbody>')
        for machine in machines:
            out.append(f'<tr><td>{esc(machine["label"])}</td><td><code>{esc(formulas(family, models[machine["key"]][family]))}</code></td></tr>')
        out.append(f'<tr><td>Envelope (pricing basis)</td><td><code>{esc(formulas(family, env_model[family]))}</code></td></tr></tbody></table></div>')
        out.append('</details></article>')
        return out

    def check_article(key, tag):
        spec = CHECKS[key]
        points = check_points(key, series)
        out = [f'<article id="{key}"><{tag}>{esc(DISPLAY[key])}</{tag}>',
               f'<p><strong>Charged as:</strong> <code>{esc(spec["charge"])}</code> varops'
               + (f' ({esc(spec["composition"])})' if 'composition' in spec else '') + '.</p>',
               f'<p class="model"><strong>Measures:</strong> {spec["models"]}</p>',
               f'<p class="muted"><strong>Fixtures:</strong> {spec["fixtures"]}</p>']
        if spec.get('note'):
            out.append(f'<p class="muted">{spec["note"]}</p>')
        if not points:
            out.append('<p><strong>Awaiting calibration:</strong> the calibration shown here predates these fixtures.</p></article>')
            return out
        out.append('<details open><summary>Measured work and implemented charge</summary>')
        out.append(machine_legend(points, fits=False, charge=spec['charge']))
        out.append(f'<div class="chart">{check_chart(key, points)}</div>')
        out.append('<div class="table-wrap"><table><thead><tr><th>Machine</th><th>Largest measured / charged</th><th>Fixture</th></tr></thead><tbody>')
        for machine in machines:
            own = [p for p in points if p['machine_key'] == machine['key']]
            if not own:
                continue
            worst = max(own, key=lambda p: p['ratio'])
            out.append(f'<tr><td>{esc(machine["label"])}</td><td>{worst["ratio"]:.3g}</td><td><code>{esc(worst["label"])}</code></td></tr>')
        out.append('</tbody></table></div></details></article>')
        return out

    for section in SECTIONS:
        slug = section["slug"]
        parts.append(f'<section class="category" id="{slug}"><h2>{esc(section["title"])}</h2><p class="section-intro">{esc(section["intro"])}</p>')
        grouped = len(section["groups"]) > 1
        parts.append('<nav class="nav" aria-label="Entries in this BIP">')
        for _, families in section["groups"]:
            for family in families:
                parts.append(f'<a href="#{family}">{esc(DISPLAY.get(family, family))}</a>')
        parts.append('</nav>')
        for group_title, families in section["groups"]:
            if grouped:
                parts.append(f'<h3 class="group-title">{esc(group_title)}</h3>')
            for family in families:
                tag = 'h4' if grouped else 'h3'
                parts.extend(check_article(family, tag) if family in CHECKS else priced_article(family, tag))
        parts.append('</section>')
    parts.append("""<script>
function showCategory(){
  const target=document.getElementById(location.hash.slice(1));
  const selected=target?.classList.contains('category')?target.id:(target?.closest('.category')?.id||'category-interpreter');
  document.querySelectorAll('.category').forEach(el=>{el.hidden=el.id!==selected});
  document.querySelectorAll('.nav a').forEach(el=>{const active=el.getAttribute('href')==='#'+selected;el.classList.toggle('active',active);if(active)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current')});
  if(target&&target.id!==selected)requestAnimationFrame(()=>target.scrollIntoView());
}
window.addEventListener('hashchange',showCategory);showCategory();
</script></main></html>""")
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                         prefix=".joint-calibration-", suffix=".html",
                                         delete=False) as destination:
            pending = Path(destination.name)
            destination.write(publish_names(restyle(''.join(parts))))
        os.replace(pending, output)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    print(f"Saved {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("joint", type=Path, help="joint-calibration.json")
    parser.add_argument("--output", type=Path, help="HTML path (default beside joint JSON)")
    parser.add_argument("--source-root", type=Path, help="Verify sources against recorded commits and save the audit")
    parser.add_argument("--title", default=TITLE, help="page title")
    args = parser.parse_args()
    source = args.joint.resolve()
    output = args.output.resolve() if args.output else source.with_suffix('.html')
    render(source, output, args.source_root, args.title)


if __name__ == "__main__":
    main()
