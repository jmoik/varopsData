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
TITLE = "Varops calibration"
METHODOLOGY_URL = "https://github.com/jmoik/varopsData/blob/master/METHODOLOGY.md"
GSR_URL = "https://github.com/jmoik/bitcoin/tree/gsr"

SECTIONS = [
    dict(slug="category-interpreter", title="Interpreter",
         intro="Every instruction pays BASE. The opcodes BIP 441 restores add no primitive: each is priced from the primitives on this page.",
         groups=[("Interpreter", ("F",))]),
    dict(slug="category-stack", title="Stack and byte processing",
         intro="Creating, reading and reordering stack values.",
         groups=[("Stack and byte processing", ("PRODUCE", "READ", "MOVE"))]),
    dict(slug="category-numeric", title="Numeric and bit operations",
         intro="Numbers of any size: preparing operands, the arithmetic itself, and turning results back into bytes.",
         groups=[("Numeric and bit operations", ("PREP", "NORMALIZE", "ARITH", "BIT", "MULCORE", "DIVCORE"))]),
    dict(slug="category-crypto", title="Hashing and signatures",
         intro="Hashes are charged per 64-byte block they process. A signature check has a fixed price.",
         groups=[("Hashing and signatures", ("H256", "H160", "H1", "SIG"))]),
    dict(slug="section-extended-primitives", title="Extended Primitives · OP_CHECKSIGFROMSTACK, OP_TWEAKADD, OP_BYTEREV",
         intro="OP_TWEAKADD adds one primitive, TWEAK. OP_CHECKSIGFROMSTACK and OP_BYTEREV are charged with existing primitives; their measurements are compared with that charge.",
         groups=[("Opcodes", ("TWEAK", "CSFS", "BYTEREV"))]),
    dict(slug="section-optx", title="OP_TX",
         intro="OP_TX adds one primitive, OP_TX_SELECT, for selecting and encoding transaction fields.",
         groups=[("OP_TX", ("SELECT",))]),
    dict(slug="section-macros", title="Reusable Macros · OP_MACRO, OP_CALLMACRO",
         intro="Macros add no primitive: unrolling costs BASE per substituted instruction and visited reference, plus WRITE of the unrolled script. The measurements are compared with that charge.",
         groups=[("Unrolling", ("UNROLL",))]),
]
# Prices implemented in src/script/varops.h, compared against the joint candidate.
IMPLEMENTED_AT = '7d0c293b64'
CURRENT_COSTS = {'F': '350', 'PREP': '200 + W(n)', 'PRODUCE': '800 + 8 × W(n)', 'NORMALIZE': '200',
                 'READ': '90 + 2 × W(n)', 'ARITH': '150 + 3 × W(n)', 'BIT': '200 + 2 × W(n)', 'MOVE': '200 + 37 × k',
                 'MULCORE': '400 + 6 × u + 120 × v + 29 × u × v', 'DIVCORE': '510 × s + 33 × s × v',
                 'H256': '300 + 38 × H(n)', 'H160': '60 + 40 × H(n)',
                 'H1': '200 + 24 × H(n)', 'SIG': '500000', 'TWEAK': '170000',
                 'SELECT': '2400 + 270 × k'}
BASE, WRITE, SHA256, BIT, SIGCHECK = 350, (800, 8), (300, 38), (200, 2), 500_000
PREPARE, READ = (200, 1), (90, 2)  # per byte of W(n)


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
        models='One BIP 340 signature check of an <code>n</code>-byte message. Its challenge hash covers R, P and the message, so the message bytes are charged as SHA256.',
        fixtures='valid signatures over messages of 0 bytes to 4 MB.'),
    'BYTEREV': dict(
        source='BIT', select=lambda p: p['group'].startswith('byterev'),
        charge='BIT(W(n))',
        charged=lambda p: BIT[0] + BIT[1] * p['x'],
        curve=lambda x: BIT[0] + BIT[1] * x,
        xlabel='Bytes rounded up to 8, W(n)',
        models='Reversing the bytes of a value in place: each 64-bit word is byte-swapped and the word order reversed.',
        fixtures='OP_BYTEREV’s complete work (pop, reverse, push) on values of 1 byte to 4 MB.'),
    'UNROLL': dict(
        source='UNROLL', select=lambda p: p.get('charged') is not None,
        charge='BASE × units + WRITE(unrolled length)',
        charged=lambda p: unroll_charge(p['units'], p['bytes']) / p['units'],
        xlabel='Unrolled bytes per charged unit',
        models='Evaluating a script whose macro references all sit in an inactive branch: decoding the references, substituting the bodies, copying their bytes into the unrolled script and skipping every instruction. Units are substituted instructions plus visited references; the charge includes the four opcodes around them.',
        fixtures='bodies of 1, 16 or 256 OP_NOPs; single pushes of 1 byte to 1 MB, referenced 16 times or up to the 4 MB unrolled limit; reference chains 16 and 256 deep that unroll to nothing.'),
}
# Fixtures of priced primitives that may not be in every calibration yet.
FIXTURES = {
    'SELECT': 'empty witness items, weight and amount scans, outputs, and all fields of every input, each with <code>k</code> charged units. Only collated output is fitted; noncollated output also pays WRITE per value.',
    'TWEAK': 'one x-only key tweak with a valid key and tweak.',
}


