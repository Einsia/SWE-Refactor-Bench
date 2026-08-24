/* consumer.c -- a downstream consumer of the installed library.
 *
 * The probe exercises the API in-process against a large corpus.  This program
 * answers a different question: can a third-party C program that was written
 * against the published zlib.h still be compiled and linked against the
 * migrated library, and does it then behave the same?  That is the actual
 * acceptance test for the migration, so this file deliberately includes only
 * <zlib.h> and calls only documented entry points -- no private headers, no
 * internal structures, nothing the reference does not publish.
 *
 * It is compiled twice per run, once against the reference install tree and
 * once against the submission's, and the two invocations' stdout, stderr and
 * exit status are compared byte for byte.  It therefore prints raw bytes as hex
 * rather than digests of them: a checksum computed by the library under test
 * could hide a difference in the very bytes it is summarizing.
 *
 * The interop-* operations go further and hand the library's output to the
 * system gzip, an independent implementation of the same wire format.  A
 * rewrite that is merely self-consistent -- one that can read back whatever it
 * wrote -- fails those.
 *
 * usage: consumer <scratch-dir> <op> [arg]...
 */

#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zlib.h>

#define PAY_LEN 1024

static unsigned char PAY[PAY_LEN];
static char SCRATCH[4096];

/* A payload with three regimes, so the level and strategy knobs have something
 * to bite on: literal-heavy prose, a long run the match finder must collapse,
 * and an incompressible tail from a fixed LCG.  Built in code rather than
 * shipped as a file so this program depends on nothing but its scratch dir. */
static void mkpay(void) {
  static const char *words[] = {"deflate", "inflate", "window",  "huffman",
                               "literal", "match",   "distance", "block"};
  size_t i = 0;
  unsigned n = 0;
  while (i < 512) {
    const char *w = words[n % 8];
    size_t wl = strlen(w);
    if (i + wl + 1 > 512)
      break;
    memcpy(PAY + i, w, wl);
    i += wl;
    PAY[i++] = (unsigned char)((n % 5) ? ' ' : '\n');
    n++;
  }
  while (i < 700)
    PAY[i++] = 'A';
  unsigned long s = 0x2545f491u;
  while (i < PAY_LEN) {
    s = s * 1103515245u + 12345u;
    PAY[i++] = (unsigned char)((s >> 16) & 0xff);
  }
}

static void hex(const unsigned char *d, size_t n) {
  static const char *H = "0123456789abcdef";
  for (size_t i = 0; i < n; i++) {
    putchar(H[d[i] >> 4]);
    putchar(H[d[i] & 15]);
  }
}

/* Every line this program prints goes through one of these, so the output is a
 * flat, greppable, order-stable record rather than prose. */
static void kv(const char *k, const char *v) { printf("%s=%s\n", k, v); }

static void kl(const char *k, long v) { printf("%s=%ld\n", k, v); }

static void kx(const char *k, unsigned long v) { printf("%s=%08lx\n", k, v); }

static void kbytes(const char *k, const unsigned char *d, size_t n) {
  printf("%s=%lu:", k, (unsigned long)n);
  hex(d, n);
  putchar('\n');
}

/* One buffer per distinct name, interned on first use.
 *
 * Several call sites hold two or more paths live at once -- gzfile-interop
 * keeps its archive path across four other spath() calls -- so a single static
 * buffer, or a small rotating set, lets one call overwrite a path another still
 * holds.  That failure does not look like a path bug from the outside: it
 * surfaces as gzip reporting a truncated or missing file.  Interning by name
 * means a name always resolves to the same storage and two different names
 * never share any, which removes the hazard rather than making it rarer.
 *
 * The table is sized well above the dozen names this program uses; overflowing
 * it aborts loudly instead of silently reusing a slot. */
static const char *spath(const char *name) {
  enum { SLOTS = 32 };
  static const char *keys[SLOTS];
  static char bufs[SLOTS][4608];
  static unsigned used;
  for (unsigned i = 0; i < used; i++)
    if (strcmp(keys[i], name) == 0)
      return bufs[i];
  if (used == SLOTS) {
    fprintf(stderr, "spath: out of slots for '%s'\n", name);
    exit(3);
  }
  keys[used] = name;
  snprintf(bufs[used], sizeof(bufs[0]), "%s/%s", SCRATCH, name);
  return bufs[used++];
}

