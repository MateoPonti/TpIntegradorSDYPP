// TPI Pilar 1 - CUDA MD5 nonce miner (grupo SDMER).
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
//
// Agregado del grupo (shares, punto 4 del contrato): con --share-prefix <hex> también junta hasta
// --max-shares nonces cuyo hash empieza con ese prefijo (más corto que el del bloque) y los agrega como
// "shares": [...] al JSON. Sin el flag, la salida es exactamente la del kit.

#include <cuda_runtime.h>

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#define MAX_BASE 160
#define BUF_BYTES 192  // base (<=160) + 20 decimal digits + MD5 padding = 3 blocks
#define MAX_SHARES 1024

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
    while (n > 0xffffffffULL) {  // la división de 64 bits es emulada en la GPU: solo mientras haga falta
        tmp[len++] = (unsigned char)('0' + n % 10);
        n /= 10;
    }
    uint32_t m = (uint32_t)n;
    do {
        tmp[len++] = (unsigned char)('0' + m % 10);
        m /= 10;
    } while (m);
    for (int i = 0; i < len; ++i) out[i] = tmp[len - 1 - i];
    return len;
}

// TODO 1 - MD5 en el device, escrito desde el RFC 1321 (https://www.rfc-editor.org/rfc/rfc1321).
// md5_block procesa un bloque de 64 bytes (16 palabras little-endian) sobre el estado. md5_tail arma el
// relleno (0x80, ceros y el largo en bits) y procesa todos los bloques que falten, así que una base
// larga ocupa 2 o 3 bloques (caso long-base-d3 de los tests).

// Constantes K[i] = floor(2^32 * |sin(i + 1)|) del RFC 1321, sección 3.4.
__constant__ uint32_t K_MD5[64] = {
    0xd76aa478, 0xe8c7b756, 0x242070db, 0xc1bdceee, 0xf57c0faf, 0x4787c62a, 0xa8304613, 0xfd469501,
    0x698098d8, 0x8b44f7af, 0xffff5bb1, 0x895cd7be, 0x6b901122, 0xfd987193, 0xa679438e, 0x49b40821,
    0xf61e2562, 0xc040b340, 0x265e5a51, 0xe9b6c7aa, 0xd62f105d, 0x02441453, 0xd8a1e681, 0xe7d3fbc8,
    0x21e1cde6, 0xc33707d6, 0xf4d50d87, 0x455a14ed, 0xa9e3e905, 0xfcefa3f8, 0x676f02d9, 0x8d2a4c8a,
    0xfffa3942, 0x8771f681, 0x6d9d6122, 0xfde5380c, 0xa4beea44, 0x4bdecfa9, 0xf6bb4b60, 0xbebfbc70,
    0x289b7ec6, 0xeaa127fa, 0xd4ef3085, 0x04881d05, 0xd9d4d039, 0xe6db99e5, 0x1fa27cf8, 0xc4ac5665,
    0xf4292244, 0x432aff97, 0xab9423a7, 0xfc93a039, 0x655b59c3, 0x8f0ccc92, 0xffeff47d, 0x85845dd1,
    0x6fa87e4f, 0xfe2ce6e0, 0xa3014314, 0x4e0811a1, 0xf7537e82, 0xbd3af235, 0x2ad7d2bb, 0xeb86d391};

// Rotaciones de cada ronda (4 por ronda, se repiten).
__constant__ int S_MD5[16] = {7, 12, 17, 22, 5, 9, 14, 20, 4, 11, 16, 23, 6, 10, 15, 21};

__device__ __forceinline__ uint32_t rotl(uint32_t x, int c) { return (x << c) | (x >> (32 - c)); }

__device__ void md5_block(uint32_t st[4], const unsigned char *p) {
    uint32_t m[16];
#pragma unroll
    for (int i = 0; i < 16; ++i)
        m[i] = (uint32_t)p[4 * i] | ((uint32_t)p[4 * i + 1] << 8) | ((uint32_t)p[4 * i + 2] << 16) |
               ((uint32_t)p[4 * i + 3] << 24);
    uint32_t a = st[0], b = st[1], c = st[2], d = st[3];
#pragma unroll
    for (int i = 0; i < 64; ++i) {
        uint32_t f;
        int g;
        if (i < 16) {         // ronda 1: F
            f = (b & c) | (~b & d); g = i;
        } else if (i < 32) {  // ronda 2: G
            f = (b & d) | (c & ~d); g = (5 * i + 1) % 16;
        } else if (i < 48) {  // ronda 3: H
            f = b ^ c ^ d; g = (3 * i + 5) % 16;
        } else {              // ronda 4: I
            f = c ^ (b | ~d); g = (7 * i) % 16;
        }
        uint32_t t = d;
        d = c;
        c = b;
        b = b + rotl(a + f + K_MD5[i] + m[g], S_MD5[(i / 16) * 4 + i % 4]);
        a = t;
    }
    st[0] += a; st[1] += b; st[2] += c; st[3] += d;
}

