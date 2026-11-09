// TPI Pilar 1 - CUDA MD5 nonce miner (SKELETON).
//
// Complete the three TODO blocks. The host side (CLI contract, batching,
// JSON output) is already written so that tools/check_contract.py,
// tools/measure_energy.py and tools/worker_adapter.py work out of the box.
//
// Build:  nvcc -O3 -arch=native -o build/miner_gpu gpu/miner_gpu.cu
//         (Colab T4: -arch=sm_75 · RTX 30xx: sm_86 · RTX 40xx: sm_89)
// Test:   python3 tools/check_contract.py -- ./build/miner_gpu
//
// Contract (identical to oracle/miner_cpu.py):
//   miner_gpu --hash <str>
//   miner_gpu --base <str> (--prefix <hex> | --difficulty <d>) --from <n0> --to <n1>
//   stdout: ONE JSON line; exit 0 whether or not a nonce is found.

#include <cuda_runtime.h>

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#define MAX_BASE 160
#define BUF_BYTES 192  // base (<=160) + 20 decimal digits + MD5 padding = 3 blocks

#define CUDA_CHECK(call)                                                        \
    do {                                                                        \
        cudaError_t err_ = (call);                                              \
        if (err_ != cudaSuccess) {                                              \
            fprintf(stderr, "CUDA error %s at %s:%d\n", cudaGetErrorString(err_), \
                    __FILE__, __LINE__);                                        \
            exit(1);                                                            \
        }                                                                       \
    } while (0)

// ---------------------------------------------------------------------------
// Device helpers
// ---------------------------------------------------------------------------

// Writes the decimal digits of n into out (no terminator); returns the length.
// The coordinator hashes base + str(nonce), so this must match Python's str().
__device__ int u64_to_dec(uint64_t n, unsigned char *out) {
    unsigned char tmp[20];
    int len = 0;
    do {
        tmp[len++] = (unsigned char)('0' + n % 10);
        n /= 10;
    } while (n);
    for (int i = 0; i < len; ++i) out[i] = tmp[len - 1 - i];
    return len;
}

// TODO 1 - MD5 on the device.
// Compute MD5(buf[0..len)) and leave the digest as 16 bytes in `digest`,
// in the same order as Python's hashlib.md5(...).digest().
// `buf` has BUF_BYTES bytes, so you may write the padding in place.
// You may write it from RFC 1321 or adapt an existing CUDA MD5 implementation
// (the TPI allows libraries): cite the source in your README.
__device__ void md5(unsigned char *buf, int len, unsigned char digest[16]) {
    // TODO 1
}

// Hex nibble i of the digest, in the order of the printed hex string.
__device__ __forceinline__ unsigned nibble(const unsigned char d[16], int i) {
    return (i & 1) ? (d[i >> 1] & 0xfu) : (d[i >> 1] >> 4);
}

// ---------------------------------------------------------------------------
// Kernels
// ---------------------------------------------------------------------------

__global__ void hash_kernel(const unsigned char *msg, int len, unsigned char *digest) {
    unsigned char buf[BUF_BYTES];
    for (int i = 0; i < len; ++i) buf[i] = msg[i];
    md5(buf, len, digest);
}

// Searches nonces in [start, start + count).
//   found_flag  0 until some thread wins; the winner writes out_nonce
//   hash_count  total MD5 evaluated by all threads (for H/s and H/J)
__global__ void mine_kernel(const unsigned char *base, int base_len,
                            const unsigned char *prefix, int prefix_len,
                            uint64_t start, uint64_t count, int *found_flag,
                            unsigned long long *out_nonce,
                            unsigned long long *hash_count) {
    unsigned char buf[BUF_BYTES];
    unsigned char digest[16];
    for (int i = 0; i < base_len; ++i) buf[i] = base[i];
    unsigned long long done = 0;

    // TODO 2 - partition of the nonce space.
    // Compute this thread's global index and the total number of threads in
    // the grid, and write a GRID-STRIDE loop over i in [0, count):
    // nonce = start + i. Use 64-bit arithmetic everywhere (the test vectors
    // cross 2^32 on purpose).
    //
    // Inside the loop:
    //   len = base_len + u64_to_dec(nonce, buf + base_len);
    //   md5(buf, len, digest); ++done;
    //   compare the first prefix_len nibbles with prefix[] (values 0..15)
    //
    // TODO 3 - early exit.
    //   * When a thread finds a match it must "win" with atomicCAS on
    //     found_flag; only the winner writes *out_nonce.
    //   * Every thread polls *(volatile int *)found_flag every K iterations
    //     (not on every hash: measure what K costs) and leaves the loop.

    atomicAdd(hash_count, done);
}

// ---------------------------------------------------------------------------
// Host
// ---------------------------------------------------------------------------

static void usage() {
    fprintf(stderr,
            "usage: miner_gpu --hash <str>\n"
            "       miner_gpu --base <str> (--prefix <hex> | --difficulty <d>)"
            " --from <n0> --to <n1> [--threads 256] [--blocks 0] [--batch 268435456]\n");
    exit(2);
}

static std::string md5_hex(const unsigned char d[16]) {
    char out[33];
    for (int i = 0; i < 16; ++i) snprintf(out + 2 * i, 3, "%02x", d[i]);
    return std::string(out, 32);
}

