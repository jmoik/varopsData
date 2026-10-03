# Varops benchmarks

`bench_varops` times complete Tapleaf 0xC2 scripts, and the Tapleaf 0xC0
reference panel, against BIP 440's runtime bound. `bench_varops_primitives`
times the individual cost primitives. Both call the gsr branch's evaluator
in-process. `src/run_calibration.py` builds them and records one machine's
measurements; see the README for the full run.

## Building

The benchmarks are built inside a gsr checkout, with Core's own flags, through
a CMake project include. No gsr file changes:

```sh
cmake -B build -DBUILD_BENCH=ON \
    -DCMAKE_PROJECT_BitcoinCore_INCLUDE=<varopsData>/bench/varops_bench.cmake
cmake --build build --target bench_varops bench_varops_primitives
```

## Running

Run on an otherwise idle machine, with a fixed power mode, and keep the gsr and
varopsData commits, compiler and build options, CPU and operating system with
the CSV.

```sh
./build/bin/bench_varops --list-opcodes
./build/bin/bench_varops --epochs 1 --file varops-quick.csv
./build/bin/bench_varops --opcodes OP_DIV OP_MOD --epochs 1 --file varops-focused.csv
./build/bin/bench_varops --file varops.csv
```

One epoch is for quick exploration; the default seven stable rounds give a
better indication of timing variability.

## Reading results

- **Realistic:** execution limited by both encoded weight and the 40-billion-unit
  block varops budget.
- **Full-varops:** repeat a legal script workload in bounded batches, ignoring
  encoded weight as a limit on logical execution. Normalize the measured runtime
  to 40 billion varops. Stack and per-execution limits still apply.

The CSV retains measured work, scaling factors and individual samples. A scaled
runtime is an extrapolation, not a measurement of an actual block containing
that many encoded operations. Inspect the measured budget fraction and spread
before relying on small differences.

Compare both modes against the slowest existing-version workload on the same
machine. The 80,000-Schnorr-check timing is also reported as a stable reference;
it is not necessarily the slowest existing workload. A filtered run may omit
that workload, so use a full run for the existing-version comparison.
