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
from fit_calibrations import COMPOSED, TARGET_FRACTION, candidate_charge, charged_by, check_source_snapshots, envelope_model, features, byte_spans, formulas, hash_span, failed_conditions, independent_models, load_calibration, predict, priced_model, selection_time


# Sections: the BIP 440 primitive categories, then one section per later BIP. A
# primitive or opcode appears under the BIP that introduced it; groups within a
# section are headed only when there is more than one.
TITLE = "Varops calibration"
METHODOLOGY_URL = "https://github.com/jmoik/varopsData/blob/master/METHODOLOGY.md"
GSR_URL = "https://github.com/jmoik/bitcoin/tree/gsr"

SECTIONS = [
    dict(slug="category-interpreter", title="Interpreter",
         groups=[("Interpreter", ("F",))]),
    dict(slug="category-stack", title="Stack and byte processing",
         intro="Creating, reading and reordering stack values. READ includes converting an operand to a number, WRITE converting a numeric result back to bytes.",
         groups=[("Stack and byte processing", ("WRITE", "READ", "MOVE"))]),
    dict(slug="category-numeric", title="Numeric and bit operations",
         intro="Numbers of any size: passes over their words, multiplication and division.",
         groups=[("Numeric and bit operations", ("ARITH", "MULCORE", "DIVCORE"))]),
    dict(slug="category-crypto", title="Hashing and signatures",
         intro="Hashes are charged per 64-byte block they process, at one price for SHA256, RIPEMD160 and SHA1. Every elliptic-curve operation, a signature check or a public key tweak, costs one SIGCHECK.",
         groups=[("Hashing and signatures", ("HASH", "SIG"))]),
    dict(slug="section-extended-primitives", title="Extended Primitives · OP_CHECKSIGFROMSTACK, OP_TWEAKADD, OP_BYTEREV",
         intro="These opcodes add no primitive. OP_CHECKSIGFROMSTACK and OP_TWEAKADD each pay one SIGCHECK (OP_TWEAKADD's tweak is measured under Hashing and signatures); their measurements are compared with that charge.",
         groups=[("Opcodes", ("CSFS", "BYTEREV"))]),
    dict(slug="section-optx", title="OP_TX",
         intro="OP_TX adds one primitive, OP_TX_SELECT, for selecting and encoding transaction fields.",
         groups=[("OP_TX", ("SELECT",))]),
    dict(slug="section-macros", title="Reusable Macros · OP_MACRO, OP_CALLMACRO",
         intro="Macros add no primitive: unrolling costs BASE per substituted instruction and visited reference, plus WRITE of the unrolled script. The measurements are compared with that charge.",
         groups=[("Unrolling", ("UNROLL",))]),
]
# Prices implemented in src/script/varops.h, compared against the joint candidate.
IMPLEMENTED_AT = '43e28c2a15'
CURRENT_COSTS = {'F': '300', 'READ': '350 + 2 × W(n)', 'WRITE': '800 + 7 × W(n)', 'ARITH': '200 + 3 × W(n)',
                 'MOVE': '200 + 23 × k',
                 'MULCORE': '700 + 1 × W(n) + 18 × W(m) + 1 × W(n) × W(m)',
                 'DIVCORE': '1150 + 60 × Q(n, m) + 11 × W(m) + 1 × Q(n, m) × W(m)',
                 'HASH': '50 + 40 × H(n)', 'SIG': '500000',
                 'SELECT': '1550 + 620 × k'}
BASE, WRITE, READ, HASH, SIGCHECK = 300, (800, 7), (350, 2), (50, 40), 500_000  # rates per byte of W(n), H(n)
ARITH, MOVE, MUL, SELECT = (200, 3), (200, 23), (700, 1, 18, 1), (1550, 620)  # MUL per byte of W(n), W(m), W(n) × W(m)