__device__ __forceinline__ void md5_init(uint32_t st[4]) {
    st[0] = 0x67452301; st[1] = 0xefcdab89; st[2] = 0x98badcfe; st[3] = 0x10325476;
}

// Rellena buf[0..len) y procesa los bloques desde el byte `from` (múltiplo de 64): `st` ya tiene que
// incluir los bloques anteriores. len es el largo del mensaje completo.
__device__ void md5_tail(uint32_t st[4], unsigned char *buf, int from, int len) {
    int padded = ((len + 8) / 64 + 1) * 64;  // mensaje + 0x80 + 8 bytes de largo, redondeado a 64
    buf[len] = 0x80;
    for (int i = len + 1; i < padded - 8; ++i) buf[i] = 0;
    uint64_t bits = (uint64_t)len * 8;
    for (int i = 0; i < 8; ++i) buf[padded - 8 + i] = (unsigned char)(bits >> (8 * i));
    for (int off = from; off < padded; off += 64) md5_block(st, buf + off);
}

// El digest son las 4 palabras del estado en little-endian (mismo orden que hashlib.md5().digest()).
__device__ __forceinline__ void md5_digest(const uint32_t st[4], unsigned char digest[16]) {
    for (int i = 0; i < 16; ++i) digest[i] = (unsigned char)(st[i >> 2] >> (8 * (i & 3)));
}

__device__ void md5(unsigned char *buf, int len, unsigned char digest[16]) {
    uint32_t st[4];
    md5_init(st);
    md5_tail(st, buf, 0, len);
    md5_digest(st, digest);
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
                            uint64_t start, uint64_t count, int poll_every, int *found_flag,
                            unsigned long long *out_nonce,
                            unsigned long long *hash_count,
                            const unsigned char *share, int share_len, int max_shares,
                            unsigned int *share_count, unsigned long long *shares) {
    unsigned char buf[BUF_BYTES];
    unsigned char digest[16];
    for (int i = 0; i < base_len; ++i) buf[i] = base[i];
    unsigned long long done = 0;

    // Midstate: los bloques de 64 bytes enteros de la base no dependen del nonce, así que cada hilo los
    // hashea una sola vez. Con nuestra base de 96 bytes, por nonce queda 1 bloque en vez de 2.
    uint32_t mid[4];
    md5_init(mid);
    int full = (base_len / 64) * 64;
    for (int off = 0; off < full; off += 64) md5_block(mid, buf + off);

    // TODO 2 - grid-stride loop en 64 bits: el hilo t prueba start + t, start + t + total, ...
    const uint64_t tid = (uint64_t)blockIdx.x * blockDim.x + threadIdx.x;
    const uint64_t total = (uint64_t)gridDim.x * blockDim.x;
    int since_poll = 0;
    for (uint64_t i = tid; i < count; i += total) {
        // TODO 3 - corte temprano: cada `poll_every` iteraciones se lee la bandera como volatile (para que
        // la busque en memoria y no use una copia en un registro). Leerla en cada hash cuesta tráfico.
        if (++since_poll >= poll_every) {
            since_poll = 0;
            if (*(volatile int *)found_flag) break;
        }
        uint64_t nonce = start + i;
        int len = base_len + u64_to_dec(nonce, buf + base_len);
        uint32_t st[4] = {mid[0], mid[1], mid[2], mid[3]};
        md5_tail(st, buf, full, len);
        ++done;
        md5_digest(st, digest);
        if (share_len > 0) {
            bool sh = true;
            for (int k = 0; k < share_len; ++k) {
                if (nibble(digest, k) != share[k]) { sh = false; break; }
            }
            if (sh) {  // share: se anota y se sigue buscando (no corta)
                unsigned int slot = atomicAdd(share_count, 1u);
                if (slot < (unsigned int)max_shares) shares[slot] = nonce;
            }
        }
        bool ok = true;
        for (int k = 0; k < prefix_len; ++k) {
            if (nibble(digest, k) != prefix[k]) { ok = false; break; }
        }
        if (ok) {
            // Gana un solo hilo: el primero que pasa la bandera de 0 a 1 escribe el nonce.
            if (atomicCAS(found_flag, 0, 1) == 0) *out_nonce = nonce;
            break;
        }
    }

    atomicAdd(hash_count, done);
}

// ---------------------------------------------------------------------------
// Host
// ---------------------------------------------------------------------------

static void usage() {
    fprintf(stderr,
            "usage: miner_gpu --hash <str>\n"
            "       miner_gpu --base <str> (--prefix <hex> | --difficulty <d>)"
            " --from <n0> --to <n1> [--threads 256] [--blocks 0] [--batch 268435456] [--poll 256]"
            " [--share-prefix <hex> --max-shares 256]\n");
    exit(2);
}

static std::string md5_hex(const unsigned char d[16]) {
    char out[33];
    for (int i = 0; i < 16; ++i) snprintf(out + 2 * i, 3, "%02x", d[i]);
    return std::string(out, 32);
}

