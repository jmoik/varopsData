# Merged primitives · 2026-10-05

Model `read-write-arith-v1`: the eleven primitives of BIP 440 (READ, WRITE and ARITH merged from the earlier
producer and normalize parts). Commit `26ed8d63da` (branch `gsr`), varopsData `ac31136`, runner defaults through
`bitcoinSetup.sh`: 5 reference epochs, 7 primitive epochs, 10 ms samples and 100 ms for storage lifetimes.

| Machine | Compiler | SHA256 | Reference (s) | Epoch noise | Notes |
|---|---|---|---|---|---|
| Intel i7-7700 | GCC 15.2.0 | sse4 | 3.485 | 0.25% | Hetzner, `bitcoinSetup.sh` |

Later gsr commits change no measured primitive: `0b644f0761` changes only which opcodes pay MOVE and how many
entries they count, and OP_TX scope operands only in what they accept.
