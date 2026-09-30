# Can storage costs be paid by producers?

Plan: measure complete lifetimes, fit a producer allowance, then check complete
opcode compositions. Do not remove RELEASE merely by renaming COPY.
No consensus prices change in this experiment.

## Measurement contract

`bench_varops_primitives --storage-only` times construction, use and destruction
inside each repetition, with one clock around the batch. Immutable source bytes
and serialized scripts are prepared outside. Mutable inputs, growth, temporaries,
stack containers and cleanup are inside. No subtraction of separately timed
destruction or allocator-history reset occurs. Raw epochs and a fixture manifest
are saved alongside the normal output. Old primitive measurements remain intact.

| Allocation path | New fixtures | Candidate owner of lifetime cost |
|---|---|---|
| Copied entry, literal, SUBSTR result | Dense vector/stack copy lifetimes; literal and SUBSTR evaluation | COPY on creation, including eventual release |
| Outer stack growth and altstack transfer | Unreserved stack creation, retained batches, DUP/altstack script | Per-created-entry allowance; moves must not pay for payload twice |
| Initial witness stack | Complete script fixtures construct `Frame` inside timing | Explicit initial-value allowance, or independently justified setup bound |
| Numeric padding/alignment | Tight/spare full conversion lifetimes; optional `--offset-spans` | Producer reserve or preparation growth allowance, not actual-capacity charging |
| Carry, concatenation, shift growth | All-ones 1ADD, CAT equal/one-byte, LSHIFT | Charge new/grown storage, including replaced storage |
| Replacement by small result | Equal SUB, zero comparison, MOD | Existing producer credit survives replacement until old allocation dies |
| Shrink retaining capacity | Empty/one/half-size shrink, retained batches; LEFT/RIGHT/SUBSTR | Original allocation remains paid; do not charge destruction by final length. Pushing a value with capacity above 2 × W(length) copies it into a tight buffer; LEFT/RIGHT/SUBSTR pay WRITE(out) for that copy |
| MUL result/scratch | Complete MUL evaluation with asymmetric operands | Arithmetic kernel/result allowance; scratch must not disappear from accounting |
| DIV/MOD stack/heap scratch | Divisor 1/2/8/16 limbs; normalized/normalizing paths and size boundaries | DIV already times kernel scratch; avoid counting it twice |
| Hash digest | Complete SHA256 evaluation | Digest producer plus hash kernel; other digest sizes already in hash probes |
| OP_TX plans, intermediate results, collated buffer | Empty/32/520-byte witness items, both output formats, varying counts | SELECT plus result creation; item count cannot be replaced by bytes |
| Macro metadata, call frames, inactive spans | Complete definition/call/conditional scripts | Interpreter/macro allowance, separate from value-buffer COPY |
| Scalar, signature, tweak, sighash work | Existing OUTPUT scalar, SIG, TWEAK, SIGHASH probes | Keep specialized helper costs; not reclassified as payload allocation |

This is a source-path inventory, not exhaustive allocator-state coverage. Rejection
paths, transaction precomputation and signature-checker caches are not newly
calibrated here. Script evaluation, not block validation, is the scope.

## Sampling and fitting

- Copy/conversion: existing dense 0–4 MB grid, including word/hash boundaries.
- Retention: two dimensions, payload size and entry count; cap the fixture at
  32 MiB plus container overhead. Small retained values keep the
  original capacity. These stress fixtures need not fit consensus stack limits.
- Complete scripts: boundary grid, valid stack sizes, no immediate success;
  include setup and final checks. Record current varops separately from time.
- Pilot: three epochs, median, 3 ms target batch. Normalize to the pinned local
  pre-v2 reference and 90% target. Keep decimal coefficients.
- Fit affine COPY-lifetime using the existing balanced-decade, 100x underprediction
  squared-log objective. Fit numeric lifetime separately as a *composite*, not
  another additive price. No hard-envelope or new safety multiplier.
- Check retained batches against `count * lifetime_cost(bytes)` without fitting
  to them. Investigate gaps. A 100x fit does not guarantee coverage.

Removal requires an accounting argument as well as timings: every allocation
must acquire credit at creation/growth, credit must survive shrink/move, initial
witnesses and final cleanup must be covered, and complete realistic scripts must
still meet the runtime target. This does not require tracking allocator capacity
in consensus. Use specified byte/item counts, and conservative lifetime bounds.
