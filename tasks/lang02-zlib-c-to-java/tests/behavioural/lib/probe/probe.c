/* probe.c -- reference half of the differential probe for lang02-zlib-c-to-java.
 *
 * This program is the verifier's instrument, and in this task it runs against
 * one thing only: the pinned C reference, built from the same tarball the agent
 * started from, statically and dynamically linked.  What it produces is the
 * oracle.  The submission is a jar with no C ABI, so it cannot be linked into
 * this program at all; it is exercised by Probe.java, a line-for-line
 * counterpart in the same directory, and the two programs' per-case output is
 * compared byte for byte.
 *
 * That pairing is the whole design, and it imposes a rule on both files: every
 * record key, every value format and every ordering decision has to agree, so
 * neither file may grow a case the other cannot answer.  Two consequences are
 * visible below and are deliberate rather than oversights:
 *
 *   - There is no `abi layout` case.  sizeof and offsetof have no meaning on a
 *     JVM, so asking for them would produce an expectation no submission could
 *     ever match.  The question they would ask -- is the caller-visible
 *     structure what the contract says -- is asked instead by structure.py,
 *     which reads
 *     the delivered jar's classes back by reflection against the contract's
 *     api_contract, over every public member rather than over two structs.
 *
 *   - The defensive-return case (`abi defensive`) drives a zeroed, never
 *     initialized z_stream rather than a NULL pointer.  A Java caller has no
 *     null receiver but does have a freshly constructed Deflater that init was
 *     never called on, and that is the same question: the library must report
 *     Z_STREAM_ERROR rather than trusting its own state.
 *
 * Three properties are deliberate:
 *
 *   1. It includes ONLY <zlib.h>, the installed public header.  Upstream's own
 *      test programs reach for internal declarations; a legitimate downstream
 *      consumer cannot, so neither does this.  Everything here goes through the
 *      88 exported symbols and the documented types, which makes the probe a
 *      fair test of the published interface rather than of any internal struct
 *      layout.
 *
 *   2. It is an interpreter, not a fixed script.  Cases arrive as data on
 *      stdin, so the catalog can describe hundreds of distinct API workflows
 *      without this file changing.
 *
 *   3. Compressed bytes are compared exactly.  Deflate is deterministic for a
 *      fixed (level, strategy, windowBits, memLevel, flush schedule), so
 *      "produces a valid stream" is not the standard being applied here --
 *      "produces the same stream" is.  A rewrite that inflates correctly but
 *      compresses differently is a different library, and downstream systems
 *      that store or checksum compressed blobs would break on it.
 *
 * Protocol.  stdin: one case per line, tab-separated
 *     <case-id> TAB <op> [TAB <arg>]...
 * stdout: for each case, a header line, then the raw payload, then a newline
 *     #CASE TAB <case-id> TAB <status> TAB <payload-length> LF
 *     <payload bytes> LF
 * Payloads are arbitrary bytes, including NUL, so they are length-prefixed
 * rather than delimited.  Records are flushed as produced: if a case crashes
 * the process, everything before it survives and the harness attributes the
 * crash to exactly one case.
 *
 * argv[1] is the corpus directory, argv[2] an optional scratch directory for
 * the gz* file cases.
 */

#define _POSIX_C_SOURCE 200809L

#include <zlib.h>

#include <errno.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define MAX_FIELDS 16
#define MAX_LINE (1 << 16)

/* Above this many bytes a payload is recorded as length + SHA-256 + its first
 * and last 256 bytes rather than in full.  Exactness is preserved by the
 * digest; the head and tail keep a failure diff readable. */
#define FULL_BYTES_LIMIT 8192
#define EDGE_BYTES 256

/* A hard ceiling on what one case may emit.  The step limit alone is not enough:
 * at a large out_chunk a submission that never terminates would ask for hundreds
 * of gigabytes before the step count ran out.  The largest legitimate output in
 * this corpus is under 2 MB, so anything past this ceiling is a defect, and the
 * case should fail on a recorded abort rather than by exhausting the machine. */
#define MAX_CASE_BYTES (24u << 20)

static const char *g_corpus_dir = ".";
static const char *g_scratch_dir = "/tmp/zprobe";

/* ---------------------------------------------------------------- buffers */

typedef struct {
  unsigned char *p;
  size_t n, cap;
} buf;

static void die(const char *msg) {
  fprintf(stderr, "probe: %s\n", msg);
  exit(70);
}

static void bgrow(buf *b, size_t need) {
  if (b->n + need <= b->cap)
    return;
  size_t cap = b->cap ? b->cap : 256;
  while (cap < b->n + need)
    cap *= 2;
  unsigned char *p = realloc(b->p, cap);
  if (!p)
    die("out of memory");
  b->p = p;
  b->cap = cap;
}

static void bput(buf *b, const void *d, size_t n) {
  if (n == 0)
    return;
  bgrow(b, n);
  memcpy(b->p + b->n, d, n);
  b->n += n;
}

static void bputs(buf *b, const char *s) { bput(b, s, strlen(s)); }

static void bprintf(buf *b, const char *fmt, ...) {
  char tmp[512];
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(tmp, sizeof(tmp), fmt, ap);
  va_end(ap);
  if (n < 0)
    return;
  if ((size_t)n < sizeof(tmp)) {
    bput(b, tmp, (size_t)n);
    return;
  }
  bgrow(b, (size_t)n + 1);
  va_start(ap, fmt);
  vsnprintf((char *)b->p + b->n, (size_t)n + 1, fmt, ap);
  va_end(ap);
  b->n += (size_t)n;
}

static void bfree(buf *b) {
  free(b->p);
  b->p = NULL;
  b->n = b->cap = 0;
}

static void emit(const char *id, const char *status, const buf *payload) {
  printf("#CASE\t%s\t%s\t%zu\n", id, status, payload ? payload->n : (size_t)0);
  if (payload && payload->n)
    fwrite(payload->p, 1, payload->n, stdout);
  putchar('\n');
  fflush(stdout);
}

/* ----------------------------------------------------------------- sha256
 *
 * The probe needs a digest to summarize large outputs.  It carries its own
 * rather than using adler32/crc32 from the library under test: a digest
 * computed by the implementation being graded could hide a difference in the
 * very bytes it is meant to summarize.
 */

typedef struct {
  uint32_t h[8];
  uint64_t len;
  unsigned char blk[64];
  size_t blen;
} sha256;

static const uint32_t SHA_K[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu, 0x59f111f1u,
    0x923f82a4u, 0xab1c5ed5u, 0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u,
    0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u, 0xe49b69c1u, 0xefbe4786u,
    0x0fc19dc6u, 0x240ca1ccu, 0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
    0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u,
    0x06ca6351u, 0x14292967u, 0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u,
    0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u, 0xa2bfe8a1u, 0xa81a664bu,
    0xc24b8b70u, 0xc76c51a3u, 0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
    0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au,
    0x5b9cca4fu, 0x682e6ff3u, 0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u,
    0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u};

#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void sha_block(sha256 *s, const unsigned char *d) {
  uint32_t w[64];
  for (int i = 0; i < 16; i++)
    w[i] = ((uint32_t)d[i * 4] << 24) | ((uint32_t)d[i * 4 + 1] << 16) |
           ((uint32_t)d[i * 4 + 2] << 8) | (uint32_t)d[i * 4 + 3];
  for (int i = 16; i < 64; i++) {
    uint32_t s0 = ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ (w[i - 15] >> 3);
    uint32_t s1 = ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ (w[i - 2] >> 10);
    w[i] = w[i - 16] + s0 + w[i - 7] + s1;
  }
  uint32_t a = s->h[0], b = s->h[1], c = s->h[2], d0 = s->h[3];
  uint32_t e = s->h[4], f = s->h[5], g = s->h[6], h = s->h[7];
  for (int i = 0; i < 64; i++) {
    uint32_t S1 = ROR(e, 6) ^ ROR(e, 11) ^ ROR(e, 25);
    uint32_t ch = (e & f) ^ ((~e) & g);
    uint32_t t1 = h + S1 + ch + SHA_K[i] + w[i];
    uint32_t S0 = ROR(a, 2) ^ ROR(a, 13) ^ ROR(a, 22);
    uint32_t mj = (a & b) ^ (a & c) ^ (b & c);
    uint32_t t2 = S0 + mj;
    h = g; g = f; f = e; e = d0 + t1;
    d0 = c; c = b; b = a; a = t1 + t2;
  }
  s->h[0] += a; s->h[1] += b; s->h[2] += c; s->h[3] += d0;
  s->h[4] += e; s->h[5] += f; s->h[6] += g; s->h[7] += h;
}

static void sha_init(sha256 *s) {
  s->h[0] = 0x6a09e667u; s->h[1] = 0xbb67ae85u; s->h[2] = 0x3c6ef372u;
  s->h[3] = 0xa54ff53au; s->h[4] = 0x510e527fu; s->h[5] = 0x9b05688cu;
  s->h[6] = 0x1f83d9abu; s->h[7] = 0x5be0cd19u;
  s->len = 0;
  s->blen = 0;
}

static void sha_update(sha256 *s, const void *data, size_t n) {
  const unsigned char *d = data;
  s->len += n;
  while (n) {
    size_t take = 64 - s->blen;
    if (take > n)
      take = n;
    memcpy(s->blk + s->blen, d, take);
    s->blen += take;
    d += take;
    n -= take;
    if (s->blen == 64) {
      sha_block(s, s->blk);
      s->blen = 0;
    }
  }
}

static void sha_hex(sha256 *s, char out[65]) {
  uint64_t bits = s->len * 8;
  unsigned char pad = 0x80;
  sha_update(s, &pad, 1);
  unsigned char zero = 0;
  while (s->blen != 56)
    sha_update(s, &zero, 1);
  unsigned char tail[8];
  for (int i = 0; i < 8; i++)
    tail[i] = (unsigned char)(bits >> (56 - i * 8));
  s->len += 8; /* keep len consistent; not used again */
  memcpy(s->blk + s->blen, tail, 8);
  sha_block(s, s->blk);
  s->blen = 0;
  for (int i = 0; i < 8; i++)
    snprintf(out + i * 8, 9, "%08x", s->h[i]);
}

static void digest_hex(const void *data, size_t n, char out[65]) {
  sha256 s;
  sha_init(&s);
  sha_update(&s, data, n);
  sha_hex(&s, out);
}

/* ------------------------------------------------------------- rendering */

static void put_hex(buf *b, const unsigned char *d, size_t n) {
  static const char *H = "0123456789abcdef";
  bgrow(b, n * 2);
  for (size_t i = 0; i < n; i++) {
    b->p[b->n++] = (unsigned char)H[d[i] >> 4];
    b->p[b->n++] = (unsigned char)H[d[i] & 15];
  }
}

/* Record a byte string.  Small ones go in whole and in hex, so a diff shows the
 * actual bytes; large ones are summarized exactly by digest plus edges. */
static void put_bytes(buf *b, const char *label, const unsigned char *d, size_t n) {
  char hex[65];
  digest_hex(d, n, hex);
  bprintf(b, "%s.len=%zu\n%s.sha=%s\n", label, n, label, hex);
  if (n <= FULL_BYTES_LIMIT) {
    bprintf(b, "%s.hex=", label);
    put_hex(b, d, n);
    bputs(b, "\n");
  } else {
    bprintf(b, "%s.head=", label);
    put_hex(b, d, EDGE_BYTES);
    bprintf(b, "\n%s.tail=", label);
    put_hex(b, d + n - EDGE_BYTES, EDGE_BYTES);
    bputs(b, "\n");
  }
}

/* ------------------------------------------------------------- decoding */

static int split_tabs(char *line, char **fields, int max) {
  int count = 0;
  char *cursor = line;
  while (count < max) {
    fields[count++] = cursor;
    char *tab = strchr(cursor, '\t');
    if (!tab)
      break;
    *tab = '\0';
    cursor = tab + 1;
  }
  return count;
}

static long arg_long(char **f, int count, int index, long fallback) {
  if (index >= count || !f[index] || !*f[index])
    return fallback;
  return strtol(f[index], NULL, 10);
}

/* ---------------------------------------------------------------- corpus */

typedef struct {
  unsigned char *data;
  size_t len;
  int loaded;
} blob;

#define MAX_BLOBS 4096
static blob g_blobs[MAX_BLOBS];

/* Corpus entries are referenced by index and loaded on demand: a batch touches
 * a handful of payloads, and reading all of them would cost more than the batch
 * itself. */
static const blob *corpus(long index) {
  static blob empty = {NULL, 0, 1};
  if (index < 0 || index >= MAX_BLOBS)
    return &empty;
  blob *slot = &g_blobs[index];
  if (slot->loaded)
    return slot;
  char path[4096];
  snprintf(path, sizeof(path), "%s/blobs/%05ld.bin", g_corpus_dir, index);
  FILE *fh = fopen(path, "rb");
  if (!fh) {
    fprintf(stderr, "probe: cannot open %s: %s\n", path, strerror(errno));
    exit(71);
  }
  if (fseek(fh, 0, SEEK_END) != 0)
    die("seek failed");
  long size = ftell(fh);
  if (size < 0)
    die("tell failed");
  rewind(fh);
  slot->data = malloc((size_t)size + 1);
  if (!slot->data)
    die("out of memory");
  if (size > 0 && fread(slot->data, 1, (size_t)size, fh) != (size_t)size)
    die("short read");
  fclose(fh);
  slot->len = (size_t)size;
  slot->loaded = 1;
  return slot;
}

/* ------------------------------------------------------------- zlib names
 *
 * Return codes are rendered as names rather than numbers so a payload diff
 * reads directly, and so a rewrite that renumbered the constants -- which would
 * be an ABI break invisible to a round-trip test -- shows up as a difference in
 * every case rather than none.
 */

static const char *zret(int code) {
  switch (code) {
  case Z_OK: return "Z_OK";
  case Z_STREAM_END: return "Z_STREAM_END";
  case Z_NEED_DICT: return "Z_NEED_DICT";
  case Z_ERRNO: return "Z_ERRNO";
  case Z_STREAM_ERROR: return "Z_STREAM_ERROR";
  case Z_DATA_ERROR: return "Z_DATA_ERROR";
  case Z_MEM_ERROR: return "Z_MEM_ERROR";
  case Z_BUF_ERROR: return "Z_BUF_ERROR";
  case Z_VERSION_ERROR: return "Z_VERSION_ERROR";
  default: return "Z_UNKNOWN";
  }
}

/* zlib sets stream->msg to a static English string on error.  Those strings are
 * part of the observable surface -- z_errmsg is indexed by return code and
 * callers do print msg -- so they are compared too. */
static void put_msg(buf *b, const char *label, const char *msg) {
  bprintf(b, "%s.msg=%s\n", label, msg ? msg : "(null)");
}

