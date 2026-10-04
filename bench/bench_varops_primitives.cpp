// Copyright (c) 2026
// Distributed under the MIT software license.
/*
 * Measure the cost primitives that this repository's METHODOLOGY.md models.
 * Production helpers supply internal measurements; F uses the real interpreter.
 * Setup is untimed, destructive probes receive fresh states, and raw epochs are
 * saved beside the summary CSV. No current varops rate sets repetition counts.
 * Epochs are taken in passes over all fixtures, so each fixture's epochs are
 * spread over the whole run rather than taken back to back.
 *
 * This tool only measures: src/fit_calibrations.py fits the rates from the raw
 * epochs. Whole-script checks belong in bench_varops.
 *
 * Build: in a gsr checkout, as bench/README.md describes.
 * Run:   src/run_calibration.py, or directly with --pre-v2-seconds set to this
 *        machine's slowest pre-v2 script evaluation from bench_varops.
 */

#include <crypto/common.h>
#include <crypto/ripemd160.h>
#include <crypto/sha1.h>
#include <crypto/sha256.h>
#include <primitives/transaction.h>
#include <pubkey.h>
#include <script/biguint.h>
#include <script/interpreter.h>
#include <script/op_tx.h>
#include <script/script.h>
#include <script/script_error.h>
#include <script/valtype_stack.h>
#include <script/varops.h>
#include <script/verify_flags.h>
#include <secp256k1.h>
#include <secp256k1_extrakeys.h>
#include <secp256k1_schnorrsig.h>
#include <tinyformat.h>
#include <util/string.h>
#include <util/translation.h>

#ifdef _WIN32
#include <compat/compat.h>
#include <windows.h>
#else
#include <sys/mman.h>
#endif

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <fstream>
#include <functional>
#include <iomanip>
#include <iostream>
#include <limits>
#include <locale>
#include <map>
#include <memory>
#include <ratio>
#include <span>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

const TranslateFn G_TRANSLATION_FUN{nullptr};

namespace primitive_bench {

// The most data one script can keep live, and the prepared-state pool.
// A larger pool would time operands evicted to memory, which a script cannot
// arrange for its own stack.
constexpr size_t SCRIPT_STACK_BYTES{MAX_TAPLEAF_0XC2_TOTAL_STACK_SIZE};
// The largest linear-probe payload: the stack element size limit.
constexpr size_t MAX_PROBE_BYTES{MAX_TAPLEAF_0XC2_STACK_ELEMENT_SIZE};
using Clock = std::chrono::steady_clock;

void Require(bool condition, const std::string& message)
{
    if (!condition) throw std::runtime_error(message);
}

// Smallest nonzero step between two clock readings: 41.7 ns on Apple silicon,
// typically 100 ns on Windows, and the cost of one reading on Linux.
double ClockTickNs()
{
    double tick{std::numeric_limits<double>::infinity()};
    for (int i = 0; i < 100; ++i) {
        const auto start{Clock::now()};
        auto now{start};
        while (now == start)
            now = Clock::now();
        tick = std::min(tick, std::chrono::duration<double, std::nano>(now - start).count());
    }
    return tick;
}

// An observation barrier, not a call/clock around each primitive.
// The memory clobber prevents repeated fills/copies from being deleted.
template <typename T>
inline void Observe(const T& value)
{
#if defined(__GNUC__) || defined(__clang__)
    asm volatile("" : : "g"(&value) : "memory");
#else
    static const void* volatile sink;
    sink = &value;
    std::atomic_signal_fence(std::memory_order_seq_cst);
#endif
}

std::string CSV(const std::string& s)
{
    std::string out{"\""};
    for (char c : s) {
        if (c == '"') out += '"';
        out += c;
    }
    return out + '"';
}

double Number(const std::string& s)
{
    double x{0};
    std::istringstream parser{s};
    parser.imbue(std::locale::classic());
    Require(static_cast<bool>(parser >> x) && parser.eof() && std::isfinite(x), "invalid number: " + s);
    return x;
}

struct Options {
    size_t epochs{7};
    double epoch_ms{10.0};
    double copy_epoch_ms{100.0};
    double reference_sec{0};
    std::string output{"primitive-measurements.csv"};
    bool self_test{false};
};

// Epochs are taken in passes: each pass times one epoch of every fixture, so a
// fixture's epochs lie a whole pass apart and a slow spell of the machine hits
// one epoch of many fixtures, which the median discards, instead of every epoch
// of a few. The first pass chooses each fixture's repetitions; later passes
// reuse them, raised only when an epoch falls short of the clock's resolution.
class Runner
{
    struct Plan {
        size_t n;
        size_t rounds;
    };

    const Clock::time_point m_started{Clock::now()};
    Clock::time_point m_last_progress{m_started};
    size_t m_completed_fixtures{0};
    // An epoch spans at least 50 clock ticks, for at most 2% quantization error.
    const double m_min_timed_ns{50 * ClockTickNs()};
    static constexpr size_t MAX_ROUNDS{1024};
    size_t m_pass{0};
    // Keyed by name and occurrence within a pass: word-rounded size aliases
    // repeat names, e.g. READ/zero/8 for every length from 1 to 8.
    std::map<std::string, size_t> m_occurrences;
    std::map<std::string, Plan> m_plans;
    std::map<std::string, size_t> m_epochs;

public:
    Options options;
    std::ofstream raw;
    std::ofstream produce;

    explicit Runner(Options o) : options(std::move(o))
    {
        raw.open(options.output + ".samples.csv");
        Require(raw.good(), "cannot open raw sample output");
        raw << "probe,repetitions,epoch,ns_per_execution\n"
            << std::setprecision(17);
        produce.open(options.output + ".produce.csv");
        Require(produce.good(), "cannot open producer manifest");
        produce << "probe,kind,items,bytes,normalize_bytes\n";
    }

    // Describe a producer fixture in the manifest. Every pass measures the same
    // fixtures, so the first pass writes it.
    void Manifest(const std::string& label, const std::string& kind, size_t items, size_t bytes, size_t normalize_bytes)
    {
        if (m_pass != 0) return;
        produce << CSV(label) << ',' << kind << ',' << items << ',' << bytes << ',' << normalize_bytes << '\n';
        Require(produce.good(), "producer manifest write failed");
    }

    // Run every step once per epoch. Passes alternate direction, so no fixture
    // is always timed early or late.
    void RunPasses(const std::vector<std::function<void()>>& steps)
    {
        for (m_pass = 0; m_pass < options.epochs; ++m_pass) {
            m_occurrences.clear();
            m_completed_fixtures = 0;
            if (options.epochs > 1) std::cerr << "  Pass " << m_pass + 1 << '/' << options.epochs << '\n';
            if (m_pass % 2 == 1) {
                for (auto step = steps.rbegin(); step != steps.rend(); ++step)
                    (*step)();
            } else {
                for (const auto& step : steps)
                    step();
            }
        }
        for (const auto& [name, epochs] : m_epochs) {
            Require(epochs == options.epochs, "fixture missing from a pass: " + name);
        }
    }

    // Key of the next occurrence of `name` in this pass.
    std::string Key(const std::string& name) const
    {
        const auto it{m_occurrences.find(name)};
        std::ostringstream key;
        key.imbue(std::locale::classic());
        key << name << '#' << (it == m_occurrences.end() ? 0 : it->second);
        return key.str();
    }

    // Record this pass's epoch of a fixture.
    void Record(const std::string& name, size_t repetitions, double ns)
    {
        auto& epochs{m_epochs[Key(name)]};
        ++m_occurrences[name];
        Require(epochs == m_pass, "fixture first measured after the first pass: " + name);
        ++epochs;
        raw << CSV(name) << ',' << repetitions << ',' << m_pass << ',' << ns << '\n';
    }

    // A state larger than the pool, such as two operands near the element size
    // limit, is measured alone.
    static size_t PoolLimit(size_t bytes_per_state, size_t pool_bytes = SCRIPT_STACK_BYTES)
    {
        return std::max<size_t>(1, std::min<size_t>(1'000'000, pool_bytes / std::max<size_t>(bytes_per_state, 1)));
    }

