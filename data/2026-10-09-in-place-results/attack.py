#!/usr/bin/env python3
"""Time adversarial in-place scripts with bitcoin-util evalscript; print varops, seconds, projected ratio."""
import json, subprocess, sys, time
UTIL, REF = sys.argv[1], float(sys.argv[2])
OP = dict(OP_SIZE=0x82, OP_1SUB=0x8c, OP_1ADD=0x8b, OP_RIGHT=0x81, OP_LEFT=0x80, OP_INVERT=0x83, OP_LSHIFT=0x98,
          OP_RSHIFT=0x99, OP_CAT=0x7e, OP_DUP=0x76, OP_DROP=0x75, OP_SWAP=0x7c, OP_1=0x51, OP_2MUL=0x8d, OP_2DIV=0x8e,
          OP_BYTEREV=0xcf, OP_ADD=0x93, OP_SUB=0x94, OP_NIP=0x77, OP_OVER=0x78, OP_2DUP=0x6e, OP_XOR=0x86, OP_MIN=0xa3, OP_MAX=0xa4, OP_AND=0x84, OP_OR=0x85, OP_DIV=0x96, OP_MOD=0x97, OP_SUBSTR=0x7f, OP_8=0x58)
def body(*tokens):
    out = bytearray()
    for t in tokens:
        out += bytes([OP[t]]) if isinstance(t, str) else t
    return bytes(out)