# What each primitive pays for (from the BIP 440 primitive table), shown under its heading.
MODELS = {
    'F': 'The work every instruction does: decoding, dispatch, metering and stack-limit checks.',
    'PREP': 'Reading one numeric operand into 64-bit words; charged per operand.',
    'PRODUCE': 'Creating one stack value of <code>n</code> bytes: allocating, filling and inserting it, and eventually releasing it.',
    'NORMALIZE': 'Turning a numeric result back into minimal bytes.',
    'READ': 'Scanning bytes without creating a value: comparisons, zero tests and length conversion.',
    'ARITH': 'One pass over the operands’ words with a carry between words: addition and subtraction.',
    'BIT': 'One pass over the operands’ words without carries: bitwise logic, shifts and OP_BYTEREV’s byte reversal.',
    'MOVE': 'Reordering <code>k</code> stack entries without copying their contents, as OP_ROLL does.',
    'MULCORE': 'Schoolbook multiplication of a <code>u</code>-limb number by a <code>v</code>-limb number (<code>v</code> ≤ <code>u</code>, 64-bit limbs), including scratch space.',
    'DIVCORE': 'Long division or remainder: <code>s</code> quotient rows, each working through the <code>v</code> limbs of the divisor.',
    'H256': 'SHA256 of an <code>n</code>-byte message, over whole 64-byte blocks.',
    'H160': 'RIPEMD160 of a message of at most 520 bytes, over whole 64-byte blocks.',
    'H1': 'SHA1 of a message of at most 520 bytes, over whole 64-byte blocks.',
    'SIG': 'One BIP 340 signature check. Its price is fixed at 500,000 varops, which keeps today’s allowance of one signature check per 50 weight units; the challenge hash is charged separately as SHA256.',
    'TWEAK': 'One BIP 449 x-only public key tweak (OP_TWEAKADD).',
    'SELECT': 'One OP_TX selection with <code>k</code> charged units: each value selected and each record scanned, including planning, framing and cleanup.',
}

# Additional explanation shown under a primitive's description.
NOTES = {
    'NORMALIZE': 'Hollow marks are a path scripts cannot reach, an unaligned buffer; they are shown but not fitted. A result hands its buffer over in place, whatever its length, so NORMALIZE is a flat charge.',
    'PRODUCE': 'Shortening a value is not fitted on its own: it takes an opcode that pays for producing its result, so creating and then shortening a value counts as two productions.',
}

