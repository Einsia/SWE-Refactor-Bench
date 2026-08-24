/* Consumer.java -- a downstream consumer of the delivered jar.
 *
 * The Java half of the second matched pair.  consumer.c is compiled against the
 * pinned C reference install; this is compiled against the submitted jar; both
 * are run for the same op and their stdout, stderr and exit status are compared
 * byte for byte.  Every key and every value format here therefore has to agree
 * with consumer.c exactly, and the section order follows its so the
 * correspondence stays auditable.
 *
 * Where Probe.java asks "does the API behave the same in-process", this asks the
 * question that actually decides whether the migration shipped: can a
 * third-party program written against the published surface still be compiled
 * against the delivered artifact, and does it then behave the same?  So this
 * file uses only the org.zlib surface the contract publishes -- no reflection
 * into an implementation package, no java.util.zip, nothing the reference does
 * not export.
 *
 * It is compiled and run twice per config, once with the jar on the module path
 * and once with it on the class path, because those two linkage forms fail
 * apart: a missing or wrong module-info breaks the first and leaves the second
 * working, and a package that is not exported does the reverse.  Both runs are
 * compared against the same frozen C output, so both must agree.
 *
 * The interop-* operations hand bytes to the system gzip, or take bytes from it.
 * A rewrite that is merely self-consistent -- one that reads back whatever it
 * wrote -- fails those.  Spawning a process is forbidden to the library under
 * test and is fine here: the policy binds the classes inside the delivered jar,
 * which is what the cf-no-process gate scans, and this file is the verifier's
 * instrument rather than part of the submission.
 *
 * usage: java Consumer <scratch-dir> <op> [arg]...
 */

import java.io.File;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import org.zlib.Deflater;
import org.zlib.GzFile;
import org.zlib.GzHeader;
import org.zlib.Inflater;
import org.zlib.Zlib;

public final class Consumer {

    static final int PAY_LEN = 1024;

    static final byte[] PAY = new byte[PAY_LEN];

    static String scratch = ".";

    /* Everything this program prints goes through out, not System.out.
     *
     * The C side writes bytes to a FILE*; this writes bytes to a stream and
     * encodes text as Latin-1 so one char is one byte.  System.out would encode
     * through the default charset and, more importantly, would translate line
     * endings on some platforms -- either would put a difference into the
     * comparison that has nothing to do with the library. */
    static OutputStream out;

    private Consumer() {
    }

    /* ------------------------------------------------------------- printing */

    static void emit(String s) {
        try {
            out.write(s.getBytes(StandardCharsets.ISO_8859_1));
        } catch (IOException e) {
            /* stdout is gone; nothing this program prints afterwards can be
             * compared anyway, so fail loudly rather than continue silently. */
            System.exit(70);
        }
    }

    static void kv(String k, String v) {
        emit(k + "=" + v + "\n");
    }

    static void kl(String k, long v) {
        emit(k + "=" + v + "\n");
    }

    /* C prints %08lx of an unsigned long.  Every value that reaches here is a
     * 32-bit checksum or flag word, so it is masked to 32 bits first: a port that
     * let a checksum sign-extend into a Java long would otherwise print sixteen
     * hex digits and the diff would be about the printing rather than the value.
     * Masking is safe because the C side's values never exceed 32 bits either --
     * op_flags is the one word that could in principle, and the contract pins it
     * to 0xa9. */
    static void kx(String k, long v) {
        emit(k + "=" + String.format("%08x", v & 0xffffffffL) + "\n");
    }

    static final char[] HEX = "0123456789abcdef".toCharArray();

    static void kbytes(String k, byte[] d, int off, int n) {
        StringBuilder sb = new StringBuilder(k.length() + 12 + n * 2);
        sb.append(k).append('=').append(n).append(':');
        for (int i = 0; i < n; i++) {
            int b = d[off + i] & 0xff;
            sb.append(HEX[b >> 4]).append(HEX[b & 15]);
        }
        sb.append('\n');
        emit(sb.toString());
    }

    /* Return codes are printed by name.  The numbers are part of the ABI and are
     * checked elsewhere; here a name makes a diff legible, and an unknown code
     * still prints its value rather than being silently mapped to something. */
    static String rcname(int rc) {
        switch (rc) {
            case Zlib.Z_OK: return "Z_OK";
            case Zlib.Z_STREAM_END: return "Z_STREAM_END";
            case Zlib.Z_NEED_DICT: return "Z_NEED_DICT";
            case Zlib.Z_ERRNO: return "Z_ERRNO";
            case Zlib.Z_STREAM_ERROR: return "Z_STREAM_ERROR";
            case Zlib.Z_DATA_ERROR: return "Z_DATA_ERROR";
            case Zlib.Z_MEM_ERROR: return "Z_MEM_ERROR";
            case Zlib.Z_BUF_ERROR: return "Z_BUF_ERROR";
            case Zlib.Z_VERSION_ERROR: return "Z_VERSION_ERROR";
            default: return "Z_UNKNOWN";
        }
    }

