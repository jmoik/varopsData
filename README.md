# varopsData
Collection of csv files produced by the bench_varops bitcoin core benchmark.

://github.com/rustyrussell/bips/blob/guilt/varops/bip-unknown-script-restoration.mediawiki).

## Benchmark Results

### Machine Performance Scatter
![Benchmark Analysis - Scatter](plots/benchmark_analysis_scatter.png)
*Figure 1: Scatter plot comparing each machine's current script worst case (including Schnorr) against its GSR worst case. Points below the diagonal indicate machines where worst-case GSR operations are faster than current worst-case operations.*

### Absolute Time (Seconds)
![Benchmark Analysis - Seconds](plots/benchmark_analysis_seconds.png)
*Figure 2: Execution time in seconds for top 5 worst-case operations on each side. Left: current Bitcoin Script; right: new GSR operations. Error bars show mean ± std across machines. Labels include varops percentage.*

### Performance by Individual Machine
![Benchmark Analysis - Per Machine](plots/benchmark_analysis_per_machine.png)
*Figure 3: Worst-case execution times across all tested machines, comparing current script operations (excluding sigops), new GSR operations, and the Schnorr baseline. Machines are grouped by architecture and vendor.*

### Performance by Vendor
![Benchmark Analysis - By Vendor](plots/benchmark_analysis_by_vendor.png)
*Figure 4: Averaged worst-case performance grouped by hardware vendor. This aggregation reveals vendor-specific performance characteristics across Apple, AMD, Intel, and ARM (Raspberry Pi) platforms.*

### Schnorr-Normalized Units
![Benchmark Analysis - Schnorr Units](plots/benchmark_analysis_schnorr_units.png)
*Figure 5: Performance expressed in Schnorr signature equivalents per block. This normalization allows comparison against the existing block validation budget of 80,000 signature operations.*
