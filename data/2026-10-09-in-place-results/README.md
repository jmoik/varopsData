# In-place results pay no WRITE · 2026-10-09

Checks that the opcodes whose result is computed in their operand's storage, never longer than it, stay within the limit once they no longer pay WRITE (gsr f75feac44a). Prices are unchanged. One machine, the M1 Pro; the reference block takes 2.255 s there.

The 16 opcodes: OP_INVERT, OP_2DIV, OP_1SUB, OP_SUB, OP_AND, OP_OR, OP_XOR, OP_RSHIFT, OP_DIV, OP_MOD, OP_MIN, OP_MAX, OP_SUBSTR, OP_LEFT, OP_RIGHT and OP_BYTEREV. OP_SUBSTR and OP_RIGHT pay ARITH of their result for moving the kept bytes to the front.

## bench_varops

    bench_varops --opcodes <the 16> --epochs 3 --sample-budget-percent 2 --file bench-varops-m1pro.csv

200 cases, worst projected full-budget time per opcode over the reference:

| Opcode | Ratio | Case |
|---|---|---|
| OP_BYTEREV | 0.66 | 15 bytes, reversed in place |
| OP_DIV, OP_MOD | 0.60 | 64 KB by 16 KB with add-back |
| OP_MIN, OP_MAX, OP_1SUB, OP_2DIV | 0.56 | 4 to 17 bytes |
| OP_INVERT | 0.55 | 17 bytes |
| OP_RSHIFT | 0.52 | 1 byte |
| OP_SUB | 0.50 | 4 bytes |
| OP_AND, OP_OR, OP_XOR | 0.48 | 1 byte |
| OP_LEFT | 0.30 | 3,950,000 bytes |
| OP_RIGHT | 0.23 | 3,950,000 bytes |
| OP_SUBSTR | 0.19 | 3,998,900 bytes |

## Attack scripts

`attack.py <bitcoin-util> 2.255 [cases]` times whole scripts with `bitcoin-util evalscript`. The baseline is the same script with a zero budget, which subtracts process start, JSON and hex parsing and script decoding. `attack-m1pro.log` holds two runs: the first on the prototype, the same code without the in-place OP_SUBSTR; the second on f75feac44a.

| Script | Ratio |
|---|---|
| OP_RIGHT or OP_SUBSTR dropping one front byte of 4 MB, 3,000 times | 0.10 |
| OP_LEFT, OP_RIGHT and OP_SUBSTR halving chains on 2 MB copies (the stack copies short results into smaller storage) | 0.06 to 0.11 |
| OP_SUBSTR of a few bytes near the front, middle or end of a 2 MB copy | 0.07 to 0.08 |
| 1 to 8-byte values: OP_BYTEREV, OP_INVERT, OP_2DIV, OP_1SUB, OP_LEFT, OP_RSHIFT, OP_MIN, OP_SUB, OP_AND, OP_DIV, OP_SUBSTR | 0.34 to 0.42 |
| Results trimmed to zero from 1 MB (OP_SUB, OP_MOD) | 0.07 to 0.21 |

Without any per-byte charge, OP_RIGHT dropping one byte at a time measured 503 times the reference: the move needs ARITH.