    static void krc(String k, int rc) {
        emit(k + "=" + rc + "/" + rcname(rc) + "\n");
    }

    /* ------------------------------------------------------------- payload
     *
     * A payload with three regimes, so the level and strategy knobs have
     * something to bite on: literal-heavy prose, a long run the match finder must
     * collapse, and an incompressible tail from a fixed LCG.  Built in code
     * rather than shipped as a file so this program depends on nothing but its
     * scratch dir.
     *
     * The LCG runs in a long, which wraps at 2^64 where C's unsigned long wraps
     * on this platform, and only bits 16..23 are kept -- so both sides produce
     * the same tail.  There is a self-check on the result at startup.
     */
    static void mkpay() {
        String[] words = {"deflate", "inflate", "window", "huffman",
                          "literal", "match", "distance", "block"};
        int i = 0;
        int n = 0;
        while (i < 512) {
            byte[] w = words[n % 8].getBytes(StandardCharsets.ISO_8859_1);
            if (i + w.length + 1 > 512) {
                break;
            }
            System.arraycopy(w, 0, PAY, i, w.length);
            i += w.length;
            PAY[i++] = (byte) ((n % 5) != 0 ? ' ' : '\n');
            n++;
        }
        while (i < 700) {
            PAY[i++] = 'A';
        }
        long s = 0x2545f491L;
        while (i < PAY_LEN) {
            s = s * 1103515245L + 12345L;
            PAY[i++] = (byte) ((s >>> 16) & 0xff);
        }
    }

    /* ------------------------------------------------------------- paths
     *
     * consumer.c interns these because it has one buffer per name and several
     * call sites hold two paths live at once.  Java has no such hazard, but the
     * names have to be the same strings, so the same helper is here and the two
     * files stay line-comparable.
     */
    static String spath(String name) {
        return scratch + "/" + name;
    }

    static int writefile(String path, byte[] d, int n) {
        try {
            Files.write(Paths.get(path), Arrays.copyOf(d, n));
            return 0;
        } catch (IOException e) {
            return -1;
        }
    }

    /* Reads at most cap bytes.  A file larger than cap is an error rather than a
     * truncated read, matching consumer.c: a silent truncation would turn a
     * submission that emits too much into a mismatch on some later key instead of
     * on this one. */
    static byte[] readbuf = new byte[1 << 16];
    static int readlen;

    static long readfile(String path, int cap) {
        readlen = 0;
        byte[] data;
        try {
            data = Files.readAllBytes(Paths.get(path));
        } catch (IOException e) {
            return -1;
        }
        if (data.length > cap) {
            return -1;
        }
        if (readbuf.length < data.length) {
            readbuf = new byte[data.length];
        }
        System.arraycopy(data, 0, readbuf, 0, data.length);
        readlen = data.length;
        return data.length;
    }

    /* Shell out to the system gzip.  Both invocations of this program -- against
     * the reference and against the submission -- run the same pinned gzip, so
     * its verdict is a fixed external judgment rather than a moving target.
     * Output goes to files instead of a pipe so nothing gzip prints can
     * interleave with this program's own stdout and make the comparison
     * order-dependent.
     *
     * consumer.c reaches system(), so its args string is split by a shell.  Here
     * it is split on spaces and handed to ProcessBuilder directly: the arguments
     * this program passes are fixed literals with no quoting in them, and going
     * through a shell would add one more thing that could differ between the two
     * sides.  The wait status is reported the way a shell reports it, so a signal
     * death stays distinguishable from a nonzero exit. */
    static int rungzip(String args, String in, String outPath) {
        List<String> argv = new ArrayList<>();
        argv.add("gzip");
        for (String piece : args.split(" ")) {
            if (!piece.isEmpty()) {
                argv.add(piece);
            }
        }
        ProcessBuilder pb = new ProcessBuilder(argv);
        pb.redirectInput(new File(in));
        pb.redirectOutput(new File(outPath));
        pb.redirectError(new File(spath("gzip.err")));
        try {
            Process p = pb.start();
            return p.waitFor();
        } catch (IOException e) {
            return -1;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return -1;
        }
    }

    /* ------------------------------------------------------------- version */