static void put_stream(buf *b, const char *label, z_stream *s) {
  bprintf(b, "%s.total_in=%lu\n%s.total_out=%lu\n%s.adler=%lu\n%s.data_type=%d\n",
          label, (unsigned long)s->total_in, label, (unsigned long)s->total_out,
          label, (unsigned long)s->adler, label, s->data_type);
  put_msg(b, label, s->msg);
}

/* ---------------------------------------------------------------- flushing
 *
 * A flush schedule is (mode, interval): apply `mode` every `interval` input
 * chunks, Z_NO_FLUSH otherwise, and Z_FINISH once the input is exhausted.
 * Z_SYNC_FLUSH and Z_PARTIAL_FLUSH insert empty stored blocks and Z_FULL_FLUSH
 * additionally resets the window, so the schedule is directly visible in the
 * output bytes.  This is one of the strongest differential signals available:
 * getting the flush bookkeeping subtly wrong still round-trips.
 */

static int flush_for(long mode, long interval, long chunk_index, int final) {
  if (final)
    return Z_FINISH;
  if (mode == Z_NO_FLUSH || interval <= 0)
    return Z_NO_FLUSH;
  if ((chunk_index + 1) % interval == 0)
    return (int)mode;
  return Z_NO_FLUSH;
}

/* --------------------------------------------------------- deflate engine */

typedef struct {
  long level, strategy, window_bits, mem_level;
  long in_chunk, out_chunk;
  long flush_mode, flush_interval;
  const unsigned char *dict;
  size_t dict_len;
} dparams;

static void dparams_init(dparams *p) {
  memset(p, 0, sizeof(*p));
  p->level = Z_DEFAULT_COMPRESSION;
  p->strategy = Z_DEFAULT_STRATEGY;
  p->window_bits = 15;
  p->mem_level = 8;
  p->in_chunk = 0;   /* 0 means "everything at once" */
  p->out_chunk = 0;
  p->flush_mode = Z_NO_FLUSH;
  p->flush_interval = 0;
}

/* Run a full deflate over `src`, appending the compressed bytes to `out`.
 * Returns the final zlib code and fills `st` with the end-of-stream state. */
static int run_deflate(const dparams *p, const unsigned char *src, size_t src_len,
                       buf *out, z_stream *st, buf *trace) {
  z_stream zs;
  memset(&zs, 0, sizeof(zs));
  int rc = deflateInit2(&zs, (int)p->level, Z_DEFLATED, (int)p->window_bits,
                        (int)p->mem_level, (int)p->strategy);
  if (rc != Z_OK) {
    if (trace)
      bprintf(trace, "init=%s\n", zret(rc));
    memcpy(st, &zs, sizeof(zs));
    return rc;
  }
  if (p->dict) {
    int drc = deflateSetDictionary(&zs, p->dict, (uInt)p->dict_len);
    if (trace)
      bprintf(trace, "setdict=%s adler=%lu\n", zret(drc), (unsigned long)zs.adler);
    if (drc != Z_OK) {
      deflateEnd(&zs);
      memcpy(st, &zs, sizeof(zs));
      return drc;
    }
  }

  size_t in_chunk = p->in_chunk > 0 ? (size_t)p->in_chunk : (src_len ? src_len : 1);
  size_t out_chunk = p->out_chunk > 0 ? (size_t)p->out_chunk : 65536;
  unsigned char *obuf = malloc(out_chunk);
  if (!obuf)
    die("out of memory");

  size_t consumed = 0;
  long chunk_index = 0;
  int final = 0;
  rc = Z_OK;
  /* A bounded step count: a rewrite that stops making progress must fail the
   * case rather than hang the batch until the harness kills it. */
  long steps = 0;
  const long max_steps = 4000000;

  while (!final) {
    size_t take = src_len - consumed;
    if (take > in_chunk)
      take = in_chunk;
    zs.next_in = (Bytef *)(src + consumed);
    zs.avail_in = (uInt)take;
    consumed += take;
    final = (consumed >= src_len);
    int flush = flush_for(p->flush_mode, p->flush_interval, chunk_index, final);
    chunk_index++;

    /* Drain this chunk, following zlib's documented protocol exactly: a return
     * that leaves avail_out == 0 means deflate was output-bound and must be
     * called again with the same flush and fresh output space; a return that
     * leaves room means it emitted everything it had.
     *
     * deflatePending must NOT be used to detect completion here, tempting as it
     * looks.  When flush_pending exhausts the output buffer, zlib returns early
     * with pending already at zero for that instant while the flush's empty
     * stored block has not been written -- so a loop that stops at pending == 0
     * truncates the flush marker and silently corrupts the stream.  The visible
     * symptom is a deflatePending bit count that drifts downward across
     * subsequent flushes instead of holding steady.
     *
     * The consequence of following the real protocol is that the output is not
     * independent of out_chunk for the flushing modes, and that is correct
     * rather than a defect: each early return sets zlib's last_flush = -1, which
     * disarms the repeated-flush Z_BUF_ERROR guard, so the next call appends
     * another empty block.  The buffer size is genuinely observable in the byte
     * stream.  Measured against the reference: Z_NO_FLUSH and Z_BLOCK are
     * out_chunk-independent, Z_PARTIAL_FLUSH cannot terminate at out_chunk 1,
     * and Z_SYNC_FLUSH / Z_FULL_FLUSH cannot terminate below out_chunk 6 because
     * a 5-byte empty block never fits.  The catalog therefore holds flushing
     * schedules to out_chunk 0 or >= 6, and pairs the one- and two-byte buffers
     * with the modes where the protocol provably converges. */
    for (;;) {
      zs.next_out = obuf;
      zs.avail_out = (uInt)out_chunk;
      rc = deflate(&zs, flush);
      size_t produced = out_chunk - zs.avail_out;
      if (produced)
        bput(out, obuf, produced);
      if (++steps > max_steps || out->n > MAX_CASE_BYTES) {
        rc = Z_BUF_ERROR;
        if (trace)
          bputs(trace, out->n > MAX_CASE_BYTES ? "abort=byte-limit\n"
                                                 : "abort=step-limit\n");
        final = 1;
        break;
      }
      if (rc == Z_STREAM_END)
        break;
      if (rc != Z_OK && rc != Z_BUF_ERROR) {
        final = 1;
        break;
      }
      if (zs.avail_out == 0)
        continue;                 /* output-bound: the contract says call again */
      if (zs.avail_in != 0)
        continue;                 /* input for this chunk is not consumed yet */
      if (flush == Z_FINISH)
        continue;                 /* only Z_STREAM_END ends a finish */
      break;                      /* room left over: this call emitted it all */
    }
    if (rc != Z_OK && rc != Z_STREAM_END && rc != Z_BUF_ERROR)
      break;
    if (rc == Z_STREAM_END)
      break;
  }

  free(obuf);
  /* Snapshot BEFORE deflateEnd.  deflateEnd clears the public counters and
   * data_type, and data_type is the field that reports which block type the
   * compressor settled on -- one of the most informative differential signals
   * available, so it has to be read while it still means something. */
  memcpy(st, &zs, sizeof(zs));
  st->state = NULL;
  int end_rc = deflateEnd(&zs);
  if (trace)
    bprintf(trace, "end=%s\n", zret(end_rc));
  return rc;
}

/* --------------------------------------------------------- inflate engine */

typedef struct {
  long window_bits;
  long in_chunk, out_chunk;
  const unsigned char *dict;
  size_t dict_len;
} iparams;

static void iparams_init(iparams *p) {
  memset(p, 0, sizeof(*p));
  p->window_bits = 15;
}

static int run_inflate(const iparams *p, const unsigned char *src, size_t src_len,
                       buf *out, z_stream *st, buf *trace) {
  z_stream zs;
  memset(&zs, 0, sizeof(zs));
  int rc = inflateInit2(&zs, (int)p->window_bits);
  if (rc != Z_OK) {
    if (trace)
      bprintf(trace, "init=%s\n", zret(rc));
    memcpy(st, &zs, sizeof(zs));
    return rc;
  }

  size_t in_chunk = p->in_chunk > 0 ? (size_t)p->in_chunk : (src_len ? src_len : 1);
  size_t out_chunk = p->out_chunk > 0 ? (size_t)p->out_chunk : 65536;
  unsigned char *obuf = malloc(out_chunk);
  if (!obuf)
    die("out of memory");

  size_t consumed = 0;
  long steps = 0;
  const long max_steps = 4000000;
  int need_dict_seen = 0;
  rc = Z_OK;

  for (;;) {
    if (zs.avail_in == 0) {
      if (consumed >= src_len)
        break;
      size_t take = src_len - consumed;
      if (take > in_chunk)
        take = in_chunk;
      zs.next_in = (Bytef *)(src + consumed);
      zs.avail_in = (uInt)take;
      consumed += take;
    }
    zs.next_out = obuf;
    zs.avail_out = (uInt)out_chunk;
    rc = inflate(&zs, Z_NO_FLUSH);
    size_t produced = out_chunk - zs.avail_out;
    if (produced)
      bput(out, obuf, produced);
    if (rc == Z_NEED_DICT) {
      need_dict_seen = 1;
      if (!p->dict) {
        if (trace)
          bputs(trace, "needdict=unsatisfied\n");
        break;
      }
      int drc = inflateSetDictionary(&zs, p->dict, (uInt)p->dict_len);
      if (trace)
        bprintf(trace, "setdict=%s\n", zret(drc));
      if (drc != Z_OK) {
        rc = drc;
        break;
      }
      continue;
    }
    if (rc == Z_STREAM_END || (rc != Z_OK && rc != Z_BUF_ERROR))
      break;
    if (++steps > max_steps || out->n > MAX_CASE_BYTES) {
      rc = Z_BUF_ERROR;
      if (trace)
        bputs(trace, out->n > MAX_CASE_BYTES ? "abort=byte-limit\n"
                                               : "abort=step-limit\n");
      break;
    }
    if (rc == Z_BUF_ERROR && produced == 0 && zs.avail_in == 0 && consumed >= src_len)
      break;
  }

  if (trace && need_dict_seen)
    bputs(trace, "saw=Z_NEED_DICT\n");
  free(obuf);
  memcpy(st, &zs, sizeof(zs));
  st->state = NULL;
  int end_rc = inflateEnd(&zs);
  if (trace)
    bprintf(trace, "end=%s\n", zret(end_rc));
  return rc;
}

/* ------------------------------------------------------------ op: deflate
 *
 * The central case.  Every argument is a knob the catalog sweeps:
 *   blob level strategy wbits memlevel in_chunk out_chunk flush_mode
 *   flush_interval dict_blob
 * The recorded payload is the compressed bytes plus the end state, so the case
 * fails if either the output or the bookkeeping differs.
 */
static void op_deflate(buf *out, char **f, int nf) {
  dparams p;
  dparams_init(&p);
  const blob *src = corpus(arg_long(f, nf, 2, 0));
  p.level = arg_long(f, nf, 3, Z_DEFAULT_COMPRESSION);
  p.strategy = arg_long(f, nf, 4, Z_DEFAULT_STRATEGY);
  p.window_bits = arg_long(f, nf, 5, 15);
  p.mem_level = arg_long(f, nf, 6, 8);
  p.in_chunk = arg_long(f, nf, 7, 0);
  p.out_chunk = arg_long(f, nf, 8, 0);
  p.flush_mode = arg_long(f, nf, 9, Z_NO_FLUSH);
  p.flush_interval = arg_long(f, nf, 10, 0);
  long dict_index = arg_long(f, nf, 11, -1);
  if (dict_index >= 0) {
    const blob *d = corpus(dict_index);
    p.dict = d->data;
    p.dict_len = d->len;
  }

  buf comp = {0}, trace = {0};
  z_stream st;
  int rc = run_deflate(&p, src->data, src->len, &comp, &st, &trace);
  bprintf(out, "rc=%s\nin.len=%zu\n", zret(rc), src->len);
  put_bytes(out, "comp", comp.p, comp.n);
  put_stream(out, "st", &st);
  bput(out, trace.p, trace.n);

  /* An immediate re-inflate: it costs little and turns "the bytes differ" into
   * "the bytes differ AND they no longer decode", which separates a cosmetic
   * difference from a broken stream in the report. */
  if (rc == Z_STREAM_END && p.window_bits >= -15) {
    iparams ip;
    iparams_init(&ip);
    ip.window_bits = p.window_bits;
    ip.dict = p.dict;
    ip.dict_len = p.dict_len;
    buf back = {0};
    z_stream ist;
    int irc = run_inflate(&ip, comp.p, comp.n, &back, &ist, NULL);
    int same = (back.n == src->len) &&
               (src->len == 0 || memcmp(back.p, src->data, src->len) == 0);
    bprintf(out, "verify.rc=%s\nverify.roundtrip=%s\n", zret(irc),
            same ? "identical" : "DIFFERENT");
    bfree(&back);
  }
  bfree(&comp);
  bfree(&trace);
}

/* ------------------------------------------------------------ op: inflate
 *
 * Inflate a stream produced here by a fixed reference-independent recipe, so the
 * case grades the decompressor on its own: level 6 default deflate of the blob,
 * then inflate with the given windowBits and buffer chunking.  Because the
 * compressed input is produced by the same library, a submission that is
 * self-consistently wrong still fails -- the recorded payload includes the
 * compressed digest.
 */
static void op_inflate(buf *out, char **f, int nf) {
  const blob *src = corpus(arg_long(f, nf, 2, 0));
  long dwbits = arg_long(f, nf, 3, 15);
  long iwbits = arg_long(f, nf, 4, 15);
  long in_chunk = arg_long(f, nf, 5, 0);
  long out_chunk = arg_long(f, nf, 6, 0);
  long level = arg_long(f, nf, 7, 6);

  dparams dp;
  dparams_init(&dp);
  dp.level = level;
  dp.window_bits = dwbits;
  buf comp = {0};
  z_stream dst;
  int drc = run_deflate(&dp, src->data, src->len, &comp, &dst, NULL);

  iparams ip;
  iparams_init(&ip);
  ip.window_bits = iwbits;
  ip.in_chunk = in_chunk;
  ip.out_chunk = out_chunk;
  buf back = {0}, trace = {0};
  z_stream ist;
  int irc = run_inflate(&ip, comp.p, comp.n, &back, &ist, &trace);

  int same = (back.n == src->len) &&
             (src->len == 0 || memcmp(back.p, src->data, src->len) == 0);
  bprintf(out, "deflate.rc=%s\ncomp.len=%zu\n", zret(drc), comp.n);
  char hex[65];
  digest_hex(comp.p, comp.n, hex);
  bprintf(out, "comp.sha=%s\ninflate.rc=%s\n", hex, zret(irc));
  put_bytes(out, "out", back.p, back.n);
  bprintf(out, "roundtrip=%s\n", same ? "identical" : "DIFFERENT");
  put_stream(out, "st", &ist);
  bput(out, trace.p, trace.n);
  bfree(&comp);
  bfree(&back);
  bfree(&trace);
}