// JSON-escapes nothing on purpose: hashes and numbers only.
int main(int argc, char **argv) {
    const char *hash_input = nullptr, *base = nullptr, *prefix_arg = nullptr, *share_arg = nullptr;
    int difficulty = -1, threads = 256, blocks = 0, poll = 256, max_shares = 256;
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
        else if (a == "--poll") poll = atoi(v);
        else if (a == "--share-prefix") share_arg = v;
        else if (a == "--max-shares") max_shares = atoi(v);
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

    if (!base || (!prefix_arg && difficulty < 0) || (prefix_arg && difficulty >= 0) || to < from ||
        threads < 1 || blocks < 0 || poll < 1 || batch < 1 || max_shares < 1 || max_shares > MAX_SHARES)
        usage();
    std::string prefix = prefix_arg ? prefix_arg : std::string(difficulty, '0');
    int base_len = (int)strlen(base);
    if (base_len > MAX_BASE || prefix.size() > 32) usage();
    std::string share_s = share_arg ? share_arg : "";
    if (share_arg && (share_s.empty() || share_s.size() > 32)) usage();
    unsigned char pref[32], spref[32];
    auto hexval = [](char c) -> int {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        return -1;
    };
    for (size_t i = 0; i < prefix.size(); ++i) {
        int v = hexval(prefix[i]);
        if (v < 0) usage();
        pref[i] = (unsigned char)v;
    }
    for (size_t i = 0; i < share_s.size(); ++i) {
        int v = hexval(share_s[i]);
        if (v < 0) usage();
        spref[i] = (unsigned char)v;
    }

    if (blocks == 0) {
        int dev = 0, sms = 0;
        CUDA_CHECK(cudaGetDevice(&dev));
        CUDA_CHECK(cudaDeviceGetAttribute(&sms, cudaDevAttrMultiProcessorCount, dev));
        blocks = sms * 8;
    }

    unsigned char *d_base, *d_pref, *d_spref;
    int *d_flag;
    unsigned int *d_scount;
    unsigned long long *d_nonce, *d_count, *d_shares;
    CUDA_CHECK(cudaMalloc(&d_base, base_len + 1));
    CUDA_CHECK(cudaMalloc(&d_pref, 32));
    CUDA_CHECK(cudaMalloc(&d_flag, sizeof(int)));
    CUDA_CHECK(cudaMalloc(&d_nonce, sizeof(unsigned long long)));
    CUDA_CHECK(cudaMalloc(&d_count, sizeof(unsigned long long)));
    CUDA_CHECK(cudaMalloc(&d_spref, 32));
    CUDA_CHECK(cudaMalloc(&d_scount, sizeof(unsigned int)));
    CUDA_CHECK(cudaMalloc(&d_shares, MAX_SHARES * sizeof(unsigned long long)));
    CUDA_CHECK(cudaMemcpy(d_spref, spref, share_s.size(), cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemset(d_scount, 0, sizeof(unsigned int)));
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
                                         cur, n, poll, d_flag, d_nonce, d_count,
                                         d_spref, (int)share_s.size(), max_shares, d_scount, d_shares);
        CUDA_CHECK(cudaGetLastError());
        CUDA_CHECK(cudaMemcpy(&found, d_flag, sizeof(int), cudaMemcpyDeviceToHost));
        if (found || to - cur < n) break;  // found, or this was the last batch
        cur += n;
    }
    double secs = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();

    unsigned long long hashes = 0, nonce = 0;
    CUDA_CHECK(cudaMemcpy(&hashes, d_count, sizeof(hashes), cudaMemcpyDeviceToHost));

    // ", \"shares\": [n1, n2, ...]" solo si se pidió --share-prefix (la salida del kit no cambia).
    std::string shares_json;
    if (share_arg) {
        unsigned int sc = 0;
        CUDA_CHECK(cudaMemcpy(&sc, d_scount, sizeof(sc), cudaMemcpyDeviceToHost));
        unsigned int kept = sc < (unsigned int)max_shares ? sc : (unsigned int)max_shares;
        static unsigned long long host_shares[MAX_SHARES];
        if (kept) CUDA_CHECK(cudaMemcpy(host_shares, d_shares, kept * sizeof(unsigned long long), cudaMemcpyDeviceToHost));
        shares_json = ", \"shares\": [";
        for (unsigned int i = 0; i < kept; ++i) {
            if (i) shares_json += ", ";
            shares_json += std::to_string(host_shares[i]);
        }
        shares_json += "]";
    }
    if (!found) {
        printf("{\"found\": false, \"hashes\": %llu, \"seconds\": %.6f%s}\n", hashes, secs, shares_json.c_str());
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
    printf("{\"found\": true, \"nonce\": %llu, \"hash\": \"%s\", \"hashes\": %llu, \"seconds\": %.6f%s}\n",
           nonce, md5_hex(dig).c_str(), hashes, secs, shares_json.c_str());
    return 0;
}