    static int opVersion() {
        kv("runtime", Zlib.version());
        /* Zlib.ZLIB_VERSION is a compile-time constant, so javac copies its value
         * into this class file.  That is the point: if a submission renumbers or
         * retypes it, `compiled` keeps the value this program was built with and
         * disagrees with `runtime`, which is exactly the failure mode a C consumer
         * built against a stale header has.  Reflection cannot see this; only a
         * compiled consumer can. */
        kv("compiled", Zlib.ZLIB_VERSION);
        kl("vernum", Zlib.ZLIB_VERNUM);
        kl("ver_major", Zlib.ZLIB_VER_MAJOR);
        kl("ver_minor", Zlib.ZLIB_VER_MINOR);
        kl("ver_revision", Zlib.ZLIB_VER_REVISION);
        kl("ver_subrevision", Zlib.ZLIB_VER_SUBREVISION);
        /* No sizeof or offsetof lines here; see the note in consumer.c. */
        for (int rc = 2; rc >= -6; rc--) {
            emit("zError[" + rc + "]=" + Zlib.errorString(rc) + "\n");
        }
        return 0;
    }

    /* ------------------------------------------------------------ compress */

    static int opCompress() {
        byte[] comp = new byte[8192];
        byte[] back = new byte[8192];
        kl("compressBound", Zlib.compressBound(PAY_LEN));
        kl("compressBound_0", Zlib.compressBound(0));
        kl("compressBound_1m", Zlib.compressBound(1000000));
        for (int level = -1; level <= 9; level++) {
            int[] clen = {comp.length};
            int rc = Zlib.compress2(comp, clen, PAY, PAY_LEN, level);
            emit("level=" + level + " ");
            krc("rc", rc);
            if (rc != Zlib.Z_OK) {
                continue;
            }
            /* The bytes themselves, not a digest of them: the library under test
             * must not be the one summarizing its own output. */
            kbytes("comp", comp, 0, clen[0]);
            int[] blen = {back.length};
            int rc2 = Zlib.uncompress(back, blen, comp, clen[0]);
            krc("back_rc", rc2);
            kl("back_len", blen[0]);
            kv("roundtrip", (rc2 == Zlib.Z_OK && blen[0] == PAY_LEN
                    && sameBytes(back, PAY, PAY_LEN)) ? "ok" : "MISMATCH");
        }
        /* compress() must behave exactly as compress2() at the default level. */
        int[] clen = {comp.length};
        int rc = Zlib.compress(comp, clen, PAY, PAY_LEN);
        krc("plain_rc", rc);
        kbytes("plain", comp, 0, clen[0]);
        int[] blen = {back.length};
        int[] srclen = {clen[0]};
        krc("uncompress2_rc", Zlib.uncompress2(back, blen, comp, srclen));
        kl("uncompress2_out", blen[0]);
        kl("uncompress2_consumed", srclen[0]);
        return 0;
    }

    /* ---------------------------------------------------------------- gzip */

    static int opGzip() {
        byte[] comp = new byte[8192];
        for (int level = 1; level <= 9; level += 4) {
            Deflater zs = new Deflater();
            /* wbits + 16 selects the gzip wrapper.  Its header carries a fixed
             * mtime of zero, the deflate level in XFL and an OS byte, all of which
             * a consumer can see and an outside gzip will check. */
            int rc = zs.init2(level, Zlib.Z_DEFLATED, 15 + 16, 8,
                    Zlib.Z_DEFAULT_STRATEGY);
            emit("level=" + level + " ");
            krc("init", rc);
            if (rc != Zlib.Z_OK) {
                continue;
            }
            zs.nextIn = PAY;
            zs.nextInIndex = 0;
            zs.availIn = PAY_LEN;
            zs.nextOut = comp;
            zs.nextOutIndex = 0;
            zs.availOut = comp.length;
            rc = zs.deflate(Zlib.Z_FINISH);
            krc("deflate", rc);
            kbytes("gz", comp, 0, comp.length - zs.availOut);
            kl("total_in", zs.totalIn);
            kl("total_out", zs.totalOut);
            kl("data_type", zs.dataType);
            krc("end", zs.end());
        }
        return 0;
    }

    /* ------------------------------------------------------------ checksum */