/* -------------------------------------------------------------- op: bound
 *
 * deflateBound and compressBound are contracts downstream code allocates
 * against: a value that is too small makes a correct caller overflow.  The
 * numbers themselves are compared, not just the inequality, because a rewrite
 * that returns a wildly generous bound would silently break callers that size
 * fixed buffers from it.
 */
static void op_bound(buf *out, char **f, int nf) {
  const blob *src = corpus(arg_long(f, nf, 2, 0));
  long level = arg_long(f, nf, 3, Z_DEFAULT_COMPRESSION);
  long wbits = arg_long(f, nf, 4, 15);
  long mem = arg_long(f, nf, 5, 8);

  z_stream zs;
  memset(&zs, 0, sizeof(zs));
  int rc = deflateInit2(&zs, (int)level, Z_DEFLATED, (int)wbits, (int)mem,
                        Z_DEFAULT_STRATEGY);
  bprintf(out, "init=%s\n", zret(rc));
  if (rc == Z_OK) {
    uLong bound = deflateBound(&zs, (uLong)src->len);
    bprintf(out, "deflateBound=%lu\n", (unsigned long)bound);
    deflateEnd(&zs);

    dparams dp;
    dparams_init(&dp);
    dp.level = level;
    dp.window_bits = wbits;
    dp.mem_level = mem;
    buf comp = {0};
    z_stream dst;
    int drc = run_deflate(&dp, src->data, src->len, &comp, &dst, NULL);
    bprintf(out, "actual=%zu\nfits=%s\nrc=%s\n", comp.n,
            comp.n <= bound ? "yes" : "NO", zret(drc));
    bfree(&comp);
  }
  bprintf(out, "compressBound=%lu\n", (unsigned long)compressBound((uLong)src->len));
  return;
}

/* ----------------------------------------------------------- op: oneshot
 *
 * compress / compress2 / uncompress / uncompress2.  These are the entry points
 * most applications actually call, and uncompress2's in-out length semantics
 * are easy to get wrong in a rewrite: it reports how much input it consumed,
 * which matters when a buffer holds trailing data.
 */
static void op_oneshot(buf *out, char **f, int nf) {
  const blob *src = corpus(arg_long(f, nf, 2, 0));
  long level = arg_long(f, nf, 3, -2);   /* -2 means "use compress()" */
  long slack = arg_long(f, nf, 4, 0);    /* shrink the destination by this much */
  long trail = arg_long(f, nf, 5, 0);    /* extra bytes appended to the stream */

  uLong cap = compressBound((uLong)src->len);
  unsigned char *dest = malloc(cap ? cap : 1);
  if (!dest)
    die("out of memory");
  uLongf dest_len = cap;
  int rc;
  if (level == -2)
    rc = compress(dest, &dest_len, src->data, (uLong)src->len);
  else
    rc = compress2(dest, &dest_len, src->data, (uLong)src->len, (int)level);
  bprintf(out, "compress.rc=%s\n", zret(rc));
  put_bytes(out, "comp", dest, rc == Z_OK ? dest_len : 0);

  if (rc == Z_OK) {
    size_t stream_len = dest_len + (size_t)(trail > 0 ? trail : 0);
    unsigned char *stream = malloc(stream_len ? stream_len : 1);
    if (!stream)
      die("out of memory");
    memcpy(stream, dest, dest_len);
    for (long i = 0; i < trail; i++)
      stream[dest_len + i] = (unsigned char)(0x5a + i);

    size_t room = src->len > (size_t)slack ? src->len - (size_t)slack : 0;
    unsigned char *back = malloc(room + 1);
    if (!back)
      die("out of memory");
    uLongf back_len = (uLongf)room;
    int urc = uncompress(back, &back_len, stream, (uLong)stream_len);
    bprintf(out, "uncompress.rc=%s\nuncompress.len=%lu\n", zret(urc),
            (unsigned long)back_len);
    if (urc == Z_OK) {
      int same = (back_len == src->len) &&
                 (src->len == 0 || memcmp(back, src->data, src->len) == 0);
      bprintf(out, "uncompress.match=%s\n", same ? "identical" : "DIFFERENT");
    }

    uLongf back2_len = (uLongf)room;
    uLong source_len = (uLong)stream_len;
    int u2 = uncompress2(back, &back2_len, stream, &source_len);
    bprintf(out, "uncompress2.rc=%s\nuncompress2.len=%lu\nuncompress2.consumed=%lu\n",
            zret(u2), (unsigned long)back2_len, (unsigned long)source_len);
    free(back);
    free(stream);
  }
  free(dest);
}

/* ----------------------------------------------------------- op: checksum
 *
 * crc32 / crc32_z / adler32 / adler32_z and the two combine functions.  These
 * are the most commonly reimplemented parts of zlib and the easiest to get
 * subtly wrong: a table generated with the wrong polynomial bit order, or a
 * combine that mishandles a zero length.  Incremental splitting is swept
 * because a rewrite that only handles the one-shot path fails only here.
 */
static void op_checksum(buf *out, char **f, int nf) {
  const blob *src = corpus(arg_long(f, nf, 2, 0));
  long split = arg_long(f, nf, 3, 0);      /* chunk size, 0 = one shot */
  unsigned long seed = (unsigned long)arg_long(f, nf, 4, 0);

  uLong crc = crc32((uLong)seed, Z_NULL, 0);
  uLong adl = adler32((uLong)seed, Z_NULL, 0);
  bprintf(out, "crc32.init=%lu\nadler32.init=%lu\n",
          (unsigned long)crc, (unsigned long)adl);

  crc = (uLong)seed;
  adl = (uLong)seed;
  uLong crcz = (uLong)seed, adlz = (uLong)seed;
  size_t step = split > 0 ? (size_t)split : (src->len ? src->len : 1);
  for (size_t off = 0; off < src->len; off += step) {
    size_t take = src->len - off;
    if (take > step)
      take = step;
    crc = crc32(crc, src->data + off, (uInt)take);
    adl = adler32(adl, src->data + off, (uInt)take);
    crcz = crc32_z(crcz, src->data + off, take);
    adlz = adler32_z(adlz, src->data + off, take);
  }
  bprintf(out, "crc32=%lu\ncrc32_z=%lu\nadler32=%lu\nadler32_z=%lu\n",
          (unsigned long)crc, (unsigned long)crcz,
          (unsigned long)adl, (unsigned long)adlz);
  bprintf(out, "crc32.agrees=%s\nadler32.agrees=%s\n",
          crc == crcz ? "yes" : "NO", adl == adlz ? "yes" : "NO");

  /* Combine: split the payload, checksum the halves independently, and rebuild
   * the whole-payload value from the parts. */
  if (src->len >= 2) {
    size_t half = src->len / 2;
    uLong c1 = crc32(0, src->data, (uInt)half);
    uLong c2 = crc32(0, src->data + half, (uInt)(src->len - half));
    uLong cc = crc32_combine(c1, c2, (z_off_t)(src->len - half));
    uLong whole_c = crc32(0, src->data, (uInt)src->len);
    uLong a1 = adler32(1, src->data, (uInt)half);
    uLong a2 = adler32(1, src->data + half, (uInt)(src->len - half));
    uLong ac = adler32_combine(a1, a2, (z_off_t)(src->len - half));
    uLong whole_a = adler32(1, src->data, (uInt)src->len);
    bprintf(out,
            "crc32_combine=%lu\ncrc32.whole=%lu\ncrc32.combine_ok=%s\n"
            "adler32_combine=%lu\nadler32.whole=%lu\nadler32.combine_ok=%s\n",
            (unsigned long)cc, (unsigned long)whole_c,
            cc == whole_c ? "yes" : "NO",
            (unsigned long)ac, (unsigned long)whole_a,
            ac == whole_a ? "yes" : "NO");
    /* A zero-length second part must be the identity. */
    bprintf(out, "crc32_combine.zero=%lu\nadler32_combine.zero=%lu\n",
            (unsigned long)crc32_combine(whole_c, crc32(0, Z_NULL, 0), 0),
            (unsigned long)adler32_combine(whole_a, adler32(1, Z_NULL, 0), 0));
  }
  bprintf(out, "crc32_combine_gen=%lu\n",
          (unsigned long)crc32_combine_gen((z_off_t)src->len));
}

/* --------------------------------------------------------- op: statechange
 *
 * deflateCopy, deflateReset, deflateParams, deflatePrime, deflateTune,
 * deflatePending, deflateGetDictionary, deflateUsed.  These are the functions a
 * rewrite is most likely to stub out, because nothing in a naive round-trip test
 * touches them -- but they are exported, versioned, and used by real callers
 * (git and rsync both change parameters mid-stream).
 *
 * The case compresses the first half, performs the operation, compresses the
 * rest, and records the whole output.  A stubbed implementation diverges in the
 * bytes, not just in a return code.
 */
static void op_statechange(buf *out, char **f, int nf) {
  const blob *src = corpus(arg_long(f, nf, 2, 0));
  const char *what = nf > 3 ? f[3] : "reset";
  long level = arg_long(f, nf, 4, 6);
  long arg_a = arg_long(f, nf, 5, 0);
  long arg_b = arg_long(f, nf, 6, 0);

  z_stream zs;
  memset(&zs, 0, sizeof(zs));
  int rc = deflateInit2(&zs, (int)level, Z_DEFLATED, 15, 8, Z_DEFAULT_STRATEGY);
  bprintf(out, "init=%s\n", zret(rc));
  if (rc != Z_OK)
    return;

  buf comp = {0};
  unsigned char obuf[16384];
  size_t half = src->len / 2;

  /* Phase one: compress the first half without finishing. */
  zs.next_in = (Bytef *)src->data;
  zs.avail_in = (uInt)half;
  while (zs.avail_in > 0) {
    zs.next_out = obuf;
    zs.avail_out = (uInt)sizeof(obuf);
    int drc = deflate(&zs, Z_NO_FLUSH);
    bput(&comp, obuf, sizeof(obuf) - zs.avail_out);
    if (drc != Z_OK)
      break;
  }
  bprintf(out, "phase1.total_in=%lu\nphase1.total_out=%lu\n",
          (unsigned long)zs.total_in, (unsigned long)zs.total_out);

  if (strcmp(what, "pending") == 0) {
    unsigned pending = 0;
    int bits = 0;
    int prc = deflatePending(&zs, &pending, &bits);
    bprintf(out, "pending.rc=%s\npending.bytes=%u\npending.bits=%d\n",
            zret(prc), pending, bits);
  } else if (strcmp(what, "params") == 0) {
    zs.next_out = obuf;
    zs.avail_out = (uInt)sizeof(obuf);
    int prc = deflateParams(&zs, (int)arg_a, (int)arg_b);
    bput(&comp, obuf, sizeof(obuf) - zs.avail_out);
    bprintf(out, "params.rc=%s\nparams.level=%ld\nparams.strategy=%ld\n",
            zret(prc), arg_a, arg_b);
  } else if (strcmp(what, "prime") == 0) {
    int prc = deflatePrime(&zs, (int)arg_a, (int)arg_b);
    bprintf(out, "prime.rc=%s\nprime.bits=%ld\nprime.value=%ld\n",
            zret(prc), arg_a, arg_b);
  } else if (strcmp(what, "tune") == 0) {
    int prc = deflateTune(&zs, (int)arg_a, (int)arg_b, 128, 64);
    bprintf(out, "tune.rc=%s\n", zret(prc));
  } else if (strcmp(what, "getdict") == 0) {
    unsigned char dict[32768];
    uInt dlen = (uInt)sizeof(dict);
    int prc = deflateGetDictionary(&zs, dict, &dlen);
    bprintf(out, "getdict.rc=%s\ngetdict.len=%u\n", zret(prc), dlen);
    if (prc == Z_OK)
      put_bytes(out, "getdict", dict, dlen);
    /* A NULL buffer with a length pointer must report the length only. */
    uInt probe_len = 0;
    int nrc = deflateGetDictionary(&zs, Z_NULL, &probe_len);
    bprintf(out, "getdict.null.rc=%s\ngetdict.null.len=%u\n", zret(nrc), probe_len);
  } else if (strcmp(what, "copy") == 0) {
    z_stream dup;
    memset(&dup, 0, sizeof(dup));
    int crc_ = deflateCopy(&dup, &zs);
    bprintf(out, "copy.rc=%s\n", zret(crc_));
    if (crc_ == Z_OK) {
      /* Finish the copy independently and record its output: a copy that shares
       * state with the original produces different bytes here. */
      buf dupout = {0};
      dup.next_in = (Bytef *)(src->data + half);
      dup.avail_in = (uInt)(src->len - half);
      int drc;
      do {
        dup.next_out = obuf;
        dup.avail_out = (uInt)sizeof(obuf);
        drc = deflate(&dup, Z_FINISH);
        bput(&dupout, obuf, sizeof(obuf) - dup.avail_out);
      } while (drc == Z_OK);
      bprintf(out, "copy.finish=%s\n", zret(drc));
      put_bytes(out, "copy.out", dupout.p, dupout.n);
      bprintf(out, "copy.total_in=%lu\ncopy.total_out=%lu\n",
              (unsigned long)dup.total_in, (unsigned long)dup.total_out);
      bfree(&dupout);
      deflateEnd(&dup);
    }
  } else if (strcmp(what, "reset") == 0) {
    int prc = deflateReset(&zs);
    bprintf(out, "reset.rc=%s\nreset.total_in=%lu\nreset.total_out=%lu\n",
            zret(prc), (unsigned long)zs.total_in, (unsigned long)zs.total_out);
    /* After a reset the stream must behave as freshly initialized, so the
     * output from here is directly comparable with a fresh deflate. */
    comp.n = 0;
  }

  /* Phase two: finish from wherever the operation left the stream. */
  zs.next_in = (Bytef *)(src->data + half);
  zs.avail_in = (uInt)(src->len - half);
  int drc;
  do {
    zs.next_out = obuf;
    zs.avail_out = (uInt)sizeof(obuf);
    drc = deflate(&zs, Z_FINISH);
    bput(&comp, obuf, sizeof(obuf) - zs.avail_out);
  } while (drc == Z_OK);
  bprintf(out, "finish=%s\n", zret(drc));
  put_bytes(out, "comp", comp.p, comp.n);
  bprintf(out, "final.total_in=%lu\nfinal.total_out=%lu\nfinal.adler=%lu\n",
          (unsigned long)zs.total_in, (unsigned long)zs.total_out,
          (unsigned long)zs.adler);
  put_msg(out, "final", zs.msg);
  bprintf(out, "end=%s\n", zret(deflateEnd(&zs)));
  bfree(&comp);
}