    template <typename Make, typename Run>
    void Measure(const std::string& name, size_t max_repetitions, Make make, Run run)
    {
        Require(max_repetitions > 0, "empty repetition limit");
        Progress(name);
        const auto batch = [&](size_t n) {
            auto state = make(n); // Untimed, independent state for every batch.
            Observe(state);
            const auto start = Clock::now();
            run(state, n);
            const auto end = Clock::now();
            Observe(state); // Keep produced state alive until after timing.
            const double elapsed = std::chrono::duration<double, std::nano>(end - start).count();
            return elapsed;
        };
        // The fixture pool can cap a batch below what the clock resolves, e.g.
        // eight O(1) operations on 4 MB values. Such a batch is repeated on fresh
        // state until the epoch spans enough clock ticks.
        size_t rounds = 1;
        const auto epoch = [&](size_t n) {
            double elapsed = 0;
            for (size_t i = 0; i < rounds; ++i)
                elapsed += batch(n);
            return elapsed;
        };
        size_t n = 1;
        if (const auto it{m_plans.find(Key(name))}; it != m_plans.end()) {
            n = it->second.n;
            rounds = it->second.rounds;
            // Warm the code paths, which the pilot did in the first pass.
            if (n > 1) batch(1);
        } else {
            Require(m_pass == 0, "fixture first measured after the first pass: " + name);
            for (;;) {
                const double elapsed = epoch(n); // Pilot/warmup; not recorded.
                if (elapsed >= options.epoch_ms * 1e6) break;
                if (n >= max_repetitions) {
                    if (elapsed >= m_min_timed_ns || rounds >= MAX_ROUNDS) break;
                    const double factor = std::clamp(m_min_timed_ns / std::max(1.0, elapsed), 2.0, 8.0);
                    rounds = std::min(MAX_ROUNDS, static_cast<size_t>(std::ceil(rounds * factor)));
                    continue;
                }
                const double factor = std::clamp(options.epoch_ms * 1e6 / std::max(1.0, elapsed), 2.0, 8.0);
                const size_t next = static_cast<size_t>(std::ceil(n * factor));
                n = std::min(max_repetitions, std::max(n + 1, next));
            }
            m_plans.emplace(Key(name), Plan{n, rounds});
        }
        // A slow pilot epoch, e.g. a cold first call or an interrupt, can plan
        // too few rounds for the clock to resolve an epoch of a pool-limited
        // batch. Raise them until the epoch spans enough clock ticks; later
        // passes keep the raised plan.
        double elapsed = epoch(n);
        while (elapsed < m_min_timed_ns && rounds < MAX_ROUNDS) {
            const double factor = std::clamp(m_min_timed_ns / std::max(1.0, elapsed), 2.0, 8.0);
            rounds = std::min(MAX_ROUNDS, static_cast<size_t>(std::ceil(rounds * factor)));
            m_plans.at(Key(name)).rounds = rounds;
            elapsed = epoch(n);
        }
        Require(elapsed > 0, "epoch below the clock resolution at the round limit: " + name);
        const size_t repetitions{n * rounds};
        Record(name, repetitions, elapsed / repetitions);
        Require(raw.good(), "raw sample write failed");
        ++m_completed_fixtures;
    }

    void Progress(const std::string& name)
    {
        const auto now{Clock::now()};
        if (m_completed_fixtures == 0 || now - m_last_progress >= std::chrono::seconds{5}) {
            const auto seconds{std::chrono::duration_cast<std::chrono::seconds>(now - m_started).count()};
            std::cerr << "  Progress: pass " << m_pass + 1 << '/' << options.epochs << ", "
                      << m_completed_fixtures << " fixtures complete; "
                      << seconds << " s elapsed; measuring " << name << '\n';
            m_last_progress = now;
        }
    }

    template <typename Make, typename Run>
    void Repeated(const std::string& name, Make make, Run one)
    {
        Measure(
            name, 1'000'000,
            [&](size_t) { return make(); },
            [&](auto& state, size_t n) {
                for (size_t i = 0; i < n; ++i) {
                    one(state, i);
                    Observe(state);
                }
            });
    }

    void Save()
    {
        Require(options.reference_sec > 0, "missing positive reference time");
        std::ofstream out(options.output);
        Require(out.good(), "cannot open output: " + options.output);
        out << std::setprecision(17)
            << "# Reference_Script_Evaluation_Seconds: " << options.reference_sec << '\n'
            << "# Primitive_Model: producer-normalize-v1\n"
            << "# Max_Probe_Bytes: " << MAX_PROBE_BYTES << '\n'
            << "# Copy_Target_Batch_MS: " << options.copy_epoch_ms << '\n'
            << "# Epochs: " << options.epochs << '\n';
        out.flush();
        raw.flush();
        produce.flush();
        Require(out.good() && raw.good() && produce.good(), "output write failed");
    }
};

constexpr uint64_t DIAGNOSTIC_BUDGET{UINT64_MAX / 4};
constexpr script_verify_flags FLAGS{SCRIPT_VERIFY_CHECKLOCKTIMEVERIFY | SCRIPT_VERIFY_CHECKSEQUENCEVERIFY};

using Bytes = std::vector<unsigned char>;

// Bytes holding `words` limbs of `value`, as the biguint limb kernels take them.
Bytes LimbBytes(size_t words, uint64_t value)
{
    Bytes bytes(8 * words);
    for (size_t i{0}; i < words; ++i)
        WriteLE64(bytes.data() + 8 * i, value);
    return bytes;
}

// Several hashing APIs require a nonnull pointer even for a zero-length input.
const unsigned char* Data(const Bytes& b)
{
    static const unsigned char empty{0};
    return b.empty() ? &empty : b.data();
}
std::span<const unsigned char> Message(const Bytes& b) { return {Data(b), b.size()}; }

std::vector<size_t> Sizes(size_t minimum = 0, size_t cap = MAX_PROBE_BYTES)
{
    std::vector<size_t> result;
    for (size_t n : std::initializer_list<size_t>{0, 1, 7, 8, 9, 15, 16, 17, 32, 55, 56, 63, 64, 65, 127, 128, 129, 256, 519, 520, 521,
                                                  1024, 4096, 16384, 65536, 262144, 1048576, 4000000}) {
        if (n >= minimum && n <= cap) result.push_back(n);
    }
    for (size_t n = minimum; n <= std::min<size_t>(cap, 64); ++n)
        result.push_back(n);
    for (size_t n = 72; n <= std::min<size_t>(cap, 520); n += 8) {
        if (n >= minimum) result.push_back(n);
    }
    for (size_t n = 640; n < cap; n += std::max<size_t>(1, n / 4)) {
        if (n >= minimum) result.push_back(n);
    }
    for (size_t n : {2'000'000U, 3'000'000U, 3'500'000U}) {
        if (n >= minimum && n <= cap) result.push_back(n);
    }
    // Portable allocation-size boundaries plus previously observed allocator
    // transitions. These are fixture sizes, never consensus charge features.
    std::vector<size_t> boundaries{65536, 86658, 135402, 169252, 211565, 330570};
    for (size_t n = 8; n <= cap; n *= 2)
        boundaries.push_back(n);
    for (size_t boundary : boundaries) {
        for (int delta : {-1, 0, 1}) {
            const size_t n{static_cast<size_t>(static_cast<int64_t>(boundary) + delta)};
            if (n >= minimum && n <= cap) result.push_back(n);
        }
    }
    if (cap >= minimum) result.push_back(cap);
    std::sort(result.begin(), result.end());
    result.erase(std::unique(result.begin(), result.end()), result.end());
    return result;
}

Bytes Pattern(size_t size, uint64_t seed = 1)
{
    Bytes out(size);
    for (auto& byte : out) {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        byte = static_cast<unsigned char>(seed);
    }
    if (!out.empty()) out.back() |= 0x80; // A normalized nonzero high byte.
    return out;
}

// Operands of the ARITH fixtures: adding 1 to a = [ff..ff, ..., ff..ff, 7f..ff]
// carries through every word of a, and subtracting 1 from a = [0, ..., 0, 1]
// borrows through every word. b is 1, one word long or as long as a.
struct ArithOperands {
    Bytes a, b;
};
ArithOperands ChainOperands(size_t words, bool one_word, bool borrow_chain)
{
    ArithOperands operands{LimbBytes(words, borrow_chain ? 0 : UINT64_MAX), LimbBytes(one_word ? 1 : words, 0)};
    WriteLE64(operands.a.data() + 8 * (words - 1), borrow_chain ? 1 : 0x7fffffffffffffffULL);
    WriteLE64(operands.b.data(), 1);
    return operands;
}

struct Crypto {
    struct ContextDeleter {
        void operator()(secp256k1_context* ctx) const { secp256k1_context_destroy(ctx); }
    };
    std::unique_ptr<secp256k1_context, ContextDeleter> ctx{
        secp256k1_context_create(SECP256K1_CONTEXT_SIGN | SECP256K1_CONTEXT_VERIFY)};
    secp256k1_keypair keypair{};
    XOnlyPubKey pubkey;
    std::array<unsigned char, 32> tweak{};
    Crypto()
    {
        Require(ctx != nullptr, "secp context creation");
        std::array<unsigned char, 32> secret{};
        secret.back() = 1;
        Require(secp256k1_keypair_create(ctx.get(), &keypair, secret.data()) == 1, "keypair creation");
        secp256k1_xonly_pubkey key{};
        Require(secp256k1_keypair_xonly_pub(ctx.get(), &key, nullptr, &keypair) == 1, "xonly key conversion");
        std::array<unsigned char, 32> bytes{};
        Require(secp256k1_xonly_pubkey_serialize(ctx.get(), bytes.data(), &key) == 1, "xonly serialize");
        pubkey = XOnlyPubKey{std::span<const unsigned char>{bytes}};
        tweak.back() = 1;
    }
    std::array<unsigned char, 64> Sign(const Bytes& message) const
    {
        std::array<unsigned char, 64> sig{};
        Require(secp256k1_schnorrsig_sign_custom(ctx.get(), sig.data(), Data(message), message.size(), &keypair, nullptr) == 1, "Schnorr sign");
        Require(pubkey.VerifySchnorr(Message(message), sig), "Schnorr fixture verification");
        return sig;
    }
};

struct TransactionFixture {
    CMutableTransaction tx;
    PrecomputedTransactionData precomputed;
    Bytes control = Bytes(33, 0);
    ScriptExecutionData context;