/* Return codes are printed by name.  The numbers are part of the ABI and are
 * checked elsewhere; here a name makes a diff legible, and an unknown code
 * still prints its value rather than being silently mapped to something. */
static const char *rcname(int rc) {
  switch (rc) {
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

static void krc(const char *k, int rc) {
  printf("%s=%d/%s\n", k, rc, rcname(rc));
}

static int writefile(const char *path, const unsigned char *d, size_t n) {
  FILE *f = fopen(path, "wb");
  if (!f)
    return -1;
  size_t w = fwrite(d, 1, n, f);
  return (fclose(f) == 0 && w == n) ? 0 : -1;
}

/* Reads at most cap bytes.  A file larger than cap is an error rather than a
 * truncated read: a silent truncation would turn a submission that emits too
 * much into a mismatch on some later key instead of on this one.
 *
 * Overflow is detected by asking for one byte past the cap rather than by
 * checking feof after a cap-sized read.  The feof form is ambiguous at exactly
 * cap bytes -- the read is satisfied without touching the end of file -- and
 * Consumer.java, which sees a byte array and a length rather than a stream, has
 * no way to reproduce that ambiguity.  Asking for cap+1 makes both sides agree
 * for every file size. */
static long readfile(const char *path, unsigned char *out, size_t cap) {
  FILE *f = fopen(path, "rb");
  if (!f)
    return -1;
  size_t n = fread(out, 1, cap, f);
  unsigned char over;
  int bad = ferror(f) || fread(&over, 1, 1, f) == 1;
  fclose(f);
  return bad ? -1 : (long)n;
}

/* Shell out to the system gzip.  Both invocations of this program -- against
 * the reference and against the submission -- run the same pinned gzip, so its
 * verdict is a fixed external judgment rather than a moving target.  Output is
 * redirected to files instead of a pipe so nothing gzip prints can interleave
 * with this program's own stdout and make the comparison order-dependent. */
static int rungzip(const char *args, const char *in, const char *out) {
  char cmd[16384];
  /* The error path is built here rather than through spath(), because callers
   * hold spath() results across this call and a shared buffer would let this
   * one overwrite gzip's own input path. */
  char err[4608];
  snprintf(err, sizeof(err), "%s/gzip.err", SCRATCH);
  snprintf(cmd, sizeof(cmd), "gzip %s < '%s' > '%s' 2> '%s'", args, in, out,
           err);
  int rc = system(cmd);
  /* Report the wait status the same way a shell would, so a signal death is
   * distinguishable from a nonzero exit rather than both showing up as -1. */
  if (rc == -1)
    return -1;
  if (rc & 0x7f)
    return 128 + (rc & 0x7f);
  return (rc >> 8) & 0xff;
}

static int op_version(void) {
  kv("runtime", zlibVersion());
  kv("compiled", ZLIB_VERSION);
  kl("vernum", ZLIB_VERNUM);
  kl("ver_major", ZLIB_VER_MAJOR);
  kl("ver_minor", ZLIB_VER_MINOR);
  kl("ver_revision", ZLIB_VER_REVISION);
  kl("ver_subrevision", ZLIB_VER_SUBREVISION);
  /* No sizeof or offsetof lines, for the reason there is no `abi layout` case
   * in probe.c: Consumer.java is the other half of this pair, and a JVM has no
   * answer to sizeof(z_stream).  Asking would freeze an expectation no
   * submission could ever match.
   *
   * The question they would ask -- is the caller-visible shape of the interface
   * still what the contract publishes -- is answered by structure.py, which
   * reads every public class, field, method signature and constant back out of
   * the delivered jar, twice, by parsing the class files and by reflecting
   * inside a JVM.  That covers the whole surface rather than two structs. */
  for (int rc = 2; rc >= -6; rc--)
    printf("zError[%d]=%s\n", rc, zError(rc));
  return 0;
}

static int op_compress(void) {
  unsigned char comp[8192], back[8192];
  kl("compressBound", (long)compressBound(PAY_LEN));
  kl("compressBound_0", (long)compressBound(0));
  kl("compressBound_1m", (long)compressBound(1000000));
  for (int level = -1; level <= 9; level++) {
    uLongf clen = sizeof(comp);
    int rc = compress2(comp, &clen, PAY, PAY_LEN, level);
    printf("level=%d ", level);
    krc("rc", rc);
    if (rc != Z_OK)
      continue;
    /* The bytes themselves, not a digest of them: the library under test must
     * not be the one summarizing its own output. */
    kbytes("comp", comp, clen);
    uLongf blen = sizeof(back);
    int rc2 = uncompress(back, &blen, comp, clen);
    krc("back_rc", rc2);
    kl("back_len", (long)blen);
    kv("roundtrip", (rc2 == Z_OK && blen == PAY_LEN &&
                     memcmp(back, PAY, PAY_LEN) == 0) ? "ok" : "MISMATCH");
  }
  /* compress() must behave exactly as compress2() at the default level. */
  uLongf clen = sizeof(comp);
  int rc = compress(comp, &clen, PAY, PAY_LEN);
  krc("plain_rc", rc);
  kbytes("plain", comp, clen);
  uLongf blen = sizeof(back), srclen = clen;
  krc("uncompress2_rc", uncompress2(back, &blen, comp, &srclen));
  kl("uncompress2_out", (long)blen);
  kl("uncompress2_consumed", (long)srclen);
  return 0;
}

static int op_gzip(void) {
  unsigned char comp[8192];
  for (int level = 1; level <= 9; level += 4) {
    z_stream zs;
    memset(&zs, 0, sizeof(zs));
    /* wbits + 16 selects the gzip wrapper.  Its header carries a fixed mtime of
     * zero, the deflate level in XFL and an OS byte, all of which a consumer can
     * see and an outside gzip will check. */
    int rc = deflateInit2(&zs, level, Z_DEFLATED, 15 + 16, 8,
                          Z_DEFAULT_STRATEGY);
    printf("level=%d ", level);
    krc("init", rc);
    if (rc != Z_OK)
      continue;
    zs.next_in = PAY;
    zs.avail_in = PAY_LEN;
    zs.next_out = comp;
    zs.avail_out = sizeof(comp);
    rc = deflate(&zs, Z_FINISH);
    krc("deflate", rc);
    kbytes("gz", comp, sizeof(comp) - zs.avail_out);
    kl("total_in", (long)zs.total_in);
    kl("total_out", (long)zs.total_out);
    kl("data_type", (long)zs.data_type);
    krc("end", deflateEnd(&zs));
  }
  return 0;
}

static int op_checksum(void) {
  kx("crc32_empty", crc32(0L, Z_NULL, 0));
  kx("adler32_empty", adler32(0L, Z_NULL, 0));
  kx("crc32_pay", crc32(crc32(0L, Z_NULL, 0), PAY, PAY_LEN));
  kx("adler32_pay", adler32(adler32(0L, Z_NULL, 0), PAY, PAY_LEN));
  kx("crc32_check", crc32(0L, (const Bytef *)"123456789", 9));
  kx("adler32_check", adler32(1L, (const Bytef *)"123456789", 9));
  kx("crc32_z_pay", crc32_z(0L, PAY, PAY_LEN));
  kx("adler32_z_pay", adler32_z(1L, PAY, PAY_LEN));
  /* Split-and-combine must agree with the one-shot form at every split, which
   * is the property crc32_combine exists to provide. */
  for (size_t cut = 0; cut <= PAY_LEN; cut += 137) {
    uLong c1 = crc32(0L, PAY, (uInt)cut);
    uLong c2 = crc32(0L, PAY + cut, (uInt)(PAY_LEN - cut));
    uLong a1 = adler32(1L, PAY, (uInt)cut);
    uLong a2 = adler32(1L, PAY + cut, (uInt)(PAY_LEN - cut));
    printf("cut=%lu ", (unsigned long)cut);
    kx("crc_comb", crc32_combine(c1, c2, (z_off_t)(PAY_LEN - cut)));
    printf("cut=%lu ", (unsigned long)cut);
    kx("adler_comb", adler32_combine(a1, a2, (z_off_t)(PAY_LEN - cut)));
  }
  /* Incremental accumulation must match the one-shot value byte for byte. */
  uLong c = crc32(0L, Z_NULL, 0), a = adler32(0L, Z_NULL, 0);
  for (size_t i = 0; i < PAY_LEN; i++) {
    c = crc32(c, PAY + i, 1);
    a = adler32(a, PAY + i, 1);
  }
  kx("crc32_incremental", c);
  kx("adler32_incremental", a);
  return 0;
}

static int op_stream(void) {
  unsigned char comp[8192], back[8192];
  z_stream zs;
  memset(&zs, 0, sizeof(zs));
  int rc = deflateInit(&zs, Z_BEST_COMPRESSION);
  krc("init", rc);
  if (rc != Z_OK)
    return 1;
  kl("deflateBound", (long)deflateBound(&zs, PAY_LEN));
  /* Feed in 64-byte chunks with 64 bytes of output space, the awkward case a
   * consumer with a fixed-size buffer actually hits. */
  size_t in_off = 0, out_len = 0;
  zs.next_out = comp;
  zs.avail_out = 64;
  int flush = Z_NO_FLUSH;
  unsigned steps = 0;
  while (steps++ < 4096) {
    if (zs.avail_in == 0 && flush == Z_NO_FLUSH) {
      size_t take = PAY_LEN - in_off;
      if (take > 64)
        take = 64;
      zs.next_in = PAY + in_off;
      zs.avail_in = (uInt)take;
      in_off += take;
      if (in_off == PAY_LEN)
        flush = Z_FINISH;
    }
    rc = deflate(&zs, flush);
    out_len = (size_t)(zs.next_out - comp);
    if (rc == Z_STREAM_END)
      break;
    if (rc != Z_OK && rc != Z_BUF_ERROR) {
      krc("deflate_fail", rc);
      break;
    }
    if (zs.avail_out == 0) {
      if (out_len + 64 > sizeof(comp))
        break;
      zs.avail_out = 64;
    }
  }
  krc("deflate_final", rc);
  kl("steps", (long)steps);
  kbytes("comp", comp, out_len);
  unsigned pend = 0;
  int bits = 0;
  krc("pending_rc", deflatePending(&zs, &pend, &bits));
  kl("pending_bytes", (long)pend);
  kl("pending_bits", (long)bits);
  kl("total_in", (long)zs.total_in);
  kl("total_out", (long)zs.total_out);
  kx("adler", zs.adler);
  krc("end", deflateEnd(&zs));

  z_stream iz;
  memset(&iz, 0, sizeof(iz));
  krc("iinit", inflateInit(&iz));
  iz.next_in = comp;
  iz.avail_in = (uInt)out_len;
  iz.next_out = back;
  iz.avail_out = sizeof(back);
  rc = inflate(&iz, Z_FINISH);
  krc("inflate", rc);
  kl("inflate_out", (long)(sizeof(back) - iz.avail_out));
  kx("inflate_adler", iz.adler);
  kl("inflate_data_type", (long)iz.data_type);
  kv("roundtrip", (sizeof(back) - iz.avail_out == PAY_LEN &&
                   memcmp(back, PAY, PAY_LEN) == 0) ? "ok" : "MISMATCH");
  krc("iend", inflateEnd(&iz));
  return 0;
}

static int op_gzfile(void) {
  const char *path = spath("consumer.gz");
  gzFile gf = gzopen(path, "wb6");
  if (!gf) {
    kv("gzopen_w", "NULL");
    return 1;
  }
  kl("gzwrite", gzwrite(gf, PAY, PAY_LEN));
  kl("gzputc", gzputc(gf, 'X'));
  kl("gzputs", gzputs(gf, "tail line\n"));
  kl("gzprintf", gzprintf(gf, "n=%d s=%s\n", 42, "forty-two"));
  kl("gzoffset_w", (long)gzoffset(gf));
  kl("gztell_w", (long)gztell(gf));
  krc("gzflush", gzflush(gf, Z_FINISH));
  krc("gzclose_w", gzclose(gf));

  unsigned char raw[8192];
  long rawlen = readfile(path, raw, sizeof(raw));
  kl("file_bytes", rawlen);
  if (rawlen > 0)
    kbytes("head", raw, rawlen < 16 ? (size_t)rawlen : 16);

  gf = gzopen(path, "rb");
  if (!gf) {
    kv("gzopen_r", "NULL");
    return 1;
  }
  unsigned char back[8192];
  int n = gzread(gf, back, sizeof(back));
  kl("gzread", n);
  kl("gzdirect", gzdirect(gf));
  kl("gzeof", gzeof(gf));
  kl("gztell_r", (long)gztell(gf));
  kv("payload", (n >= PAY_LEN && memcmp(back, PAY, PAY_LEN) == 0)
                    ? "ok" : "MISMATCH");
  if (n > PAY_LEN)
    kbytes("trailer", back + PAY_LEN, (size_t)(n - PAY_LEN));
  int errnum = 0;
  const char *msg = gzerror(gf, &errnum);
  kl("gzerror_num", errnum);
  kv("gzerror_msg", msg ? msg : "(null)");
  /* Seeking backwards on a compressed stream forces the reader to restart and
   * re-inflate, which is a distinct code path from sequential reading. */
  kl("gzseek_0", (long)gzseek(gf, 0, SEEK_SET));
  kl("gzgetc_after_rewind", gzgetc(gf));
  kl("gzseek_300", (long)gzseek(gf, 300, SEEK_SET));
  kl("gzgetc_at_300", gzgetc(gf));
  kl("gzungetc", gzungetc('Q', gf));
  kl("gzgetc_after_unget", gzgetc(gf));
  krc("gzrewind", gzrewind(gf));
  char line[128];
  kv("gzgets", gzgets(gf, line, 40) ? "ok" : "NULL");
  krc("gzclose_r", gzclose(gf));
  return 0;
}

static int op_flags(void) {
  uLong f = zlibCompileFlags();
  kx("flags", f);
  /* The flag bits are a published description of how the library was built.  A
   * consumer reads them to decide what it may assume, so their meaning has to
   * survive the migration, not just their aggregate value. */
  kl("sizeof_uInt_code", (long)(f & 3));
  kl("sizeof_uLong_code", (long)((f >> 2) & 3));
  kl("sizeof_voidpf_code", (long)((f >> 4) & 3));
  kl("sizeof_z_off_t_code", (long)((f >> 6) & 3));
  kl("debug", (long)((f >> 8) & 1));
  kl("asm", (long)((f >> 9) & 1));
  kl("winapi", (long)((f >> 10) & 1));
  kl("buildfixed", (long)((f >> 12) & 1));
  kl("dynamic_crc_table", (long)((f >> 13) & 1));
  kl("no_gzcompress", (long)((f >> 16) & 1));
  kl("no_gzip", (long)((f >> 17) & 1));
  kl("pkzip_bug_workaround", (long)((f >> 20) & 1));
  kl("fastest", (long)((f >> 21) & 1));
  return 0;
}

static int op_errors(void) {
  z_stream zs;
  unsigned char comp[256], back[256];
  /* Every one of these is a documented rejection.  A rewrite that is merely
   * permissive passes the happy path and fails here. */
  memset(&zs, 0, sizeof(zs));
  krc("bad_level", deflateInit(&zs, 10));
  memset(&zs, 0, sizeof(zs));
  krc("bad_level_neg", deflateInit(&zs, -2));
  memset(&zs, 0, sizeof(zs));
  krc("bad_wbits", deflateInit2(&zs, 6, Z_DEFLATED, 7, 8, Z_DEFAULT_STRATEGY));
  memset(&zs, 0, sizeof(zs));
  krc("bad_memlevel", deflateInit2(&zs, 6, Z_DEFLATED, 15, 0,
                                  Z_DEFAULT_STRATEGY));
  memset(&zs, 0, sizeof(zs));
  krc("bad_method", deflateInit2(&zs, 6, 9, 15, 8, Z_DEFAULT_STRATEGY));
  memset(&zs, 0, sizeof(zs));
  krc("bad_strategy", deflateInit2(&zs, 6, Z_DEFLATED, 15, 8, 5));
  /* These six drive a zeroed, never-initialized stream rather than a NULL
   * pointer, for the reason `abi defensive` does in probe.c: Java has no null
   * receiver, so a NULL z_stream* would pin an expectation the other half of
   * this pair could not answer.  A never-initialized stream does have a
   * counterpart -- a constructed Deflater whose init was never called -- and it
   * asks the same question of the same code, which is whether the library
   * checks its own state before trusting it. */
  memset(&zs, 0, sizeof(zs));
  krc("uninit_deflate", deflate(&zs, Z_NO_FLUSH));
  memset(&zs, 0, sizeof(zs));
  krc("uninit_deflateEnd", deflateEnd(&zs));
  memset(&zs, 0, sizeof(zs));
  krc("uninit_inflate", inflate(&zs, Z_NO_FLUSH));
  memset(&zs, 0, sizeof(zs));
  krc("uninit_inflateEnd", inflateEnd(&zs));
  memset(&zs, 0, sizeof(zs));
  krc("uninit_deflateReset", deflateReset(&zs));
  memset(&zs, 0, sizeof(zs));
  krc("uninit_inflateReset", inflateReset(&zs));
  /* A version string the library does not recognize must be refused: that check
   * is what protects a consumer built against a different header. */
  memset(&zs, 0, sizeof(zs));
  krc("wrong_version", deflateInit_(&zs, 6, "0.0.0", (int)sizeof(z_stream)));
  memset(&zs, 0, sizeof(zs));
  krc("wrong_size", deflateInit_(&zs, 6, ZLIB_VERSION, 3));
  memset(&zs, 0, sizeof(zs));
  krc("iwrong_version", inflateInit_(&zs, "0.0.0", (int)sizeof(z_stream)));

  uLongf n = 4;
  krc("compress_tiny_out", compress2(comp, &n, PAY, PAY_LEN, 6));
  n = sizeof(back);
  krc("uncompress_garbage",
      uncompress(back, &n, (const Bytef *)"not a zlib stream", 17));
  n = sizeof(back);
  krc("uncompress_empty", uncompress(back, &n, (const Bytef *)"", 0));
  uLongf c = sizeof(comp);
  if (compress2(comp, &c, PAY, 200, 6) == Z_OK) {
    n = sizeof(back);
    krc("uncompress_truncated", uncompress(back, &n, comp, c / 2));
    comp[c - 1] ^= 0xff;
    n = sizeof(back);
    krc("uncompress_bad_adler", uncompress(back, &n, comp, c));
    comp[c - 1] ^= 0xff;
    comp[0] ^= 0x10;
    n = sizeof(back);
    krc("uncompress_bad_header", uncompress(back, &n, comp, c));
  }
  kv("gzopen_missing", gzopen(spath("no-such-file.gz"), "rb") ? "ok" : "NULL");
  kv("gzopen_bad_mode", gzopen(spath("consumer.gz"), "qq") ? "ok" : "NULL");
  return 0;
}

/* -------------------------------------------------------------- interop
 *
 * These four hand bytes to the system gzip, or take bytes from it.  The system
 * gzip is pinned by the image and is not the code under test, so it is a fixed
 * outside judgment on whether the migrated library still speaks the format.
 */

static int op_interop_write(void) {
  unsigned char comp[8192];
  for (int level = 1; level <= 9; level += 4) {
    z_stream zs;
    memset(&zs, 0, sizeof(zs));
    if (deflateInit2(&zs, level, Z_DEFLATED, 31, 8, Z_DEFAULT_STRATEGY) != Z_OK)
      return 1;
    zs.next_in = PAY;
    zs.avail_in = PAY_LEN;
    zs.next_out = comp;
    zs.avail_out = sizeof(comp);
    int rc = deflate(&zs, Z_FINISH);
    size_t clen = sizeof(comp) - zs.avail_out;
    deflateEnd(&zs);
    printf("level=%d ", level);
    krc("deflate", rc);
    const char *gz = spath("ours.gz");
    const char *out = spath("ours.out");
    if (writefile(gz, comp, clen) != 0)
      return 1;
    /* -d decompresses, -c writes to stdout; a nonzero status means gzip judged
     * our stream malformed. */
    printf("level=%d ", level);
    kl("gzip_status", rungzip("-dc", gz, out));
    unsigned char back[8192];
    long n = readfile(out, back, sizeof(back));
    printf("level=%d ", level);
    kl("gzip_out_bytes", n);
    printf("level=%d ", level);
    kv("gzip_agrees", (n == PAY_LEN && memcmp(back, PAY, PAY_LEN) == 0)
                          ? "ok" : "MISMATCH");
    /* gzip -t is a stricter check than -d: it verifies the trailer's length and
     * CRC even when the data happens to decode. */
    printf("level=%d ", level);
    kl("gzip_test_status", rungzip("-t", gz, spath("test.out")));
  }
  return 0;
}

static int op_interop_read(void) {
  const char *raw = spath("plain.bin");
  const char *gz = spath("theirs.gz");
  if (writefile(raw, PAY, PAY_LEN) != 0)
    return 1;
  /* -n suppresses the name and timestamp, so what gzip writes is a function of
   * its input alone and this case stays reproducible. */
  for (int level = 1; level <= 9; level += 4) {
    char args[32];
    snprintf(args, sizeof(args), "-n -%d -c", level);
    printf("level=%d ", level);
    kl("gzip_status", rungzip(args, raw, gz));
    unsigned char comp[8192];
    long clen = readfile(gz, comp, sizeof(comp));
    printf("level=%d ", level);
    kl("gzip_bytes", clen);
    if (clen <= 0)
      continue;
    z_stream zs;
    memset(&zs, 0, sizeof(zs));
    if (inflateInit2(&zs, 31) != Z_OK)
      return 1;
    /* Reading the header back is the part a self-consistent rewrite can miss:
     * the fields gzip wrote have to be surfaced with the values gzip chose. */
    gz_header hd;
    unsigned char namebuf[256], extrabuf[256], commbuf[256];
    memset(&hd, 0, sizeof(hd));
    hd.name = namebuf;
    hd.name_max = sizeof(namebuf);
    hd.extra = extrabuf;
    hd.extra_max = sizeof(extrabuf);
    hd.comment = commbuf;
    hd.comm_max = sizeof(commbuf);
    printf("level=%d ", level);
    krc("getheader", inflateGetHeader(&zs, &hd));
    unsigned char back[8192];
    zs.next_in = comp;
    zs.avail_in = (uInt)clen;
    zs.next_out = back;
    zs.avail_out = sizeof(back);
    int rc = inflate(&zs, Z_FINISH);
    size_t got = sizeof(back) - zs.avail_out;
    printf("level=%d ", level);
    krc("inflate", rc);
    printf("level=%d ", level);
    kl("out_bytes", (long)got);
    printf("level=%d ", level);
    kv("matches", (got == PAY_LEN && memcmp(back, PAY, PAY_LEN) == 0)
                      ? "ok" : "MISMATCH");
    printf("level=%d ", level);
    kl("hdr_done", (long)hd.done);
    printf("level=%d ", level);
    kl("hdr_text", (long)hd.text);
    printf("level=%d ", level);
    kl("hdr_time", (long)hd.time);
    printf("level=%d ", level);
    kl("hdr_xflags", (long)hd.xflags);
    printf("level=%d ", level);
    kl("hdr_os", (long)hd.os);
    printf("level=%d ", level);
    kl("hdr_extra_len", (long)hd.extra_len);
    printf("level=%d ", level);
    kx("adler", zs.adler);
    inflateEnd(&zs);
  }
  return 0;
}

static int op_interop_gzfile(void) {
  /* gzopen writes a full gzip member including a header this program never
   * touches, so this checks the gz* layer's framing rather than deflate's. */
  const char *path = spath("gzfile-interop.gz");
  gzFile gf = gzopen(path, "wb9");
  if (!gf)
    return 1;
  kl("written", gzwrite(gf, PAY, PAY_LEN));
  krc("closed", gzclose(gf));
  kl("gzip_test", rungzip("-t", path, spath("t.out")));
  kl("gzip_status", rungzip("-dc", path, spath("g.out")));
  unsigned char back[8192];
  long n = readfile(spath("g.out"), back, sizeof(back));
  kl("out_bytes", n);
  kv("agrees", (n == PAY_LEN && memcmp(back, PAY, PAY_LEN) == 0)
                   ? "ok" : "MISMATCH");
  /* gzip -l reports the stored uncompressed length from the trailer, so a wrong
   * ISIZE shows up here even when the data decodes. */
  char cmd[8192];
  snprintf(cmd, sizeof(cmd), "gzip -l '%s' > '%s' 2>&1", path,
           spath("l.out"));
  kl("gzip_list_status", system(cmd) == 0 ? 0 : 1);
  unsigned char listing[4096];
  long ln = readfile(spath("l.out"), listing, sizeof(listing) - 1);
  if (ln > 0) {
    listing[ln] = 0;
    /* The listing embeds the scratch path and a compression ratio that depends
     * on it, so only the stored length is compared. */
    kv("list_has_size", strstr((char *)listing, "1024") ? "ok" : "MISSING");
  }
  return 0;
}

static int op_interop_concat(void) {
  /* Concatenated members are one gzip stream, and a reader that stops after the
   * first member is a common and silent bug. */
  unsigned char comp[8192];
  size_t total = 0;
  for (int part = 0; part < 3; part++) {
    z_stream zs;
    memset(&zs, 0, sizeof(zs));
    if (deflateInit2(&zs, 6 + part, Z_DEFLATED, 31, 8, Z_DEFAULT_STRATEGY) !=
        Z_OK)
      return 1;
    zs.next_in = PAY + (size_t)part * 256;
    zs.avail_in = 256;
    zs.next_out = comp + total;
    zs.avail_out = (uInt)(sizeof(comp) - total);
    int rc = deflate(&zs, Z_FINISH);
    total = sizeof(comp) - zs.avail_out;
    deflateEnd(&zs);
    printf("part=%d ", part);
    krc("deflate", rc);
  }
  kl("stream_bytes", (long)total);
  const char *path = spath("concat.gz");
  if (writefile(path, comp, total) != 0)
    return 1;
  kl("gzip_test", rungzip("-t", path, spath("ct.out")));
  kl("gzip_status", rungzip("-dc", path, spath("cd.out")));
  unsigned char back[8192];
  long n = readfile(spath("cd.out"), back, sizeof(back));
  kl("gzip_out_bytes", n);
  kv("gzip_agrees", (n == 768 && memcmp(back, PAY, 768) == 0)
                        ? "ok" : "MISMATCH");
  /* And the library's own reader must cross the member boundaries too. */
  gzFile gf = gzopen(path, "rb");
  if (!gf)
    return 1;
  unsigned char ours[8192];
  int got = gzread(gf, ours, sizeof(ours));
  kl("gzread", got);
  kl("gzeof", gzeof(gf));
  krc("gzclose", gzclose(gf));
  kv("gzread_agrees", (got == 768 && memcmp(ours, PAY, 768) == 0)
                          ? "ok" : "MISMATCH");
  return 0;
}

int main(int argc, char **argv) {
  if (argc < 3) {
    fprintf(stderr, "usage: %s <scratch-dir> <op>\n", argv[0]);
    return 2;
  }
  snprintf(SCRATCH, sizeof(SCRATCH), "%s", argv[1]);
  mkpay();
  const char *op = argv[2];
  /* The runtime version is printed first by every op.  If a submission's
   * library is a different vintage than its header, that shows up once at the
   * top of the diff instead of as scattered value differences. */
  printf("op=%s zlib=%s\n", op, zlibVersion());
  int rc;
  if (!strcmp(op, "version")) rc = op_version();
  else if (!strcmp(op, "compress")) rc = op_compress();
  else if (!strcmp(op, "gzip")) rc = op_gzip();
  else if (!strcmp(op, "checksum")) rc = op_checksum();
  else if (!strcmp(op, "stream")) rc = op_stream();
  else if (!strcmp(op, "gzfile")) rc = op_gzfile();
  else if (!strcmp(op, "flags")) rc = op_flags();
  else if (!strcmp(op, "errors")) rc = op_errors();
  else if (!strcmp(op, "interop-write")) rc = op_interop_write();
  else if (!strcmp(op, "interop-read")) rc = op_interop_read();
  else if (!strcmp(op, "interop-gzfile")) rc = op_interop_gzfile();
  else if (!strcmp(op, "interop-concat")) rc = op_interop_concat();
  else {
    fprintf(stderr, "unknown op: %s\n", op);
    return 2;
  }
  printf("done=%s rc=%d\n", op, rc);
  return rc;
}