/* ------------------------------------------------------------ op: gzheader
 *
 * deflateSetHeader / inflateGetHeader.  The gzip header is a wire format with
 * fixed field offsets, and the round trip through a gz_header struct is the only
 * way a caller reads a member's name, comment or extra field.  A rewrite that
 * treats the header as opaque bytes fails here.
 *
 * MTIME is set explicitly rather than left to the clock, so the case is
 * reproducible: upstream never calls time() itself, and neither may a rewrite.
 */
static void op_gzheader(buf *out, char **f, int nf) {
  const blob *src = corpus(arg_long(f, nf, 2, 0));
  long variant = arg_long(f, nf, 3, 0);
  long level = arg_long(f, nf, 4, 6);

  static unsigned char extra_field[64];
  for (size_t i = 0; i < sizeof(extra_field); i++)
    extra_field[i] = (unsigned char)(i * 3 + 1);

  gz_header head;
  memset(&head, 0, sizeof(head));
  head.time = 0x5f5e100;   /* fixed, not the clock */
  head.os = 3;
  head.text = (variant & 1) ? 1 : 0;
  if (variant & 2) {
    head.name = (Bytef *)"payload.bin";
  }
  if (variant & 4) {
    head.comment = (Bytef *)"a comment field with spaces and punctuation!";
  }
  if (variant & 8) {
    head.extra = extra_field;
    head.extra_len = (uInt)sizeof(extra_field);
  }
  if (variant & 16) {
    head.hcrc = 1;
  }
  if (variant & 32) {
    head.time = 0;
    head.os = 255;
  }

  z_stream zs;
  memset(&zs, 0, sizeof(zs));
  int rc = deflateInit2(&zs, (int)level, Z_DEFLATED, 15 + 16, 8,
                        Z_DEFAULT_STRATEGY);
  bprintf(out, "init=%s\n", zret(rc));
  if (rc != Z_OK)
    return;
  int hrc = deflateSetHeader(&zs, &head);
  bprintf(out, "setheader=%s\n", zret(hrc));

  buf comp = {0};
  unsigned char obuf[16384];
  zs.next_in = (Bytef *)src->data;
  zs.avail_in = (uInt)src->len;
  int drc;
  do {
    zs.next_out = obuf;
    zs.avail_out = (uInt)sizeof(obuf);
    drc = deflate(&zs, Z_FINISH);
    bput(&comp, obuf, sizeof(obuf) - zs.avail_out);
  } while (drc == Z_OK);
  bprintf(out, "deflate=%s\n", zret(drc));
  put_bytes(out, "comp", comp.p, comp.n);
  deflateEnd(&zs);

  /* Read the header back through the public struct. */
  z_stream iz;
  memset(&iz, 0, sizeof(iz));
  int irc = inflateInit2(&iz, 15 + 16);
  bprintf(out, "inflate.init=%s\n", zret(irc));
  if (irc == Z_OK) {
    gz_header got;
    memset(&got, 0, sizeof(got));
    unsigned char name_buf[128], comment_buf[256], extra_buf[128];
    memset(name_buf, 0, sizeof(name_buf));
    memset(comment_buf, 0, sizeof(comment_buf));
    memset(extra_buf, 0, sizeof(extra_buf));
    got.name = name_buf;
    got.name_max = (uInt)sizeof(name_buf);
    got.comment = comment_buf;
    got.comm_max = (uInt)sizeof(comment_buf);
    got.extra = extra_buf;
    got.extra_max = (uInt)sizeof(extra_buf);
    int grc = inflateGetHeader(&iz, &got);
    bprintf(out, "getheader=%s\n", zret(grc));

    buf back = {0};
    iz.next_in = comp.p;
    iz.avail_in = (uInt)comp.n;
    int rc2;
    do {
      iz.next_out = obuf;
      iz.avail_out = (uInt)sizeof(obuf);
      rc2 = inflate(&iz, Z_NO_FLUSH);
      bput(&back, obuf, sizeof(obuf) - iz.avail_out);
    } while (rc2 == Z_OK && iz.avail_in > 0);
    bprintf(out, "inflate.rc=%s\n", zret(rc2));
    bprintf(out,
            "head.done=%d\nhead.text=%d\nhead.time=%lu\nhead.xflags=%d\n"
            "head.os=%d\nhead.extra_len=%u\nhead.hcrc=%d\n",
            got.done, got.text, (unsigned long)got.time, got.xflags, got.os,
            got.extra_len, got.hcrc);
    bprintf(out, "head.name=%s\n", got.name ? (const char *)got.name : "(null)");
    bprintf(out, "head.comment=%s\n",
            got.comment ? (const char *)got.comment : "(null)");
    if (got.extra_len && got.extra_len <= sizeof(extra_buf)) {
      bputs(out, "head.extra=");
      put_hex(out, extra_buf, got.extra_len);
      bputs(out, "\n");
    }
    int same = (back.n == src->len) &&
               (src->len == 0 || memcmp(back.p, src->data, src->len) == 0);
    bprintf(out, "roundtrip=%s\n", same ? "identical" : "DIFFERENT");
    bfree(&back);
    inflateEnd(&iz);
  }
  bfree(&comp);
}

/* --------------------------------------------------------- op: inflatestate
 *
 * inflateCopy, inflateReset, inflateReset2, inflatePrime, inflateMark,
 * inflateSync, inflateSyncPoint, inflateCodesUsed, inflateValidate,
 * inflateUndermine, inflateGetDictionary.  Same reasoning as the deflate side:
 * exported, versioned, used by real callers, and invisible to a naive test.
 *
 * inflateMark deserves a note: it packs the bit position and the pending-match
 * length into one long, and its sign convention differs between "no match in
 * progress" and "inflating". Rewrites usually get the packing wrong.
 */
static void op_inflatestate(buf *out, char **f, int nf) {
  const blob *src = corpus(arg_long(f, nf, 2, 0));
  const char *what = nf > 3 ? f[3] : "reset";
  long wbits = arg_long(f, nf, 4, 15);
  long arg_a = arg_long(f, nf, 5, 0);

  /* A stream to work on, produced here. */
  dparams dp;
  dparams_init(&dp);
  dp.level = 6;
  dp.window_bits = wbits;
  buf comp = {0};
  z_stream dst;
  run_deflate(&dp, src->data, src->len, &comp, &dst, NULL);

  z_stream iz;
  memset(&iz, 0, sizeof(iz));
  int rc = inflateInit2(&iz, (int)wbits);
  bprintf(out, "init=%s\n", zret(rc));
  if (rc != Z_OK) {
    bfree(&comp);
    return;
  }

  unsigned char obuf[16384];
  buf back = {0};
  /* Inflate roughly half the stream so the state is genuinely mid-stream. */
  size_t feed = comp.n / 2;
  iz.next_in = comp.p;
  iz.avail_in = (uInt)feed;
  int irc = Z_OK;
  while (iz.avail_in > 0 && irc == Z_OK) {
    iz.next_out = obuf;
    iz.avail_out = (uInt)sizeof(obuf);
    irc = inflate(&iz, Z_NO_FLUSH);
    bput(&back, obuf, sizeof(obuf) - iz.avail_out);
  }
  bprintf(out, "mid.rc=%s\nmid.total_in=%lu\nmid.total_out=%lu\n",
          zret(irc), (unsigned long)iz.total_in, (unsigned long)iz.total_out);

  if (strcmp(what, "mark") == 0) {
    long mark = inflateMark(&iz);
    bprintf(out, "mark=%ld\nmark.bits=%ld\nmark.length=%ld\n",
            mark, mark >> 16, mark & 0xffff);
  } else if (strcmp(what, "codesused") == 0) {
    bprintf(out, "codesUsed=%lu\n", (unsigned long)inflateCodesUsed(&iz));
  } else if (strcmp(what, "validate") == 0) {
    int vrc = inflateValidate(&iz, (int)arg_a);
    bprintf(out, "validate.rc=%s\nvalidate.check=%ld\n", zret(vrc), arg_a);
  } else if (strcmp(what, "undermine") == 0) {
    int urc = inflateUndermine(&iz, (int)arg_a);
    bprintf(out, "undermine.rc=%s\n", zret(urc));
  } else if (strcmp(what, "syncpoint") == 0) {
    bprintf(out, "syncPoint=%d\n", inflateSyncPoint(&iz));
  } else if (strcmp(what, "getdict") == 0) {
    unsigned char dict[32768];
    uInt dlen = (uInt)sizeof(dict);
    int grc = inflateGetDictionary(&iz, dict, &dlen);
    bprintf(out, "getdict.rc=%s\ngetdict.len=%u\n", zret(grc), dlen);
    if (grc == Z_OK && dlen)
      put_bytes(out, "getdict", dict, dlen);
  } else if (strcmp(what, "copy") == 0) {
    z_stream dup;
    memset(&dup, 0, sizeof(dup));
    int crc_ = inflateCopy(&dup, &iz);
    bprintf(out, "copy.rc=%s\n", zret(crc_));
    if (crc_ == Z_OK) {
      buf dupout = {0};
      dup.next_in = comp.p + feed;
      dup.avail_in = (uInt)(comp.n - feed);
      int rc2 = Z_OK;
      while (rc2 == Z_OK) {
        dup.next_out = obuf;
        dup.avail_out = (uInt)sizeof(obuf);
        rc2 = inflate(&dup, Z_NO_FLUSH);
        bput(&dupout, obuf, sizeof(obuf) - dup.avail_out);
        if (dup.avail_in == 0 && rc2 != Z_STREAM_END)
          break;
      }
      bprintf(out, "copy.rc2=%s\ncopy.total_out=%lu\n",
              zret(rc2), (unsigned long)dup.total_out);
      put_bytes(out, "copy.out", dupout.p, dupout.n);
      bfree(&dupout);
      inflateEnd(&dup);
    }
  } else if (strcmp(what, "reset") == 0) {
    int prc = inflateReset(&iz);
    bprintf(out, "reset.rc=%s\nreset.total_in=%lu\n", zret(prc),
            (unsigned long)iz.total_in);
    back.n = 0;
    /* Re-inflate the whole stream from the reset state. */
    iz.next_in = comp.p;
    iz.avail_in = (uInt)comp.n;
    int rc2 = Z_OK;
    while (rc2 == Z_OK) {
      iz.next_out = obuf;
      iz.avail_out = (uInt)sizeof(obuf);
      rc2 = inflate(&iz, Z_NO_FLUSH);
      bput(&back, obuf, sizeof(obuf) - iz.avail_out);
      if (iz.avail_in == 0 && rc2 != Z_STREAM_END)
        break;
    }
    bprintf(out, "after_reset.rc=%s\n", zret(rc2));
    int same = (back.n == src->len) &&
               (src->len == 0 || memcmp(back.p, src->data, src->len) == 0);
    bprintf(out, "after_reset.match=%s\n", same ? "identical" : "DIFFERENT");
  } else if (strcmp(what, "reset2") == 0) {
    int prc = inflateReset2(&iz, (int)arg_a);
    bprintf(out, "reset2.rc=%s\nreset2.wbits=%ld\n", zret(prc), arg_a);
  } else if (strcmp(what, "prime") == 0) {
    int prc = inflatePrime(&iz, (int)arg_a, 0);
    bprintf(out, "prime.rc=%s\n", zret(prc));
    /* Draining the primed bits must be reported by inflateMark. */
    bprintf(out, "prime.mark=%ld\n", inflateMark(&iz));
  } else if (strcmp(what, "sync") == 0) {
    int prc = inflateSync(&iz);
    bprintf(out, "sync.rc=%s\nsync.total_in=%lu\n", zret(prc),
            (unsigned long)iz.total_in);
  }

  put_bytes(out, "out", back.p, back.n);
  put_msg(out, "mid", iz.msg);
  bprintf(out, "end=%s\n", zret(inflateEnd(&iz)));
  bfree(&back);
  bfree(&comp);
}

/* --------------------------------------------------------- op: syncrecover
 *
 * inflateSync's actual job: resynchronize after damage by finding the next
 * full-flush marker.  This is separate from the `sync` case in op_inflatestate,
 * which calls inflateSync on an undamaged stream that has no marker in it -- a
 * call that can only scan to the end and report Z_DATA_ERROR, and therefore
 * never exercises the search itself.
 *
 * The marker a Z_FULL_FLUSH leaves is five bytes: 00 00 00 ff ff (an empty
 * stored block, byte-aligned, then LEN=0000 NLEN=ffff).  syncsearch walks the
 * input counting how much of `00 00 ff ff` it has, and the interesting branch is
 * the one taken on a zero byte that arrives when two zeroes are already banked:
 * that third zero is not a mismatch, it is the first zero of the marker whose
 * second zero it also is.  A search that resets its counter there walks straight
 * past every real marker, because every real marker has that third zero in
 * front of it.  Only a stream that contains one can tell the two apart.
 *
 * So: compress with full flushes, record where the markers landed, damage the
 * stream, then inflate until it complains, resync, and keep going.  What is
 * recorded is the whole recovery -- where the damage stopped it, what
 * inflateSync returned and how far it moved, and how much of the tail came back
 * out -- because a rewrite can get the return code right and the position wrong.
 *
 *   blob damage wbits chunk arg
 */