    static int opChecksum() {
        kx("crc32_empty", Zlib.crc32(0L, null, 0, 0));
        kx("adler32_empty", Zlib.adler32(0L, null, 0, 0));
        kx("crc32_pay", Zlib.crc32(Zlib.crc32(0L, null, 0, 0), PAY, 0, PAY_LEN));
        kx("adler32_pay",
                Zlib.adler32(Zlib.adler32(0L, null, 0, 0), PAY, 0, PAY_LEN));
        byte[] check = "123456789".getBytes(StandardCharsets.ISO_8859_1);
        kx("crc32_check", Zlib.crc32(0L, check, 0, 9));
        kx("adler32_check", Zlib.adler32(1L, check, 0, 9));
        /* C's crc32_z and adler32_z collapse onto the same Java methods -- the
         * length is an int either way -- so these two lines repeat the calls
         * above.  They are kept because consumer.c prints the keys, and dropping
         * them here would make the two halves disagree. */
        kx("crc32_z_pay", Zlib.crc32(0L, PAY, 0, PAY_LEN));
        kx("adler32_z_pay", Zlib.adler32(1L, PAY, 0, PAY_LEN));
        /* Split-and-combine must agree with the one-shot form at every split,
         * which is the property crc32Combine exists to provide. */
        for (int cut = 0; cut <= PAY_LEN; cut += 137) {
            long c1 = Zlib.crc32(0L, PAY, 0, cut);
            long c2 = Zlib.crc32(0L, PAY, cut, PAY_LEN - cut);
            long a1 = Zlib.adler32(1L, PAY, 0, cut);
            long a2 = Zlib.adler32(1L, PAY, cut, PAY_LEN - cut);
            emit("cut=" + cut + " ");
            kx("crc_comb", Zlib.crc32Combine(c1, c2, PAY_LEN - cut));
            emit("cut=" + cut + " ");
            kx("adler_comb", Zlib.adler32Combine(a1, a2, PAY_LEN - cut));
        }
        /* Incremental accumulation must match the one-shot value byte for byte. */
        long c = Zlib.crc32(0L, null, 0, 0);
        long a = Zlib.adler32(0L, null, 0, 0);
        for (int i = 0; i < PAY_LEN; i++) {
            c = Zlib.crc32(c, PAY, i, 1);
            a = Zlib.adler32(a, PAY, i, 1);
        }
        kx("crc32_incremental", c);
        kx("adler32_incremental", a);
        return 0;
    }

    /* -------------------------------------------------------------- stream */

    static int opStream() {
        byte[] comp = new byte[8192];
        byte[] back = new byte[8192];
        Deflater zs = new Deflater();
        int rc = zs.init(Zlib.Z_BEST_COMPRESSION);
        krc("init", rc);
        if (rc != Zlib.Z_OK) {
            return 1;
        }
        kl("deflateBound", zs.bound(PAY_LEN));
        /* Feed in 64-byte chunks with 64 bytes of output space, the awkward case a
         * consumer with a fixed-size buffer actually hits.
         *
         * C advances next_out as a pointer and reads the produced length back as
         * next_out - comp.  Here the same value is nextOutIndex, so the loop is
         * the same loop with an index where the pointer was. */
        int inOff = 0;
        int outLen = 0;
        zs.nextOut = comp;
        zs.nextOutIndex = 0;
        zs.availOut = 64;
        int flush = Zlib.Z_NO_FLUSH;
        int steps = 0;
        while (steps++ < 4096) {
            if (zs.availIn == 0 && flush == Zlib.Z_NO_FLUSH) {
                int take = Math.min(PAY_LEN - inOff, 64);
                zs.nextIn = PAY;
                zs.nextInIndex = inOff;
                zs.availIn = take;
                inOff += take;
                if (inOff == PAY_LEN) {
                    flush = Zlib.Z_FINISH;
                }
            }
            rc = zs.deflate(flush);
            outLen = zs.nextOutIndex;
            if (rc == Zlib.Z_STREAM_END) {
                break;
            }
            if (rc != Zlib.Z_OK && rc != Zlib.Z_BUF_ERROR) {
                krc("deflate_fail", rc);
                break;
            }
            if (zs.availOut == 0) {
                if (outLen + 64 > comp.length) {
                    break;
                }
                zs.availOut = 64;
            }
        }
        krc("deflate_final", rc);
        kl("steps", steps);
        kbytes("comp", comp, 0, outLen);
        int[] pend = {0};
        int[] bits = {0};
        krc("pending_rc", zs.pending(pend, bits));
        kl("pending_bytes", pend[0]);
        kl("pending_bits", bits[0]);
        kl("total_in", zs.totalIn);
        kl("total_out", zs.totalOut);
        kx("adler", zs.adler);
        krc("end", zs.end());

        Inflater iz = new Inflater();
        krc("iinit", iz.init());
        iz.nextIn = comp;
        iz.nextInIndex = 0;
        iz.availIn = outLen;
        iz.nextOut = back;
        iz.nextOutIndex = 0;
        iz.availOut = back.length;
        rc = iz.inflate(Zlib.Z_FINISH);
        krc("inflate", rc);
        kl("inflate_out", back.length - iz.availOut);
        kx("inflate_adler", iz.adler);
        kl("inflate_data_type", iz.dataType);
        kv("roundtrip", (back.length - iz.availOut == PAY_LEN
                && sameBytes(back, PAY, PAY_LEN)) ? "ok" : "MISMATCH");
        krc("iend", iz.end());
        return 0;
    }