def group_digits(formula):
    """Thousands separators for the whole numbers of a displayed formula."""
    return re.sub(r'(?<![\d.])\d{5,}(?![\d.])', lambda m: f'{int(m.group(0)):,}', formula)


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
    description = ('; '.join(f'{marker}: {label}' for key, label, marker in LEGEND_DESC if any(p['machine_key'] == key for p in shown)) + '. Hollow marks are measured but not fitted.')
    if family == 'PRODUCE' and machine_count > 1:
        description = 'Machine is encoded by colour and path by marker shape.'
    if 'envelope' in models:
        description += ' The solid line is the envelope: the cheapest curve of the same form on or above every machine’s fit.'
    if candidates is not None:
        description += ' The dotted line is the price: the envelope rounded up.'
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
    xlabel = ("Quotient rows s" if family == "DIVCORE" else "Limbs of the longer operand u" if family == "MULCORE" else
              "Charged units k" if family == "SELECT" else "Stack entries moved k" if family == "MOVE" else
              "Unrolled bytes per charged unit" if family == "UNROLL" else "Size in bytes")
    pieces.append(f'<text x="{(LEFT+RIGHT)/2:.1f}" y="{HEIGHT-12}" text-anchor="middle" class="axis">{xlabel} (log scale)</text>')
    ylabel = "Varops per charged unit" if family == "UNROLL" else "Varops per operation"
    pieces.append(f'<text transform="translate(17 {(TOP+BOTTOM)/2:.1f}) rotate(-90)" text-anchor="middle" class="axis">{ylabel} (log scale)</text>')
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
        legend = '<p>Colour is the machine, shape is the path. Each machine is fitted over all its paths.</p><div class="plot-legend">'
        for key, label in MACHINE_NAMES.items():
            if not any(p['machine_key'] == key for p in shown): continue
            legend += f'<span style="color:{COLORS[key]}">● {label}</span>'
        if 'envelope' in models:
            legend += '<span style="color:#172536">━ Envelope of the fits</span>'
        if candidates is not None:
            legend += '<span><i class="plot-line candidate" aria-hidden="true"></i>Price</span>'
        legend += '</div><div class="plot-legend">'
        for path, (_, _, label) in PRODUCE_MARKERS.items():
            legend += f'<span><svg class="legend-icon" width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">{produce_marker(path, 7, 7, "#526174")}</svg>{esc(label)}</span>'
        legend += '</div><p class="muted">Each measurement includes releasing the value. Growth and churn are two productions each, so their size is the average per production. Hover a mark for details.</p>'
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
              '<desc>Each mark is one measurement. The solid line is the charge; marks above it cost more than they are charged.</desc>',
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
    pieces.append(f'<text x="{(LEFT+RIGHT)/2:.1f}" y="{HEIGHT-12}" text-anchor="middle" class="axis">{esc(spec["xlabel"])} (log scale)</text>')
    ylabel = "Varops per charged unit" if key == "UNROLL" else "Varops per operation"
    pieces.append(f'<text transform="translate(17 {(TOP+BOTTOM)/2:.1f}) rotate(-90)" text-anchor="middle" class="axis">{ylabel} (log scale)</text>')
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


def reference_row(artifact):
    """Summary row of the worst Tapleaf 0xC0 reference case recorded in an artifact."""
    reference = json.loads(Path(artifact).read_text())['reference']
    return next((row for row in reference['raw_rows'] if row['Record_Type'] == 'summary'
                 and row['Name'] == reference['worst_case'] and row['Wall_Min_Seconds']), None)


def reference_spread(artifact):
    """Per-round spread of the worst Tapleaf 0xC0 reference case, from the artifact's summary row."""
    row = reference_row(artifact)
    if row is None:
        return None
    low, high = float(row['Wall_Min_Seconds']), float(row['Wall_Max_Seconds'])
    return dict(case=row['Opcode'], rounds=int(row['Samples']), low=low, high=high, spread=high / low - 1)


def reference_workload(artifact):
    """The slowest Tapleaf 0xC0 workload of a machine, in plain words."""
    row = reference_row(artifact)
    if row is None:
        return 'not recorded'
    opcode = row['Opcode'].removeprefix('OP_')
    if 'CHECKSIG' in opcode:
        return 'signature checks'
    size = row.get('Operand_Shape', '').removesuffix('B')
    return f'{opcode} of {size}-byte values' if size.isdigit() else opcode


def size_range(decade):
    """Sizes of a quality-gate decade: 10^d to 10^(d+1) - 1, and 0 for decade -1."""
    return '0' if decade < 0 else f'{10 ** decade:,}–{10 ** (decade + 1) - 1:,}'