static void op_syncrecover(buf *out, char **f, int nf) {
  const blob *src = corpus(arg_long(f, nf, 2, 0));
  const char *damage = nf > 3 ? f[3] : "flip";
  long wbits = arg_long(f, nf, 4, 15);
  long chunk = arg_long(f, nf, 5, 4096);
  long arg = arg_long(f, nf, 6, 0);

  /* Full flushes every chunk, so the stream carries several markers. */
  dparams dp;
  dparams_init(&dp);
  dp.level = 6;
  dp.window_bits = wbits;
  dp.in_chunk = chunk > 0 ? chunk : 4096;
  dp.flush_mode = Z_FULL_FLUSH;
  dp.flush_interval = 1;
  buf comp = {0};
  z_stream dst;
  int drc = run_deflate(&dp, src->data, src->len, &comp, &dst, NULL);
  bprintf(out, "deflate.rc=%s\ndeflate.len=%zu\n", zret(drc), comp.n);

  /* Where the markers are.  Reported, because a rewrite whose full flush emits
   * a different marker fails here rather than somewhere downstream. */
  size_t markers = 0, first_marker = 0;
  for (size_t i = 0; i + 4 < comp.n; i++) {
    if (comp.p[i] == 0 && comp.p[i + 1] == 0 && comp.p[i + 2] == 0 &&
        comp.p[i + 3] == 0xff && comp.p[i + 4] == 0xff) {
      if (!markers)
        first_marker = i;
      markers++;
    }
  }
  bprintf(out, "markers=%zu\nmarker.first=%zu\n", markers, first_marker);

  if (comp.n == 0) {
    bfree(&comp);
    return;
  }

  /* Damage.  Each kind is a different shape of loss a real archive suffers, and
   * each leaves the search starting from a different place. */
  size_t start = 0;                 /* where the damaged stream begins */
  if (strcmp(damage, "flip") == 0) {
    /* One bit inside the first block, so the header is intact and the failure
     * happens mid-symbol. */
    size_t at = comp.n > 40 ? 20 + (size_t)(arg % 16) : comp.n / 2;
    comp.p[at] ^= 0x40;
    bprintf(out, "damage=flip at=%zu\n", at);
  } else if (strcmp(damage, "zero") == 0) {
    /* A run of zeroes, which is also a run of near-markers: the search has to
     * carry its count across them and not fire early. */
    size_t at = comp.n > 64 ? 24 : comp.n / 4;
    size_t n = (size_t)(arg > 0 ? arg : 8);
    if (at + n > comp.n)
      n = comp.n - at;
    memset(comp.p + at, 0, n);
    bprintf(out, "damage=zero at=%zu n=%zu\n", at, n);
  } else if (strcmp(damage, "behead") == 0) {
    /* The head is gone entirely -- no zlib header, no first block.  This is the
     * case that has nothing but the marker to find, and the one a naive search
     * fails outright. */
    start = comp.n > 32 ? 12 + (size_t)(arg % 8) : 1;
    bprintf(out, "damage=behead start=%zu\n", start);
  } else if (strcmp(damage, "prefix") == 0) {
    /* A partial marker in front of the real one: 00 00 00 00 ff, then the
     * stream.  The count has to survive the extra zeroes and the false ff. */
    static const unsigned char lure[] = {0x00, 0x00, 0x00, 0x00, 0xff};
    buf lured = {0};
    bput(&lured, lure, sizeof(lure));
    bput(&lured, comp.p, comp.n);
    bfree(&comp);
    comp = lured;
    bprintf(out, "damage=prefix added=%zu\n", sizeof(lure));
  } else if (strcmp(damage, "truncate") == 0) {
    /* Cut after the first marker, so the recovery runs out of input rather than
     * reaching the end of the stream. */
    size_t keep = first_marker ? first_marker + 5 + (size_t)(arg % 64) : comp.n / 2;
    if (keep > comp.n)
      keep = comp.n;
    comp.n = keep;
    bprintf(out, "damage=truncate keep=%zu\n", keep);
  }

  z_stream iz;
  memset(&iz, 0, sizeof(iz));
  int rc = inflateInit2(&iz, (int)wbits);
  bprintf(out, "init=%s\n", zret(rc));
  if (rc != Z_OK) {
    bfree(&comp);
    return;
  }

  unsigned char obuf[16384];
  buf back = {0};
  iz.next_in = comp.p + start;
  iz.avail_in = (uInt)(comp.n - start);

  /* First pass: inflate until it stops, whatever the reason. */
  int irc = Z_OK;
  while (irc == Z_OK && iz.avail_in > 0) {
    iz.next_out = obuf;
    iz.avail_out = (uInt)sizeof(obuf);
    irc = inflate(&iz, Z_NO_FLUSH);
    bput(&back, obuf, sizeof(obuf) - iz.avail_out);
    if (back.n > MAX_CASE_BYTES)
      break;
  }
  bprintf(out, "first.rc=%s\nfirst.total_in=%lu\nfirst.total_out=%lu\n",
          zret(irc), (unsigned long)iz.total_in, (unsigned long)iz.total_out);
  put_msg(out, "first", iz.msg);
  bprintf(out, "first.syncpoint=%d\n", inflateSyncPoint(&iz));
  size_t before_sync = back.n;

  /* The resynchronization, and how far it moved. */
  int src_rc = inflateSync(&iz);
  bprintf(out, "sync.rc=%s\nsync.total_in=%lu\nsync.avail_in=%u\n",
          zret(src_rc), (unsigned long)iz.total_in, iz.avail_in);
  put_msg(out, "sync", iz.msg);

  /* Second pass: what comes back out after the resync.  A stream that resumed
   * at the wrong marker decodes to different bytes, not to nothing. */
  if (src_rc == Z_OK) {
    int rc2 = Z_OK;
    while (rc2 == Z_OK) {
      iz.next_out = obuf;
      iz.avail_out = (uInt)sizeof(obuf);
      rc2 = inflate(&iz, Z_NO_FLUSH);
      bput(&back, obuf, sizeof(obuf) - iz.avail_out);
      if (iz.avail_in == 0 && rc2 != Z_STREAM_END)
        break;
      if (back.n > MAX_CASE_BYTES)
        break;
    }
    bprintf(out, "second.rc=%s\nsecond.total_out=%lu\nsecond.gained=%zu\n",
            zret(rc2), (unsigned long)iz.total_out, back.n - before_sync);
    put_msg(out, "second", iz.msg);
  }

  /* Recovered bytes, and whether they are a tail of the original.  Both matter:
   * the digest catches a wrong resume point, the suffix flag says whether the
   * recovery was coherent at all. */
  put_bytes(out, "recovered", back.p, back.n);
  int suffix = back.n <= src->len && back.n > 0 &&
               memcmp(src->data + src->len - back.n, back.p, back.n) == 0;
  bprintf(out, "recovered.is_suffix=%s\n", suffix ? "yes" : "no");
  bprintf(out, "end=%s\n", zret(inflateEnd(&iz)));
  bfree(&back);
  bfree(&comp);
}

/* ---------------------------------------------------------------- op: error
 *
 * Corrupt a valid stream in a specific way and record exactly how the library
 * complains: the return code, the message string, and how many bytes it consumed
 * before noticing.  Error behavior is part of the interface -- callers branch on
 * Z_DATA_ERROR versus Z_BUF_ERROR -- and it is where a rewrite that "works"
 * usually differs most.
 */
static void op_error(buf *out, char **f, int nf) {
  const blob *src = corpus(arg_long(f, nf, 2, 0));
  const char *kind = nf > 3 ? f[3] : "flipbyte";
  long offset = arg_long(f, nf, 4, 0);
  long wbits = arg_long(f, nf, 5, 15);

  dparams dp;
  dparams_init(&dp);
  dp.level = 6;
  dp.window_bits = wbits;
  buf comp = {0};
  z_stream dst;
  run_deflate(&dp, src->data, src->len, &comp, &dst, NULL);
  if (comp.n == 0) {
    bputs(out, "rc=Z_STREAM_ERROR\nnote=empty-stream\n");
    bfree(&comp);
    return;
  }

  /* Build the corrupted variant. */
  buf bad = {0};
  bput(&bad, comp.p, comp.n);
  size_t pos = (size_t)(offset < 0 ? 0 : offset);
  if (pos >= bad.n)
    pos = bad.n - 1;

  if (strcmp(kind, "flipbyte") == 0) {
    bad.p[pos] ^= 0xff;
  } else if (strcmp(kind, "flipbit") == 0) {
    bad.p[pos] ^= 0x01;
  } else if (strcmp(kind, "truncate") == 0) {
    size_t keep = bad.n > pos ? bad.n - pos : 0;
    bad.n = keep;
  } else if (strcmp(kind, "truncate-head") == 0) {
    bad.n = pos;
  } else if (strcmp(kind, "zero") == 0) {
    bad.p[pos] = 0;
  } else if (strcmp(kind, "append") == 0) {
    unsigned char junk[8] = {0xde, 0xad, 0xbe, 0xef, 0x00, 0x11, 0x22, 0x33};
    bput(&bad, junk, sizeof(junk));
  } else if (strcmp(kind, "checksum") == 0) {
    /* Corrupt the trailing adler32/crc32 only, so the compressed data itself
     * is valid and only the audit check fails. */
    if (bad.n >= 4)
      bad.p[bad.n - 1] ^= 0x01;
  } else if (strcmp(kind, "header") == 0) {
    bad.p[0] ^= 0x0f;
  } else if (strcmp(kind, "empty") == 0) {
    bad.n = 0;
  } else if (strcmp(kind, "swap") == 0) {
    if (bad.n >= 2) {
      size_t other = (pos + 1) % bad.n;
      unsigned char t = bad.p[pos];
      bad.p[pos] = bad.p[other];
      bad.p[other] = t;
    }
  }

  char hex[65];
  digest_hex(bad.p, bad.n, hex);
  bprintf(out, "input.len=%zu\ninput.sha=%s\n", bad.n, hex);

  z_stream iz;
  memset(&iz, 0, sizeof(iz));
  int rc = inflateInit2(&iz, (int)wbits);
  if (rc != Z_OK) {
    bprintf(out, "init=%s\n", zret(rc));
    bfree(&comp);
    bfree(&bad);
    return;
  }
  unsigned char obuf[16384];
  buf back = {0};
  iz.next_in = bad.p;
  iz.avail_in = (uInt)bad.n;
  int irc = Z_OK;
  long guard = 0;
  /* Drive to a terminal answer rather than stopping when the input runs out.
   * A stream whose trailing checksum was corrupted consumes every byte and
   * only then reports Z_DATA_ERROR, so a loop that breaks on "no input left"
   * records Z_OK and grades nothing.  Once the input is exhausted, inflate is
   * called with avail_in == 0 until it stops making progress, which is how it
   * reports the verdict on the stream as a whole. */
  int drained = 0;
  while (guard++ < 100000) {
    iz.next_out = obuf;
    iz.avail_out = (uInt)sizeof(obuf);
    size_t before_in = iz.avail_in;
    irc = inflate(&iz, Z_NO_FLUSH);
    size_t produced = sizeof(obuf) - iz.avail_out;
    bput(&back, obuf, produced);
    if (irc != Z_OK)
      break;
    if (iz.avail_in == 0) {
      /* No input left.  Allow exactly one more no-progress call to elicit the
       * final code, then stop: a second one would spin. */
      if (produced == 0 && before_in == 0) {
        if (drained++)
          break;
      }
    }
  }
  bprintf(out, "rc=%s\nconsumed=%lu\nproduced=%lu\n", zret(irc),
          (unsigned long)iz.total_in, (unsigned long)iz.total_out);
  put_msg(out, "err", iz.msg);
  /* How much correct output was recovered before the error matters: callers
   * that stream to a consumer have already forwarded it. */
  size_t prefix = back.n < src->len ? back.n : src->len;
  size_t good = 0;
  while (good < prefix && back.p[good] == src->data[good])
    good++;
  bprintf(out, "valid_prefix=%zu\nout.len=%zu\n", good, back.n);
  digest_hex(back.p, back.n, hex);
  bprintf(out, "out.sha=%s\nend=%s\n", hex, zret(inflateEnd(&iz)));
  bfree(&back);
  bfree(&bad);
  bfree(&comp);
}

/* ------------------------------------------------------------ op: inflateback
 *
 * inflateBack is a separate decompressor with a callback interface, used by gzip
 * itself.  It shares the inflate tables but none of the stream loop, so it is a
 * distinct code path that a rewrite can easily leave unimplemented while every
 * ordinary inflate case passes.
 */
typedef struct {
  const unsigned char *data;
  size_t len, pos;
  size_t chunk;
} back_in;

typedef struct {
  buf *out;
} back_out;

static unsigned back_read(void *desc, unsigned char **buffer) {
  back_in *st = desc;
  if (st->pos >= st->len)
    return 0;
  size_t take = st->len - st->pos;
  if (st->chunk && take > st->chunk)
    take = st->chunk;
  *buffer = (unsigned char *)(st->data + st->pos);
  st->pos += take;
  return (unsigned)take;
}

static int back_write(void *desc, unsigned char *data, unsigned len) {
  back_out *st = desc;
  bput(st->out, data, len);
  return 0;
}

static void op_inflateback(buf *out, char **f, int nf) {
  const blob *src = corpus(arg_long(f, nf, 2, 0));
  long in_chunk = arg_long(f, nf, 3, 0);
  long wbits = arg_long(f, nf, 4, 15);

  /* inflateBack consumes raw deflate, so the producer uses negative windowBits. */
  dparams dp;
  dparams_init(&dp);
  dp.level = 6;
  dp.window_bits = -(wbits < 0 ? -wbits : wbits);
  buf comp = {0};
  z_stream dst;
  int drc = run_deflate(&dp, src->data, src->len, &comp, &dst, NULL);
  bprintf(out, "deflate.rc=%s\ncomp.len=%zu\n", zret(drc), comp.n);

  z_stream zs;
  memset(&zs, 0, sizeof(zs));
  unsigned char *window = malloc((size_t)1 << (wbits < 0 ? -wbits : wbits));
  if (!window)
    die("out of memory");
  int rc = inflateBackInit(&zs, (int)(wbits < 0 ? -wbits : wbits), window);
  bprintf(out, "backinit=%s\n", zret(rc));
  if (rc == Z_OK) {
    back_in in_state = {comp.p, comp.n, 0, (size_t)(in_chunk > 0 ? in_chunk : 0)};
    buf produced = {0};
    back_out out_state = {&produced};
    int brc = inflateBack(&zs, back_read, &in_state, back_write, &out_state);
    bprintf(out, "inflateBack=%s\n", zret(brc));
    put_msg(out, "back", zs.msg);
    int same = (produced.n == src->len) &&
               (src->len == 0 || memcmp(produced.p, src->data, src->len) == 0);
    bprintf(out, "roundtrip=%s\n", same ? "identical" : "DIFFERENT");
    put_bytes(out, "out", produced.p, produced.n);
    bprintf(out, "backend=%s\n", zret(inflateBackEnd(&zs)));
    bfree(&produced);
  }
  free(window);
  bfree(&comp);
}

/* --------------------------------------------------------------- op: gzfile
 *
 * The gz* API is a stdio-shaped layer over gzip streams: 32 of the 88 exported
 * symbols live here.  It has its own buffering, its own error reporting through
 * gzerror, and a seek that decompresses forward.  It also writes real files, so
 * this op is the only one that touches the filesystem -- into a scratch
 * directory, with the file removed afterwards.
 */