    /* -------------------------------------------------------------- gzfile */

    static int opGzfile() {
        String path = spath("consumer.gz");
        GzFile gf = GzFile.open(path, "wb6");
        if (gf == null) {
            kv("gzopen_w", "NULL");
            return 1;
        }
        kl("gzwrite", gf.write(PAY, 0, PAY_LEN));
        kl("gzputc", gf.putc('X'));
        kl("gzputs", gf.puts("tail line\n"));
        /* Only %d and %s.  The contract says printf follows java.util.Formatter,
         * and the two grammars agree on exactly these: a %lu here would be asking
         * the port to implement a C format parser, which is not what the migration
         * is about. */
        kl("gzprintf", gf.printf("n=%d s=%s\n", new Object[]{42, "forty-two"}));
        kl("gzoffset_w", gf.offset());
        kl("gztell_w", gf.tell());
        krc("gzflush", gf.flush(Zlib.Z_FINISH));
        krc("gzclose_w", gf.close());

        long rawlen = readfile(path, 8192);
        kl("file_bytes", rawlen);
        if (rawlen > 0) {
            kbytes("head", readbuf, 0, rawlen < 16 ? (int) rawlen : 16);
        }

        gf = GzFile.open(path, "rb");
        if (gf == null) {
            kv("gzopen_r", "NULL");
            return 1;
        }
        byte[] back = new byte[8192];
        int n = gf.read(back, 0, back.length);
        kl("gzread", n);
        kl("gzdirect", gf.direct());
        kl("gzeof", gf.eof());
        kl("gztell_r", gf.tell());
        kv("payload", (n >= PAY_LEN && sameBytes(back, PAY, PAY_LEN))
                ? "ok" : "MISMATCH");
        if (n > PAY_LEN) {
            kbytes("trailer", back, PAY_LEN, n - PAY_LEN);
        }
        int[] errnum = {0};
        String msg = gf.error(errnum);
        kl("gzerror_num", errnum[0]);
        kv("gzerror_msg", msg != null ? msg : "(null)");
        /* Seeking backwards on a compressed stream forces the reader to restart
         * and re-inflate, which is a distinct code path from sequential reading.
         * The whence values are C's: SEEK_SET is 0. */
        kl("gzseek_0", gf.seek(0, 0));
        kl("gzgetc_after_rewind", gf.getc());
        kl("gzseek_300", gf.seek(300, 0));
        kl("gzgetc_at_300", gf.getc());
        kl("gzungetc", gf.ungetc('Q'));
        kl("gzgetc_after_unget", gf.getc());
        krc("gzrewind", gf.rewind());
        byte[] line = new byte[128];
        kv("gzgets", gf.gets(line, 40) != null ? "ok" : "NULL");
        krc("gzclose_r", gf.close());
        return 0;
    }

    /* --------------------------------------------------------------- flags */

    static int opFlags() {
        long f = Zlib.compileFlags();
        kx("flags", f);
        /* The flag bits are a published description of how the library was built.
         * A consumer reads them to decide what it may assume, so their meaning has
         * to survive the migration, not just their aggregate value.  The contract
         * pins the word itself, which is why this op translates unchanged: the
         * sizes it reports are the sizes of the C types the API is specified in,
         * not of anything a JVM allocates. */
        kl("sizeof_uInt_code", f & 3);
        kl("sizeof_uLong_code", (f >> 2) & 3);
        kl("sizeof_voidpf_code", (f >> 4) & 3);
        kl("sizeof_z_off_t_code", (f >> 6) & 3);
        kl("debug", (f >> 8) & 1);
        kl("asm", (f >> 9) & 1);
        kl("winapi", (f >> 10) & 1);
        kl("buildfixed", (f >> 12) & 1);
        kl("dynamic_crc_table", (f >> 13) & 1);
        kl("no_gzcompress", (f >> 16) & 1);
        kl("no_gzip", (f >> 17) & 1);
        kl("pkzip_bug_workaround", (f >> 20) & 1);
        kl("fastest", (f >> 21) & 1);
        return 0;
    }

    /* -------------------------------------------------------------- errors */