    //! One spending input with `witnesses` empty witness items, `inputs - 1` further
    //! inputs and `outputs` outputs. Extra inputs and outputs have empty scripts, so
    //! selecting them does per-record work with the least payload to copy.
    explicit TransactionFixture(size_t witnesses = 0, size_t inputs = 1, size_t outputs = 1)
    {
        Require(inputs > 0 && outputs > 0, "transaction fixture needs an input and an output");
        tx.version = 2;
        tx.nLockTime = 500000;
        tx.vin.resize(inputs);
        for (size_t i = 0; i < inputs; ++i) {
            tx.vin[i].nSequence = 144;
            tx.vin[i].prevout.n = static_cast<uint32_t>(i);
        }
        tx.vin[0].scriptWitness.stack.resize(witnesses); // All empty: item work, no payload copying.
        CScript p2tr;
        p2tr << OP_1 << Bytes(32, 1);
        tx.vout.emplace_back(1000, p2tr);
        tx.vout.resize(outputs, CTxOut{0, CScript{}});
        std::vector<CTxOut> spent;
        spent.emplace_back(2000, p2tr);
        spent.resize(inputs, CTxOut{0, CScript{}});
        precomputed.Init(tx, std::move(spent), true);
        context.m_annex_init = true;
        context.m_annex_present = false;
        context.m_tapleaf_hash_init = true;
        context.m_taptree_root_init = true;
        context.m_tapscript_init = true;
        context.m_control_block_init = true;
        context.m_codeseparator_pos_init = true;
        context.m_codeseparator_pos = 0xffffffff;
        control[0] = 0xc2;
        context.m_control_block = control;
    }
    MutableTransactionSignatureChecker Checker() const
    {
        return MutableTransactionSignatureChecker{&tx, 0, 2000, precomputed, MissingDataBehavior::FAIL};
    }
};

// Grow a stack's own storage outside timed regions, as a running script's
// stack already has room.
void MakeRoom(ValtypeStack& stack, size_t count)
{
    for (size_t i{0}; i < count; ++i)
        stack.push_back(Bytes{});
    for (size_t i{0}; i < count; ++i)
        stack.pop_back();
}

// A prepared frame owns a separate mutable stack and finite metering state.
struct Frame {
    ValtypeStack stack;
    ScriptExecutionData context;
    varops::Budget budget{DIAGNOSTIC_BUDGET};
    explicit Frame(const std::vector<Bytes>& initial, const ScriptExecutionData& data = {})
        : stack(std::span<const Bytes>{initial}), context(data) {}
};

//! Run OP_TX on frame with the arguments the interpreter passes it.
op_tx::Result RunOpTx(Frame& frame, const ValtypeStack& alt, const BaseSignatureChecker& checker, ScriptError* error)
{
    const ScriptExecutionData& d{frame.context};
    const op_tx::ScriptContext context{d.m_annex_present ? d.m_annex : std::span<const unsigned char>{}, d.m_tapscript,
                                    d.m_tapleaf_hash, d.m_control_block, d.m_taptree_root, d.m_codeseparator_pos};
    varops::Meter meter;
    return op_tx::Eval(frame.stack, alt, checker.GetOpTxView(), context, meter, frame.budget, error);
}

void ScriptSample(Runner& runner, const std::string& label, const CScript& script,
                  const std::vector<Bytes>& initial, const BaseSignatureChecker& checker)
{
    size_t bytes = sizeof(Frame) + 256;
    for (const auto& v : initial)
        bytes += 2 * v.size() + 64;
    runner.Measure(
        label, runner.PoolLimit(bytes),
        [&](size_t n) {
            std::vector<std::unique_ptr<Frame>> frames;
            frames.reserve(n);
            for (size_t i = 0; i < n; ++i)
                frames.push_back(std::make_unique<Frame>(initial));
            return frames;
        },
        [&](auto& frames, size_t n) {
            for (size_t i = 0; i < n; ++i) {
                ScriptError error{SCRIPT_ERR_UNKNOWN_ERROR};
                bool immediate = false;
                bool ok = EvalTapleaf0xC2(frames[i]->stack, script, FLAGS, checker, frames[i]->context, frames[i]->budget, &error, &immediate);
                if (ok && !immediate) ok = CheckTapleaf0xC2ScriptResult(frames[i]->stack, frames[i]->budget, &error);
                if (!ok || immediate || error != SCRIPT_ERR_OK) throw std::runtime_error(strprintf("interpreter fixture failed: %s (error %d)", label, static_cast<int>(error)));
                Observe(ok);
            }
        });
}

// F: common evaluator overhead of instructions that pay only F, including
// parsing/prescan. Entry/finalization is amortized by long scripts, not priced
// again per opcode. Each F/<group>/<n> script executes n charged instructions
// before its final OP_1; F/skipped/<n> skips n uncharged NOPs as a diagnostic.
void MeasureFixed(Runner& r)
{
    BaseSignatureChecker checker;
    const auto repeat = [](CScript& script, std::initializer_list<opcodetype> unit, size_t count) {
        for (size_t i = 0; i < count; ++i) {
            for (const opcodetype opcode : unit)
                script << opcode;
        }
    };
    const std::array<opcodetype, 8> upgradable{OP_NOP1, OP_NOP4, OP_NOP5, OP_NOP6, OP_NOP7, OP_NOP8, OP_NOP9, OP_NOP10};
    for (size_t n : {256U, 1024U, 4096U, 16384U}) {
        std::vector<std::pair<std::string, CScript>> scripts;
        CScript nop, nops, separator, toggle, pairs, nested;
        repeat(nop, {OP_NOP}, n);
        for (size_t i = 0; i < n; ++i)
            nops << upgradable[i % upgradable.size()];
        repeat(separator, {OP_CODESEPARATOR}, n);
        // Condition push, IF and ENDIF are three of the n charged instructions.
        toggle << OP_1 << OP_IF;
        repeat(toggle, {OP_ELSE}, n - 3);
        toggle << OP_ENDIF;
        // Inactive IF/ENDIF pay F without popping; the trailing active NOP completes n.
        pairs << OP_0 << OP_IF;
        repeat(pairs, {OP_IF, OP_ENDIF}, (n - 4) / 2);
        pairs << OP_ENDIF << OP_NOP;
        nested << OP_0 << OP_IF;
        repeat(nested, {OP_IF}, (n - 4) / 2);
        repeat(nested, {OP_ENDIF}, (n - 4) / 2 + 1);
        nested << OP_NOP;
        for (auto& [group, script] : std::initializer_list<std::pair<std::string, CScript*>>{
                 {"nop", &nop}, {"upgradable-nop", &nops}, {"codeseparator", &separator}, {"else", &toggle}, {"inactive-if-pairs", &pairs}, {"inactive-if-nested", &nested}}) {
            *script << OP_1;
            ScriptSample(r, "F/" + group + "/" + util::ToString(n), *script, {}, checker);
        }
        CScript skipped;
        skipped << OP_0 << OP_IF;
        repeat(skipped, {OP_NOP}, n);
        skipped << OP_ENDIF << OP_1;
        ScriptSample(r, "F/skipped/" + util::ToString(n), skipped, {}, checker);
    }
}

// PREP: directly call the production ownership/conversion path. Operands carry
// the word-padding capacity every stack value is given.
void MeasurePreparation(Runner& r)
{
    for (size_t n : Sizes()) {
        const Bytes source = Pattern(n);
        const size_t cap = r.PoolLimit(2 * biguint::WordPaddedCapacity(n) + 256);
        struct State {
            std::vector<Bytes> bytes;
            std::vector<BigUint> numbers;
        };
        auto make = [&](size_t count) {
            State s;
            s.bytes.resize(count);
            s.numbers.resize(count);
            for (size_t i = 0; i < count; ++i) {
                Bytes b = source;
                b.reserve(biguint::WordPaddedCapacity(n));
                s.bytes[i] = std::move(b);
            }
            return s;
        };
        r.Measure("PREP/" + util::ToString(n) + "/spare", cap, make, [](auto& s, size_t c) {
            for (size_t i = 0; i < c; ++i)
                s.numbers[i].MoveFromValtype(std::move(s.bytes[i]));
        });
    }
}

// Producer lifetime model: complete creation/use/destruction is timed together.
// NORMALIZE isolates numeric materialization; no insertion or destruction occurs
// inside its timer. Sizes/counts describe semantic values, never capacity.
void MeasureProducer(Runner& r)
{
    const auto cycle = [&](const std::string& label, const std::string& kind,
                           size_t items, size_t bytes, size_t numeric_bytes, auto work) {
        work();
        const double old_ms{r.options.epoch_ms};
        r.options.epoch_ms = r.options.copy_epoch_ms;
        r.Repeated(label, [] { return 0; }, [&](auto&, size_t) { work(); });
        r.options.epoch_ms = old_ms;
        r.Manifest(label, kind, items, bytes, numeric_bytes);
    };
    auto sizes = Sizes();
    for (size_t boundary : {65536U, 86658U, 135402U, 169252U, 211565U, 330570U}) {
        for (int delta : {-16, -8, -1, 0, 1, 8, 16}) {
            sizes.push_back(static_cast<size_t>(static_cast<int64_t>(boundary) + delta));
        }
    }
    std::sort(sizes.begin(), sizes.end());
    sizes.erase(std::unique(sizes.begin(), sizes.end()), sizes.end());
    // The source every PRODUCE/churn fixture shortens, near the element size limit.
    const Bytes large_source{Pattern(3'998'900)};
    for (const size_t n : sizes) {
        const Bytes source{Pattern(n)};
        ValtypeStack stack;
        MakeRoom(stack, 8);
        // Shrinking needs an opcode, which charges its result's production
        // separately; PRODUCE/churn measures that pair.
        for (const std::string mode : {"stack", "vector", "zero", "grow"}) {
            // Growth funds both the original value and its enlarged replacement.
            cycle("PRODUCE/" + mode + "/" + util::ToString(n), "produce",
                  mode == "grow" ? 2 : 1, mode == "grow" ? n + n / 2 : n, 0, [&] {
                      if (mode == "stack") {
                          stack.push_back(source);
                          Observe(stack.Top());
                          stack.pop_back();
                      } else {
                          // Producers reserve the word-padded capacity before filling a
                          // value, as the interpreter does, so pushing never reallocates.
                          Bytes value;
                          if (mode == "zero") {
                              value.reserve(biguint::WordPaddedCapacity(n));
                              value.resize(n);
                          } else if (mode == "grow") {
                              // As OP_CAT: the first operand is grown once to the result's capacity.
                              value.reserve(biguint::WordPaddedCapacity(n / 2));
                              value.assign(source.begin(), source.begin() + n / 2);
                              value.reserve(biguint::WordPaddedCapacity(n));
                              value.insert(value.end(), source.begin() + n / 2, source.end());
                          } else {
                              value.reserve(biguint::WordPaddedCapacity(n));
                              value.assign(source.begin(), source.end());
                          }
                          Observe(value);
                          stack.push_back(std::move(value));
                          Observe(stack.Top());
                          stack.pop_back();
                      }
                  });
        }
        // Worst-case allocator: every value lands on freshly mapped pages, so each
        // lifetime pays the page faults, kernel zeroing and unmapping. Warm reuse is
        // the stack mode above; this bounds allocators that return large blocks to the OS.
        if (n >= 16384) {
            cycle("PRODUCE/fresh-pages/" + util::ToString(n), "produce", 1, n, 0, [&] {
#ifdef _WIN32
                void* pages{VirtualAlloc(nullptr, n, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)};
                Require(pages != nullptr, "fresh-page allocation failed");
                std::memcpy(pages, source.data(), n);
                Observe(pages);
                VirtualFree(pages, 0, MEM_RELEASE);
#else
                void* pages{mmap(nullptr, n, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0)};
                Require(pages != MAP_FAILED, "fresh-page allocation failed");
                std::memcpy(pages, source.data(), n);
                Observe(pages);
                munmap(pages, n);
#endif
            });
        }
        // Source and result are both funded: never attribute freeing a 4 MB
        // source to the size of a one-byte result.
        const size_t result_size{std::min(n, large_source.size())};
        cycle("PRODUCE/churn/" + util::ToString(n), "produce", 2, large_source.size() + result_size, 0, [&] {
            Bytes temporary{large_source};
            // As OP_SUBSTR: the result is copied into a word-padded buffer.
            Bytes result;
            result.reserve(biguint::WordPaddedCapacity(result_size));
            result.assign(temporary.begin(), temporary.begin() + result_size);
            Observe(temporary);
            stack.push_back(std::move(result));
            stack.pop_back();
        });
        {
            struct State {
                std::vector<BigUint> numbers;
                std::vector<Bytes> results;
            };
            const std::string label{"NORMALIZE/aligned/" + util::ToString(n)};
            // Conversion takes the same few nanoseconds at every size, so a timed epoch
            // needs many conversions. Where the pool holds fewer than 256 values
            // (above about 31 KB), allow up to 1 GiB. Each state holds one
            // value buffer.
            constexpr size_t MIN_CONVERSIONS{256};
            constexpr size_t LARGE_POOL_BYTES{size_t{1} << 30};
            const size_t per_state{biguint::WordPaddedCapacity(n) + 256};
            const size_t conversions{std::max(r.PoolLimit(per_state),
                                              std::min(MIN_CONVERSIONS, r.PoolLimit(per_state, LARGE_POOL_BYTES)))};
            r.Measure(
                label, conversions,
                [&](size_t count) {
                    State state;
                    state.numbers.resize(count);
                    state.results.resize(count);
                    // Copy one pattern rather than regenerating it for every value.
                    for (size_t i = 0; i < count; ++i)
                        state.numbers[i].MoveFromValtype(Bytes{source});
                    return state;
                },
                [](auto& state, size_t count) {
                    for (size_t i = 0; i < count; ++i) {
                        state.results[i] = state.numbers[i].MoveToValtype();
                        Observe(state.results[i]);
                    }
                });
            r.Manifest(label, "normalize", 1, 0, n);
        }
        // These are held-out composition checks, not more fit constraints.
        for (const bool spare : {false, true}) {
            cycle("PRODUCER_CHECK/numeric/" + std::string(spare ? "spare/" : "tight/") + util::ToString(n),
                  "numeric", 1, n, n, [&] {
                      Bytes bytes;
                      if (spare) bytes.reserve(biguint::WordPaddedCapacity(n) + 16);
                      bytes.insert(bytes.end(), source.begin(), source.end());
                      BigUint number{std::move(bytes)};
                      stack.push_back(number.MoveToValtype());
                      Observe(stack.Top());
                      stack.pop_back();
                  });
        }
    }
    for (uint64_t value : {uint64_t{0}, uint64_t{1}, uint64_t{255}, uint64_t{256}, uint64_t{65536}, UINT64_MAX}) {
        BigUint fixture(value);
        const size_t n{fixture.MoveToValtype().size()};
        const std::string label{"NORMALIZE/scalar/" + util::ToString(value)};
        struct State {
            std::vector<BigUint> numbers;
            std::vector<Bytes> results;
        };
        r.Measure(
            label, r.PoolLimit(512),
            [&](size_t count) {
                State state;
                state.results.resize(count);
                state.numbers.reserve(count);
                for (size_t i = 0; i < count; ++i)
                    state.numbers.emplace_back(value);
                return state;
            },
            [](auto& state, size_t count) {
                for (size_t i = 0; i < count; ++i) {
                    state.results[i] = state.numbers[i].MoveToValtype();
                    Observe(state.results[i]);
                }
            });
        r.Manifest(label, "normalize", 1, 0, n);
    }
}

// READ: BigUint zero/comparison helpers. Full scans are forced by equal/all-zero
// operands. Normalization uses
// independent padded-zero values because repeating TrimTail would time empties.
void MeasureTraversal(Runner& r)
{
    for (size_t n : Sizes(1)) {
        const size_t words = (n + 7) / 8, bytes = words * 8;
        r.Repeated(
            "READ/zero/" + util::ToString(bytes),
            [&] { return LimbBytes(words, 0); },
            [](auto& a, size_t) {
                const bool v = biguint::IsZero(biguint::ConstLimbs{a});
                Observe(v);
            });
        r.Repeated(
            "READ/compare/" + util::ToString(bytes),
            [&] { return std::pair{LimbBytes(words, 1), LimbBytes(words, 1)}; },
            [](auto& a, size_t) {
                const int v = biguint::Compare(biguint::ConstLimbs{a.first}, biguint::ConstLimbs{a.second});
                Observe(v);
            });
        // OP_EQUAL/OP_EQUALVERIFY compare the stack bytes directly; equal values scan fully.
        r.Repeated(
            "READ/equal-bytes/" + util::ToString(n),
            [&] { return std::pair{Pattern(n), Pattern(n)}; },
            [](auto& a, size_t) {
                const bool v = a.first == a.second;
                Observe(v);
            });
        r.Measure(
            "READ/trim/" + util::ToString(n), r.PoolLimit(2 * biguint::WordPaddedCapacity(n) + 128),
            [&](size_t count) {
                std::vector<BigUint> v;
                v.reserve(count);
                for (size_t i = 0; i < count; ++i)
                    v.emplace_back(Bytes(n, 0));
                return v;
            },
            [](auto& v, size_t count) {
                for (size_t i = 0; i < count; ++i)
                    v[i].TrimTrailingZeros();
            });
    }
}

// ARITH: biguint::Add/Subtract are timed separately on fresh prepared input.
// A shared affine envelope exposes the kernel's fixed call/loop work instead
// of folding it into a small-operand per-byte rate. With a one-word b, Add
// carries through a only on the carry-chain operand and Subtract borrows only on
// the borrow-chain operand; the other two pairings stop after one word, take
// constant time, and are not measured.
void MeasureArithmetic(Runner& r)
{
    for (size_t n : Sizes(1)) {
        const size_t words = (n + 7) / 8;
        for (bool unequal : {false, true}) {
            for (bool borrow_chain : {false, true}) {
                const auto make = [&](size_t count) {
                    std::vector<ArithOperands> states;
                    states.reserve(count);
                    for (size_t i = 0; i < count; ++i)
                        states.push_back(ChainOperands(words, unequal, borrow_chain));
                    return states;
                };
                const auto suffix = util::ToString(words) + (unequal ? "/one-word" : "/equal") +
                                    (borrow_chain ? "/borrow-chain" : "/carry-chain");
                const size_t capacity = r.PoolLimit(16 * words + 128);
                if (!unequal || !borrow_chain) {
                    r.Measure("ARITH/add/" + suffix, capacity, make, [](auto& states, size_t count) {
                        for (size_t i = 0; i < count; ++i) {
                            size_t nonzero = 0;
                            const bool carry = biguint::Add(biguint::Limbs{states[i].a}, biguint::ConstLimbs{states[i].b}, &nonzero);
                            Observe(carry);
                            Observe(nonzero);
                        }
                    });
                }
                if (!unequal || borrow_chain) {
                    r.Measure("ARITH/sub/" + suffix, capacity, make, [](auto& states, size_t count) {
                        for (size_t i = 0; i < count; ++i) {
                            size_t nonzero = 0;
                            const bool borrow = biguint::Subtract(biguint::Limbs{states[i].a}, biguint::ConstLimbs{states[i].b}, &nonzero);
                            Observe(borrow);
                            Observe(nonzero);
                        }
                    });
                }
            }
        }
    }
}

// BIT: no conversions in the timed region. Exercise actual BigUint inversion and
// XOR, OP_BYTEREV's work after dispatch (it pops the value, reverses it with the
// biguint::ReverseBytes word kernel and pushes it back, as the interpreter does)
// and the raw shift kernels. Repetition never shrinks an operand: inversion, XOR
// and reversal toggle or reorder bits, and the shift kernels keep the view size
// as bits shift out.
void MeasureBit(Runner& r)
{
    for (size_t n : Sizes(1)) {
        r.Repeated(
            "BIT/invert/" + util::ToString(n),
            [&] { return std::make_unique<BigUint>(Pattern(n)); },
            [](auto& v, size_t) {
                BigUint::OpInvert(*v);
                Observe(*v);
            });
        r.Repeated(
            "BIT/byterev/" + util::ToString(n),
            [&] {
                auto stack = std::make_unique<ValtypeStack>();
                stack->push_back(Pattern(n));
                return stack;
            },
            [](auto& stack, size_t) {
                valtype value{stack->PopValue()};
                biguint::ReverseBytes(value);
                stack->push_back(std::move(value));
            });
        struct State {
            BigUint a, b;
            explicit State(size_t n) : a(Pattern(n, 1)), b(Pattern(n, 2)) {}
        };
        r.Repeated(
            "BIT/xor/" + util::ToString(n),
            [&] { return std::make_unique<State>(n); },
            [](auto& s, size_t) {
                BigUint::OpXor(s->a, s->b);
                Observe(s->a);
            });
        for (size_t shift : std::array<size_t, 3>{1, 7, 63}) {
            // Whole-word-sized values avoid introducing representation padding.
            // The raw shift helpers preserve the view size even after bits vanish.
            r.Repeated(
                "BIT/down/" + util::ToString(n) + "/" + util::ToString(shift),
                [&] { return Pattern(biguint::WordPaddedCapacity(n)); },
                [&](auto& v, size_t) { biguint::ShiftDown(biguint::Limbs{v}, shift); });
            r.Repeated(
                "BIT/up/" + util::ToString(n) + "/" + util::ToString(shift),
                [&] { return Pattern(biguint::WordPaddedCapacity(n)); },
                [&](auto& v, size_t) {
                    const uint64_t carry = biguint::ShiftUp(biguint::Limbs{v}, static_cast<unsigned>(shift));
                    Observe(carry);
                });
            // As OP_LSHIFT after prepending 64 KiB of zeros: only A's words are shifted.
            constexpr size_t PREFIX{65536};
            r.Repeated(
                "BIT/upshift/" + util::ToString(n) + "/" + util::ToString(shift),
                [&] {
                    Bytes bytes(PREFIX, 0);
                    const Bytes a{Pattern(biguint::WordPaddedCapacity(n))};
                    bytes.insert(bytes.end(), a.begin(), a.end());
                    return bytes;
                },
                [&](auto& v, size_t) { biguint::ShiftDown(biguint::Limbs{v}.subspan(PREFIX / 8 - 1), shift); });
        }
    }
}

// MOVE: real stack.Roll(depth), with payloads held constant. This moves owning
// headers, not payload bytes. Test empty and one-byte elements.
void MeasureMove(Runner& r)
{
    for (size_t d : std::initializer_list<size_t>{1, 2, 8, 32, 128, 1024, 8192, 32767}) {
        for (bool nonempty : {false, true}) {
            r.Repeated(
                "MOVE/" + util::ToString(d) + (nonempty ? "/nonempty" : "/empty"),
                [&] {
                    auto stack = std::make_unique<ValtypeStack>();
                    MakeRoom(*stack, d + 1);
                    const Bytes item{nonempty ? Bytes{1} : Bytes{}};
                    for (size_t i = 0; i <= d; ++i)
                        stack->push_back(item); // Copies get word-padded capacity.
                    return stack;
                },
                [&](auto& stack, size_t) { stack->Roll(d); });
        }
    }
}

// DIVCORE includes normalization and temporary storage in the prepared DIV/MOD
// call. Fresh operands prevent repetition from measuring an already divided value.
void MeasureDivision(Runner& r)
{
    size_t fixtures{0};
    for (size_t bw : {1U, 2U, 3U, 4U, 8U, 16U, 32U, 64U, 128U, 256U, 1024U}) {
        // bw + 1024 gives many quotient rows at every divisor width, which identifies the per-row term.
        std::vector<size_t> dividends{std::max(size_t{1}, bw - 1), bw, bw + 1, 2 * bw, 4 * bw, bw + 32, bw + 1024};
        // Dividends up to the element size limit check that the per-row cost holds beyond the caches.
        const bool large{bw == 1 || bw == 2 || bw == 64};
        if (large) {
            dividends.push_back(65536);
            dividends.push_back(MAX_PROBE_BYTES / 8);
        }
        std::sort(dividends.begin(), dividends.end());
        dividends.erase(std::unique(dividends.begin(), dividends.end()), dividends.end());
        for (size_t aw : dividends) {
            const bool huge{aw >= 65536};
            for (uint64_t seed : {17U, 127U}) {
                if (huge && seed != 17) continue;
                for (const std::string pattern : {"normalized", "top-clear", "top-one", "padded"}) {
                    if (huge && pattern != "normalized" && pattern != "top-one") continue;
                    // Pattern sets the divisor's top bit, as "normalized" needs.
                    Bytes a{Pattern(aw * 8, seed)}, b{Pattern(bw * 8, seed + 5)};
                    if (pattern == "top-clear") {
                        b.back() = 0x40;
                    } else if (pattern == "top-one") {
                        std::fill(b.end() - 8, b.end(), 0);
                        b[b.size() - 8] = 1;
                    } else if (pattern == "padded") {
                        // Names and DIVCORE features use the trimmed divisor width.
                        b.resize(std::max(aw, bw) * 8, 0);
                    }
                    for (bool modulo : {false, true}) {
                        struct State {
                            BigUint a, b;
                            State(Bytes x, Bytes y) : a(std::move(x)), b(std::move(y)) {}
                        };
                        const std::string name{"DIVCORE/" + util::ToString(aw) + "/" + util::ToString(bw) +
                                               "/" + util::ToString(seed) + (modulo ? "/MOD/" : "/DIV/") + pattern};
                        r.Measure(
                            name, r.PoolLimit(2 * (aw + bw) * 8 + 256),
                            [&](size_t count) {
                                std::vector<std::unique_ptr<State>> states;
                                states.reserve(count);
                                for (size_t i{0}; i < count; ++i)
                                    states.push_back(std::make_unique<State>(a, b));
                                return states;
                            },
                            [&](auto& states, size_t count) {
                                for (size_t i{0}; i < count; ++i) {
                                    const bool ok{modulo ? BigUint::OpMod(states[i]->a, states[i]->b) : BigUint::OpDiv(states[i]->a, states[i]->b)};
                                    if (!ok) throw std::runtime_error("division fixture failed");
                                }
                            });
                        if (++fixtures % 100 == 0) std::cerr << "  DIVCORE: " << fixtures << " fixtures measured\n";
                    }
                }
            }
        }
    }
}

// MULCORE times the complete prepared OP_MUL call, like DIVCORE for division:
// one schoolbook row for each of the v shorter-operand limbs, over the u longer-operand limbs.
// Operand preparation and the zeroed product buffer happen before timing, since
// the interpreter charges the product's WRITE separately; the product is kept
// alive until after timing, so its release and final byte conversion also stay
// with PRODUCE and NORMALIZE.
void MeasureMultiplication(Runner& r)
{
    constexpr size_t max_limbs{MAX_PROBE_BYTES / 8};
    // Cap single products so the largest fixtures stay near the full-budget scale.
    constexpr size_t MAX_CELLS{size_t{1} << 28};
    size_t fixtures{0};
    for (size_t v : {1U, 2U, 3U, 4U, 8U, 16U, 32U, 64U, 128U, 256U, 1024U, 4096U, 16384U}) {
        std::vector<size_t> rows;
        for (size_t factor : {1U, 2U, 4U, 16U, 128U, 1024U, 8192U, 65536U})
            rows.push_back(v * factor);
        rows.push_back(v + 1);
        rows.push_back(max_limbs);
        std::sort(rows.begin(), rows.end());
        rows.erase(std::unique(rows.begin(), rows.end()), rows.end());
        for (size_t u : rows) {
            if (u < v || u > max_limbs || u * v > MAX_CELLS) continue;
            for (const std::string pattern : {"ones", "random"}) {
                Bytes a, b;
                if (pattern == "ones") {
                    // All-ones limbs maximize every carry chain.
                    a.assign(u * 8, 0xff);
                    b.assign(v * 8, 0xff);
                } else {
                    a = Pattern(u * 8, 17 + u);
                    b = Pattern(v * 8, 29 + v);
                }
                struct State {
                    BigUint a, b, product;
                    Bytes product_bytes;
                    State(const Bytes& x, const Bytes& y) : a(Bytes{x}), b(Bytes{y}), product_bytes(BigUint::ProductSize(a, b)) {}
                };
                const std::string name{"MULCORE/" + util::ToString(u) + "/" + util::ToString(v) + "/" + pattern};
                // Each state holds both operands and the (u + v)-limb product
                // buffer, in which OpMul builds the product.
                r.Measure(
                    name, r.PoolLimit(2 * (u + v) * 8 + 256),
                    [&](size_t count) {
                        std::vector<std::unique_ptr<State>> states;
                        states.reserve(count);
                        for (size_t i{0}; i < count; ++i)
                            states.push_back(std::make_unique<State>(a, b));
                        return states;
                    },
                    [&](auto& states, size_t count) {
                        for (size_t i{0}; i < count; ++i) {
                            states[i]->product = BigUint::OpMul(states[i]->a, states[i]->b, std::move(states[i]->product_bytes));
                        }
                    });
                if (++fixtures % 25 == 0) std::cerr << "  MULCORE: " << fixtures << " fixtures measured\n";
            }
        }
    }
}

// H_*: initialized/finalized production hash passes over Core's hash implementations.
// Outputs are observed. No use of an existing opcode hash price.
template <typename Hash>
void HashSample(Runner& r, const std::string& name, size_t n)
{
    struct State {
        Bytes data;
        std::array<unsigned char, 32> digest{};
        explicit State(size_t n) : data(Pattern(n)) {}
    };
    r.Repeated(
        name + "/" + util::ToString(n),
        [&] { return State{n}; },
        [](auto& s, size_t) {
            Hash().Write(Data(s.data), s.data.size()).Finalize(s.digest.data());
            Observe(s.digest);
        });
}

void MeasureHashes(Runner& r)
{
    for (size_t n : Sizes())
        HashSample<CSHA256>(r, "H256/core", n);
    for (size_t n : Sizes(0, 520)) {
        HashSample<CRIPEMD160>(r, "H160", n);
        HashSample<CSHA1>(r, "H1", n);
    }
}

// SIG: actual Schnorr verification, whose fit subtracts the challenge hash
// H256(64+msglen). OP_CHECKSIGFROMSTACK verifies messages up to the
// stack-element limit, so the sweep reaches 4 MB and checks that the challenge
// hash over R || P || msg stays within the SHA256 rate at every message length.
void MeasureSignatures(Runner& r, const Crypto& crypto)
{
    for (size_t n : std::initializer_list<size_t>{0, 32, 64, 128, 1024, 8192, 65536, 262144, 1048576, 4000000}) {
        Bytes msg = Pattern(n);
        auto signature = crypto.Sign(msg);
        r.Repeated(
            "SIG/" + util::ToString(n),
            [] { return uint64_t{0}; },
            [&](auto& good, size_t) {
                bool valid = crypto.pubkey.VerifySchnorr(Message(msg), signature);
                good += valid;
                Observe(valid);
                if (!valid) throw std::runtime_error("verification failed");
            });
    }
    r.Repeated(
        "TWEAK",
        [] { return uint64_t{0}; },
        [&](auto& good, size_t) {
            auto result = crypto.pubkey.AddTweak(crypto.tweak);
            if (!result) throw std::runtime_error("tweak failed");
            good += result->data()[0];
            Observe(result);
        });
}

// SELECT: complete op_tx::Eval lifetimes with collated output, one fixture per kind of
// counted unit. k is the charged unit count: every planned value (including each
// witness item count) plus every record scanned for an aggregate field. The whole
// time, including planning, collated framing, production of the one result and
// cleanup, is attributed to SELECT; the result's WRITE is not subtracted.
// Noncollated output also pays WRITE per value, so it is a diagnostic here and is
// validated as a whole-script composition by bench_varops.
// Labels are SELECT/<kind>/<format>/<records>/<k>.
void MeasureItems(Runner& r)
{
    struct Kind {
        std::string name;
        Bytes selector;
        std::vector<size_t> records;
        //! Fixture shape (witness items of the spending input, inputs, outputs) for n records.
        std::function<std::array<size_t, 3>(size_t)> shape;
        //! Charged units for n records.
        std::function<size_t(size_t)> units;
    };
    const std::vector<size_t> items{0, 1, 2, 8, 32, 128, 512, 2048, 4096, 8192, 12288, 16384, 24576, 30000};
    const std::vector<Kind> kinds{
        // Input 0's witness items: a count plus one value per item.
        {"empty_items", {0, 1, 0, 0x20, 0x80, 0}, items, [](size_t n) { return std::array<size_t, 3>{n, 1, 1}; }, [](size_t n) { return n + 1; }},
        // TX_WEIGHT alone: one value; scans every input, witness item and output.
        {"weight-scan", {0, 1 | 0x08, 0, 0, 0, 0}, items, [](size_t n) { return std::array<size_t, 3>{n, 1, 1}; }, [](size_t n) { return n + 3; }},
        // Both total amounts: two values; scans every input and output.
        {"amount-scan", {0, 1 | 0x20 | 0x80, 0, 0, 0, 0}, {1, 8, 128, 2048, 16384, 65536, 100000}, [](size_t n) { return std::array<size_t, 3>{0, 1, n}; }, [](size_t n) { return n + 3; }},
        // Every output's amount and scriptPubKey.
        {"outputs", {0, 1, 0, 0x02, 0, 0x03}, {1, 8, 128, 2048, 16384, 65536, 100000}, [](size_t n) { return std::array<size_t, 3>{0, 1, n}; }, [](size_t n) { return 2 * n; }},
        // Every input field of every input, none with witness items: eight values each.
        {"inputs", {0, 1, 0, 0x20, 0xff, 0}, {1, 8, 128, 2048, 8192, 24000}, [](size_t n) { return std::array<size_t, 3>{0, n, 1}; }, [](size_t n) { return 8 * n; }},
    };
    for (const Kind& kind : kinds) {
        for (bool collate : {true, false}) {
            if (!collate && kind.name != "empty_items") continue;
            Bytes selector{kind.selector};
            selector[1] = static_cast<unsigned char>(collate ? selector[1] | 1 : selector[1] & ~1);
            for (size_t n : kind.records) {
                const auto [witnesses, inputs, outputs]{kind.shape(n)};
                TransactionFixture fixture(witnesses, inputs, outputs);
                auto checker = fixture.Checker();
                const std::vector<Bytes> initial{selector};
                const size_t units{kind.units(n)};
                const std::string label{"SELECT/" + kind.name + "/" + (collate ? "collated/" : "noncollated/") +
                                        util::ToString(n) + "/" + util::ToString(units)};
                size_t output_entries{0};
                {
                    // Untimed check that the fixture is charged for exactly `units`.
                    Frame frame(initial, fixture.context);
                    ValtypeStack alt;
                    ScriptError error{SCRIPT_ERR_UNKNOWN_ERROR};
                    Require(RunOpTx(frame, alt, checker, &error) == op_tx::Result::NORMAL,
                            "OP_TX fixture failed: " + label);
                    output_entries = frame.stack.size();
                    uint64_t outputs_cost{0};
                    // Collated output and noncollated witness items are byte values.
                    for (const Bytes& output : frame.stack.GetStack())
                        outputs_cost += varops::WriteCost(output.size());
                    const uint64_t charged{DIAGNOSTIC_BUDGET - frame.budget.Remaining()};
                    Require(charged == varops::BaseCost() + varops::TxSelectCost(units) + outputs_cost,
                            "OP_TX fixture unit count mismatch: " + label);
                }
                r.Measure(
                    label, r.PoolLimit(1024 + (witnesses + inputs + outputs) * 96),
                    [&](size_t count) {
                        std::vector<std::unique_ptr<Frame>> v;
                        v.reserve(count);
                        for (size_t i{0}; i < count; ++i) {
                            v.push_back(std::make_unique<Frame>(initial, fixture.context));
                        }
                        return v;
                    },
                    [&](auto& v, size_t count) {
                        ValtypeStack alt;
                        for (size_t i{0}; i < count; ++i) {
                            ScriptError error{SCRIPT_ERR_UNKNOWN_ERROR};
                            auto status = RunOpTx(*v[i], alt, checker, &error);
                            if (status != op_tx::Result::NORMAL || v[i]->stack.size() != output_entries) {
                                throw std::runtime_error("OP_TX item fixture failed");
                            }
                            while (v[i]->stack.size() != 0)
                                v[i]->stack.pop_back();
                        }
                    });
            }
        }
    }
}

// UNROLL: complete evaluation of a script whose macro references sit in an
// inactive branch, so no unrolled instruction pays BASE when it is reached. The
// unrolling charge, BASE per unit plus WRITE of the unrolled script, must cover
// decoding the references, substituting instructions, copying their bytes into
// the unrolled script and skipping them unexecuted. These fixtures check that
// composition; they do not define a new primitive.
// Labels are UNROLL/<shape>/<units>/<unrolled bytes>/<charged varops>; units are
// substituted instructions plus visited references, bytes the unrolled length, as
// charged, and the last field the complete script's charge. Per-script entry work
// is attributed to the units.
void AppendMacroCompactSize(CScript& script, uint64_t value)
{
    if (value < 253) {
        script.push_back(static_cast<unsigned char>(value));
        return;
    }
    const unsigned width{value <= 0xffff ? 2U : value <= 0xffffffff ? 4U :
                                                                      8U};
    script.push_back(width == 2 ? 0xfd : width == 4 ? 0xfe :
                                                      0xff);
    for (unsigned i{0}; i < width; ++i)
        script.push_back(static_cast<unsigned char>((value >> (8 * i)) & 0xff));
}

//! Main-script bytes InactiveMacroCalls adds around its references: OP_0 OP_IF ... OP_ENDIF OP_1.
constexpr size_t INACTIVE_WRAPPER_BYTES{4};

//! Declarations of `bodies`, then `calls` references to body `index` inside OP_0 OP_IF ... OP_ENDIF.
CScript InactiveMacroCalls(const std::vector<CScript>& bodies, uint64_t index, size_t calls)
{
    CScript script;
    for (const CScript& body : bodies) {
        script << OP_MACRO;
        AppendMacroCompactSize(script, body.size());
        script.insert(script.end(), body.begin(), body.end());
    }
    script << OP_0 << OP_IF;
    for (size_t i{0}; i < calls; ++i) {
        script << OP_CALLMACRO;
        AppendMacroCompactSize(script, index);
    }
    script << OP_ENDIF << OP_1;
    return script;
}

void MeasureUnroll(Runner& r)
{
    constexpr size_t MAX_CALLS{500'000}; // Keeps committed scripts near 1 MB.
    BaseSignatureChecker checker;
    const auto measure = [&](const std::string& shape, const std::vector<CScript>& bodies, uint64_t index,
                             size_t calls, uint64_t units, uint64_t bytes) {
        const CScript script{InactiveMacroCalls(bodies, index, calls)};
        uint64_t charged{0};
        {
            ValtypeStack stack;
            ScriptExecutionData execdata;
            varops::Budget budget{DIAGNOSTIC_BUDGET};
            ScriptError error{SCRIPT_ERR_UNKNOWN_ERROR};
            Require(EvalTapleaf0xC2(stack, script, FLAGS, checker, execdata, budget, &error) &&
                        CheckTapleaf0xC2ScriptResult(stack, budget, &error),
                    "macro fixture failed: " + shape);
            charged = DIAGNOSTIC_BUDGET - budget.Remaining();
        }
        // The unrolling charge for units and bytes, then OP_0, OP_IF, OP_ENDIF,
        // OP_1 and the final check of its result.
        const uint64_t expected{units * varops::BaseCost() + varops::WriteCost(bytes + INACTIVE_WRAPPER_BYTES) +
                                4 * varops::BaseCost() + varops::WriteCost(0) + varops::WriteCost(8) +
                                varops::PrepareCost(1) + varops::ReadCost(1)};
        Require(charged == expected, "macro fixture charge mismatch: " + shape);
        const std::string label{"UNROLL/" + shape + "/" + util::ToString(units) + "/" +
                                util::ToString(bytes + INACTIVE_WRAPPER_BYTES) + "/" + util::ToString(charged)};
        ScriptSample(r, label, script, {}, checker);
    };
    for (const size_t body_size : {1U, 16U, 256U}) {
        CScript body;
        body.insert(body.end(), body_size, static_cast<unsigned char>(OP_NOP));
        const size_t most{std::min(MAX_CALLS, static_cast<size_t>(MAX_TAPLEAF_0XC2_UNROLLED_SIZE / body_size) - 64)};
        for (const size_t calls : {size_t{1024}, size_t{65536}, most}) {
            if (calls > most) continue;
            measure("nop-" + util::ToString(body_size), {body}, 0, calls, calls * (body_size + 1), calls * body_size);
        }
    }
    for (const size_t push_size : {1U, 75U, 520U, 4096U, 65536U, 1'000'000U}) {
        const CScript body{CScript{} << Bytes(push_size, 0x42)};
        // The wrapper also counts toward the unrolled size limit.
        const size_t most{std::min(MAX_CALLS, static_cast<size_t>((MAX_TAPLEAF_0XC2_UNROLLED_SIZE - INACTIVE_WRAPPER_BYTES) / body.size()))};
        for (const size_t calls : {size_t{16}, most}) {
            if (calls > most) continue;
            measure("push-" + util::ToString(push_size), {body}, 0, calls, 2 * calls, calls * body.size());
        }
    }
    for (const size_t depth : {16U, 256U}) {
        // Body i references body i-1; body 0 is empty, so every call unrolls to nothing.
        std::vector<CScript> bodies{CScript{}};
        for (size_t i{1}; i < depth; ++i) {
            CScript body{CScript{} << OP_CALLMACRO};
            AppendMacroCompactSize(body, i - 1);
            bodies.push_back(body);
        }
        for (const size_t calls : {size_t{1024}, size_t{65536}}) {
            measure("chain-" + util::ToString(depth), bodies, depth - 1, calls, calls * depth, 0);
        }
    }
}

void Help()
{
    std::cout << "Usage: bench_varops_primitives --pre-v2-seconds SECONDS [options]\n"
                 "  --pre-v2-seconds S  This machine's slowest pre-v2 script evaluation, from bench_varops (required)\n"
                 "  --out FILE          Run metadata (default primitive-measurements.csv); raw epochs use\n"
                 "                      FILE.samples.csv, the producer manifest FILE.produce.csv\n"
                 "  --epochs N          Measured epochs per fixture, one per pass over all fixtures (default 7)\n"
                 "  --sample-ms MS      Target timed duration per epoch (default 10)\n"
                 "  --copy-sample-ms MS Target duration for producer lifetime fixtures (default 100)\n"
                 "  --self-test         Check the production helper fixtures, then exit\n";
}

Options Parse(int argc, char** argv)
{
    Options o;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        auto value = [&]() {
            Require(i + 1 < argc, "missing value for " + arg);
            return std::string(argv[++i]);
        };
        auto integer = [&]() {
            const double x = Number(value());
            Require(x >= 0 && x <= double(UINT32_MAX) && x == std::floor(x), "invalid integer for " + arg);
            return static_cast<size_t>(x);
        };
        if (arg == "--help" || arg == "-h") {
            Help();
            std::exit(0);
        } else if (arg == "--pre-v2-seconds") {
            o.reference_sec = Number(value());
        } else if (arg == "--out") {
            o.output = value();
        } else if (arg == "--epochs") {
            o.epochs = integer();
        } else if (arg == "--sample-ms") {
            o.epoch_ms = Number(value());
        } else if (arg == "--copy-sample-ms") {
            o.copy_epoch_ms = Number(value());
        } else if (arg == "--self-test") {
            o.self_test = true;
        } else {
            throw std::runtime_error("unknown option: " + arg);
        }
    }
    Require(o.epochs >= 1 && o.epochs <= 1000, "epochs must be 1..1000");
    Require(o.epoch_ms > 0 && o.epoch_ms <= 1000, "sample-ms must be >0 and <=1000");
    if (o.copy_epoch_ms == 0) o.copy_epoch_ms = o.epoch_ms;
    Require(o.copy_epoch_ms > 0 && o.copy_epoch_ms <= 1000, "copy-sample-ms must be >0 and <=1000");
    if (!o.self_test) Require(o.reference_sec > 0, "supply --pre-v2-seconds: this machine's slowest pre-v2 script evaluation from bench_varops");
    return o;
}

void ProductionSelfTests()
{
    Bytes a{LimbBytes(2, 0)}, b{LimbBytes(2, 0)};
    WriteLE64(a.data(), 5);
    WriteLE64(b.data(), 7);
    const biguint::Limbs a_limbs{a};
    Require(!biguint::Add(a_limbs, biguint::ConstLimbs{b}) && a_limbs[0] == 12, "Add self-test");
    Require(!biguint::Subtract(a_limbs, biguint::ConstLimbs{b}) && a_limbs[0] == 5, "Subtract self-test");
    // The measured ARITH chains carry or borrow through every word of a.
    for (size_t words : {1U, 2U, 8U, 65U}) {
        for (bool one_word : {false, true}) {
            ArithOperands borrow{ChainOperands(words, one_word, /*borrow_chain=*/true)};
            const biguint::Limbs difference{borrow.a};
            Require(!biguint::Subtract(difference, biguint::ConstLimbs{borrow.b}), "borrow-chain underflow");
            bool full_chain{difference[words - 1] == 0};
            for (size_t i{0}; i + 1 < words; ++i)
                full_chain = full_chain && difference[i] == UINT64_MAX;
            Require(full_chain, "full borrow chain not exercised");

            ArithOperands carry{ChainOperands(words, one_word, /*borrow_chain=*/false)};
            const biguint::Limbs sum{carry.a};
            Require(!biguint::Add(sum, biguint::ConstLimbs{carry.b}), "carry-chain overflow");
            full_chain = sum[words - 1] == 0x8000000000000000ULL;
            for (size_t i{0}; i + 1 < words; ++i)
                full_chain = full_chain && sum[i] == 0;
            Require(full_chain, "full carry chain not exercised");
        }
    }
    Bytes product{LimbBytes(2, 0)};
    Require(biguint::AddMul(biguint::Limbs{product}, biguint::ConstLimbs{a}, 3) == 0 && biguint::ConstLimbs{product}[0] == 15, "AddMul self-test");
    BigUint x(Bytes{100}), y(Bytes{7});
    Require(BigUint::OpDiv(x, y), "division self-test");
    Require(x.MoveToValtype() == Bytes{14}, "division result");
    Crypto crypto;
    auto signature = crypto.Sign(Bytes{1, 2, 3});
    Require(crypto.pubkey.VerifySchnorr(std::array<unsigned char, 3>{1, 2, 3}, signature), "signature self-test");
    TransactionFixture f(8);
    auto checker = f.Checker();
    std::vector<Bytes> initial{Bytes{0, 0, 0, 0x20, 0x80, 0}};
    Frame frame(initial, f.context);
    ValtypeStack alt;
    ScriptError error{SCRIPT_ERR_UNKNOWN_ERROR};
    Require(RunOpTx(frame, alt, checker, &error) == op_tx::Result::NORMAL, "OP_TX self-test");
    Require(frame.stack.size() == 8 && frame.stack.GetTotalSize() == 0, "OP_TX result count");
}

} // namespace primitive_bench