static void op_gzfile(buf *out, char **f, int nf) {
  const blob *src = corpus(arg_long(f, nf, 2, 0));
  const char *what = nf > 3 ? f[3] : "roundtrip";
  long level = arg_long(f, nf, 4, 6);
  long arg_a = arg_long(f, nf, 5, 0);
  long arg_b = arg_long(f, nf, 6, 0);

  char path[4096];
  snprintf(path, sizeof(path), "%s/gz-%s-%ld-%ld-%ld.gz", g_scratch_dir, what,
           level, arg_a, arg_b);
  unlink(path);

  char mode[16];
  snprintf(mode, sizeof(mode), "wb%ld", level >= 0 && level <= 9 ? level : 6);
  gzFile gz = gzopen(path, mode);
  if (!gz) {
    bputs(out, "gzopen=NULL\n");
    return;
  }
  bprintf(out, "gzopen=ok\ngzdirect.write=%d\n", gzdirect(gz));

  /* Write the payload, in chunks if asked, so gzwrite's buffering is exercised
   * rather than bypassed by one large call. */
  size_t step = arg_a > 0 ? (size_t)arg_a : (src->len ? src->len : 1);
  size_t written = 0;
  int wrc = 0;
  while (written < src->len) {
    size_t take = src->len - written;
    if (take > step)
      take = step;
    wrc = gzwrite(gz, src->data + written, (unsigned)take);
    if (wrc <= 0)
      break;
    written += (size_t)wrc;
    if (arg_b == 1)
      gzflush(gz, Z_SYNC_FLUSH);
    else if (arg_b == 2)
      gzflush(gz, Z_FULL_FLUSH);
  }
  bprintf(out, "written=%zu\nlast_write=%d\n", written, wrc);
  if (strcmp(what, "printf") == 0) {
    /* Only conversions C's printf and java.util.Formatter agree on: %s and %d
     * with no length modifiers.  The contract says the Java gzprintf follows
     * Formatter, so a %lu here would be asking the port to implement a C format
     * parser -- a different task than porting zlib. */
    int prc = gzprintf(gz, "|%s|%d|%d|", "tail", 42, (int)src->len);
    bprintf(out, "gzprintf=%d\n", prc);
  }
  if (strcmp(what, "putc") == 0) {
    bprintf(out, "gzputc=%d\n", gzputc(gz, 0x41));
    bprintf(out, "gzputs=%d\n", gzputs(gz, "puts-tail\n"));
  }
  int off_w = (int)gzoffset(gz);
  bprintf(out, "gzoffset.write=%d\ngztell.write=%ld\n", off_w,
          (long)gztell(gz));
  bprintf(out, "gzclose=%s\n", zret(gzclose(gz)));

  /* The compressed file's bytes are part of the comparison: gzopen's header,
   * including its OS byte and zero mtime, must match the reference. */
  FILE *fh = fopen(path, "rb");
  if (fh) {
    buf raw = {0};
    unsigned char tmp[8192];
    size_t got;
    while ((got = fread(tmp, 1, sizeof(tmp), fh)) > 0)
      bput(&raw, tmp, got);
    fclose(fh);
    put_bytes(out, "file", raw.p, raw.n);
    bfree(&raw);
  } else {
    /* Unreachable if gzclose above succeeded, which is the point of recording
     * it: a silent branch is a branch where the two halves of the pair can
     * disagree without anyone noticing.  Both sides emit this sentinel. */
    bputs(out, "file=UNREADABLE\n");
  }

  /* Read it back through whichever accessor the case names. */
  gz = gzopen(path, "rb");
  if (!gz) {
    bputs(out, "reopen=NULL\n");
    unlink(path);
    return;
  }
  bprintf(out, "reopen=ok\ngzdirect.read=%d\n", gzdirect(gz));
  buf back = {0};

  if (strcmp(what, "gets") == 0) {
    char line[512];
    int lines = 0;
    while (gzgets(gz, line, (int)sizeof(line)) != Z_NULL) {
      bput(&back, line, strlen(line));
      lines++;
      if (lines > 20000)
        break;
    }
    bprintf(out, "lines=%d\n", lines);
  } else if (strcmp(what, "getc") == 0) {
    int c, count = 0;
    while ((c = gzgetc(gz)) != -1) {
      unsigned char b = (unsigned char)c;
      bput(&back, &b, 1);
      count++;
      /* Push one byte back and re-read it, every 1000 bytes. */
      if (count % 1000 == 0) {
        gzungetc(c, gz);
        int again = gzgetc(gz);
        if (again != c)
          bprintf(out, "ungetc.mismatch@%d\n", count);
      }
      if (count > 200000)
        break;
    }
    bprintf(out, "getc.count=%d\n", count);
  } else if (strcmp(what, "seek") == 0) {
    /* Seek forward, read, rewind, read again: gzseek decompresses forward and
     * gzrewind restarts the stream. */
    z_off_t target = (z_off_t)(src->len / 3);
    z_off_t landed = gzseek(gz, target, SEEK_SET);
    bprintf(out, "gzseek=%ld\ngztell=%ld\n", (long)landed, (long)gztell(gz));
    unsigned char tmp[4096];
    int got = gzread(gz, tmp, (unsigned)sizeof(tmp));
    bprintf(out, "after_seek.read=%d\n", got);
    if (got > 0)
      put_bytes(out, "after_seek", tmp, (size_t)got);
    /* Sequenced deliberately, one call per statement.  These three lines used to
     * be one bprintf with gzrewind(gz) and gzeof(gz) as sibling arguments, and C
     * does not specify which of those runs first: this compiler evaluated right
     * to left, so the "after" flag was in fact read *before* the rewind, and the
     * expected output said the stream was still at EOF after being rewound.  The
     * counterpart could never reproduce it -- Java fixes argument order left to
     * right, so it rewound first and correctly reported 0 -- and the case failed
     * on the C half's undefined behaviour rather than on anything about the port.
     * Both flags are worth having, so both are read, in a stated order. */
    int eof_pre = gzeof(gz);
    int rewound = gzrewind(gz);
    int eof_post = gzeof(gz);
    bprintf(out, "gzeof.pre_rewind=%d\ngzrewind=%d\ngzeof.mid=%d\n", eof_pre,
            rewound, eof_post);
    int total = 0;
    while ((got = gzread(gz, tmp, (unsigned)sizeof(tmp))) > 0) {
      bput(&back, tmp, (size_t)got);
      total += got;
    }
    bprintf(out, "after_rewind.total=%d\n", total);
  } else if (strcmp(what, "fread") == 0) {
    unsigned char tmp[4096];
    size_t items;
    while ((items = gzfread(tmp, 4, sizeof(tmp) / 4, gz)) > 0)
      bput(&back, tmp, items * 4);
    bprintf(out, "gzfread.done=1\n");
  } else {
    unsigned char tmp[4096];
    size_t rstep = arg_a > 0 && (size_t)arg_a < sizeof(tmp)
                       ? (size_t)arg_a
                       : sizeof(tmp);
    int got;
    while ((got = gzread(gz, tmp, (unsigned)rstep)) > 0)
      bput(&back, tmp, (size_t)got);
    bprintf(out, "last_read=%d\n", got);
  }

  int errnum = 0;
  const char *msg = gzerror(gz, &errnum);
  bprintf(out, "gzeof=%d\ngzerror.num=%d\ngzerror.msg=%s\n", gzeof(gz), errnum,
          msg ? msg : "(null)");
  bprintf(out, "gztell.read=%ld\ngzoffset.read=%ld\n", (long)gztell(gz),
          (long)gzoffset(gz));
  gzclearerr(gz);
  bprintf(out, "after_clearerr.eof=%d\n", gzeof(gz));
  bprintf(out, "close_r=%s\n", zret(gzclose_r(gz)));

  int same = (back.n == src->len) &&
             (src->len == 0 || memcmp(back.p, src->data, src->len) == 0);
  put_bytes(out, "out", back.p, back.n);
  bprintf(out, "roundtrip=%s\n", same ? "identical" : "DIFFERENT");
  bfree(&back);
  unlink(path);
}

/* ----------------------------------------------------------------- op: abi
 *
 * The parts of the interface that are not behavior: the numeric values of the
 * public constants, the strings returned by zlibVersion / zError, the
 * compile-flag word, and what every entry point does when it is handed a stream
 * it never initialized or an argument it must refuse.
 *
 * The constants matter because a compiler inlines them.  C bakes a Z_* value
 * into every caller at its use site, and so does javac for a `static final int`
 * -- so a renumbered constant is the same silent break in both languages: an
 * already-compiled consumer keeps the old number and quietly means something
 * else.  No round-trip test can see that.
 */