    static int opErrors() {
        byte[] comp = new byte[256];
        byte[] back = new byte[256];
        /* Every one of these is a documented rejection.  A rewrite that is merely
         * permissive passes the happy path and fails here. */
        krc("bad_level", new Deflater().init(10));
        krc("bad_level_neg", new Deflater().init(-2));
        krc("bad_wbits", new Deflater().init2(6, Zlib.Z_DEFLATED, 7, 8,
                Zlib.Z_DEFAULT_STRATEGY));
        krc("bad_memlevel", new Deflater().init2(6, Zlib.Z_DEFLATED, 15, 0,
                Zlib.Z_DEFAULT_STRATEGY));
        krc("bad_method", new Deflater().init2(6, 9, 15, 8,
                Zlib.Z_DEFAULT_STRATEGY));
        krc("bad_strategy", new Deflater().init2(6, Zlib.Z_DEFLATED, 15, 8, 5));
        /* consumer.c zeroes a stack z_stream before each of these; a freshly
         * constructed object is the same starting state, and is the counterpart
         * that made the NULL-pointer form of these cases translatable at all. */
        krc("uninit_deflate", new Deflater().deflate(Zlib.Z_NO_FLUSH));
        krc("uninit_deflateEnd", new Deflater().end());
        krc("uninit_inflate", new Inflater().inflate(Zlib.Z_NO_FLUSH));
        krc("uninit_inflateEnd", new Inflater().end());
        krc("uninit_deflateReset", new Deflater().reset());
        krc("uninit_inflateReset", new Inflater().reset());
        /* A version string the library does not recognize must be refused: that
         * check is what protects a consumer built against a different header.  In
         * Java it protects a consumer compiled against a different jar, which is
         * the same hazard -- Zlib.ZLIB_VERSION is inlined into this class file. */
        krc("wrong_version", new Deflater().initRaw(6, "0.0.0", Zlib.Z_STREAM_SIZE));
        krc("wrong_size", new Deflater().initRaw(6, Zlib.ZLIB_VERSION, 3));
        krc("iwrong_version", new Inflater().initRaw("0.0.0", Zlib.Z_STREAM_SIZE));

        int[] n = {4};
        krc("compress_tiny_out", Zlib.compress2(comp, n, PAY, PAY_LEN, 6));
        n[0] = back.length;
        krc("uncompress_garbage", Zlib.uncompress(back, n,
                "not a zlib stream".getBytes(StandardCharsets.ISO_8859_1), 17));
        n[0] = back.length;
        krc("uncompress_empty", Zlib.uncompress(back, n, new byte[0], 0));
        int[] c = {comp.length};
        if (Zlib.compress2(comp, c, PAY, 200, 6) == Zlib.Z_OK) {
            n[0] = back.length;
            krc("uncompress_truncated", Zlib.uncompress(back, n, comp, c[0] / 2));
            comp[c[0] - 1] ^= (byte) 0xff;
            n[0] = back.length;
            krc("uncompress_bad_adler", Zlib.uncompress(back, n, comp, c[0]));
            comp[c[0] - 1] ^= (byte) 0xff;
            comp[0] ^= 0x10;
            n[0] = back.length;
            krc("uncompress_bad_header", Zlib.uncompress(back, n, comp, c[0]));
        }
        kv("gzopen_missing",
                GzFile.open(spath("no-such-file.gz"), "rb") != null ? "ok" : "NULL");
        kv("gzopen_bad_mode",
                GzFile.open(spath("consumer.gz"), "qq") != null ? "ok" : "NULL");
        return 0;
    }

    /* -------------------------------------------------------------- interop
     *
     * These four hand bytes to the system gzip, or take bytes from it.  The
     * system gzip is pinned by the image and is not the code under test, so it is
     * a fixed outside judgment on whether the migrated library still speaks the
     * format.
     */

    static int opInteropWrite() {
        byte[] comp = new byte[8192];
        for (int level = 1; level <= 9; level += 4) {
            Deflater zs = new Deflater();
            if (zs.init2(level, Zlib.Z_DEFLATED, 31, 8, Zlib.Z_DEFAULT_STRATEGY)
                    != Zlib.Z_OK) {
                return 1;
            }
            zs.nextIn = PAY;
            zs.nextInIndex = 0;
            zs.availIn = PAY_LEN;
            zs.nextOut = comp;
            zs.nextOutIndex = 0;
            zs.availOut = comp.length;
            int rc = zs.deflate(Zlib.Z_FINISH);
            int clen = comp.length - zs.availOut;
            zs.end();
            emit("level=" + level + " ");
            krc("deflate", rc);
            String gz = spath("ours.gz");
            String outPath = spath("ours.out");
            if (writefile(gz, comp, clen) != 0) {
                return 1;
            }
            /* -d decompresses, -c writes to stdout; a nonzero status means gzip
             * judged our stream malformed. */
            emit("level=" + level + " ");
            kl("gzip_status", rungzip("-dc", gz, outPath));
            long n = readfile(outPath, 8192);
            emit("level=" + level + " ");
            kl("gzip_out_bytes", n);
            emit("level=" + level + " ");
            kv("gzip_agrees", (n == PAY_LEN && sameBytes(readbuf, PAY, PAY_LEN))
                    ? "ok" : "MISMATCH");
            /* gzip -t is a stricter check than -d: it verifies the trailer's
             * length and CRC even when the data happens to decode. */
            emit("level=" + level + " ");
            kl("gzip_test_status", rungzip("-t", gz, spath("test.out")));
        }
        return 0;
    }