// JSON-escapes nothing on purpose: hashes and numbers only.
int main(int argc, char **argv) {
    const char *hash_input = nullptr, *base = nullptr, *prefix_arg = nullptr;
    int difficulty = -1, threads = 256, blocks = 0;
    unsigned long long from = 0, to = 4294967295ULL, batch = 1ULL << 28;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (i + 1 >= argc) usage();
        const char *v = argv[++i];
        if (a == "--hash") hash_input = v;
        else if (a == "--base") base = v;
        else if (a == "--prefix") prefix_arg = v;
        else if (a == "--difficulty") difficulty = atoi(v);
        else if (a == "--from") from = strtoull(v, nullptr, 10);
        else if (a == "--to") to = strtoull(v, nullptr, 10);
        else if (a == "--threads") threads = atoi(v);
        else if (a == "--blocks") blocks = atoi(v);
        else if (a == "--batch") batch = strtoull(v, nullptr, 10);
        else usage();
    }

    if (hash_input) {
        int len = (int)strlen(hash_input);
        if (len > MAX_BASE) usage();
        unsigned char *d_msg, *d_dig, dig[16];
        CUDA_CHECK(cudaMalloc(&d_msg, len + 1));
        CUDA_CHECK(cudaMalloc(&d_dig, 16));
        CUDA_CHECK(cudaMemcpy(d_msg, hash_input, len, cudaMemcpyHostToDevice));
        hash_kernel<<<1, 1>>>(d_msg, len, d_dig);
        CUDA_CHECK(cudaGetLastError());
        CUDA_CHECK(cudaMemcpy(dig, d_dig, 16, cudaMemcpyDeviceToHost));
        printf("{\"hash\": \"%s\"}\n", md5_hex(dig).c_str());
        return 0;
    }

    if (!base || (!prefix_arg && difficulty < 0) || (prefix_arg && difficulty >= 0) || to < from)
        usage();
    std::string prefix = prefix_arg ? prefix_arg : std::string(difficulty, '0');
    int base_len = (int)strlen(base);
    if (base_len > MAX_BASE || prefix.size() > 32) usage();
    unsigned char pref[32];
    for (size_t i = 0; i < prefix.size(); ++i) {
        char c = prefix[i];
        if (c >= '0' && c <= '9') pref[i] = c - '0';
        else if (c >= 'a' && c <= 'f') pref[i] = c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') pref[i] = c - 'A' + 10;
        else usage();
    }

    if (blocks == 0) {
        int dev = 0, sms = 0;
        CUDA_CHECK(cudaGetDevice(&dev));
        CUDA_CHECK(cudaDeviceGetAttribute(&sms, cudaDevAttrMultiProcessorCount, dev));
        blocks = sms * 8;
    }

    unsigned char *d_base, *d_pref;
    int *d_flag;
    unsigned long long *d_nonce, *d_count;
    CUDA_CHECK(cudaMalloc(&d_base, base_len + 1));
    CUDA_CHECK(cudaMalloc(&d_pref, 32));
    CUDA_CHECK(cudaMalloc(&d_flag, sizeof(int)));
    CUDA_CHECK(cudaMalloc(&d_nonce, sizeof(unsigned long long)));
    CUDA_CHECK(cudaMalloc(&d_count, sizeof(unsigned long long)));
    CUDA_CHECK(cudaMemcpy(d_base, base, base_len, cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemcpy(d_pref, pref, prefix.size(), cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemset(d_flag, 0, sizeof(int)));
    CUDA_CHECK(cudaMemset(d_count, 0, sizeof(unsigned long long)));

    // The range is processed in batches: each kernel launch is short (no
    // display watchdog), and between batches the host can stop early.
    auto t0 = std::chrono::steady_clock::now();
    int found = 0;
    unsigned long long cur = from;
    while (true) {
        unsigned long long n = (to - cur < batch) ? (to - cur + 1) : batch;
        mine_kernel<<<blocks, threads>>>(d_base, base_len, d_pref, (int)prefix.size(),
                                         cur, n, d_flag, d_nonce, d_count);
        CUDA_CHECK(cudaGetLastError());
        CUDA_CHECK(cudaMemcpy(&found, d_flag, sizeof(int), cudaMemcpyDeviceToHost));
        if (found || to - cur < n) break;  // found, or this was the last batch
        cur += n;
    }
    double secs = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();

    unsigned long long hashes = 0, nonce = 0;
    CUDA_CHECK(cudaMemcpy(&hashes, d_count, sizeof(hashes), cudaMemcpyDeviceToHost));
    if (!found) {
        printf("{\"found\": false, \"hashes\": %llu, \"seconds\": %.6f}\n", hashes, secs);
        return 0;
    }
    CUDA_CHECK(cudaMemcpy(&nonce, d_nonce, sizeof(nonce), cudaMemcpyDeviceToHost));

    // Recompute the winning hash on the device to print it.
    std::string msg = std::string(base) + std::to_string(nonce);
    unsigned char *d_msg, *d_dig, dig[16];
    CUDA_CHECK(cudaMalloc(&d_msg, msg.size() + 1));
    CUDA_CHECK(cudaMalloc(&d_dig, 16));
    CUDA_CHECK(cudaMemcpy(d_msg, msg.data(), msg.size(), cudaMemcpyHostToDevice));
    hash_kernel<<<1, 1>>>(d_msg, (int)msg.size(), d_dig);
    CUDA_CHECK(cudaMemcpy(dig, d_dig, 16, cudaMemcpyDeviceToHost));
    printf("{\"found\": true, \"nonce\": %llu, \"hash\": \"%s\", \"hashes\": %llu, \"seconds\": %.6f}\n",
           nonce, md5_hex(dig).c_str(), hashes, secs);
    return 0;
}