static void op_abi(buf *out, char **f, int nf) {
  const char *what = nf > 2 ? f[2] : "constants";

  if (strcmp(what, "constants") == 0) {
    /* Every public constant, by value.  A renumbered Z_* constant is an ABI
     * break that a source rebuild hides and a binary consumer does not. */
#define K(name) bprintf(out, #name "=%d\n", (int)(name))
    K(Z_NO_FLUSH); K(Z_PARTIAL_FLUSH); K(Z_SYNC_FLUSH); K(Z_FULL_FLUSH);
    K(Z_FINISH); K(Z_BLOCK); K(Z_TREES);
    K(Z_OK); K(Z_STREAM_END); K(Z_NEED_DICT); K(Z_ERRNO); K(Z_STREAM_ERROR);
    K(Z_DATA_ERROR); K(Z_MEM_ERROR); K(Z_BUF_ERROR); K(Z_VERSION_ERROR);
    K(Z_NO_COMPRESSION); K(Z_BEST_SPEED); K(Z_BEST_COMPRESSION);
    K(Z_DEFAULT_COMPRESSION);
    K(Z_FILTERED); K(Z_HUFFMAN_ONLY); K(Z_RLE); K(Z_FIXED);
    K(Z_DEFAULT_STRATEGY);
    K(Z_BINARY); K(Z_TEXT); K(Z_ASCII); K(Z_UNKNOWN);
    K(Z_DEFLATED); K(Z_NULL); K(MAX_WBITS); K(MAX_MEM_LEVEL);
#undef K
    bprintf(out, "ZLIB_VERSION=%s\n", ZLIB_VERSION);
    bprintf(out, "ZLIB_VERNUM=0x%x\n", (unsigned)ZLIB_VERNUM);
    bprintf(out, "ZLIB_VER_MAJOR=%d\nZLIB_VER_MINOR=%d\nZLIB_VER_REVISION=%d\n",
            ZLIB_VER_MAJOR, ZLIB_VER_MINOR, ZLIB_VER_REVISION);
    bprintf(out, "ZLIB_VER_SUBREVISION=%d\n", ZLIB_VER_SUBREVISION);
    return;
  }

  if (strcmp(what, "runtime") == 0) {
    bprintf(out, "zlibVersion=%s\n", zlibVersion());
    bprintf(out, "zlibCompileFlags=0x%lx\n", (unsigned long)zlibCompileFlags());
    /* The compile-flag bit fields, decoded: these describe the ABI the binary
     * was built with, and a consumer can read them at runtime. */
    unsigned long flags = (unsigned long)zlibCompileFlags();
    bprintf(out, "flags.sizeof_uInt=%lu\nflags.sizeof_uLong=%lu\n",
            (flags & 0x3), ((flags >> 2) & 0x3));
    bprintf(out, "flags.sizeof_voidpf=%lu\nflags.sizeof_z_off_t=%lu\n",
            ((flags >> 4) & 0x3), ((flags >> 6) & 0x3));
    bprintf(out, "flags.debug=%lu\nflags.asm=%lu\nflags.winapi=%lu\n",
            ((flags >> 8) & 1), ((flags >> 9) & 1), ((flags >> 10) & 1));
    bprintf(out, "flags.builtin_memcpy=%lu\nflags.dynamic_crc=%lu\n",
            ((flags >> 16) & 1), ((flags >> 17) & 1));
    bprintf(out, "flags.no_gzfile=%lu\nflags.no_gzip=%lu\n",
            ((flags >> 20) & 1), ((flags >> 21) & 1));
    bprintf(out, "flags.pkzip_bug=%lu\nflags.fastest=%lu\n",
            ((flags >> 24) & 1), ((flags >> 25) & 1));
    for (int code = 2; code >= -6; code--)
      bprintf(out, "zError(%d)=%s\n", code, zError(code));
    /* get_crc_table must return a 256-entry table whose contents are fixed by
     * the CRC-32 polynomial; the first and last entries pin it. */
    const z_crc_t *table = get_crc_table();
    bprintf(out, "crc_table[0]=%lu\ncrc_table[1]=%lu\ncrc_table[255]=%lu\n",
            (unsigned long)table[0], (unsigned long)table[1],
            (unsigned long)table[255]);
    uLong sum = 0;
    for (int i = 0; i < 256; i++)
      sum += (uLong)table[i];
    bprintf(out, "crc_table.sum=%lu\n", (unsigned long)sum);
    return;
  }

  if (strcmp(what, "defensive") == 0) {
    /* What every entry point does when the stream it is handed was never
     * initialized, and again after it was ended.  zlib checks its own state on
     * every call and answers Z_STREAM_ERROR; that is a documented promise and
     * callers branch on it.
     *
     * The stream here is zeroed rather than NULL on purpose.  A NULL z_stream*
     * has no counterpart in a language with no null receiver, so grading it
     * would pin an expectation nothing could match.  A zeroed, never-initialized
     * stream does have one -- a freshly constructed Deflater whose init was
     * never called -- and it asks the same question of the same code: the state
     * check, not the pointer check.
     *
     * A rewrite is likely to trust its own state here and crash or throw
     * instead.  Either is visible: a crash aborts the process and the harness
     * records it against exactly this case, and an exception escaping into the
     * probe does the same. */
    z_stream zs;
    memset(&zs, 0, sizeof(zs));
    bprintf(out, "deflate=%s\n", zret(deflate(&zs, Z_NO_FLUSH)));
    bprintf(out, "deflateEnd=%s\n", zret(deflateEnd(&zs)));
    bprintf(out, "deflateReset=%s\n", zret(deflateReset(&zs)));
    bprintf(out, "deflateResetKeep=%s\n", zret(deflateResetKeep(&zs)));
    bprintf(out, "deflateParams=%s\n", zret(deflateParams(&zs, 6, 0)));
    bprintf(out, "deflateSetDictionary=%s\n",
            zret(deflateSetDictionary(&zs, (const Bytef *)"x", 1)));
    {
      uInt dlen = 0;
      bprintf(out, "deflateGetDictionary=%s\n",
              zret(deflateGetDictionary(&zs, Z_NULL, &dlen)));
    }
    bprintf(out, "deflatePrime=%s\n", zret(deflatePrime(&zs, 1, 0)));
    bprintf(out, "deflateTune=%s\n", zret(deflateTune(&zs, 8, 8, 8, 8)));
    bprintf(out, "deflateSetHeader=%s\n", zret(deflateSetHeader(&zs, Z_NULL)));
    {
      unsigned pending = 0;
      int bits = 0;
      bprintf(out, "deflatePending=%s\n",
              zret(deflatePending(&zs, &pending, &bits)));
    }
    /* deflateBound answers even on an unusable stream: without parameters it has
     * to return the larger bound plus a wrapper, because a caller sizing a
     * buffer needs a number it can trust rather than an error. */
    bprintf(out, "deflateBound.0=%lu\ndeflateBound.1000=%lu\n",
            (unsigned long)deflateBound(&zs, 0),
            (unsigned long)deflateBound(&zs, 1000));
    {
      z_stream dst;
      memset(&dst, 0, sizeof(dst));
      bprintf(out, "deflateCopy=%s\n", zret(deflateCopy(&dst, &zs)));
    }

    z_stream iz;
    memset(&iz, 0, sizeof(iz));
    bprintf(out, "inflate=%s\n", zret(inflate(&iz, Z_NO_FLUSH)));
    bprintf(out, "inflateEnd=%s\n", zret(inflateEnd(&iz)));
    bprintf(out, "inflateReset=%s\n", zret(inflateReset(&iz)));
    bprintf(out, "inflateReset2=%s\n", zret(inflateReset2(&iz, 15)));
    bprintf(out, "inflateResetKeep=%s\n", zret(inflateResetKeep(&iz)));
    bprintf(out, "inflateSetDictionary=%s\n",
            zret(inflateSetDictionary(&iz, (const Bytef *)"x", 1)));
    {
      uInt dlen = 0;
      bprintf(out, "inflateGetDictionary=%s\n",
              zret(inflateGetDictionary(&iz, Z_NULL, &dlen)));
    }
    bprintf(out, "inflateSync=%s\n", zret(inflateSync(&iz)));
    bprintf(out, "inflatePrime=%s\n", zret(inflatePrime(&iz, 1, 0)));
    bprintf(out, "inflateGetHeader=%s\n", zret(inflateGetHeader(&iz, Z_NULL)));
    bprintf(out, "inflateValidate=%s\n", zret(inflateValidate(&iz, 1)));
    bprintf(out, "inflateUndermine=%s\n", zret(inflateUndermine(&iz, 1)));
    bprintf(out, "inflateSyncPoint=%s\n", zret(inflateSyncPoint(&iz)));
    /* Both of these report an unusable stream with a sentinel rather than a
     * code, and the sentinel is part of the contract: -65536 is (-1 << 16),
     * which unpacks to a back distance of -1. */
    bprintf(out, "inflateMark=%ld\n", inflateMark(&iz));
    bprintf(out, "inflateCodesUsed=%ld\n", (long)inflateCodesUsed(&iz));
    {
      z_stream dst;
      memset(&dst, 0, sizeof(dst));
      bprintf(out, "inflateCopy=%s\n", zret(inflateCopy(&dst, &iz)));
    }
    {
      z_stream bs;
      memset(&bs, 0, sizeof(bs));
      bprintf(out, "inflateBackEnd=%s\n", zret(inflateBackEnd(&bs)));
      /* A missing window and an out-of-range windowBits are both refused.  A
       * window that is merely too short is not asked about here: C cannot see an
       * array's length and a JVM cannot help seeing it, so the two sides would
       * have to disagree.  The contract requires the Java side to reject it --
       * a bounds-check failure is not an acceptable answer -- and structure.py
       * grades that separately. */
      unsigned char window[1 << 15];
      bprintf(out, "inflateBackInit.nullwin=%s\n",
              zret(inflateBackInit(&bs, 15, Z_NULL)));
      bprintf(out, "inflateBackInit.wbits7=%s\n",
              zret(inflateBackInit(&bs, 7, window)));
      bprintf(out, "inflateBackInit.wbits16=%s\n",
              zret(inflateBackInit(&bs, 16, window)));
    }

    /* After a clean end, every call must refuse again: end() releases the state
     * and a stream that kept working afterwards would be using freed memory. */
    memset(&zs, 0, sizeof(zs));
    int irc = deflateInit2(&zs, 6, Z_DEFLATED, 15, 8, Z_DEFAULT_STRATEGY);
    bprintf(out, "after_end.init=%s\n", zret(irc));
    if (irc == Z_OK) {
      bprintf(out, "after_end.end=%s\n", zret(deflateEnd(&zs)));
      bprintf(out, "after_end.deflate=%s\n", zret(deflate(&zs, Z_FINISH)));
      bprintf(out, "after_end.reset=%s\n", zret(deflateReset(&zs)));
      bprintf(out, "after_end.end2=%s\n", zret(deflateEnd(&zs)));
    }
    memset(&iz, 0, sizeof(iz));
    irc = inflateInit2(&iz, 15);
    bprintf(out, "after_end.inflate_init=%s\n", zret(irc));
    if (irc == Z_OK) {
      bprintf(out, "after_end.inflate_end=%s\n", zret(inflateEnd(&iz)));
      bprintf(out, "after_end.inflate=%s\n", zret(inflate(&iz, Z_NO_FLUSH)));
      bprintf(out, "after_end.inflate_reset=%s\n", zret(inflateReset(&iz)));
      bprintf(out, "after_end.inflate_mark=%ld\n", inflateMark(&iz));
    }

    /* Empty buffers with a zero length: the documented way to query a
     * checksum's initial value, and the one call in the library that takes a
     * null pointer as data rather than as a mistake. */
    bprintf(out, "crc32.null=%lu\n", (unsigned long)crc32(0, Z_NULL, 0));
    bprintf(out, "adler32.null=%lu\n", (unsigned long)adler32(0, Z_NULL, 0));
    bprintf(out, "crc32.seeded=%lu\n", (unsigned long)crc32(12345, Z_NULL, 0));
    bprintf(out, "adler32.seeded=%lu\n",
            (unsigned long)adler32(99999, Z_NULL, 0));

    /* The one-shot helpers must report a destination that is too small rather
     * than writing past it. */
    {
      static const Bytef payload[64] = {0};
      Bytef small[4];
      uLongf small_len = sizeof(small);
      bprintf(out, "compress.tight=%s\n",
              zret(compress(small, &small_len, payload, sizeof(payload))));
      uLongf zero_len = 0;
      bprintf(out, "compress.zero=%s\n",
              zret(compress(small, &zero_len, payload, sizeof(payload))));
      uLongf ulen = sizeof(small);
      bprintf(out, "uncompress.garbage=%s\n",
              zret(uncompress(small, &ulen, payload, sizeof(payload))));
    }

    /* A path that cannot be opened must come back as a null handle, not as a
     * handle that fails later. */
    bprintf(out, "gzopen.missing=%s\n",
            gzopen("/nonexistent/path/x.gz", "rb") ? "ok" : "NULL");
    bprintf(out, "gzopen.badmode=%s\n",
            gzopen("/nonexistent/path/x.gz", "qq") ? "ok" : "NULL");
    return;
  }

  if (strcmp(what, "badargs") == 0) {
    /* Out-of-range parameters must be rejected, not clamped: a caller that
     * passes level 10 has a bug and needs to hear about it. */
    struct { int level, wbits, mem, strat; } bad[] = {
      {10, 15, 8, 0}, {-2, 15, 8, 0}, {-100, 15, 8, 0},
      {6, 16, 8, 0}, {6, 7, 8, 0}, {6, 0, 8, 0}, {6, 1, 8, 0},
      {6, 48, 8, 0}, {6, 15, 0, 0}, {6, 15, 10, 0}, {6, 15, -1, 0},
      {6, 15, 8, 5}, {6, 15, 8, -1}, {6, 15, 8, 99},
      {6, -16, 8, 0}, {6, -7, 8, 0}, {6, 32, 8, 0},
    };
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
      z_stream zs;
      memset(&zs, 0, sizeof(zs));
      int rc = deflateInit2(&zs, bad[i].level, Z_DEFLATED, bad[i].wbits,
                            bad[i].mem, bad[i].strat);
      bprintf(out, "deflateInit2(%d,%d,%d,%d)=%s\n", bad[i].level, bad[i].wbits,
              bad[i].mem, bad[i].strat, zret(rc));
      if (rc == Z_OK)
        deflateEnd(&zs);
    }
    int iwbits[] = {16, 7, 0, 1, 48, 49, -16, -7, 32, 47, 31, 8, -8, 15, 40};
    for (size_t i = 0; i < sizeof(iwbits) / sizeof(iwbits[0]); i++) {
      z_stream zs;
      memset(&zs, 0, sizeof(zs));
      int rc = inflateInit2(&zs, iwbits[i]);
      bprintf(out, "inflateInit2(%d)=%s\n", iwbits[i], zret(rc));
      if (rc == Z_OK)
        inflateEnd(&zs);
    }
    /* An unsupported method must be refused. */
    for (int method = 0; method <= 9; method++) {
      if (method == Z_DEFLATED)
        continue;
      z_stream zs;
      memset(&zs, 0, sizeof(zs));
      int rc = deflateInit2(&zs, 6, method, 15, 8, 0);
      bprintf(out, "method(%d)=%s\n", method, zret(rc));
      if (rc == Z_OK)
        deflateEnd(&zs);
    }
    /* A version string that does not match must be refused: this is the
     * mechanism that protects a caller compiled against a different header. */
    z_stream zs;
    memset(&zs, 0, sizeof(zs));
    bprintf(out, "version.mismatch=%s\n",
            zret(deflateInit_(&zs, 6, "0.0.0", (int)sizeof(z_stream))));
    memset(&zs, 0, sizeof(zs));
    bprintf(out, "size.mismatch=%s\n",
            zret(deflateInit_(&zs, 6, ZLIB_VERSION, 1)));
    memset(&zs, 0, sizeof(zs));
    bprintf(out, "inflate.version.mismatch=%s\n",
            zret(inflateInit_(&zs, "0.0.0", (int)sizeof(z_stream))));
    memset(&zs, 0, sizeof(zs));
    bprintf(out, "inflate.size.mismatch=%s\n",
            zret(inflateInit_(&zs, ZLIB_VERSION, 1)));
    /* The major-version-only match that zlib documents as acceptable. */
    memset(&zs, 0, sizeof(zs));
    int ok = deflateInit_(&zs, 6, "1.0.0", (int)sizeof(z_stream));
    bprintf(out, "version.major_only=%s\n", zret(ok));
    if (ok == Z_OK)
      deflateEnd(&zs);
    return;
  }

  bputs(out, "unknown-abi-subop\n");
}

/* ----------------------------------------------------------------- op: alloc
 *
 * zalloc/zfree/opaque are caller-supplied hooks, and zlib promises to route its
 * buffer allocations through them.  Embedded and sandboxed consumers rely on
 * this to keep the library out of the system heap, so the hooks survive the port
 * as ZStream.Allocator / ZStream.Deallocator.
 *
 * What is recorded here is deliberately count-free, and that is the one place
 * this op had to be weakened for the port.  C allocates five blocks at
 * deflateInit2 -- the state struct and the window, prev, head and pending
 * buffers -- and a JVM port cannot allocate a state *object* through a hook that
 * returns primitive arrays.  Its count is necessarily different, and grading the
 * reference's number would fail every correct port.  So the graded facts are the
 * ones that are true of any implementation honoring the contract:
 *
 *   - the hook was used at all, rather than quietly ignored;
 *   - the opaque reference arrived unchanged, which is what a caller keeping an
 *     arena there depends on;
 *   - every block handed out came back, so nothing leaked;
 *   - the compressed bytes are identical, which is the whole point;
 *   - and a hook that refuses produces Z_MEM_ERROR rather than a crash.
 *
 * That last one is why fail_after is only ever 0 or 1 in the catalog: "fail the
 * first allocation" is a question every implementation answers the same way,
 * while "fail the fifth" depends on how many blocks it asks for.  An
 * implementation that ignores the hook is caught precisely here -- it would
 * sail through a case whose whole point is that allocation was refused.
 */
typedef struct {
  long calls;                 /* hook entries, including the refused ones */
  long allocs, frees;
  size_t live, peak;
  long fail_after;
  int magic_ok;
} allocstat;

static allocstat g_alloc;
static const unsigned OPAQUE_MAGIC = 0x5a5ac0deu;

static void *probe_alloc(void *opaque, unsigned items, unsigned size) {
  /* The opaque pointer must arrive unchanged; a rewrite that drops it breaks
   * every caller that keeps an arena there. */
  if (opaque == &OPAQUE_MAGIC)
    g_alloc.magic_ok = 1;
  else
    g_alloc.magic_ok = 0;
  g_alloc.calls++;
  if (g_alloc.fail_after > 0 && g_alloc.calls >= g_alloc.fail_after)
    return NULL;
  size_t bytes = (size_t)items * size;
  void *p = malloc(bytes ? bytes : 1);
  if (p) {
    g_alloc.allocs++;
    g_alloc.live += bytes;
    if (g_alloc.live > g_alloc.peak)
      g_alloc.peak = g_alloc.live;
  }
  return p;
}

static void probe_free(void *opaque, void *address) {
  (void)opaque;
  if (address) {
    g_alloc.frees++;
    free(address);
  }
}