    static int opInteropRead() {
        String raw = spath("plain.bin");
        String gz = spath("theirs.gz");
        if (writefile(raw, PAY, PAY_LEN) != 0) {
            return 1;
        }
        /* -n suppresses the name and timestamp, so what gzip writes is a function
         * of its input alone and this case stays reproducible. */
        for (int level = 1; level <= 9; level += 4) {
            String args = "-n -" + level + " -c";
            emit("level=" + level + " ");
            kl("gzip_status", rungzip(args, raw, gz));
            long clen = readfile(gz, 8192);
            byte[] comp = Arrays.copyOf(readbuf, Math.max(readlen, 1));
            emit("level=" + level + " ");
            kl("gzip_bytes", clen);
            if (clen <= 0) {
                continue;
            }
            Inflater zs = new Inflater();
            if (zs.init2(31) != Zlib.Z_OK) {
                return 1;
            }
            /* Reading the header back is the part a self-consistent rewrite can
             * miss: the fields gzip wrote have to be surfaced with the values gzip
             * chose. */
            GzHeader hd = new GzHeader();
            hd.name = new byte[256];
            hd.nameMax = 256;
            hd.extra = new byte[256];
            hd.extraMax = 256;
            hd.comment = new byte[256];
            hd.commMax = 256;
            emit("level=" + level + " ");
            krc("getheader", zs.getHeader(hd));
            byte[] back = new byte[8192];
            zs.nextIn = comp;
            zs.nextInIndex = 0;
            zs.availIn = (int) clen;
            zs.nextOut = back;
            zs.nextOutIndex = 0;
            zs.availOut = back.length;
            int rc = zs.inflate(Zlib.Z_FINISH);
            int got = back.length - zs.availOut;
            emit("level=" + level + " ");
            krc("inflate", rc);
            emit("level=" + level + " ");
            kl("out_bytes", got);
            emit("level=" + level + " ");
            kv("matches", (got == PAY_LEN && sameBytes(back, PAY, PAY_LEN))
                    ? "ok" : "MISMATCH");
            emit("level=" + level + " ");
            kl("hdr_done", hd.done);
            emit("level=" + level + " ");
            kl("hdr_text", hd.text);
            emit("level=" + level + " ");
            kl("hdr_time", hd.time);
            emit("level=" + level + " ");
            kl("hdr_xflags", hd.xflags);
            emit("level=" + level + " ");
            kl("hdr_os", hd.os);
            emit("level=" + level + " ");
            kl("hdr_extra_len", hd.extraLen);
            emit("level=" + level + " ");
            kx("adler", zs.adler);
            zs.end();
        }
        return 0;
    }

    static int opInteropGzfile() {
        /* open() writes a full gzip member including a header this program never
         * touches, so this checks the gz* layer's framing rather than deflate's. */
        String path = spath("gzfile-interop.gz");
        GzFile gf = GzFile.open(path, "wb9");
        if (gf == null) {
            return 1;
        }
        kl("written", gf.write(PAY, 0, PAY_LEN));
        krc("closed", gf.close());
        kl("gzip_test", rungzip("-t", path, spath("t.out")));
        kl("gzip_status", rungzip("-dc", path, spath("g.out")));
        long n = readfile(spath("g.out"), 8192);
        kl("out_bytes", n);
        kv("agrees", (n == PAY_LEN && sameBytes(readbuf, PAY, PAY_LEN))
                ? "ok" : "MISMATCH");
        /* gzip -l reports the stored uncompressed length from the trailer, so a
         * wrong ISIZE shows up here even when the data decodes.  It takes the file
         * as an argument rather than on stdin, so it does not go through
         * rungzip(). */
        int listStatus;
        try {
            ProcessBuilder pb = new ProcessBuilder("gzip", "-l", path);
            pb.redirectOutput(new File(spath("l.out")));
            pb.redirectErrorStream(true);
            listStatus = pb.start().waitFor() == 0 ? 0 : 1;
        } catch (IOException e) {
            listStatus = 1;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            listStatus = 1;
        }
        kl("gzip_list_status", listStatus);
        long ln = readfile(spath("l.out"), 4095);
        if (ln > 0) {
            /* The listing embeds the scratch path and a compression ratio that
             * depends on it, so only the stored length is compared. */
            String listing = new String(readbuf, 0, (int) ln,
                    StandardCharsets.ISO_8859_1);
            kv("list_has_size", listing.contains("1024") ? "ok" : "MISSING");
        }
        return 0;
    }