def unroll_charge(units, length, base=BASE, write=WRITE, padded=True):
    """Complete charge of an UNROLL fixture, as bench_varops_primitives composes it: the
    unrolling charge, then OP_0, OP_IF, OP_ENDIF and OP_1."""
    span = (lambda n: (n + 7) // 8 * 8) if padded else (lambda n: n)
    def write_cost(n):
        return write[0] + write[1] * span(n)
    return units * base + write_cost(length) + 4 * base + write_cost(0) + write_cost(8)

# Common opcodes composed from the implemented prices, as the v2 evaluator adds the charges.
OPCODE_PRIMITIVES = {'BASE': 'F', 'WRITE': 'WRITE', 'READ': 'READ', 'MOVE': 'MOVE', 'ARITH': 'ARITH',
                     'MUL': 'MULCORE', 'HASH': 'HASH', 'SIG': 'SIG', 'OP_TX_SELECT': 'SELECT'}


def opcode_examples():
    """Rows of (opcode, charge, example, varops); the charge links its primitives."""
    span = lambda n: (n + 7) // 8 * 8
    write = lambda n: WRITE[0] + WRITE[1] * span(n)
    read = lambda n: READ[0] + READ[1] * span(n)
    hash_cost = lambda n: HASH[0] + HASH[1] * hash_span(n)
    rows = [
        ('OP_DUP', 'BASE + WRITE(n)', 'a 32-byte value', BASE + write(32)),
        ('OP_EQUAL', 'BASE + READ(n) + WRITE(8), READ only if both sizes are n', 'two 32-byte values',
         BASE + read(32) + write(8)),
        ('OP_ROLL', 'BASE + READ(m) + MOVE(k + 1), for an m-byte depth k', 'depth 10',
         BASE + read(1) + MOVE[0] + MOVE[1] * 11),
        ('OP_ADD', 'BASE + READ(a) + READ(b) + ARITH(max(a, b)) + WRITE(r)',
         'two 8-byte numbers, 8-byte sum', BASE + 2 * read(8) + ARITH[0] + ARITH[1] * 8 + write(8)),
        ('OP_MUL', 'BASE + READ(a) + READ(b) + MUL(n, m) + WRITE(W(a) + W(b))', 'two 8-byte numbers',
         BASE + 2 * read(8) + MUL[0] + MUL[1] * 8 + MUL[2] * 8 + MUL[3] * 64 + write(16)),
        ('OP_BYTEREV', 'BASE + ARITH(n) + WRITE(n)', 'a 32-byte value', BASE + ARITH[0] + ARITH[1] * 32 + write(32)),
        ('OP_SHA256', 'BASE + HASH(n) + WRITE(32)', 'a 32-byte value', BASE + hash_cost(32) + write(32)),
        ('OP_HASH160', 'BASE + HASH(n) + HASH(32) + WRITE(20)', 'a 33-byte public key',
         BASE + hash_cost(33) + hash_cost(32) + write(20)),
        ('OP_CHECKSIG', 'BASE + HASH(96) + SIG + WRITE(8), HASH and SIG only for a non-empty signature',
         'a valid signature', BASE + hash_cost(96) + SIGCHECK + write(8)),
        ('OP_CHECKSIGFROMSTACK', 'BASE + HASH(64 + n) + SIG + WRITE(8), HASH and SIG only for a non-empty signature',
         'a 32-byte message', BASE + hash_cost(64 + 32) + SIGCHECK + write(8)),
        ('OP_TX', 'BASE + READ per scope operand + OP_TX_SELECT(k) + WRITE of each result, WRITE(8) for a number', 'one number, e.g. nVersion',
         BASE + SELECT[0] + SELECT[1] + write(8)),
    ]
    link = lambda m: f'<a href="#{OPCODE_PRIMITIVES[m.group(0)]}">{m.group(0)}</a>'
    pattern = r'\b(' + '|'.join(OPCODE_PRIMITIVES) + r')\b'
    return [(op, re.sub(pattern, link, charge, count=0), example, value) for op, charge, example, value in rows]


def opcodes_html():
    rows = ''.join(f'<tr><td><code>{op}</code></td><td>{charge}</td><td>{example}</td><td class="num">{value:,}</td></tr>'
                   for op, charge, example, value in opcode_examples())
    return ('<div class="card prose" id="opcodes"><div class="card-title">How opcodes are charged</div>'
            '<p>Opcodes have no prices of their own. Each pays BASE plus the primitives for the work it does, sized '
            'by its actual operands, and the sum is deducted from the budget once per opcode. A few common opcodes, '
            'at example sizes:</p>'
            '<table><thead><tr><th>Opcode</th><th>Charge</th><th>Example</th><th>Varops</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
            '<p>a and b are operand sizes in bytes and r the size of the result. Every numeric operand pays READ, '
            'and every result WRITE of its size; counts, comparison results, booleans and constants pay WRITE(8). '
            'Macros add no primitive: unrolling pays BASE per substituted instruction and visited reference, plus '
            'WRITE of the unrolled script.</p></div>')


# Opcodes charged from existing primitives. Each measured fixture is divided by
# its implemented charge; a ratio above 1 means the charge does not cover it.
CHECKS = {
    'CSFS': dict(
        source='SIG', select=lambda p: True,
        charge=f'{SIGCHECK + HASH[0]} + {HASH[1]} × H(64 + n)',
        composition='SIGCHECK + HASH(64 + n)',
        charged=lambda p: SIGCHECK + HASH[0] + HASH[1] * hash_span(64 + p['x']),
        curve=lambda x: SIGCHECK + HASH[0] + HASH[1] * hash_span(64 + round(x)),
        xlabel='Message bytes n',
        models='One BIP 340 signature check of an <code>n</code>-byte message. Its challenge hash, a SHA256, covers R, P and the message, so the message bytes are charged as HASH.',
        fixtures='valid signatures over messages of 0 bytes to 4 MB.'),
    'BYTEREV': dict(
        source=('ARITH', 'BIT'), select=lambda p: p['group'].startswith('byterev'),
        charge='ARITH(W(n)) + WRITE(n)',
        charged=lambda p: ARITH[0] + ARITH[1] * p['x'] + WRITE[0] + WRITE[1] * p['x'],
        curve=lambda x: ARITH[0] + ARITH[1] * x + WRITE[0] + WRITE[1] * x,
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
    'SELECT': 'empty witness items, weight and amount scans, outputs, all fields of every input, and one-byte witness items of another input, each with <code>k</code> charged units. Only collated output is fitted, net of the result’s WRITE and a scope operand’s READ, which OP_TX charges separately; noncollated output also pays WRITE per value. One-byte items shuffled against their allocation order are measured but not fitted.',
    'TWEAK': 'one x-only key tweak of a valid key by a hash-sized tweak.',
}


# What each primitive pays for (from the BIP 440 primitive table), shown under its heading.
MODELS = {
    'F': 'The work every instruction does: decoding, dispatch, metering and stack-limit checks.',
    'READ': 'Reading one operand: converting it into 64-bit words and scanning it for comparisons, zero tests and length conversion; charged per operand.',
    'WRITE': 'Creating one stack value of <code>n</code> bytes: converting a numeric result back to bytes, or allocating and filling a buffer, then inserting it and eventually releasing it.',
    'ARITH': 'One pass over the operands’ words, with or without a carry between words: addition, subtraction, bitwise logic, shifts and OP_BYTEREV’s byte reversal.',
    'MOVE': 'Taking the top <code>k</code> entries off a stack and putting back some or all of them, in any order and on either stack, without copying their contents.',
    'MULCORE': 'Schoolbook multiplication of an <code>n</code>-byte number by an <code>m</code>-byte number (<code>m</code> ≤ <code>n</code>): one row per 64-bit word of the shorter operand over the words of the longer, including scratch space.',
    'DIVCORE': 'Long division or remainder of an <code>n</code>-byte dividend by an <code>m</code>-byte divisor: one row per word by which the dividend exceeds the divisor, plus a fixed few, each working through the words of the divisor.',
    'HASH': 'One SHA256, RIPEMD160 or SHA1 pass over an <code>n</code>-byte message, in whole 64-byte blocks. RIPEMD160 and SHA1 take at most 520 bytes.',
    'SIG': 'One BIP 340 signature check. Its price is fixed at 500,000 varops, which keeps today’s allowance of one signature check per 50 weight units; the challenge hash is charged separately as HASH.',
    'TWEAK': 'One BIP 449 x-only public key tweak (OP_TWEAKADD), P + t·G: the same elliptic-curve work as a signature check without the challenge hash, at about 80% of its time, so it is charged one SIGCHECK too.',
    'SELECT': 'One OP_TX selection with <code>k</code> charged units: each value selected and each record scanned, including planning and framing; the result’s WRITE is charged separately.',
}

# Additional explanation shown under a primitive's description.
NOTES = {
    'NORMALIZE': 'Hollow marks are a path scripts cannot reach, an unaligned buffer; they are shown but not fitted. A result hands its buffer over in place, whatever its length, so this part is flat.',
    'PRODUCE': 'Shortening a value is not fitted on its own: it takes an opcode that pays for producing its result, so creating and then shortening a value counts as two productions.',
    'WRITE': 'Shortening a value is not fitted on its own: it takes an opcode that pays for producing its result, so creating and then shortening a value counts as two productions.',
}

# How a primitive measured in parts is priced from them (fit_calibrations.COMPOSED).
COMPOSED_TEXT = {
    'READ': 'This dataset measured READ in two parts: converting an operand into 64-bit words, and scanning it. '
            'READ is charged once per operand for both, so its price adds the two parts&#39; rounded prices, '
            '{shares}, and each machine&#39;s curve adds its two fits.',
    'WRITE': 'This dataset measured WRITE in two parts: producing a value, and converting a numeric result back to '
             'bytes. WRITE is charged once per value for both, so its price adds the two parts&#39; rounded prices, '
             '{shares}, and each machine&#39;s curve adds its two fits. A value that is not a number pays for the '
             'conversion as well.',
    'ARITH': 'This dataset measured ARITH in two parts: passes with a carry chain and passes without one. An opcode '
             'makes one kind of pass, so the price is the rounded envelope of every machine&#39;s fit of both parts, '
             'whose own prices would be {shares}.',
    'HASH': 'SHA256, RIPEMD160 and SHA1 are measured separately, and one price covers all three: the rounded envelope '
            'of every machine&#39;s fit of each hash function, each over the sizes it takes: SHA256 any size, RIPEMD160 '
            'and SHA1 at most 520 bytes. A hash processes at least one 64-byte block, so the rate carries most of the '
            'cost and the flat stays small. Apart, they would be priced {shares}.',
}
# Parts of a primitive that a dataset measured separately (fit_calibrations.COMPOSED):
# what each part times.
PARTS = {
    'PREP': ('Converting the operand', 'Taking an operand from its stack bytes into 64-bit words.'),
    'READ': ('Scanning the operand', 'Comparisons, zero tests and trimming over the operand&#39;s words, and OP_EQUAL&#39;s byte comparison.'),
    'PRODUCE': ('Producing the value', 'Allocating, filling and inserting a value of <code>n</code> bytes, and eventually releasing it.'),
    'NORMALIZE': ('Converting a numeric result', 'Turning a numeric result back into minimal bytes.'),
    'ARITH': ('With a carry chain', 'Addition and subtraction.'),
    'BIT': ('Without a carry chain', 'Bitwise logic, shifts and OP_BYTEREV&#39;s byte reversal.'),
    'H256': ('SHA256', 'SHA256 of an <code>n</code>-byte message, over whole 64-byte blocks.'),
    'H160': ('RIPEMD160', 'RIPEMD160 of a message of at most 520 bytes, over whole 64-byte blocks.'),
    'H1': ('SHA1', 'SHA1 of a message of at most 520 bytes, over whole 64-byte blocks.'),
}

def and_list(items):
    """Names joined as prose: a, b and c."""
    return ', '.join(items[:-1]) + ' and ' + items[-1] if len(items) > 1 else ''.join(items)


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
    sources = spec['source'] if isinstance(spec['source'], tuple) else (spec['source'],)
    points = [dict(p) for source in sources for p in series.get(source, []) if spec['select'](p)]
    for p in points:
        p['charged'] = spec['charged'](p)
        p['ratio'] = p['y'] / p['charged']
    return points


DISPLAY = {"SELECT": "OP_TX_SELECT", "UNROLL": "Macro unrolling",
           "CSFS": "OP_CHECKSIGFROMSTACK", "BYTEREV": "OP_BYTEREV"}
# Published primitive names; raw calibration data keeps the original family labels.
PUBLISHED_NAMES = [(r'\bhashblockspan\(', 'H('), (r'\bF\b', 'BASE'), (r'\bMULCORE\b', 'MUL'), (r'\bDIVCORE\b', 'DIV'), (r'\bH256\b', 'SHA256'),
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
COLORS = {"m1": "#2563eb", "m4": "#dc6b18", "ryzen": "#7c3aed", "intel": "#b91c1c", "i7": "#be185d", "r5": "#4a3aa7", "r7": "#8f5a2b", "envelope": "#172536", "basis": "#172536",
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
    'numeric': ('#be185d', 'diamond', 'Numeric result'),
    'scalar': ('#4a3aa7', 'down', 'Count or boolean'),
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
# Why each machine is in the sample, shown in the machine table.
MACHINE_NOTES = {'i7': 'Added for SHA256 without hardware acceleration (no SHA extensions)',
                 'intel': 'Recent Intel desktop', 'r5': 'Older AMD desktop (Zen 2)', 'r7': 'Recent AMD desktop (Zen 4)',
                 'ryzen': 'Added to represent Windows (clang-cl build)', 'm1': 'Older Apple ARM',
                 'm4': 'Recent Apple ARM'}
MACHINE_NAMES = {'m1': 'Apple M1 Pro', 'm4': 'Apple M4 Pro', 'ryzen': 'AMD Ryzen 9 9950X',
                 'intel': 'Intel Core i5-12500', 'i7': 'Intel Core i7-7700', 'r5': 'AMD Ryzen 5 3600',
                 'r7': 'AMD Ryzen 7 7700'}
LEGEND_MACHINES = [(key, name, name) for key, name in MACHINE_NAMES.items()]
LEGEND_DESC = [(key, MACHINE_NAMES[key], marker) for key, marker in
               [('m1', 'Blue circles'), ('m4', 'orange squares'), ('ryzen', 'purple diamonds'),
                ('intel', 'red triangles'), ('i7', 'plum inverted triangles'), ('r5', 'indigo left-pointing triangles'),
                ('r7', 'brown right-pointing triangles')]]


def describe(path):
    """Operating system, compiler and SHA256 implementation recorded in an artifact."""
    machine = json.loads(Path(path).read_text())['machine']
    platform = machine['platform']
    system = ('macOS' if platform.startswith('macOS') else
              'Windows 11' if platform.startswith('Windows-11') else
              'Linux' if platform.startswith('Linux') else platform.split('-')[0])
    sha = 'hardware SHA256' if 'shani' in machine['sha256_backend'] else 'software SHA256'
    return f"{system}, {machine['compiler']}, {sha}"


def chart(family, points, models, group=None, id_prefix="", candidates=None, price_label="Price",
          price_note="The dotted line is the price: the envelope rounded up."):
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
    if family in {'PRODUCE', 'WRITE'} and machine_count > 1:
        description = 'Machine is encoded by colour and path by marker shape.'
    if 'envelope' in models:
        description += ' The solid line is the envelope: the cheapest curve of the same form on or above every machine’s fit.'
    if candidates is not None:
        description += ' ' + price_note
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
        tick = value * 8 if family in {"MULCORE", "DIVCORE"} else value  # word counts shown as bytes
        pieces.append(f'<text x="{px:.2f}" y="{BOTTOM+21}" text-anchor="middle" class="tick">{short(tick)}</text>')
        last_tick = px
    xlabel = ("Bytes the dividend exceeds the divisor, Q(n, m)" if family == "DIVCORE" else
              "Longer operand, W(n) bytes" if family == "MULCORE" else
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
    for key in ("m1", "m4", "ryzen", "intel", "i7", "r5", "r7"):
        color = COLORS[key]
        for p in shown:
            if p["machine_key"] != key:
                continue
            px, py = xy(p["x"], p["y"])
            color = ("#087f8c" if p["group"] == "aligned" else "#dc6b18") if family == "NORMALIZE" and machine_count == 1 else COLORS[key]
            fill = color if p["included"] else "white"
            if family in {"PRODUCE", "WRITE"}:
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
            elif key == "r7":
                mark = f'<path d="M{px+3.6:.2f},{py:.2f}L{px-3:.2f},{py-3.6:.2f}L{px-3:.2f},{py+3.6:.2f}Z" fill="{fill}" stroke="{color}" stroke-width=".8"/>'
            else:
                mark = f'<path d="M{px:.2f},{py-3.4:.2f}L{px+3.4:.2f},{py:.2f}L{px:.2f},{py+3.4:.2f}L{px-3.4:.2f},{py:.2f}Z" fill="{fill}" stroke="{color}" stroke-width=".8"/>'
            pieces.append(f'<g><title>{esc(key)} · {esc(p.get("label", p["group"]))}: {p["y"]:.6g} varops</title>{mark}</g>')
    pieces.append("</g></svg>")
    legend = ''
    if family in {'PRODUCE', 'WRITE'}:
        legend = ('<p>Colour is the machine, shape is the path. Each machine is fitted over all its paths.</p>'
                  '<div class="plot-legend">')
        for path, (_, _, label) in PRODUCE_MARKERS.items():
            if not any(p['group'] == path for p in shown): continue
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
    if key == "r7":
        return f'<path d="M{px+3.6:.2f},{py:.2f}L{px-3:.2f},{py-3.6:.2f}L{px-3:.2f},{py+3.6:.2f}Z" fill="{color}" stroke="{color}" stroke-width=".8"/>'
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
    for machine in ("m1", "m4", "ryzen", "intel", "i7", "r5", "r7"):
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
        # Inputs are recorded relative to the joint fit.
        meta['file'] = str(joint_path.parent / meta['file'])
    if joint.get("schema") != "varop-joint-fit-v3":
        raise ValueError("this comparison requires a v3 joint fit")
    model_id = joint.get("model_id")
    if model_id not in COMPOSED:
        raise ValueError(f"unknown primitive model: {model_id}")
    composed = COMPOSED[model_id]
    charged = charged_by(model_id)
    machines = []
    for meta in joint["machines"]:
        source = Path(meta["file"])
        if hashlib.sha256(source.read_bytes()).hexdigest() != meta["sha256"]:
            raise ValueError(f"input changed since the joint fit: {source}")
    machine_models = independent_models([Path(meta["file"]) for meta in joint["machines"]])
    for meta, model in zip(joint["machines"], machine_models):
        source = Path(meta["file"])
        points, _ = load_calibration(source)
        identity = (meta['cpu'] + ' ' + source.name).lower()
        key = next((key for key, token in [('m1', 'm1'), ('m4', 'm4'), ('r5', 'ryzen 5 3600'), ('r7', 'ryzen 7 7700'), ('ryzen', 'ryzen'), ('i7', 'i7-7700'), ('intel', 'intel')] if token in identity), None)
        if key is None:
            raise ValueError(f'Unknown machine identity: {identity}')
        # Repeated runs of a machine are numbered in input order.
        run = 1 + sum(m['key'] == key for m in machines)
        machine_id = f'{key}-{run}'
        for point in points:
            point["machine_key"] = key
            point["machine_id"] = machine_id
            if point["family"] == "SELECT":
                # Charts show what SELECT prices: the time less the result's WRITE
                # and a scope operand's READ at this machine's fits.
                point["y"] = selection_time(point, model)
                del point["result_bytes"]
        machines.append(dict(meta=meta, points=points, model=model, key=key, run=run, id=machine_id))
    runs = max(machine['run'] for machine in machines)
    if any(sum(m['key'] == machine['key'] for m in machines) != runs for machine in machines):
        raise ValueError('every machine needs the same number of runs')
    for machine in machines:
        machine['label'] = MACHINE_NAMES[machine['key']] + (f' · run {machine["run"]}' if runs > 1 else '')
    models = {machine["id"]: machine["model"] for machine in machines}
    priced = {machine["id"]: priced_model(machine["model"], model_id) for machine in machines}
    series = defaultdict(list)
    priced_series = defaultdict(list)
    for machine in machines:
        for point in machine["points"]:
            series[point["family"]].append(point)
            if point["family"] in charged:
                priced_series[charged[point["family"]]].append(point)
    # env_model holds the measured families' envelopes, priced_env the priced primitives'.
    env_model = envelope_model(machine_models, series)
    priced_env = envelope_model([priced[machine["id"]] for machine in machines], priced_series, machine_models,
                                {family: members for family, (how, members) in composed.items() if how == 'max'})
    for records, envelopes in ((joint['primitives'], priced_env), (joint.get('measured_parts', {}), env_model)):
        for family, record in records.items():
            if any(abs(a - b) > 1e-6 * max(1.0, abs(a))
                   for a, b in zip(record['envelope_coefficients'], envelopes[family])):
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
    keys = list(dict.fromkeys(machine['key'] for machine in machines))
    chips = [f'{len(keys)} machines' + (f' × {runs} runs' if runs > 1 else ''),
             f'{sum(len(m["points"]) for m in machines):,} measurements']
    if dated:
        chips.append(f'measured {dated.group(0)}')
    status = (f' These are the prices the <a href="{GSR_URL}">gsr branch</a> implements.' if same_schedule else '')
    parts.append(f'<p><strong>{" · ".join(chips)}</strong> Under BIP 440, a transaction '
                 'with Tapleaf 0xC2 inputs gets a budget of 10,000 varops per weight unit, 40 billion for a full block. Every '
                 'operation pays BASE plus the primitives below, priced so that on each of these machines a block of the '
                 'most expensive scripts takes no longer to validate than the slowest block of today&#39;s scripts.'
                 f'{status}</p>')

    references = [machine['meta']['reference_seconds'] for machine in machines]
    two_parts = [family for family, (_, members) in composed.items() if len(members) == 2]
    epochs = sorted({machine['meta']['epochs'] for machine in machines})
    passes = f'{epochs[0]} passes' if len(epochs) == 1 else f'{epochs[0]} to {epochs[-1]} passes'
    parts.append(opcodes_html())
    parts.append('<div class="card prose" id="method"><div class="card-title">How the prices are derived</div><ol class="steps">'
                 '<li><strong>Reference.</strong> Each machine times the most expensive blocks possible today, such as '
                 '80,000 signature checks or repeated hashing of 520-byte values. The slowest is that machine&#39;s '
                 f'reference: {min(references):.1f} to {max(references):.1f} seconds here.</li>'
                 '<li><strong>Measure.</strong> Every primitive is timed on prepared operands, from empty values to the '
                 f'4 MB element limit, in {passes}; each measurement is the median.'
                 + (f' This dataset timed {and_list(two_parts)} in two parts each.' if two_parts else '')
                 + (' SHA256, RIPEMD160 and SHA1 are timed separately and priced as one primitive, HASH.'
                    if 'HASH' in composed else '')
                 + (' A price composed from parts is described under its primitive.' if composed else '') + '</li>'
                 '<li><strong>Fit.</strong> Times are converted to varops so that 40 billion varops of fitted work take '
                 f'{TARGET_FRACTION:g} × the reference. Each machine&#39;s measurements are fitted with the primitive&#39;s '
                 'formula, penalizing under-estimates 100 times more than over-estimates.</li>'
                 '<li><strong>Combine.</strong> The envelope is the cheapest formula of the same form that lies on or '
                 'above every machine&#39;s fit at every size.</li>'
                 '<li><strong>Round.</strong> Each price rounds the envelope up: flat parts to multiples of 50, rates '
                 'to whole varops. SIG stays at 500,000.</li>'
                 '<li><strong>Check.</strong> Complete scripts of every opcode run on every machine; prices are accepted '
                 'only if no block of them takes longer than the reference.</li>'
                 f'</ol><p>The full method is in <a href="{METHODOLOGY_URL}">METHODOLOGY.md</a>.</p></div>')

    # One row per machine; a machine's runs share its system and list their references in run order.
    first_runs = [machine for machine in machines if machine['run'] == 1]
    rows = ''.join(f'<tr><td><i class="plot-mark {machine["key"]}" aria-hidden="true"></i>{esc(MACHINE_NAMES[machine["key"]])}</td><td>{esc(describe(machine["meta"]["file"]))}</td>'
                   '<td>' + ' / '.join(f'{m["meta"]["reference_seconds"]:.2f}' for m in machines if m['key'] == machine['key']) + ' s</td>'
                   f'<td>{esc(reference_workload(machine["meta"]["file"]))}</td>'
                   f'<td>{esc(MACHINE_NOTES.get(machine["key"], ""))}</td></tr>' for machine in first_runs)
    reference_header = 'Reference' + (f' (runs 1–{runs})' if runs > 1 else '')
    parts.append('<div class="card" id="machines"><div class="card-title">Machines</div><table><thead><tr>'
                 f'<th>Machine</th><th>System</th><th>{reference_header}</th><th>Slowest block today</th><th>Note</th></tr></thead>'
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
    parts.append('<nav class="nav" aria-label="Sections">')
    for section in SECTIONS:
        parts.append(f'<a href="#{section["slug"]}">{esc(section["title"].split(" · ")[0])}</a>')
    parts.append('</nav></header>')

    def charge_legend(charge):
        return ('<div class="plot-legend" aria-label="Plot legend"><span><i class="plot-line basis" aria-hidden="true"></i>'
                f'Charge <code>{esc(group_digits(charge))}</code></span></div>')

    def fitted(family, coefficients):
        """A fitted formula to three significant figures."""
        spans = byte_spans(family, coefficients)  # MUL and DIV shown per byte
        return group_digits(formulas(family, [float(f'{c / span:.3g}') * span for c, span in zip(coefficients, spans)]))

    def sha_note():
        """Why one machine sets SHA256's byte rate, when one machine without SHA extensions does."""
        i7 = [models[machine['id']]['H256'][1] for machine in machines if machine['key'] == 'i7']
        others = [models[machine['id']]['H256'][1] for machine in machines if machine['key'] != 'i7']
        if not i7 or not others or min(i7) < max(others):
            return None
        own = f'{min(i7):.0f}' if round(min(i7)) == round(max(i7)) else f'{min(i7):.0f}–{max(i7):.0f}'
        return (f'The Intel Core i7-7700 has no SHA extensions, so SHA256 runs in software there: {own} '
                f'varops per hashed byte, against {min(others):.0f}–{max(others):.0f} on the other machines. It alone '
                'sets SHA256&#39;s byte rate.')

    def signature_table(pts):
        """OP_CHECKSIG verifies a signature over the 32-byte transaction digest, the only
        message size Bitcoin signs; other sizes belong to OP_CHECKSIGFROMSTACK."""
        span = hash_span(96)
        charge = SIGCHECK + HASH[0] + HASH[1] * span
        out = [f'<p>OP_CHECKSIG checks a signature over the 32-byte transaction digest, so its challenge hash covers '
               f'96 bytes: R, P and the digest. It is charged <code>SIGCHECK + HASH(96)</code> = 500,000 + '
               f'{HASH[0]} + {HASH[1]} × {span} = {charge:,} varops. The table compares one such check, measured on '
               'each machine, with that charge. Other message sizes are checked only by '
               '<a href="#CSFS">OP_CHECKSIGFROMSTACK</a>, whose charge grows with the message.</p>',
               '<div class="table-wrap"><table><thead><tr><th>Machine</th><th>Measured (varops)</th>'
               '<th>Charge (varops)</th><th>Measured ÷ charge</th></tr></thead><tbody>']
        # A machine's slowest run.
        for key in keys:
            own = [p for p in pts if p['machine_key'] == key and p['x'] == 32]
            if own:
                worst = max(own, key=lambda p: p['y'])
                y = worst['y']
                out.append(f'<tr><td>{esc(run_label(key, worst))}</td><td>{y:,.0f}</td><td>{charge:,}</td><td>{y / charge:.2f}×</td></tr>')
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
        note = NOTES.get(family)
        if note:
            out.append(f'<p>{note}</p>')
        if family in FIXTURES:
            out.append(f'<p class="muted"><strong>Measured with:</strong> {FIXTURES[family]}</p>')
        if family == 'SIG':
            out.extend(signature_table(pts))
            if series.get('TWEAK'):
                out.append(f'<p><strong>Key tweak.</strong> {MODELS["TWEAK"]} Measured with {FIXTURES["TWEAK"]}</p>')
                out.extend(charts('TWEAK', series['TWEAK'], candidates))
            out.append('</article>')
            return out
        out.extend(charts(family, pts, candidates))
        out.extend(fit_table(family, models, env_model))
        out.append('</article>')
        return out

    def charts(family, pts, prices, prefix='', price_label='Price',
               price_note='The dotted line is the price: the envelope rounded up.'):
        """A family's measurements with its envelope and a dotted price line from prices."""
        out = []
        if family not in {'PRODUCE', 'WRITE'}:
            groups = sorted({p["group"] for p in pts})
            if family in {"H256", "DIVCORE", "MULCORE", "NORMALIZE"} and len(groups) > 1:
                for group in groups:
                    out.append(f'<div class="facet-title">{esc(group)}</div><div class="chart">'
                               f'{chart(family, pts, family_models(family), group, prefix, prices, price_label, price_note)}</div>')
                return out
        out.append(f'<div class="chart">{chart(family, pts, family_models(family), None, prefix, prices, price_label, price_note)}</div>')
        return out

    def run_label(key, point):
        """A machine's name, with the run a point comes from when there are several."""
        return MACHINE_NAMES[key] + (f' (run {point["machine_id"].rsplit("-", 1)[1]})' if runs > 1 else '')

    def fit_table(family, curves, envelope, envelope_label='Envelope of all machines'):
        """Each machine's fitted curve of a family, one column per run, and their envelope."""
        heads = ''.join(f'<th>Run {run}</th>' for run in range(1, runs + 1)) if runs > 1 else '<th>Fitted cost (varops)</th>'
        out = [f'<div class="table-wrap"><table><thead><tr><th>Machine</th>{heads}</tr></thead><tbody>']
        for key in keys:
            cells = ''.join(f'<td><code>{esc(fitted(family, curves[machine["id"]][family]))}</code></td>'
                            for machine in machines if machine['key'] == key)
            out.append(f'<tr><td>{esc(MACHINE_NAMES[key])}</td>{cells}</tr>')
        out.append(f'<tr><td>{esc(envelope_label)}</td><td colspan="{runs}"><code>{esc(fitted(family, envelope[family]))}</code></td></tr></tbody></table></div>')
        return out

    def composed_article(family, tag):
        """A primitive this dataset measured in parts: its composed fits against its price, then each part."""
        how, members = composed[family]
        record = joint['primitives'][family]
        price = record['candidate_coefficients']
        candidate = formulas(family, price, candidate=True)
        implemented = CURRENT_COSTS[family]
        shares = [joint['measured_parts'][m]['candidate_coefficients'] for m in members]
        share_text = and_list([f'<code>{esc(group_digits(formulas(m, c, candidate=True)))}</code>'
                               for m, c in zip(members, shares)])
        out = [f'<article id="{family}"><{tag}>{esc(DISPLAY.get(family, family))}</{tag}>',
               f'<p><strong>Price:</strong> <code>{esc(group_digits(candidate))}</code> varops.'
               + ('' if implemented == candidate else
                  f' The implementation still charges <code>{esc(group_digits(implemented))}</code>.') + '</p>',
               f'<p class="model"><strong>Pays for:</strong> {MODELS[family]}</p>']
        out.append(f'<p>{COMPOSED_TEXT[family].format(shares=share_text)}</p>')
        # Coefficients and sizes are nonnegative, so covering every coefficient covers every size.
        envelope = priced_env[family]
        covered = all(e <= c + 1e-9 for e, c in zip(envelope, price))
        if how == 'sum':
            out.append('<details open><summary>Composed fits and price</summary>')
            out.extend(fit_table(family, priced, priced_env))
        else:
            # A larger-of price covers each part: each part's envelope and rounded price, then the
            # envelope of every machine's curve of every part.
            everything = f'Envelope of all machines and {"hash functions" if family == "HASH" else "parts"}'
            out.append('<details open><summary>Parts and price</summary><div class="table-wrap"><table><thead><tr>'
                       '<th>Part</th><th>Envelope of all machines</th><th>Rounded</th></tr></thead><tbody>')
            for m, share in zip(members, shares):
                out.append(f'<tr><td><a href="#{family}-{m}">{esc(PARTS[m][0])}</a></td>'
                           f'<td><code>{esc(fitted(m, env_model[m]))}</code></td>'
                           f'<td><code>{esc(group_digits(formulas(m, share, candidate=True)))}</code></td></tr>')
            out.append(f'<tr><td>{everything}</td><td><code>{esc(fitted(family, envelope))}</code></td>'
                       f'<td><code>{esc(group_digits(candidate))}</code>, the price</td></tr></tbody></table></div>')
        out.append(f'<p>The envelope {"lies at or below the price at every size" if covered else "exceeds the price"}: '
                   f'<code>{esc(fitted(family, envelope))}</code> against <code>{esc(group_digits(candidate))}</code>.</p>')
        out.append('</details>')
        for m, share in zip(members, shares):
            title, description = PARTS[m]
            pts = series[m]
            out.append(f'<details open id="{family}-{m}"><summary>{esc(title)}</summary><p class="model">{description}</p>')
            note = NOTES.get(m) or (sha_note() if m == 'H256' else None)
            if note:
                out.append(f'<p>{note}</p>')
            if how == 'sum':
                out.extend(charts(m, pts, {m: share}, f'{family}-', 'Part of the price',
                                  'The dotted line is this part&#39;s share of the price.'))
            else:
                out.extend(charts(m, pts, {m: price}, f'{family}-',
                                  price_note='The dotted line is the price, which covers every part.'))
            out.extend(fit_table(m, models, env_model))
            out.append('</details>')
        out.append('</article>')
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
        out.append(charge_legend(spec['charge']))
        out.append(f'<div class="chart">{check_chart(key, points)}</div>')
        column = 'Message' if key == 'CSFS' else 'Value' if key == 'BYTEREV' else 'Measurement'
        out.append(f'<div class="table-wrap"><table><thead><tr><th>Machine</th><th>Highest measured ÷ charge</th><th>{column}</th></tr></thead><tbody>')
        for machine_key in keys:
            own = [p for p in points if p['machine_key'] == machine_key]
            if not own:
                continue
            worst = max(own, key=lambda p: p['ratio'])
            size = re.search(r'/(\d+)$', worst['label']) if key in {'CSFS', 'BYTEREV'} else None
            shown = f'{int(size.group(1)):,} bytes' if size else f'<code>{esc(worst["label"])}</code>'
            out.append(f'<tr><td>{esc(run_label(machine_key, worst))}</td><td>{worst["ratio"]:.2f}×</td><td>{shown}</td></tr>')
        out.append('</tbody></table></div></article>')
        return out

    for section in SECTIONS:
        slug = section["slug"]
        parts.append(f'<section class="category" id="{slug}"><h2>{esc(section["title"])}</h2>{f'<p class="section-intro">{esc(section["intro"])}</p>' if section.get("intro") else ''}')
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
                parts.extend(check_article(family, tag) if family in CHECKS else
                             composed_article(family, tag) if family in composed else priced_article(family, tag))
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