static void op_alloc(buf *out, char **f, int nf) {
  const blob *src = corpus(arg_long(f, nf, 2, 0));
  long fail_after = arg_long(f, nf, 3, 0);
  long level = arg_long(f, nf, 4, 6);
  long wbits = arg_long(f, nf, 5, 15);
  long mem = arg_long(f, nf, 6, 8);
  int inflate_side = (int)arg_long(f, nf, 7, 0);

  memset(&g_alloc, 0, sizeof(g_alloc));
  g_alloc.fail_after = fail_after;

  z_stream zs;
  memset(&zs, 0, sizeof(zs));
  zs.zalloc = probe_alloc;
  zs.zfree = probe_free;
  zs.opaque = (void *)&OPAQUE_MAGIC;

  int rc = deflateInit2(&zs, (int)level, Z_DEFLATED, (int)wbits, (int)mem,
                        Z_DEFAULT_STRATEGY);
  bprintf(out, "init=%s\nused_hook=%s\nopaque_ok=%d\n", zret(rc),
          g_alloc.calls > 0 ? "yes" : "no", g_alloc.magic_ok);
  if (rc != Z_OK) {
    /* A failed init must not have leaked: everything it took, it gave back. */
    bprintf(out, "outcome=init-failed\nleaked=%s\n",
            g_alloc.allocs == g_alloc.frees ? "no" : "YES");
    return;
  }

  buf comp = {0};
  unsigned char obuf[16384];
  zs.next_in = (Bytef *)src->data;
  zs.avail_in = (uInt)src->len;
  int drc;
  do {
    zs.next_out = obuf;
    zs.avail_out = (uInt)sizeof(obuf);
    drc = deflate(&zs, Z_FINISH);
    bput(&comp, obuf, sizeof(obuf) - zs.avail_out);
  } while (drc == Z_OK);
  /* The compressed bytes, in full: the hooks must not change what the
   * compressor decides, so this is the same stream op_deflate would produce. */
  bprintf(out, "deflate=%s\n", zret(drc));
  put_bytes(out, "comp", comp.p, comp.n);
  bprintf(out, "end=%s\n", zret(deflateEnd(&zs)));
  bprintf(out, "balanced=%s\noutcome=%s\n",
          g_alloc.allocs == g_alloc.frees ? "yes" : "NO",
          drc == Z_STREAM_END ? "complete" : "incomplete");

  if (inflate_side) {
    memset(&g_alloc, 0, sizeof(g_alloc));
    g_alloc.fail_after = fail_after;
    z_stream iz;
    memset(&iz, 0, sizeof(iz));
    iz.zalloc = probe_alloc;
    iz.zfree = probe_free;
    iz.opaque = (void *)&OPAQUE_MAGIC;
    int irc = inflateInit2(&iz, (int)wbits);
    bprintf(out, "inflate.init=%s\n", zret(irc));
    if (irc == Z_OK) {
      buf back = {0};
      /* A deliberately small output buffer, and it is load-bearing.  The window
       * is the one allocation a JVM port has any business asking the hook for,
       * and inflate only allocates it when the output spans more than one call.
       * At 256 bytes every payload here but the shortest spans more than one
       * call, so the flag below asks a question both halves can answer; through
       * a buffer larger than any payload it would ask about C's state block, the
       * allocation a port whose state *is* an object cannot make.  See
       * catalog.build_alloc. */
      unsigned char ibuf[256];
      iz.next_in = comp.p;
      iz.avail_in = (uInt)comp.n;
      int rc2 = Z_OK;
      while (rc2 == Z_OK) {
        iz.next_out = ibuf;
        iz.avail_out = (uInt)sizeof(ibuf);
        uInt in_before = iz.avail_in;
        rc2 = inflate(&iz, Z_NO_FLUSH);
        size_t produced = sizeof(ibuf) - iz.avail_out;
        bput(&back, ibuf, produced);
        /* Stop on a call that neither consumed nor produced.  Stopping once
         * avail_in hits zero would be safe only with a buffer bigger than any
         * payload: with a small one, "input exhausted" is not "finished",
         * inflate still has buffered output to hand back, and stopping there
         * would truncate the answer and report it as a mismatch. */
        if (produced == 0 && iz.avail_in == in_before)
          break;
      }
      /* Asked after the round trip, not after the init, and that placement is
       * the whole point.  inflateInit2 here allocates the state block through
       * the hook immediately; the window it defers to updatewindow, on first
       * need.  A JVM port has no state block to ask for -- `new Inflater()` is
       * the state -- so its first hook call is that deferred window, and it
       * arrives during inflate rather than during init.  Reading the flag at
       * init would therefore have demanded a hook call that only C has a reason
       * to make, and would have failed every correct port on a difference in
       * where the memory lives rather than in who supplied it.  Read here, both
       * halves answer the same portable question: the window came from the
       * caller's allocator.  See catalog.build_alloc.
       *
       * The init-failure branch below does not print it, for the same reason in
       * reverse: a refused inflateInit2 is a hook call C makes and a JVM port
       * does not.  fail_after is never 1 on this side of a case, so nothing is
       * left ungraded -- the refusal is graded on the deflate side, which
       * allocates everything it needs while still inside its init. */
      /* Printed only when the round trip needed a window, which is the only
       * allocation the two halves can agree on.  A payload that fits in one
       * output buffer never needs one, so on such a case this asks nothing and
       * says nothing rather than recording C's state-block call as the answer.
       * The condition is computed from the payload length, which both halves
       * know before they start, so the two outputs stay line-for-line
       * identical. */
      if (src->len > sizeof(ibuf))
        bprintf(out, "inflate.used_hook=%s\n",
                g_alloc.calls > 0 ? "yes" : "no");
      int same = (back.n == src->len) &&
                 (src->len == 0 || memcmp(back.p, src->data, src->len) == 0);
      bprintf(out, "inflate.rc=%s\ninflate.match=%s\n", zret(rc2),
              same ? "identical" : "DIFFERENT");
      bprintf(out, "inflate.end=%s\n", zret(inflateEnd(&iz)));
      bprintf(out, "inflate.balanced=%s\n",
              g_alloc.allocs == g_alloc.frees ? "yes" : "NO");
      bfree(&back);
    } else {
      bprintf(out, "inflate.leaked=%s\n",
              g_alloc.allocs == g_alloc.frees ? "no" : "YES");
    }
  }
  bfree(&comp);
}

/* -------------------------------------------------------------- list-keys
 *
 * Not a case: the self-description structure.py asks for before it trusts a
 * linkage mode.  `--list-keys` prints the ops this probe knows, one per line,
 * and then a handful of values it could only have obtained from the library.
 *
 * The mode exists for Probe.java's sake -- on a JVM "the program started" is not
 * "the jar linked", because a class is resolved only when something touches it,
 * so the counterpart forces every public type to load and then uses it.  This
 * half has nothing to force: its 88 symbols were bound when it was linked, and a
 * missing one would have failed the link.  It is carried anyway, printing the
 * same lines in the same order, because the two halves are a matched pair and
 * neither may answer a question the other cannot.  Keeping it here also means
 * the expected output is producible from the reference: if the line set ever
 * needs to be compared rather than merely counted, the oracle for it already
 * exists.
 *
 * `types=` and the `type=` lines are the JVM's class names, which have no C
 * counterpart at all.  Rather than drop them -- which would leave the two
 * outputs different -- they are printed from the same fixed list, and the C half
 * accompanies each with the address of a function belonging to that type, so the
 * list is not merely a string table here either.
 */
static const char *const g_ops[] = {
    "deflate",      "inflate", "bound",        "oneshot", "checksum",
    "statechange",  "gzheader", "syncrecover", "inflatestate", "error",
    "inflateback",  "gzfile",  "abi",          "alloc",
};

static void list_keys(void) {
  buf out = {0};
  size_t i;

  for (i = 0; i < sizeof(g_ops) / sizeof(g_ops[0]); i++)
    bprintf(&out, "op=%s\n", g_ops[i]);

  /* The eleven names Probe.java loads, each paired with a function that belongs
   * to it.  Taking the address through a volatile sink keeps the compiler from
   * discarding the reference, so a build against a header that lost one of these
   * fails to compile rather than printing a name it cannot back. */
  {
    static const char *const types[] = {
        "org.zlib.Zlib",         "org.zlib.ZStream",
        "org.zlib.Deflater",     "org.zlib.Inflater",
        "org.zlib.InflateBack",  "org.zlib.GzHeader",
        "org.zlib.GzFile",       "org.zlib.ZStream$Allocator",
        "org.zlib.ZStream$Deallocator", "org.zlib.InflateBack$In",
        "org.zlib.InflateBack$Out",
    };
    void *const anchors[] = {
        (void *)(size_t)zlibVersion,        (void *)(size_t)deflateSetDictionary,
        (void *)(size_t)deflateInit2_,      (void *)(size_t)inflateInit2_,
        (void *)(size_t)inflateBackInit_,   (void *)(size_t)deflateSetHeader,
        (void *)(size_t)gzopen,             (void *)(size_t)deflateCopy,
        (void *)(size_t)deflateEnd,         (void *)(size_t)inflateBack,
        (void *)(size_t)inflateBackEnd,
    };
    static void *volatile sink;
    size_t n = sizeof(types) / sizeof(types[0]);
    bprintf(&out, "types=%zu\n", n);
    for (i = 0; i < n; i++) {
      sink = anchors[i];
      bprintf(&out, "type=%s\n", types[i]);
    }
    (void)sink;
  }

  /* Values only the library can produce.  Recorded rather than checked: a wrong
   * checksum is the business of the checksum cases, which grade it against this
   * same reference.  What is established here is that the calls returned. */
  {
    const Bytef *check = (const Bytef *)"123456789";
    uLong len = 9;
    const z_crc_t *table = get_crc_table();
    bprintf(&out, "version=%s\n", zlibVersion());
    bprintf(&out, "vernum=0x%x\n", (unsigned)ZLIB_VERNUM);
    bprintf(&out, "flags=0x%lx\n", (unsigned long)zlibCompileFlags());
    bprintf(&out, "zError=%s\n", zError(Z_DATA_ERROR));
    bprintf(&out, "crc32=%lu\n", (unsigned long)crc32(0, check, (uInt)len));
    bprintf(&out, "adler32=%lu\n", (unsigned long)adler32(1, check, (uInt)len));
    bprintf(&out, "crc_table[8]=%lu\n", (unsigned long)table[8]);
    bprintf(&out, "compressBound=%lu\n", (unsigned long)compressBound(len));

    /* A real round trip through the streaming engine, the one-shot helpers and
     * inflateBack: the same three entry paths the counterpart walks. */
    {
      z_stream zs;
      buf comp = {0};
      unsigned char obuf[256];
      int rc;
      memset(&zs, 0, sizeof(zs));
      rc = deflateInit2(&zs, 6, Z_DEFLATED, 15, 8, Z_DEFAULT_STRATEGY);
      bprintf(&out, "deflateInit2=%s\n", zret(rc));
      if (rc == Z_OK) {
        bprintf(&out, "deflateBound=%lu\n", (unsigned long)deflateBound(&zs, len));
        int spins = 0;
        zs.next_in = (Bytef *)check;
        zs.avail_in = (uInt)len;
        /* Bounded for the same reason the counterpart's loop is: nine bytes
         * finish in one call, so a loop that keeps going is a defect rather than
         * a workload, and the bound turns it into a recorded answer. */
        do {
          zs.next_out = obuf;
          zs.avail_out = (uInt)sizeof(obuf);
          rc = deflate(&zs, Z_FINISH);
          bput(&comp, obuf, sizeof(obuf) - zs.avail_out);
        } while (rc == Z_OK && ++spins < 16);
        bprintf(&out, "deflate=%s\ncomp.len=%zu\n", zret(rc), comp.n);
        bprintf(&out, "deflateEnd=%s\n", zret(deflateEnd(&zs)));
      }
      if (comp.n > 0) {
        z_stream iz;
        unsigned char back[64];
        memset(&iz, 0, sizeof(iz));
        rc = inflateInit2(&iz, 15);
        bprintf(&out, "inflateInit2=%s\n", zret(rc));
        if (rc == Z_OK) {
          iz.next_in = comp.p;
          iz.avail_in = (uInt)comp.n;
          iz.next_out = back;
          iz.avail_out = (uInt)sizeof(back);
          rc = inflate(&iz, Z_FINISH);
          bprintf(&out, "inflate=%s\ninflate.total_out=%lu\n", zret(rc),
                  (unsigned long)iz.total_out);
          bprintf(&out, "inflateEnd=%s\n", zret(inflateEnd(&iz)));
        }
      }
      bfree(&comp);
    }

    {
      uLong cap = compressBound(len);
      Bytef *dest = malloc(cap ? cap : 1);
      Bytef plain[16];
      uLongf dest_len = cap;
      uLongf plain_len = len;
      if (!dest)
        die("out of memory");
      bprintf(&out, "compress=%s\n",
              zret(compress(dest, &dest_len, check, len)));
      bprintf(&out, "uncompress=%s\n",
              zret(uncompress(plain, &plain_len, dest, dest_len)));
      free(dest);
    }

    {
      z_stream bz;
      unsigned char *window = malloc((size_t)1 << 15);
      if (!window)
        die("out of memory");
      memset(&bz, 0, sizeof(bz));
      bprintf(&out, "inflateBackInit=%s\n",
              zret(inflateBackInit(&bz, 15, window)));
      bprintf(&out, "inflateBackEnd=%s\n", zret(inflateBackEnd(&bz)));
      free(window);
    }

    {
      gz_header head;
      gzFile missing;
      memset(&head, 0, sizeof(head));
      head.os = 3;
      bprintf(&out, "gzheader.os=%d\n", head.os);
      missing = gzopen("/nonexistent/probe/list-keys.gz", "rb");
      bprintf(&out, "gzopen.missing=%s\n", missing == NULL ? "NULL" : "ok");
      if (missing != NULL)
        gzclose(missing);
    }
  }

  fwrite(out.p, 1, out.n, stdout);
  fflush(stdout);
  bfree(&out);
}

/* ------------------------------------------------------------------- main */

static void dispatch(const char *id, char **f, int nf) {
  const char *op = nf > 1 ? f[1] : "";
  buf out = {0};

  if (strcmp(op, "deflate") == 0)
    op_deflate(&out, f, nf);
  else if (strcmp(op, "inflate") == 0)
    op_inflate(&out, f, nf);
  else if (strcmp(op, "bound") == 0)
    op_bound(&out, f, nf);
  else if (strcmp(op, "oneshot") == 0)
    op_oneshot(&out, f, nf);
  else if (strcmp(op, "checksum") == 0)
    op_checksum(&out, f, nf);
  else if (strcmp(op, "statechange") == 0)
    op_statechange(&out, f, nf);
  else if (strcmp(op, "gzheader") == 0)
    op_gzheader(&out, f, nf);
  else if (strcmp(op, "syncrecover") == 0)
    op_syncrecover(&out, f, nf);
  else if (strcmp(op, "inflatestate") == 0)
    op_inflatestate(&out, f, nf);
  else if (strcmp(op, "error") == 0)
    op_error(&out, f, nf);
  else if (strcmp(op, "inflateback") == 0)
    op_inflateback(&out, f, nf);
  else if (strcmp(op, "gzfile") == 0)
    op_gzfile(&out, f, nf);
  else if (strcmp(op, "abi") == 0)
    op_abi(&out, f, nf);
  else if (strcmp(op, "alloc") == 0)
    op_alloc(&out, f, nf);
  else {
    bprintf(&out, "unknown-op=%s\n", op);
    emit(id, "badop", &out);
    bfree(&out);
    return;
  }

  emit(id, "ok", &out);
  bfree(&out);
}

int main(int argc, char **argv) {
  /* Flags are recognized in any position and removed before the positional
   * arguments are read, matching Probe.java, so that `--list-keys` alone is not
   * mistaken for a corpus directory. */
  int want_keys = 0;
  int positional = 0;
  int i;
  const char *rest[3] = {NULL, NULL, NULL};
  for (i = 1; i < argc; i++) {
    if (strcmp(argv[i], "--list-keys") == 0)
      want_keys = 1;
    else if (positional < 3)
      rest[positional++] = argv[i];
  }
  if (positional > 0)
    g_corpus_dir = rest[0];
  if (positional > 1)
    g_scratch_dir = rest[1];
  if (want_keys) {
    /* No corpus, no scratch directory, no stdin: the self-description needs
     * none of them. */
    list_keys();
    return 0;
  }
  mkdir(g_scratch_dir, 0755);

  /* Unbuffered stderr, line-buffered stdout is not enough: emit() flushes
   * explicitly so a record is on the wire before the next case can crash. */
  setvbuf(stderr, NULL, _IONBF, 0);

  char *line = malloc(MAX_LINE);
  if (!line)
    die("out of memory");
  while (fgets(line, MAX_LINE, stdin)) {
    size_t len = strlen(line);
    while (len && (line[len - 1] == '\n' || line[len - 1] == '\r'))
      line[--len] = '\0';
    if (len == 0 || line[0] == '#')
      continue;
    char *fields[MAX_FIELDS];
    int nf = split_tabs(line, fields, MAX_FIELDS);
    if (nf < 2)
      continue;
    dispatch(fields[0], fields, nf);
  }
  free(line);
  return 0;
}