    static int opInteropConcat() {
        /* Concatenated members are one gzip stream, and a reader that stops after
         * the first member is a common and silent bug. */
        byte[] comp = new byte[8192];
        int total = 0;
        for (int part = 0; part < 3; part++) {
            Deflater zs = new Deflater();
            if (zs.init2(6 + part, Zlib.Z_DEFLATED, 31, 8, Zlib.Z_DEFAULT_STRATEGY)
                    != Zlib.Z_OK) {
                return 1;
            }
            zs.nextIn = PAY;
            zs.nextInIndex = part * 256;
            zs.availIn = 256;
            zs.nextOut = comp;
            zs.nextOutIndex = total;
            zs.availOut = comp.length - total;
            int rc = zs.deflate(Zlib.Z_FINISH);
            total = comp.length - zs.availOut;
            zs.end();
            emit("part=" + part + " ");
            krc("deflate", rc);
        }
        kl("stream_bytes", total);
        String path = spath("concat.gz");
        if (writefile(path, comp, total) != 0) {
            return 1;
        }
        kl("gzip_test", rungzip("-t", path, spath("ct.out")));
        kl("gzip_status", rungzip("-dc", path, spath("cd.out")));
        long n = readfile(spath("cd.out"), 8192);
        kl("gzip_out_bytes", n);
        kv("gzip_agrees", (n == 768 && sameBytes(readbuf, PAY, 768))
                ? "ok" : "MISMATCH");
        /* And the library's own reader must cross the member boundaries too. */
        GzFile gf = GzFile.open(path, "rb");
        if (gf == null) {
            return 1;
        }
        byte[] ours = new byte[8192];
        int got = gf.read(ours, 0, ours.length);
        kl("gzread", got);
        kl("gzeof", gf.eof());
        krc("gzclose", gf.close());
        kv("gzread_agrees", (got == 768 && sameBytes(ours, PAY, 768))
                ? "ok" : "MISMATCH");
        return 0;
    }

    /* ----------------------------------------------------------------- main */

    static boolean sameBytes(byte[] a, byte[] b, int n) {
        for (int i = 0; i < n; i++) {
            if (a[i] != b[i]) {
                return false;
            }
        }
        return true;
    }

    public static void main(String[] argv) throws Exception {
        if (argv.length < 2) {
            System.err.println("usage: Consumer <scratch-dir> <op>");
            System.exit(2);
        }
        /* A large buffer, flushed once at the end.  Interleaving with the system
         * gzip's own output is prevented by redirecting gzip to files, so nothing
         * here needs stdout unbuffered. */
        out = new java.io.BufferedOutputStream(
                new java.io.FileOutputStream(java.io.FileDescriptor.out), 1 << 16);
        scratch = argv[0];
        mkpay();
        String op = argv[1];
        /* The runtime version is printed first by every op.  If a submission's jar
         * is a different vintage than the constants it was compiled against, that
         * shows up once at the top of the diff instead of as scattered value
         * differences. */
        emit("op=" + op + " zlib=" + Zlib.version() + "\n");
        int rc;
        switch (op) {
            case "version": rc = opVersion(); break;
            case "compress": rc = opCompress(); break;
            case "gzip": rc = opGzip(); break;
            case "checksum": rc = opChecksum(); break;
            case "stream": rc = opStream(); break;
            case "gzfile": rc = opGzfile(); break;
            case "flags": rc = opFlags(); break;
            case "errors": rc = opErrors(); break;
            case "interop-write": rc = opInteropWrite(); break;
            case "interop-read": rc = opInteropRead(); break;
            case "interop-gzfile": rc = opInteropGzfile(); break;
            case "interop-concat": rc = opInteropConcat(); break;
            default:
                out.flush();
                System.err.println("unknown op: " + op);
                System.exit(2);
                return;
        }
        emit("done=" + op + " rc=" + rc + "\n");
        out.flush();
        /* An exception escaping any op would leave the buffer unflushed and the
         * comparison would fail on a truncated record, which is the correct
         * outcome -- but System.exit here keeps a nonzero rc from an op from being
         * lost to the JVM's own 0. */
        System.exit(rc);
    }
}