def diagnostics_html(joint, machines, dataset):
    """Measurements above their price, the fit quality gate and run quality, from the joint fit's diagnostics."""
    diag = joint.get('diagnostics')
    if not diag:
        return ''
    names = {machine['meta']['label']: machine['label'] for machine in machines}
    gate = diag['quality_gate']
    limits = gate['quality_gate']
    within = limits['within_factor']
    parts = ['<div id="diagnostics"><h2>Measurement checks</h2>',
             '<p>These checks qualify the measurements; they do not change the prices. Whether a script can take '
             'longer than its machine&#39;s reference is decided by complete-script benchmarks, not by single '
             'measurements.</p>']

    coverage = diag['charge_coverage'].get('candidate') or next(iter(diag['charge_coverage'].values()))
    above = sorted(coverage['above_charge'], key=lambda item: -item['ratio'])
    worst = {}
    for item in above:
        worst.setdefault(item['family'], item)
    summary = f'{len(above):,} of {coverage["checked"]:,} measurements cost more than their price'
    if above:
        summary += f', at most {above[0]["ratio"]:.2f}×'
    parts.append(f'<details><summary>{summary}</summary>'
                 '<p>Measured ÷ price uses the pricing rate, at which a full budget of fitted work takes '
                 f'{TARGET_FRACTION:g} × the machine&#39;s reference. Above 1, a budget spent on that operation alone '
                 'would take longer.</p>')
    if above:
        parts.append('<div class="table-wrap"><table><thead><tr><th>Primitive</th><th>Above the price</th>'
                     '<th>Highest</th><th>Machine</th><th>Measured ÷ price</th></tr></thead><tbody>')
        for family, item in worst.items():
            count = sum(other['family'] == family for other in above)
            parts.append(f'<tr><td><a href="#{family}">{esc(DISPLAY.get(family, family))}</a></td><td>{count}</td>'
                         f'<td><code>{esc(item["fixture"])}</code></td>'
                         f'<td>{esc(names.get(item["machine"], item["machine"]))}</td>'
                         f'<td>{item["ratio"]:.2f}×</td></tr>')
        parts.append('</tbody></table></div>')
        parts.append(f'<details><summary>All {len(above)} measurements</summary>'
                     '<div class="table-wrap"><table><thead><tr><th>Measurement</th><th>Machine</th>'
                     '<th>Measured varops</th><th>Price</th><th>Measured ÷ price</th></tr></thead><tbody>')
        for item in above:
            parts.append(f'<tr><td><code>{esc(item["fixture"])}</code></td>'
                         f'<td>{esc(names.get(item["machine"], item["machine"]))}</td>'
                         f'<td>{item["measured_varops"]:,.0f}</td><td>{item["charged_varops"]:,.0f}</td>'
                         f'<td>{item["ratio"]:.2f}×</td></tr>')
        parts.append('</tbody></table></div></details>')
    parts.append('</details>')

    failures = gate['gate_failures']
    under = [item for item in failures if item['max_above_fit'] > within]
    below = [item for item in failures if item not in under and item['max_below_fit'] > within]
    parts.append(f'<details><summary>Fit quality: {len(under)} of {gate["gate_bins"]:,} size ranges have a measurement '
                 f'more than {within:g}× above its machine&#39;s fit</summary>'
                 '<p>BIP 440 asks each machine&#39;s fit to stay close to its measurements in every path and size '
                 f'decade: a root-mean-square factor of at most {limits["max_rms_factor"]:.2f}, and at least '
                 f'{100 * limits["min_within_share"]:g}% of measurements within {within:g}× of the fit. '
                 f'{len(failures):,} ranges miss that target. In {len(below):,} of them measurements lie well below the '
                 f'fit, where the price errs on the safe side, and {len(failures) - len(under) - len(below)} miss only '
                 f'the root-mean-square factor. In {len(under)} a measurement lies more than {within:g}× above the fit.</p>')
    parts.append('<div class="table-wrap"><table><thead><tr><th>Machine</th><th>Size ranges</th>'
                 f'<th>Missing the target</th><th>More than {within:g}× above the fit</th></tr></thead><tbody>')
    for machine in machines:
        label = machine['meta']['label']
        failing = [item for item in failures if item['machine'] == label]
        parts.append(f'<tr><td>{esc(machine["label"])}</td><td>{gate["bins_by_machine"][label]}</td>'
                     f'<td>{len(failing)}</td><td>{sum(item in under for item in failing)}</td></tr>')
    parts.append('</tbody></table></div>')
    if under:
        parts.append(f'<details><summary>The {len(under)} size ranges</summary><div class="table-wrap"><table><thead>'
                     '<tr><th>Primitive</th><th>Path</th><th>Sizes</th><th>Machine</th><th>Measurements</th>'
                     '<th>Most above the fit</th></tr></thead><tbody>')
        for item in sorted(under, key=lambda item: -item['max_above_fit']):
            parts.append(f'<tr><td><a href="#{item["family"]}">{esc(DISPLAY.get(item["family"], item["family"]))}</a></td>'
                         f'<td><code>{esc(item["group"])}</code></td><td>{size_range(item["decade"])}</td>'
                         f'<td>{esc(names.get(item["machine"], item["machine"]))}</td><td>{item["fixtures"]}</td>'
                         f'<td>{item["max_above_fit"]:.2f}×</td></tr>')
        parts.append('</tbody></table></div></details>')
    parts.append('</details>')

    noises = [machine['meta']['epoch_noise'] for machine in machines if machine['meta']['epoch_noise'] is not None]
    epochs = sorted({machine['meta']['epochs'] for machine in machines})
    passes = f'{epochs[0]}' if len(epochs) == 1 else f'{epochs[0]} to {epochs[-1]}'
    noise_range = (f'{100 * min(noises):.2f}–{100 * max(noises):.2f}%' if noises else 'not measured')
    parts.append(f'<details><summary>Repeatability: passes differ by {noise_range} (limit {100 * MAX_EPOCH_NOISE:g}%)'
                 f'</summary><p>Each measurement is the median of {passes} passes over all primitives. A run is accepted '
                 'when its passes agree: over all measurements, the median of each one&#39;s median deviation between '
                 f'passes must stay below {100 * MAX_EPOCH_NOISE:g}%; otherwise the run is repeated. The last column '
                 'shows how much the slowest reference case varied over its rounds.</p>')
    parts.append('<div class="table-wrap"><table><thead><tr><th>Machine</th><th>Reference</th>'
                 '<th>Pass difference</th><th>Slowest reference case over its rounds</th></tr></thead><tbody>')
    for machine in machines:
        meta = machine['meta']
        spread = reference_spread(meta['file'])
        noise = meta['epoch_noise']
        noise = 'not measured' if noise is None else f'{100 * noise:.2f}%' + (' (fails)' if noise > MAX_EPOCH_NOISE else '')
        rounds = (f'{esc(spread["case"])}, {spread["rounds"]} rounds: {spread["low"]:.3f}–{spread["high"]:.3f} s '
                  f'({100 * spread["spread"]:.1f}%)' if spread else 'not recorded')
        parts.append(f'<tr><td>{esc(machine["label"])}</td><td>{meta["reference_seconds"]:.3f} s</td><td>{noise}</td>'
                     f'<td>{rounds}</td></tr>')
    parts.append('</tbody></table></div>')
    if (dataset / 'reference-csv').exists():
        parts.append('<p>The recovered per-round reference samples are in the dataset&#39;s <code>reference-csv/</code>.</p>')
    parts.append('</details></div><!--/diagnostics-->')
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
</style><main><header><h1>How Tapleaf 0xC2 operations are priced</h1>"""]
    heads = sorted({machine['meta']['head'][:10] for machine in machines if machine['meta'].get('head')})
    same_schedule = all(cost_comparison(joint, family)[2] for family in CURRENT_COSTS)
    dated = re.match(r'\d{4}-\d{2}-\d{2}', joint_path.parent.name)
    chips = [f'{len(machines)} machines', f'{sum(len(m["points"]) for m in machines):,} measurements']
    if dated:
        chips.append(f'measured {dated.group(0)}')
    status = (f' These are the prices the <a href="{GSR_URL}">gsr branch</a> implements.' if same_schedule else '')
    parts.append(f'<p><strong>{" · ".join(chips)}</strong> Under BIP 440, a transaction '
                 'with Tapleaf 0xC2 inputs gets a budget of 10,000 varops per weight unit, 40 billion for a full block. Every '
                 'operation pays BASE plus the primitives below, priced so that on each of these machines a block of the '
                 'most expensive scripts takes no longer to validate than the slowest block of today&#39;s scripts.'
                 f'{status}</p>')

    references = [machine['meta']['reference_seconds'] for machine in machines]
    epochs = sorted({machine['meta']['epochs'] for machine in machines})
    passes = f'{epochs[0]} passes' if len(epochs) == 1 else f'{epochs[0]} to {epochs[-1]} passes'
    parts.append('<div class="card prose" id="method"><div class="card-title">How the prices are derived</div><ol class="steps">'
                 '<li><strong>Reference.</strong> Each machine times the most expensive blocks possible today, such as '
                 '80,000 signature checks or repeated hashing of 520-byte values. The slowest is that machine&#39;s '
                 f'reference: {min(references):.1f} to {max(references):.1f} seconds here.</li>'
                 '<li><strong>Measure.</strong> Every primitive is timed on prepared operands, from empty values to the '
                 f'4 MB element limit, in {passes}; each measurement is the median.</li>'
                 '<li><strong>Fit.</strong> Times are converted to varops so that 40 billion varops of fitted work take '
                 f'{TARGET_FRACTION:g} × the reference. Each machine&#39;s measurements are fitted with the primitive&#39;s '
                 'formula, penalizing under-estimates 100 times more than over-estimates.</li>'
                 '<li><strong>Combine.</strong> The envelope is the cheapest formula of the same form that lies on or '
                 'above every machine&#39;s fit at every size.</li>'
                 '<li><strong>Round.</strong> Each price rounds the envelope up: rates to two significant figures, flat '
                 'parts to multiples of 10 below 100 and of 50 from 100. SIG stays at 500,000.</li>'
                 '<li><strong>Check.</strong> Complete scripts of every opcode run on every machine; prices are accepted '
                 'only if no block of them takes longer than the reference.</li>'
                 f'</ol><p>The full method is in <a href="{METHODOLOGY_URL}">METHODOLOGY.md</a>.</p></div>')

    rows = ''.join(f'<tr><td>{esc(machine["label"])}</td><td>{esc(describe(machine["meta"]["file"]))}</td>'
                   f'<td>{machine["meta"]["reference_seconds"]:.2f} s</td>'
                   f'<td>{esc(reference_workload(machine["meta"]["file"]))}</td></tr>' for machine in machines)
    parts.append('<div class="card" id="machines"><div class="card-title">Machines</div><table><thead><tr>'
                 '<th>Machine</th><th>System</th><th>Reference</th><th>Slowest block today</th></tr></thead>'
                 f'<tbody>{rows}</tbody></table><div class="plot-legend">'
                 '<span><i class="plot-line basis" aria-hidden="true"></i>Envelope of the fits</span>'
                 '<span><i class="plot-line candidate" aria-hidden="true"></i>Price</span>'
                 '<span><i class="plot-mark hollow" aria-hidden="true"></i>Measured, not fitted</span></div></div>')

    warnings = []
    if not same_schedule:
        older = [f'{DISPLAY.get(family, family)} <code>{esc(implemented)}</code> instead of <code>{esc(candidate)}</code>'
                 for family in CURRENT_COSTS
                 for implemented, candidate, same in [cost_comparison(joint, family)] if not same]
        warnings.append('The implementation still charges older prices: ' + '; '.join(older) + '.')
    unmatched = source_check.get("unmatched", [])
    if unmatched:
        warnings.append(f'{len(unmatched)} benchmark source files differ from the recorded commits: '
                        + ', '.join(f'<code>{esc(item["path"])}</code> ({esc(Path(item["machine"]).name)})' for item in unmatched)
                        + '. Resolve them before relying on these prices.')
    quick = []
    for machine in machines:
        settings = json.loads(Path(machine['meta']['file']).read_text()).get('measurement_settings')
        # Five epochs at full sample lengths is the adopted calibration setting (2026-09-28).
        if settings and (settings['primitive_epochs'] < 5 or settings['reference_epochs'] < 5 or
                         (settings['sample_ms'], settings['copy_sample_ms']) != (10, 100)):
            quick.append(f"{machine['label']}: {settings['primitive_epochs']} passes, "
                         f"{settings['sample_ms']:g}/{settings['copy_sample_ms']:g} ms samples")
    if quick:
        warnings.append('<strong>Short runs.</strong> These measurements show the direction of changes, not final '
                        'prices: ' + esc('; '.join(quick)) + '.')
    failed = failed_conditions([machine['meta'] for machine in machines])
    if failed:
        labels = {id(machine['meta']): machine['label'] for machine in machines}
        warnings.append('<strong>Exploratory fit.</strong> These runs were too noisy and must be repeated: ' + esc('; '.join(
            f"{labels[id(meta)]} (passes differ by {100 * meta['epoch_noise']:.2f}%)" for meta in failed)) + '.')
    if warnings:
        parts.append('<div class="card">' + ''.join(f'<p class="warning">{w}</p>' for w in warnings) + '</div>')
    parts.append(diagnostics_html(joint, machines, joint_path.parent))
    parts.append('<nav class="nav" aria-label="Sections">')
    for section in SECTIONS:
        parts.append(f'<a href="#{section["slug"]}">{esc(section["title"].split(" · ")[0])}</a>')
    parts.append('</nav></header>')

    def machine_legend(pts, fits=True, charge=None):
        present = [key for key, _, _ in LEGEND_MACHINES if any(q['machine_key'] == key for q in pts)]
        legend = ''.join(f'<span><i class="plot-mark {key}" aria-hidden="true"></i>{label}</span>'
                         for key, label, _ in LEGEND_MACHINES if key in present)
        if fits:
            legend += '<span><i class="plot-line basis" aria-hidden="true"></i>Envelope of the fits</span>'
            legend += '<span><i class="plot-line candidate" aria-hidden="true"></i>Price</span>'
        else:
            legend += ('<span><i class="plot-line basis" aria-hidden="true"></i>Charge'
                       + (f' <code>{esc(group_digits(charge))}</code>' if charge else '') + '</span>')
        return f'<div class="plot-legend" aria-label="Plot legend">{legend}</div>'

    def fitted(family, coefficients):
        """A fitted formula to three significant figures."""
        return group_digits(formulas(family, [float(f'{c:.3g}') for c in coefficients]))

    def sha_note():
        """Why one machine sets SHA256's byte rate, when one machine without SHA extensions does."""
        rates = {machine['key']: models[machine['key']]['H256'][1] for machine in machines}
        others = [rate for key, rate in rates.items() if key != 'i7']
        if 'i7' not in rates or not others or rates['i7'] < max(others):
            return None
        return (f'The Intel Core i7-7700 has no SHA extensions, so SHA256 runs in software there: {rates["i7"]:.0f} '
                f'varops per hashed byte, against {min(others):.0f}–{max(others):.0f} on the other machines. It alone '
                'sets SHA256&#39;s byte rate.')

    def signature_table(pts):
        """OP_CHECKSIG verifies a signature over the 32-byte transaction digest, the only
        message size Bitcoin signs; other sizes belong to OP_CHECKSIGFROMSTACK."""
        span = hash_span(96)
        charge = SIGCHECK + SHA256[0] + SHA256[1] * span
        out = [f'<p>OP_CHECKSIG checks a signature over the 32-byte transaction digest, so its challenge hash covers '
               f'96 bytes: R, P and the digest. It is charged <code>SIGCHECK + SHA256(96)</code> = 500,000 + '
               f'{SHA256[0]} + {SHA256[1]} × {span} = {charge:,} varops. The table compares one such check, measured on '
               'each machine, with that charge. Other message sizes are checked only by '
               '<a href="#CSFS">OP_CHECKSIGFROMSTACK</a>, whose charge grows with the message.</p>',
               '<div class="table-wrap"><table><thead><tr><th>Machine</th><th>Measured (varops)</th>'
               '<th>Charge (varops)</th><th>Measured ÷ charge</th></tr></thead><tbody>']
        for machine in machines:
            own = [p for p in pts if p['machine_key'] == machine['key'] and p['x'] == 32]
            if own:
                y = own[0]['y']
                out.append(f'<tr><td>{esc(machine["label"])}</td><td>{y:,.0f}</td><td>{charge:,}</td><td>{y / charge:.2f}×</td></tr>')
        out.append('</tbody></table></div>')
        return out

    def priced_article(family, tag):
        pts = series[family]
        record = joint['primitives'].get(family)
        out = [f'<article id="{family}"><{tag}>{esc(DISPLAY.get(family, family))}</{tag}>']
        if record is None:
            out.append(f'<p><strong>Price:</strong> <code>{esc(group_digits(CURRENT_COSTS[family]))}</code> varops. '
                       'Not yet measured in this dataset.</p>')
            out.append(f'<p class="model"><strong>Pays for:</strong> {MODELS[family]}</p>')
            out.append(f'<p class="muted"><strong>Measured with:</strong> {FIXTURES[family]}</p></article>')
            return out
        candidate = formulas(family, record['candidate_coefficients'], candidate=True)
        implemented = CURRENT_COSTS[family]
        out.append(f'<p><strong>Price:</strong> <code>{esc(group_digits(candidate))}</code> varops.'
                   + ('' if implemented == candidate else
                      f' The implementation still charges <code>{esc(group_digits(implemented))}</code>.') + '</p>')
        out.append(f'<p class="model"><strong>Pays for:</strong> {MODELS[family]}</p>')
        note = NOTES.get(family) or (sha_note() if family == 'H256' else None)
        if note:
            out.append(f'<p>{note}</p>')
        if family in FIXTURES:
            out.append(f'<p class="muted"><strong>Measured with:</strong> {FIXTURES[family]}</p>')
        out.append('<details open><summary>Measurements and fits</summary>')
        if family == 'SIG':
            out.extend(signature_table(pts))
            out.append('</details></article>')
            return out
        if family != 'PRODUCE':
            out.append(machine_legend(pts))
            groups = sorted({p["group"] for p in pts})
            if family in {"H256", "DIVCORE", "MULCORE", "NORMALIZE"} and len(groups) > 1:
                for group in groups:
                    out.append(f'<div class="facet-title">{esc(group)}</div><div class="chart">{chart(family, pts, family_models(family), group, candidates=candidates)}</div>')
            else:
                out.append(f'<div class="chart">{chart(family, pts, family_models(family), candidates=candidates)}</div>')
        else:
            out.append(f'<div class="chart">{chart(family, pts, {"envelope": env_model}, candidates=candidates)}</div>')
        out.append('<div class="table-wrap"><table><thead><tr><th>Machine</th><th>Fitted cost (varops)</th></tr></thead><tbody>')
        for machine in machines:
            out.append(f'<tr><td>{esc(machine["label"])}</td><td><code>{esc(fitted(family, models[machine["key"]][family]))}</code></td></tr>')
        out.append(f'<tr><td>Envelope of all machines</td><td><code>{esc(fitted(family, env_model[family]))}</code></td></tr></tbody></table></div>')
        out.append('</details></article>')
        return out

    def check_article(key, tag):
        spec = CHECKS[key]
        points = check_points(key, series)
        out = [f'<article id="{key}"><{tag}>{esc(DISPLAY[key])}</{tag}>',
               f'<p><strong>Charged as:</strong> <code>{esc(group_digits(spec["charge"]))}</code> varops'
               + (f' ({esc(spec["composition"])})' if 'composition' in spec else '') + '.</p>',
               f'<p class="model"><strong>Measures:</strong> {spec["models"]}</p>',
               f'<p class="muted"><strong>Measured with:</strong> {spec["fixtures"]}</p>']
        if not points:
            out.append('<p>Not yet measured in this dataset.</p></article>')
            return out
        out.append('<details open><summary>Measurements and charge</summary>')
        out.append(machine_legend(points, fits=False, charge=spec['charge']))
        out.append(f'<div class="chart">{check_chart(key, points)}</div>')
        column = 'Message' if key == 'CSFS' else 'Value' if key == 'BYTEREV' else 'Measurement'
        out.append(f'<div class="table-wrap"><table><thead><tr><th>Machine</th><th>Highest measured ÷ charge</th><th>{column}</th></tr></thead><tbody>')
        for machine in machines:
            own = [p for p in points if p['machine_key'] == machine['key']]
            if not own:
                continue
            worst = max(own, key=lambda p: p['ratio'])
            size = re.search(r'/(\d+)$', worst['label']) if key in {'CSFS', 'BYTEREV'} else None
            shown = f'{int(size.group(1)):,} bytes' if size else f'<code>{esc(worst["label"])}</code>'
            out.append(f'<tr><td>{esc(machine["label"])}</td><td>{worst["ratio"]:.2f}×</td><td>{shown}</td></tr>')
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
    parts.append('<footer class="footer">' + (f'Measured at gsr {", ".join(heads)}. ' if heads else '')
                 + (f'Prices implemented at gsr {IMPLEMENTED_AT}. ' if same_schedule else '')
                 + 'Data, code and method: <a href="https://github.com/jmoik/varopsData">jmoik/varopsData</a>.</footer>')
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