M = 4_000_000
def num(n):
    b = n.to_bytes((n.bit_length() + 7) // 8, 'little') if n else b''
    return bytes([len(b)]) + b
CASES = {
    # RIGHT dropping one front byte: memmove of the rest, charged BASE + READ only.
    'right-shrink-1': ([b'\x55' * M], body('OP_SIZE', 'OP_1SUB', 'OP_RIGHT'), 3000),
    'left-shrink-1': ([b'\x55' * M], body('OP_SIZE', 'OP_1SUB', 'OP_LEFT'), 3000),
    'invert': ([b'\x55' * M], body('OP_INVERT'), 3000),
    'bytrev': ([b'\x55' * M], body('OP_BYTEREV'), 3000),
    '2mul-2div': ([b'\x55' * (M - 8)], body('OP_2MUL', 'OP_2DIV'), 1500),
    'xor-copy': ([b'\x55' * (M // 2)], body('OP_DUP', 'OP_2MUL', 'OP_XOR'), 1500),
    # UPSHIFT by 64 bits: grows a word, exact-capacity realloc and copy each time.
    'upshift-64': ([b'\x55' * (M - 100_000)], body(b'\x01\x40', 'OP_LSHIFT'), 3000),
    'upshift-64-downshift': ([b'\x55' * (M - 100)], body(b'\x01\x40', 'OP_LSHIFT', b'\x01\x40', 'OP_RSHIFT'), 1500),
    # CAT of a small value with a fresh 2 MB copy: the copy pays WRITE, CAT copies it again.
    'cat-small-big': ([b'\x55' * (M // 2)], body('OP_DUP', 'OP_1', 'OP_SWAP', 'OP_CAT', 'OP_DROP'), 1500),
    'cat-big-1': ([b'\x55' * (M - 100_000)], body('OP_1', 'OP_CAT'), 3000),
    # 1ADD carry growth on all-ones, then 1SUB back.
    'carry-1add-1sub': ([b'\xff' * (M - 8)], body('OP_1ADD', 'OP_1SUB'), 1500),
    # 2MUL / 1ADD / ADD carry out of a word-aligned all-ones copy: AppendCarry reallocates past the exact capacity.
    'dup-2mul-carry-8': ([b'\xff' * 8], body('OP_DUP', 'OP_2MUL', 'OP_DROP'), 600000),
    'dup-1add-carry-8': ([b'\xff' * 8], body('OP_DUP', 'OP_1ADD', 'OP_DROP'), 600000),
    'dup-add-carry-8': ([b'\xff' * 8, b'\x01'], body('OP_2DUP', 'OP_ADD', 'OP_DROP'), 600000),
    'dup-add-carry-sq-8': ([b'\xff' * 8], body('OP_DUP', 'OP_DUP', 'OP_ADD', 'OP_DROP'), 600000),
    'dup-2mul-carry-16': ([b'\xff' * 16], body('OP_DUP', 'OP_2MUL', 'OP_DROP'), 500000),
    'dup-1add-carry-16': ([b'\xff' * 16], body('OP_DUP', 'OP_1ADD', 'OP_DROP'), 500000),
    'dup-add-carry-16': ([b'\xff' * 16, b'\x01'], body('OP_2DUP', 'OP_ADD', 'OP_DROP'), 500000),
    'dup-add-carry-sq-16': ([b'\xff' * 16], body('OP_DUP', 'OP_DUP', 'OP_ADD', 'OP_DROP'), 500000),
    'dup-2mul-carry-64': ([b'\xff' * 64], body('OP_DUP', 'OP_2MUL', 'OP_DROP'), 300000),
    'dup-1add-carry-64': ([b'\xff' * 64], body('OP_DUP', 'OP_1ADD', 'OP_DROP'), 300000),
    'dup-add-carry-64': ([b'\xff' * 64, b'\x01'], body('OP_2DUP', 'OP_ADD', 'OP_DROP'), 300000),
    'dup-add-carry-sq-64': ([b'\xff' * 64], body('OP_DUP', 'OP_DUP', 'OP_ADD', 'OP_DROP'), 300000),
    'dup-2mul-carry-4096': ([b'\xff' * 4096], body('OP_DUP', 'OP_2MUL', 'OP_DROP'), 20000),
    'dup-1add-carry-4096': ([b'\xff' * 4096], body('OP_DUP', 'OP_1ADD', 'OP_DROP'), 20000),
    'dup-add-carry-4096': ([b'\xff' * 4096, b'\x01'], body('OP_2DUP', 'OP_ADD', 'OP_DROP'), 20000),
    'dup-add-carry-sq-4096': ([b'\xff' * 4096], body('OP_DUP', 'OP_DUP', 'OP_ADD', 'OP_DROP'), 20000),
    'dup-2mul-carry-65536': ([b'\xff' * 65536], body('OP_DUP', 'OP_2MUL', 'OP_DROP'), 3000),
    'dup-1add-carry-65536': ([b'\xff' * 65536], body('OP_DUP', 'OP_1ADD', 'OP_DROP'), 3000),
    'dup-add-carry-65536': ([b'\xff' * 65536, b'\x01'], body('OP_2DUP', 'OP_ADD', 'OP_DROP'), 3000),
    'dup-add-carry-sq-65536': ([b'\xff' * 65536], body('OP_DUP', 'OP_DUP', 'OP_ADD', 'OP_DROP'), 3000),
    'dup-2mul-carry-1999992': ([b'\xff' * 1999992], body('OP_DUP', 'OP_2MUL', 'OP_DROP'), 100),
    'dup-1add-carry-1999992': ([b'\xff' * 1999992], body('OP_DUP', 'OP_1ADD', 'OP_DROP'), 100),
    'dup-add-carry-1999992': ([b'\xff' * 1999992, b'\x01'], body('OP_2DUP', 'OP_ADD', 'OP_DROP'), 100),
    'dup-add-carry-sq-1999992': ([b'\xff' * 1999992], body('OP_DUP', 'OP_DUP', 'OP_ADD', 'OP_DROP'), 100),
    'ctl-2mul-nocarry-8': ([b'\x55' * 8], body('OP_DUP', 'OP_2MUL', 'OP_DROP'), 600000),
    'ctl-1add-nocarry-8': ([b'\x55' * 8], body('OP_DUP', 'OP_1ADD', 'OP_DROP'), 600000),
    'ctl-invert-nocarry-8': ([b'\x55' * 8], body('OP_DUP', 'OP_INVERT', 'OP_DROP'), 600000),
    'ctl-2div-nocarry-8': ([b'\x55' * 8], body('OP_DUP', 'OP_2DIV', 'OP_DROP'), 600000),
    'ctl-2mul-nocarry-16': ([b'\x55' * 16], body('OP_DUP', 'OP_2MUL', 'OP_DROP'), 500000),
    'ctl-1add-nocarry-16': ([b'\x55' * 16], body('OP_DUP', 'OP_1ADD', 'OP_DROP'), 500000),
    'ctl-invert-nocarry-16': ([b'\x55' * 16], body('OP_DUP', 'OP_INVERT', 'OP_DROP'), 500000),
    'ctl-2div-nocarry-16': ([b'\x55' * 16], body('OP_DUP', 'OP_2DIV', 'OP_DROP'), 500000),
    'bin-and-1': ([b'\x55' * 1, b'\x33' * 1], body('OP_2DUP', 'OP_AND', 'OP_DROP'), 600000),
    'bin-or-1': ([b'\x55' * 1, b'\x33' * 1], body('OP_2DUP', 'OP_OR', 'OP_DROP'), 600000),
    'bin-xor-1': ([b'\x55' * 1, b'\x33' * 1], body('OP_2DUP', 'OP_XOR', 'OP_DROP'), 600000),
    'bin-sub-1': ([b'\x55' * 1, b'\x33' * 1], body('OP_2DUP', 'OP_SUB', 'OP_DROP'), 600000),
    'bin-add-1': ([b'\x55' * 1, b'\x33' * 1], body('OP_2DUP', 'OP_ADD', 'OP_DROP'), 600000),
    'bin-min-1': ([b'\x55' * 1, b'\x33' * 1], body('OP_2DUP', 'OP_MIN', 'OP_DROP'), 600000),
    'bin-max-1': ([b'\x55' * 1, b'\x33' * 1], body('OP_2DUP', 'OP_MAX', 'OP_DROP'), 600000),
    'bin-div-1': ([b'\x55' * 1, b'\x03'], body('OP_2DUP', 'OP_DIV', 'OP_DROP'), 600000),
    'bin-mod-1': ([b'\x55' * 1, b'\x03'], body('OP_2DUP', 'OP_MOD', 'OP_DROP'), 600000),
    'un-1sub-1': ([b'\x55' * 1], body('OP_DUP', 'OP_1SUB', 'OP_DROP'), 600000),
    'un-byterev-1': ([b'\x55' * 1], body('OP_DUP', 'OP_BYTEREV', 'OP_DROP'), 600000),
    'un-invert-1': ([b'\x55' * 1], body('OP_DUP', 'OP_INVERT', 'OP_DROP'), 600000),
    'un-2div-1': ([b'\x55' * 1], body('OP_DUP', 'OP_2DIV', 'OP_DROP'), 600000),
    'un-rshift-1': ([b'\x55' * 1], body('OP_DUP', 'OP_1', 'OP_RSHIFT', 'OP_DROP'), 600000),
    'un-left-1': ([b'\x55' * 1], body('OP_DUP', 'OP_1', 'OP_LEFT', 'OP_DROP'), 600000),
    'bin-and-8': ([b'\x55' * 8, b'\x33' * 8], body('OP_2DUP', 'OP_AND', 'OP_DROP'), 600000),
    'bin-or-8': ([b'\x55' * 8, b'\x33' * 8], body('OP_2DUP', 'OP_OR', 'OP_DROP'), 600000),
    'bin-xor-8': ([b'\x55' * 8, b'\x33' * 8], body('OP_2DUP', 'OP_XOR', 'OP_DROP'), 600000),
    'bin-sub-8': ([b'\x55' * 8, b'\x33' * 8], body('OP_2DUP', 'OP_SUB', 'OP_DROP'), 600000),
    'bin-add-8': ([b'\x55' * 8, b'\x33' * 8], body('OP_2DUP', 'OP_ADD', 'OP_DROP'), 600000),
    'bin-min-8': ([b'\x55' * 8, b'\x33' * 8], body('OP_2DUP', 'OP_MIN', 'OP_DROP'), 600000),
    'bin-max-8': ([b'\x55' * 8, b'\x33' * 8], body('OP_2DUP', 'OP_MAX', 'OP_DROP'), 600000),
    'bin-div-8': ([b'\x55' * 8, b'\x03'], body('OP_2DUP', 'OP_DIV', 'OP_DROP'), 600000),
    'bin-mod-8': ([b'\x55' * 8, b'\x03'], body('OP_2DUP', 'OP_MOD', 'OP_DROP'), 600000),
    'un-1sub-8': ([b'\x55' * 8], body('OP_DUP', 'OP_1SUB', 'OP_DROP'), 600000),
    'un-byterev-8': ([b'\x55' * 8], body('OP_DUP', 'OP_BYTEREV', 'OP_DROP'), 600000),
    'un-invert-8': ([b'\x55' * 8], body('OP_DUP', 'OP_INVERT', 'OP_DROP'), 600000),
    'un-2div-8': ([b'\x55' * 8], body('OP_DUP', 'OP_2DIV', 'OP_DROP'), 600000),
    'un-rshift-8': ([b'\x55' * 8], body('OP_DUP', 'OP_1', 'OP_RSHIFT', 'OP_DROP'), 600000),
    'un-left-8': ([b'\x55' * 8], body('OP_DUP', 'OP_1', 'OP_LEFT', 'OP_DROP'), 600000),
    'bin-and-64': ([b'\x55' * 64, b'\x33' * 64], body('OP_2DUP', 'OP_AND', 'OP_DROP'), 300000),
    'bin-or-64': ([b'\x55' * 64, b'\x33' * 64], body('OP_2DUP', 'OP_OR', 'OP_DROP'), 300000),
    'bin-xor-64': ([b'\x55' * 64, b'\x33' * 64], body('OP_2DUP', 'OP_XOR', 'OP_DROP'), 300000),
    'bin-sub-64': ([b'\x55' * 64, b'\x33' * 64], body('OP_2DUP', 'OP_SUB', 'OP_DROP'), 300000),
    'bin-add-64': ([b'\x55' * 64, b'\x33' * 64], body('OP_2DUP', 'OP_ADD', 'OP_DROP'), 300000),
    'bin-min-64': ([b'\x55' * 64, b'\x33' * 64], body('OP_2DUP', 'OP_MIN', 'OP_DROP'), 300000),
    'bin-max-64': ([b'\x55' * 64, b'\x33' * 64], body('OP_2DUP', 'OP_MAX', 'OP_DROP'), 300000),
    'bin-div-64': ([b'\x55' * 64, b'\x03'], body('OP_2DUP', 'OP_DIV', 'OP_DROP'), 300000),
    'bin-mod-64': ([b'\x55' * 64, b'\x03'], body('OP_2DUP', 'OP_MOD', 'OP_DROP'), 300000),
    'un-1sub-64': ([b'\x55' * 64], body('OP_DUP', 'OP_1SUB', 'OP_DROP'), 300000),
    'un-byterev-64': ([b'\x55' * 64], body('OP_DUP', 'OP_BYTEREV', 'OP_DROP'), 300000),
    'un-invert-64': ([b'\x55' * 64], body('OP_DUP', 'OP_INVERT', 'OP_DROP'), 300000),
    'un-2div-64': ([b'\x55' * 64], body('OP_DUP', 'OP_2DIV', 'OP_DROP'), 300000),
    'un-rshift-64': ([b'\x55' * 64], body('OP_DUP', 'OP_1', 'OP_RSHIFT', 'OP_DROP'), 300000),
    'un-left-64': ([b'\x55' * 64], body('OP_DUP', 'OP_1', 'OP_LEFT', 'OP_DROP'), 300000),
    'big-div-4096x1024': ([b'\xff' * 4096, b'\x01' + b'\x00' * 1022 + b'\x80'], body('OP_2DUP', 'OP_DIV', 'OP_DROP'), 3000),
    'big-mod-4096x1024': ([b'\xff' * 4096, b'\x01' + b'\x00' * 1022 + b'\x80'], body('OP_2DUP', 'OP_MOD', 'OP_DROP'), 3000),
    'big-div-65536x16384': ([b'\xff' * 65536, b'\x01' + b'\x00' * 16382 + b'\x80'], body('OP_2DUP', 'OP_DIV', 'OP_DROP'), 200),
    'big-mod-65536x16384': ([b'\xff' * 65536, b'\x01' + b'\x00' * 16382 + b'\x80'], body('OP_2DUP', 'OP_MOD', 'OP_DROP'), 200),
    'big-div-65536x32768': ([b'\xff' * 65536, b'\x01' + b'\x00' * 32766 + b'\x80'], body('OP_2DUP', 'OP_DIV', 'OP_DROP'), 200),
    'big-mod-65536x32768': ([b'\xff' * 65536, b'\x01' + b'\x00' * 32766 + b'\x80'], body('OP_2DUP', 'OP_MOD', 'OP_DROP'), 200),
    'big-div-65536x64000': ([b'\xff' * 65536, b'\x01' + b'\x00' * 63998 + b'\x80'], body('OP_2DUP', 'OP_DIV', 'OP_DROP'), 300),
    'big-mod-65536x64000': ([b'\xff' * 65536, b'\x01' + b'\x00' * 63998 + b'\x80'], body('OP_2DUP', 'OP_MOD', 'OP_DROP'), 300),
    'big-div-1024x8': ([b'\xff' * 1024, b'\x01' + b'\x00' * 6 + b'\x80'], body('OP_2DUP', 'OP_DIV', 'OP_DROP'), 20000),
    'big-mod-1024x8': ([b'\xff' * 1024, b'\x01' + b'\x00' * 6 + b'\x80'], body('OP_2DUP', 'OP_MOD', 'OP_DROP'), 20000),
    'big-div-200x100': ([b'\xff' * 200, b'\x01' + b'\x00' * 98 + b'\x80'], body('OP_2DUP', 'OP_DIV', 'OP_DROP'), 50000),
    'big-mod-200x100': ([b'\xff' * 200, b'\x01' + b'\x00' * 98 + b'\x80'], body('OP_2DUP', 'OP_MOD', 'OP_DROP'), 50000),
    'trim-sub-8': ([b'\x55' * 8], body('OP_DUP', 'OP_DUP', 'OP_SUB', 'OP_DROP'), 600000),
    'trim-xor-8': ([b'\x55' * 8], body('OP_DUP', 'OP_DUP', 'OP_XOR', 'OP_DROP'), 600000),
    'trim-mod-8': ([b'\x55' * 8], body('OP_DUP', 'OP_DUP', 'OP_MOD', 'OP_DROP'), 600000),
    'trim-div-8': ([b'\x55' * 8], body('OP_DUP', 'OP_DUP', 'OP_DIV', 'OP_DROP'), 600000),
    'trim-rshift-8': ([b'\x55' * 8], body('OP_DUP', b'\x03\x00\x09\x3d', 'OP_RSHIFT', 'OP_DROP'), 600000),
    'trim-sub-4096': ([b'\x55' * 4096], body('OP_DUP', 'OP_DUP', 'OP_SUB', 'OP_DROP'), 20000),
    'trim-xor-4096': ([b'\x55' * 4096], body('OP_DUP', 'OP_DUP', 'OP_XOR', 'OP_DROP'), 20000),
    'trim-mod-4096': ([b'\x55' * 4096], body('OP_DUP', 'OP_DUP', 'OP_MOD', 'OP_DROP'), 20000),
    'trim-div-4096': ([b'\x55' * 4096], body('OP_DUP', 'OP_DUP', 'OP_DIV', 'OP_DROP'), 20000),
    'trim-rshift-4096': ([b'\x55' * 4096], body('OP_DUP', b'\x03\x00\x09\x3d', 'OP_RSHIFT', 'OP_DROP'), 20000),
    'trim-sub-65536': ([b'\x55' * 65536], body('OP_DUP', 'OP_DUP', 'OP_SUB', 'OP_DROP'), 2000),
    'trim-xor-65536': ([b'\x55' * 65536], body('OP_DUP', 'OP_DUP', 'OP_XOR', 'OP_DROP'), 2000),
    'trim-mod-65536': ([b'\x55' * 65536], body('OP_DUP', 'OP_DUP', 'OP_MOD', 'OP_DROP'), 2000),
    'trim-div-65536': ([b'\x55' * 65536], body('OP_DUP', 'OP_DUP', 'OP_DIV', 'OP_DROP'), 2000),
    'trim-rshift-65536': ([b'\x55' * 65536], body('OP_DUP', b'\x03\x00\x09\x3d', 'OP_RSHIFT', 'OP_DROP'), 2000),
    'trim-sub-1000000': ([b'\x55' * 1000000], body('OP_DUP', 'OP_DUP', 'OP_SUB', 'OP_DROP'), 200),
    'trim-xor-1000000': ([b'\x55' * 1000000], body('OP_DUP', 'OP_DUP', 'OP_XOR', 'OP_DROP'), 200),
    'trim-mod-1000000': ([b'\x55' * 1000000], body('OP_DUP', 'OP_DUP', 'OP_MOD', 'OP_DROP'), 200),
    'trim-div-1000000': ([b'\x55' * 1000000], body('OP_DUP', 'OP_DUP', 'OP_DIV', 'OP_DROP'), 200),
    'trim-rshift-1000000': ([b'\x55' * 1000000], body('OP_DUP', b'\x03\x00\x09\x3d', 'OP_RSHIFT', 'OP_DROP'), 200),
    # LEFT halving a fresh 4 MB copy: the stack copies each result out of the larger buffer.
    'left-halve-chain': ([b'\x55' * (M // 2)], body('OP_DUP', b'\x03\x00\x00\x10', 'OP_LEFT', b'\x03\x00\x00\x08', 'OP_LEFT', b'\x03\x00\x00\x04', 'OP_LEFT', 'OP_DROP'), 300),
    'right-halve-chain': ([b'\x55' * (M // 2)], body('OP_DUP', b'\x03\x00\x00\x10', 'OP_RIGHT', b'\x03\x00\x00\x08', 'OP_RIGHT', b'\x03\x00\x00\x04', 'OP_RIGHT', 'OP_DROP'), 300),
    'left-to-1': ([b'\x55' * (M // 2)], body('OP_DUP', 'OP_1', 'OP_LEFT', 'OP_DROP'), 300),
    'sub-to-small': ([b'\x55' * (M // 2)], body('OP_DUP', 'OP_DUP', 'OP_1SUB', 'OP_SUB', 'OP_DROP'), 300),
    # OP_SUBSTR in place: begin bytes off the front, truncated to len.
    'substr-front-1': ([b'\x55' * M], body('OP_1', num(M), 'OP_SUBSTR'), 3000),
    'substr-front-small-dup': ([b'\x55' * (M // 2)], body('OP_DUP', 'OP_1', 'OP_1', 'OP_SUBSTR', 'OP_DROP'), 300),
    'substr-end-small-dup': ([b'\x55' * (M // 2)], body('OP_DUP', num(M // 2 - 8), 'OP_8', 'OP_SUBSTR', 'OP_DROP'), 300),
    'substr-mid-dup': ([b'\x55' * (M // 2)], body('OP_DUP', num(500_000), num(1_000_000), 'OP_SUBSTR', 'OP_DROP'), 300),
    'substr-halve-chain': ([b'\x55' * (M // 2)], body('OP_DUP', 'OP_1', num(1 << 20), 'OP_SUBSTR', 'OP_1', num(1 << 19), 'OP_SUBSTR',
                                                   'OP_1', num(1 << 18), 'OP_SUBSTR', 'OP_DROP'), 300),
    'substr-small-1': ([b'\x55' * 8], body('OP_DUP', 'OP_1', 'OP_1', 'OP_SUBSTR', 'OP_DROP'), 300000),
    'min-dup': ([b'\x55' * (M // 2)], body('OP_DUP', 'OP_MIN'), 1500),
    # 2MUL / 1ADD carry out of a word-aligned all-ones copy: AppendCarry reallocates past the exact capacity.
}
names = sys.argv[3:] or list(CASES)
for name in names:
    stack, seq, reps = CASES[name]
    script = seq * reps
    req = json.dumps(dict(protocol=1, sigversion='tapleaf_0xc2', script=script.hex(), stack=[v.hex() for v in stack],
                          varops_budget=2**63)).encode()
    best = None
    for _ in range(3):
        t = time.perf_counter()
        out = subprocess.run([UTIL, 'evalscript'], input=req, capture_output=True, check=True).stdout
        dt = time.perf_counter() - t
        best = dt if best is None else min(best, dt)
    res = json.loads(out)
    used = 2**63 - res['varops-budget-remaining']
    # Subtract the same script run with a zero budget: process start, JSON and hex parsing, script decoding.
    base_req = json.dumps(dict(protocol=1, sigversion='tapleaf_0xc2', script=script.hex(), stack=[v.hex() for v in stack], varops_budget=0)).encode()
    base = None
    for _ in range(3):
        t = time.perf_counter()
        subprocess.run([UTIL, 'evalscript'], input=base_req, capture_output=True)
        dt = time.perf_counter() - t
        base = dt if base is None else min(base, dt)
    net = best - base
    ratio = net * (40e9 / used) / REF if used else float('inf')
    print(f'{name:22} reps {reps:5} varops {used:>14,} net {net*1e3:8.1f} ms  ratio {ratio:7.2f}  ok={res["success"]} err={res["error"]}', flush=True)
