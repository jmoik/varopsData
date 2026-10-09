#!/usr/bin/env python3
"""Evaluate every example of the report's opcode table with the BIP 441 reference evaluator.

Usage: check_opcode_table.py <bips checkout>

OP_CHECKSIG and OP_CHECKSIGADD run without a transaction, so their signature check fails
before the result is pushed; the result's WRITE is added back. OP_TX is compared with the
BIP 441 vector of the same selection. Exits nonzero on any mismatch.
"""

import importlib.util
import json
import sys
from pathlib import Path

from render_joint_calibration import BASE, WRITE, opcode_table


def main():
    bips = Path(sys.argv[1])
    spec = importlib.util.spec_from_file_location('evaluator', bips / 'bip-0441' / 'evaluator.py')
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    failures = 0
    for _, op, _, _, value, check in opcode_table():
        if op == 'OP_TX':
            vectors = json.loads((bips / 'bip-0441' / 'op_tx.json').read_text())
            got = next(v['varops'] for c in vectors['scripts'] for v in c['vectors']
                       if v['comment'].startswith('No executed OP_CODESEPARATOR'))
        elif check is None:
            continue
        else:
            stack, tokens = check
            interp = ev.Interpreter(ev.parse_script(tokens), [bytes.fromhex(x) for x in stack], 2**64 - 1)
            error = None
            try:
                interp.run(False)
            except ev.Fail as fail:
                error = fail.name
            got = interp.cost
            if op.startswith('Unrolling'):
                got -= 1000 * BASE  # the 1,000 unrolled OP_NOPs it then executes
            if error == 'SCHNORR_SIG' and op in ('OP_CHECKSIG', 'OP_CHECKSIGADD'):
                got += WRITE[0] + WRITE[1] * 8
        status = 'ok' if got == value else 'MISMATCH'
        failures += got != value
        print(f'{status:8} {op}: table {value}, evaluator {got}')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