int main(int argc, char** argv)
{
    using namespace primitive_bench;
    try {
        Options options = Parse(argc, argv);
        std::cerr << "SHA256 backend: " << SHA256AutoDetect() << '\n';
        std::cerr << "Clock tick: " << ClockTickNs() << " ns\n";
        ProductionSelfTests();
        if (options.self_test) {
            std::cout << "Self-tests passed.\n";
            return 0;
        }
        std::cerr << "Reference: " << options.reference_sec << " s (Script evaluation only).\n";
        Runner runner(options);
        Crypto crypto;
        runner.RunPasses({[&] { MeasureFixed(runner); }, [&] { MeasureProducer(runner); },
                          [&] { MeasurePreparation(runner); }, [&] { MeasureTraversal(runner); },
                          [&] { MeasureArithmetic(runner); }, [&] { MeasureBit(runner); },
                          [&] { MeasureMove(runner); }, [&] { MeasureMultiplication(runner); },
                          [&] { MeasureDivision(runner); }, [&] { MeasureHashes(runner); },
                          [&] { MeasureSignatures(runner, crypto); }, [&] { MeasureItems(runner); },
                          [&] { MeasureUnroll(runner); }});
        runner.Save();
        std::cerr << "Wrote " << options.output << ", " << options.output << ".samples.csv and " << options.output << ".produce.csv\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "bench_varops_primitives: " << e.what() << '\n';
        return 1;
    }
}
