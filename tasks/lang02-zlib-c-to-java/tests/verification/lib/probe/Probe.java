/* Probe.java -- differential API consumer for lang02-zlib-c-to-java.
 *
 * The Java half of a matched pair.  probe.c is compiled against the pinned C
 * reference; this is compiled against the submitted jar; both are fed the same
 * case list, and their per-case output is compared byte for byte.  So the two
 * files are not merely similar programs: every record key, every value format
 * and every ordering decision here has to agree with probe.c exactly, and the
 * section order below follows probe.c's so the correspondence stays auditable.
 *
 * Three properties are deliberate, matching probe.c:
 *
 *   1. It uses ONLY the public org.zlib surface named in the source contract's
 *      api_contract, reached through the module path.  Nothing here reflects
 *      into an implementation package, and nothing imports java.util.zip -- the
 *      probe holds itself to the same rule the submission is graded on, because
 *      a probe that borrowed the JDK's zlib would be comparing that library
 *      against itself.
 *
 *   2. It is an interpreter, not a fixed script.  Cases arrive as data on
 *      stdin, so the catalog can describe thousands of workflows without this
 *      file changing.
 *
 *   3. Compressed bytes are compared exactly.  "Produces a valid deflate
 *      stream" is not the standard: "produces the same stream the C reference
 *      produced" is.
 *
 * Where C and Java genuinely differ, the difference is absorbed here rather
 * than allowed to reach the record:
 *
 *   - C's `unsigned long` checksums print as unsigned decimal.  Java's long is
 *      signed, so every checksum is masked to 32 bits before printing.
 *   - C reads `strm.msg`, a char*, and prints "(null)" when unset.  Java reads a
 *      String field and prints the same "(null)" for null.
 *   - C's next_in is an interior pointer.  Java's is (array, index), so every
 *      place probe.c advances a pointer, this advances an index over the same
 *      array.
 *   - Two cases had to be reshaped rather than translated, and both are marked
 *      where they appear: `abi` has no `layout` sub-case, because sizeof and
 *      offsetof have no JVM counterpart -- structure.py asks that question by
 *      reflection against the contract instead -- and `alloc` records no
 *      allocation counts, because a state object cannot be routed through a hook
 *      that hands back primitive arrays.  probe.c was changed to match on both,
 *      so the two sides still record identical keys.
 *
 * Protocol.  stdin: one case per line, tab-separated
 *     <case-id> TAB <op> [TAB <arg>]...
 * stdout: for each case, a header line, then the raw payload, then a newline
 *     #CASE TAB <case-id> TAB <status> TAB <payload-length> LF
 *     <payload bytes> LF
 * Payloads are arbitrary bytes including NUL, so they are length-prefixed.
 * Records are flushed as produced: if a case kills the JVM, everything before
 * it survives and the harness attributes the failure to exactly one case.
 *
 * argv[0] is the corpus directory, argv[1] an optional scratch directory for the
 * gz* file cases.
 */

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Arrays;
import java.util.HashMap;
import java.util.Map;

import org.zlib.Deflater;
import org.zlib.GzFile;
import org.zlib.GzHeader;
import org.zlib.InflateBack;
import org.zlib.Inflater;
import org.zlib.ZStream;
import org.zlib.Zlib;

public final class Probe {

    /* Above this many bytes a payload is recorded as length + SHA-256 + its
     * first and last 256 bytes rather than in full.  Exactness is preserved by
     * the digest; the head and tail keep a failure diff readable. */
    static final int FULL_BYTES_LIMIT = 8192;
    static final int EDGE_BYTES = 256;

    /* A hard ceiling on what one case may emit.  The step limit alone is not
     * enough: at a large outChunk an implementation that never terminates would
     * ask for hundreds of gigabytes before the step count ran out. */
    static final int MAX_CASE_BYTES = 24 << 20;

    static final long MAX_STEPS = 4000000L;

    static String corpusDir = ".";
    static String scratchDir = "/tmp/zprobe";

    static OutputStream rawOut;

    private Probe() {
    }

    /* ------------------------------------------------------------- buffers
     *
     * A growable byte sink.  ByteArrayOutputStream would do for most of this,
     * but the payload builder also needs formatted text appended in the same
     * order, so the two are wrapped together.
     */
    static final class Buf {
        byte[] p = new byte[256];
        int n;

        void grow(int need) {
            if (n + need <= p.length) {
                return;
            }
            int cap = p.length;
            while (cap < n + need) {
                cap *= 2;
            }
            p = Arrays.copyOf(p, cap);
        }

        void put(byte[] d, int off, int len) {
            if (len <= 0) {
                return;
            }
            grow(len);
            System.arraycopy(d, off, p, n, len);
            n += len;
        }

        void put(byte[] d) {
            put(d, 0, d.length);
        }

        void put(Buf other) {
            put(other.p, 0, other.n);
        }

        /* Text goes out as Latin-1 so one char is one byte: the records hold
         * only ASCII, and a UTF-8 encoder would silently widen any stray byte a
         * message string carried. */
        void puts(String s) {
            put(s.getBytes(StandardCharsets.ISO_8859_1));
        }

        void printf(String fmt, Object... args) {
            puts(String.format(fmt, args));
        }

        byte[] toArray() {
            return Arrays.copyOf(p, n);
        }
    }

    static void emit(String id, String status, Buf payload) {
        int len = payload == null ? 0 : payload.n;
        try {
            rawOut.write(String.format("#CASE\t%s\t%s\t%d\n", id, status, len)
                    .getBytes(StandardCharsets.ISO_8859_1));
            if (payload != null && len > 0) {
                rawOut.write(payload.p, 0, len);
            }
            rawOut.write('\n');
            rawOut.flush();
        } catch (IOException e) {
            System.err.println("probe: write failed: " + e);
            System.exit(70);
        }
    }

    static void die(String msg) {
        System.err.println("probe: " + msg);
        System.exit(70);
    }

    /* -------------------------------------------------------------- sha256
     *
     * probe.c carries its own SHA-256 rather than using the library under test.
     * Here MessageDigest is used instead: it is in java.base, it is not part of
     * the surface being graded, and it cannot be influenced by the submission.
     */
    static String digestHex(byte[] data, int off, int len) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            md.update(data, off, len);
            byte[] d = md.digest();
            StringBuilder sb = new StringBuilder(64);
            for (byte b : d) {
                sb.append(Character.forDigit((b >> 4) & 15, 16));
                sb.append(Character.forDigit(b & 15, 16));
            }
            return sb.toString();
        } catch (NoSuchAlgorithmException e) {
            die("no SHA-256");
            return null;
        }
    }

    /* ---------------------------------------------------------- rendering */

    static void putHex(Buf b, byte[] d, int off, int len) {
        b.grow(len * 2);
        for (int i = 0; i < len; i++) {
            int v = d[off + i] & 0xff;
            b.p[b.n++] = (byte) Character.forDigit(v >> 4, 16);
            b.p[b.n++] = (byte) Character.forDigit(v & 15, 16);
        }
    }

    /* Record a byte string.  Small ones go in whole and in hex, so a diff shows
     * the actual bytes; large ones are summarized exactly by digest plus edges. */
    static void putBytes(Buf b, String label, byte[] d, int len) {
        if (d == null) {
            d = new byte[0];
            len = 0;
        }
        b.printf("%s.len=%d\n%s.sha=%s\n", label, len, label, digestHex(d, 0, len));
        if (len <= FULL_BYTES_LIMIT) {
            b.printf("%s.hex=", label);
            putHex(b, d, 0, len);
            b.puts("\n");
        } else {
            b.printf("%s.head=", label);
            putHex(b, d, 0, EDGE_BYTES);
            b.printf("\n%s.tail=", label);
            putHex(b, d, len - EDGE_BYTES, EDGE_BYTES);
            b.puts("\n");
        }
    }

    /* ----------------------------------------------------------- decoding */

    static long argLong(String[] f, int index, long fallback) {
        if (index >= f.length || f[index] == null || f[index].isEmpty()) {
            return fallback;
        }
        try {
            return Long.parseLong(f[index].trim());
        } catch (NumberFormatException e) {
            /* C's strtol returns 0 on an unparseable string rather than
             * failing, and the catalog never emits one, so matching that
             * keeps a malformed line from diverging the two probes. */
            return 0L;
        }
    }

    static String argStr(String[] f, int index, String fallback) {
        return index < f.length && f[index] != null ? f[index] : fallback;
    }

    /* ------------------------------------------------------------- corpus */

    static final Map<Long, byte[]> BLOBS = new HashMap<>();
    static final byte[] EMPTY = new byte[0];

    /* Corpus entries are referenced by index and loaded on demand: a batch
     * touches a handful of payloads, and reading all of them would cost more
     * than the batch itself. */
    static byte[] corpus(long index) {
        if (index < 0 || index >= 4096) {
            return EMPTY;
        }
        byte[] hit = BLOBS.get(index);
        if (hit != null) {
            return hit;
        }
        Path path = Paths.get(corpusDir, "blobs", String.format("%05d.bin", index));
        byte[] data;
        try {
            data = Files.readAllBytes(path);
        } catch (IOException e) {
            System.err.println("probe: cannot open " + path + ": " + e.getMessage());
            System.exit(71);
            return null;
        }
        BLOBS.put(index, data);
        return data;
    }

    /* --------------------------------------------------------- zlib names
     *
     * Return codes are rendered as names rather than numbers so a payload diff
     * reads directly, and so a port that renumbered the constants -- which
     * javac inlines into every consumer, and which a round-trip test cannot
     * see -- shows up as a difference in every case rather than none.
     */
    static String zret(int code) {
        switch (code) {
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

    /* zlib sets msg to a static English string on error.  Those strings are
     * part of the observable surface -- callers do print msg -- so they are
     * compared too. */
    static void putMsg(Buf b, String label, String msg) {
        b.printf("%s.msg=%s\n", label, msg == null ? "(null)" : msg);
    }

    /* A snapshot of the public counters, taken before end() so the values still
     * mean something: end() is entitled to clear them, and dataType -- which
     * reports the block type the compressor settled on -- is one of the most
     * informative signals available. */
    static final class State {
        long totalIn;
        long totalOut;
        long adler;
        int dataType;
        String msg;
        int availIn;
        int availOut;

        static State of(ZStream z) {
            State s = new State();
            s.totalIn = z.totalIn;
            s.totalOut = z.totalOut;
            s.adler = z.adler;
            s.dataType = z.dataType;
            s.msg = z.msg;
            s.availIn = z.availIn;
            s.availOut = z.availOut;
            return s;
        }
    }

    static void putStream(Buf b, String label, State s) {
        b.printf("%s.total_in=%d\n%s.total_out=%d\n%s.adler=%d\n%s.data_type=%d\n",
                label, s.totalIn, label, s.totalOut, label, s.adler & 0xffffffffL,
                label, s.dataType);
        putMsg(b, label, s.msg);
    }

    /* ---------------------------------------------------------- flushing
     *
     * A flush schedule is (mode, interval): apply `mode` every `interval` input
     * chunks, Z_NO_FLUSH otherwise, and Z_FINISH once the input is exhausted.
     * Z_SYNC_FLUSH and Z_PARTIAL_FLUSH insert empty stored blocks and
     * Z_FULL_FLUSH additionally resets the window, so the schedule is directly
     * visible in the output bytes.  This is one of the strongest differential
     * signals available: getting the flush bookkeeping subtly wrong still
     * round-trips.
     */
    static int flushFor(long mode, long interval, long chunkIndex, boolean isFinal) {
        if (isFinal) {
            return Zlib.Z_FINISH;
        }
        if (mode == Zlib.Z_NO_FLUSH || interval <= 0) {
            return Zlib.Z_NO_FLUSH;
        }
        if ((chunkIndex + 1) % interval == 0) {
            return (int) mode;
        }
        return Zlib.Z_NO_FLUSH;
    }

    /* ------------------------------------------------------ deflate engine */

    static final class DParams {
        long level = Zlib.Z_DEFAULT_COMPRESSION;
        long strategy = Zlib.Z_DEFAULT_STRATEGY;
        long windowBits = 15;
        long memLevel = 8;
        long inChunk;                 /* 0 means "everything at once" */
        long outChunk;
        long flushMode = Zlib.Z_NO_FLUSH;
        long flushInterval;
        byte[] dict;
    }

    /* Run a full deflate over `src`, appending the compressed bytes to `out`.
     * Returns the final code and fills `st` with the end-of-stream state.
     *
     * The drain loop follows zlib's documented protocol exactly, for the reason
     * probe.c spells out at length: a return that leaves availOut == 0 means
     * deflate was output-bound and must be called again with the same flush and
     * fresh output space; a return that leaves room means it emitted everything
     * it had.  pending() must NOT be used to detect completion -- when flushing
     * exhausts the output buffer, pending reads zero for an instant while the
     * flush's empty stored block has not been written, so a loop that stops
     * there truncates the marker and silently corrupts the stream.
     *
     * The consequence is that output is not independent of outChunk for the
     * flushing modes, and that is correct rather than a defect: each early
     * return disarms the repeated-flush Z_BUF_ERROR guard, so the next call
     * appends another empty block.  The buffer size is genuinely observable in
     * the byte stream. */
    static int runDeflate(DParams p, byte[] src, Buf out, State[] stOut, Buf trace) {
        Deflater zs = new Deflater();
        int rc = zs.init2((int) p.level, Zlib.Z_DEFLATED, (int) p.windowBits,
                (int) p.memLevel, (int) p.strategy);
        if (rc != Zlib.Z_OK) {
            if (trace != null) {
                trace.printf("init=%s\n", zret(rc));
            }
            stOut[0] = State.of(zs);
            return rc;
        }
        if (p.dict != null) {
            int drc = zs.setDictionary(p.dict, p.dict.length);
            if (trace != null) {
                trace.printf("setdict=%s adler=%d\n", zret(drc), zs.adler & 0xffffffffL);
            }
            if (drc != Zlib.Z_OK) {
                zs.end();
                stOut[0] = State.of(zs);
                return drc;
            }
        }

        int srcLen = src.length;
        int inChunk = p.inChunk > 0 ? (int) p.inChunk : (srcLen != 0 ? srcLen : 1);
        int outChunk = p.outChunk > 0 ? (int) p.outChunk : 65536;
        byte[] obuf = new byte[outChunk];

        int consumed = 0;
        long chunkIndex = 0;
        boolean isFinal = false;
        rc = Zlib.Z_OK;
        long steps = 0;

        while (!isFinal) {
            int take = Math.min(srcLen - consumed, inChunk);
            zs.nextIn = src;
            zs.nextInIndex = consumed;
            zs.availIn = take;
            consumed += take;
            isFinal = consumed >= srcLen;
            int flush = flushFor(p.flushMode, p.flushInterval, chunkIndex, isFinal);
            chunkIndex++;

            for (;;) {
                zs.nextOut = obuf;
                zs.nextOutIndex = 0;
                zs.availOut = outChunk;
                rc = zs.deflate(flush);
                int produced = outChunk - zs.availOut;
                if (produced > 0) {
                    out.put(obuf, 0, produced);
                }
                if (++steps > MAX_STEPS || out.n > MAX_CASE_BYTES) {
                    rc = Zlib.Z_BUF_ERROR;
                    if (trace != null) {
                        trace.puts(out.n > MAX_CASE_BYTES ? "abort=byte-limit\n"
                                : "abort=step-limit\n");
                    }
                    isFinal = true;
                    break;
                }
                if (rc == Zlib.Z_STREAM_END) {
                    break;
                }
                if (rc != Zlib.Z_OK && rc != Zlib.Z_BUF_ERROR) {
                    isFinal = true;
                    break;
                }
                if (zs.availOut == 0) {
                    continue;         /* output-bound: the contract says call again */
                }
                if (zs.availIn != 0) {
                    continue;         /* input for this chunk is not consumed yet */
                }
                if (flush == Zlib.Z_FINISH) {
                    continue;         /* only Z_STREAM_END ends a finish */
                }
                break;                /* room left over: this call emitted it all */
            }
            if (rc != Zlib.Z_OK && rc != Zlib.Z_STREAM_END && rc != Zlib.Z_BUF_ERROR) {
                break;
            }
            if (rc == Zlib.Z_STREAM_END) {
                break;
            }
        }

        stOut[0] = State.of(zs);
        int endRc = zs.end();
        if (trace != null) {
            trace.printf("end=%s\n", zret(endRc));
        }
        return rc;
    }

    /* ------------------------------------------------------ inflate engine */

    static final class IParams {
        long windowBits = 15;
        long inChunk;
        long outChunk;
        byte[] dict;
    }

    static int runInflate(IParams p, byte[] src, int srcLen, Buf out, State[] stOut,
                          Buf trace) {
        Inflater zs = new Inflater();
        int rc = zs.init2((int) p.windowBits);
        if (rc != Zlib.Z_OK) {
            if (trace != null) {
                trace.printf("init=%s\n", zret(rc));
            }
            stOut[0] = State.of(zs);
            return rc;
        }

        int inChunk = p.inChunk > 0 ? (int) p.inChunk : (srcLen != 0 ? srcLen : 1);
        int outChunk = p.outChunk > 0 ? (int) p.outChunk : 65536;
        byte[] obuf = new byte[outChunk];

        int consumed = 0;
        long steps = 0;
        boolean needDictSeen = false;
        rc = Zlib.Z_OK;

        for (;;) {
            if (zs.availIn == 0) {
                if (consumed >= srcLen) {
                    break;
                }
                int take = Math.min(srcLen - consumed, inChunk);
                zs.nextIn = src;
                zs.nextInIndex = consumed;
                zs.availIn = take;
                consumed += take;
            }
            zs.nextOut = obuf;
            zs.nextOutIndex = 0;
            zs.availOut = outChunk;
            rc = zs.inflate(Zlib.Z_NO_FLUSH);
            int produced = outChunk - zs.availOut;
            if (produced > 0) {
                out.put(obuf, 0, produced);
            }
            if (rc == Zlib.Z_NEED_DICT) {
                needDictSeen = true;
                if (p.dict == null) {
                    if (trace != null) {
                        trace.puts("needdict=unsatisfied\n");
                    }
                    break;
                }
                int drc = zs.setDictionary(p.dict, p.dict.length);
                if (trace != null) {
                    trace.printf("setdict=%s\n", zret(drc));
                }
                if (drc != Zlib.Z_OK) {
                    rc = drc;
                    break;
                }
                continue;
            }
            if (rc == Zlib.Z_STREAM_END || (rc != Zlib.Z_OK && rc != Zlib.Z_BUF_ERROR)) {
                break;
            }
            if (++steps > MAX_STEPS || out.n > MAX_CASE_BYTES) {
                rc = Zlib.Z_BUF_ERROR;
                if (trace != null) {
                    trace.puts(out.n > MAX_CASE_BYTES ? "abort=byte-limit\n"
                            : "abort=step-limit\n");
                }
                break;
            }
            if (rc == Zlib.Z_BUF_ERROR && produced == 0 && zs.availIn == 0
                    && consumed >= srcLen) {
                break;
            }
        }

        if (trace != null && needDictSeen) {
            trace.puts("saw=Z_NEED_DICT\n");
        }
        stOut[0] = State.of(zs);
        int endRc = zs.end();
        if (trace != null) {
            trace.printf("end=%s\n", zret(endRc));
        }
        return rc;
    }

    static boolean sameBytes(Buf got, byte[] want) {
        if (got.n != want.length) {
            return false;
        }
        for (int i = 0; i < want.length; i++) {
            if (got.p[i] != want[i]) {
                return false;
            }
        }
        return true;
    }

    /* --------------------------------------------------------- op: deflate
     *
     * The central case.  Every argument is a knob the catalog sweeps:
     *   blob level strategy wbits memlevel in_chunk out_chunk flush_mode
     *   flush_interval dict_blob
     * The recorded payload is the compressed bytes plus the end state, so the
     * case fails if either the output or the bookkeeping differs.
     */
    static void opDeflate(Buf out, String[] f) {
        DParams p = new DParams();
        byte[] src = corpus(argLong(f, 2, 0));
        p.level = argLong(f, 3, Zlib.Z_DEFAULT_COMPRESSION);
        p.strategy = argLong(f, 4, Zlib.Z_DEFAULT_STRATEGY);
        p.windowBits = argLong(f, 5, 15);
        p.memLevel = argLong(f, 6, 8);
        p.inChunk = argLong(f, 7, 0);
        p.outChunk = argLong(f, 8, 0);
        p.flushMode = argLong(f, 9, Zlib.Z_NO_FLUSH);
        p.flushInterval = argLong(f, 10, 0);
        long dictIndex = argLong(f, 11, -1);
        if (dictIndex >= 0) {
            p.dict = corpus(dictIndex);
        }

        Buf comp = new Buf();
        Buf trace = new Buf();
        State[] st = new State[1];
        int rc = runDeflate(p, src, comp, st, trace);
        out.printf("rc=%s\nin.len=%d\n", zret(rc), src.length);
        putBytes(out, "comp", comp.p, comp.n);
        putStream(out, "st", st[0]);
        out.put(trace);

        /* An immediate re-inflate: it costs little and turns "the bytes differ"
         * into "the bytes differ AND they no longer decode", which separates a
         * cosmetic difference from a broken stream in the report. */
        if (rc == Zlib.Z_STREAM_END && p.windowBits >= -15) {
            IParams ip = new IParams();
            ip.windowBits = p.windowBits;
            ip.dict = p.dict;
            Buf back = new Buf();
            State[] ist = new State[1];
            int irc = runInflate(ip, comp.p, comp.n, back, ist, null);
            out.printf("verify.rc=%s\nverify.roundtrip=%s\n", zret(irc),
                    sameBytes(back, src) ? "identical" : "DIFFERENT");
        }
    }

    /* --------------------------------------------------------- op: inflate
     *
     * Inflate a stream produced here by a fixed reference-independent recipe, so
     * the case grades the decompressor on its own.  Because the compressed input
     * is produced by the same library, a submission that is self-consistently
     * wrong still fails -- the recorded payload includes the compressed digest.
     */
    static void opInflate(Buf out, String[] f) {
        byte[] src = corpus(argLong(f, 2, 0));
        long dwbits = argLong(f, 3, 15);
        long iwbits = argLong(f, 4, 15);
        long inChunk = argLong(f, 5, 0);
        long outChunk = argLong(f, 6, 0);
        long level = argLong(f, 7, 6);

        DParams dp = new DParams();
        dp.level = level;
        dp.windowBits = dwbits;
        Buf comp = new Buf();
        State[] dst = new State[1];
        int drc = runDeflate(dp, src, comp, dst, null);

        IParams ip = new IParams();
        ip.windowBits = iwbits;
        ip.inChunk = inChunk;
        ip.outChunk = outChunk;
        Buf back = new Buf();
        Buf trace = new Buf();
        State[] ist = new State[1];
        int irc = runInflate(ip, comp.p, comp.n, back, ist, trace);

        out.printf("deflate.rc=%s\ncomp.len=%d\n", zret(drc), comp.n);
        out.printf("comp.sha=%s\ninflate.rc=%s\n", digestHex(comp.p, 0, comp.n),
                zret(irc));
        putBytes(out, "out", back.p, back.n);
        out.printf("roundtrip=%s\n", sameBytes(back, src) ? "identical" : "DIFFERENT");
        putStream(out, "st", ist[0]);
        out.put(trace);
    }

    /* ----------------------------------------------------------- op: bound
     *
     * bound and compressBound are contracts downstream code allocates against:
     * a value that is too small makes a correct caller overflow.  The numbers
     * themselves are compared, not just the inequality, because a rewrite that
     * returned a wildly generous bound would silently break callers that size
     * fixed buffers from it.
     */
    static void opBound(Buf out, String[] f) {
        byte[] src = corpus(argLong(f, 2, 0));
        long level = argLong(f, 3, Zlib.Z_DEFAULT_COMPRESSION);
        long wbits = argLong(f, 4, 15);
        long mem = argLong(f, 5, 8);

        Deflater zs = new Deflater();
        int rc = zs.init2((int) level, Zlib.Z_DEFLATED, (int) wbits, (int) mem,
                Zlib.Z_DEFAULT_STRATEGY);
        out.printf("init=%s\n", zret(rc));
        if (rc == Zlib.Z_OK) {
            long bound = zs.bound(src.length);
            out.printf("deflateBound=%d\n", bound);
            zs.end();

            DParams dp = new DParams();
            dp.level = level;
            dp.windowBits = wbits;
            dp.memLevel = mem;
            Buf comp = new Buf();
            State[] dst = new State[1];
            int drc = runDeflate(dp, src, comp, dst, null);
            out.printf("actual=%d\nfits=%s\nrc=%s\n", comp.n,
                    comp.n <= bound ? "yes" : "NO", zret(drc));
        }
        out.printf("compressBound=%d\n", Zlib.compressBound(src.length));
    }

    /* ---------------------------------------------------------- op: oneshot
     *
     * compress / compress2 / uncompress / uncompress2.  These are the entry
     * points most applications actually call, and uncompress2's in-out length
     * semantics are easy to get wrong: it reports how much input it consumed,
     * which matters when a buffer holds trailing data.
     */
    static void opOneshot(Buf out, String[] f) {
        byte[] src = corpus(argLong(f, 2, 0));
        long level = argLong(f, 3, -2);      /* -2 means "use compress()" */
        long slack = argLong(f, 4, 0);       /* shrink the destination by this much */
        long trail = argLong(f, 5, 0);       /* extra bytes appended to the stream */

        int cap = (int) Zlib.compressBound(src.length);
        byte[] dest = new byte[cap != 0 ? cap : 1];
        int[] destLen = {cap};
        int rc = level == -2
                ? Zlib.compress(dest, destLen, src, src.length)
                : Zlib.compress2(dest, destLen, src, src.length, (int) level);
        out.printf("compress.rc=%s\n", zret(rc));
        putBytes(out, "comp", dest, rc == Zlib.Z_OK ? destLen[0] : 0);

        if (rc == Zlib.Z_OK) {
            int streamLen = destLen[0] + (int) (trail > 0 ? trail : 0);
            byte[] stream = new byte[streamLen != 0 ? streamLen : 1];
            System.arraycopy(dest, 0, stream, 0, destLen[0]);
            for (long i = 0; i < trail; i++) {
                stream[destLen[0] + (int) i] = (byte) (0x5a + i);
            }

            int room = src.length > slack ? src.length - (int) slack : 0;
            byte[] back = new byte[room + 1];
            int[] backLen = {room};
            int urc = Zlib.uncompress(back, backLen, stream, streamLen);
            out.printf("uncompress.rc=%s\nuncompress.len=%d\n", zret(urc), backLen[0]);
            if (urc == Zlib.Z_OK) {
                boolean same = backLen[0] == src.length
                        && Arrays.equals(back, 0, src.length, src, 0, src.length);
                out.printf("uncompress.match=%s\n", same ? "identical" : "DIFFERENT");
            }

            int[] back2Len = {room};
            int[] sourceLen = {streamLen};
            int u2 = Zlib.uncompress2(back, back2Len, stream, sourceLen);
            out.printf("uncompress2.rc=%s\nuncompress2.len=%d\nuncompress2.consumed=%d\n",
                    zret(u2), back2Len[0], sourceLen[0]);
        }
    }

    /* --------------------------------------------------------- op: checksum
     *
     * crc32 / adler32 and the combine functions.  These are the most commonly
     * reimplemented parts of zlib and the easiest to get subtly wrong: a table
     * generated with the wrong polynomial bit order, or a combine that
     * mishandles a zero length.  Incremental splitting is swept because a
     * rewrite that only handles the one-shot path fails only here.
     *
     * crc32_z and adler32_z collapse onto the same Java method as crc32 and
     * adler32 -- the length is an int either way on a JVM -- so the *_z rows
     * hold the same values by construction.  They are still recorded, and the
     * .agrees lines still computed, because probe.c records them and the two
     * records have to match key for key.
     */
    static void opChecksum(Buf out, String[] f) {
        byte[] src = corpus(argLong(f, 2, 0));
        long split = argLong(f, 3, 0);       /* chunk size, 0 = one shot */
        long seed = argLong(f, 4, 0);

        out.printf("crc32.init=%d\nadler32.init=%d\n",
                Zlib.crc32(seed, null, 0, 0) & 0xffffffffL,
                Zlib.adler32(seed, null, 0, 0) & 0xffffffffL);

        long crc = seed;
        long adl = seed;
        long crcz = seed;
        long adlz = seed;
        int step = split > 0 ? (int) split : (src.length != 0 ? src.length : 1);
        for (int off = 0; off < src.length; off += step) {
            int take = Math.min(src.length - off, step);
            crc = Zlib.crc32(crc, src, off, take);
            adl = Zlib.adler32(adl, src, off, take);
            crcz = Zlib.crc32(crcz, src, off, take);
            adlz = Zlib.adler32(adlz, src, off, take);
        }
        out.printf("crc32=%d\ncrc32_z=%d\nadler32=%d\nadler32_z=%d\n",
                crc & 0xffffffffL, crcz & 0xffffffffL,
                adl & 0xffffffffL, adlz & 0xffffffffL);
        out.printf("crc32.agrees=%s\nadler32.agrees=%s\n",
                crc == crcz ? "yes" : "NO", adl == adlz ? "yes" : "NO");

        /* Combine: split the payload, checksum the halves independently, and
         * rebuild the whole-payload value from the parts. */
        if (src.length >= 2) {
            int half = src.length / 2;
            long c1 = Zlib.crc32(0, src, 0, half);
            long c2 = Zlib.crc32(0, src, half, src.length - half);
            long cc = Zlib.crc32Combine(c1, c2, src.length - half);
            long wholeC = Zlib.crc32(0, src, 0, src.length);
            long a1 = Zlib.adler32(1, src, 0, half);
            long a2 = Zlib.adler32(1, src, half, src.length - half);
            long ac = Zlib.adler32Combine(a1, a2, src.length - half);
            long wholeA = Zlib.adler32(1, src, 0, src.length);
            out.printf("crc32_combine=%d\ncrc32.whole=%d\ncrc32.combine_ok=%s\n"
                            + "adler32_combine=%d\nadler32.whole=%d\nadler32.combine_ok=%s\n",
                    cc & 0xffffffffL, wholeC & 0xffffffffL,
                    cc == wholeC ? "yes" : "NO",
                    ac & 0xffffffffL, wholeA & 0xffffffffL,
                    ac == wholeA ? "yes" : "NO");
            /* A zero-length second part must be the identity. */
            out.printf("crc32_combine.zero=%d\nadler32_combine.zero=%d\n",
                    Zlib.crc32Combine(wholeC, Zlib.crc32(0, null, 0, 0), 0) & 0xffffffffL,
                    Zlib.adler32Combine(wholeA, Zlib.adler32(1, null, 0, 0), 0)
                            & 0xffffffffL);
        }
        out.printf("crc32_combine_gen=%d\n",
                Zlib.crc32CombineGen(src.length) & 0xffffffffL);
    }

    /* ------------------------------------------------------ op: statechange
     *
     * copy, reset, params, prime, tune, pending, getDictionary.  These are the
     * methods a rewrite is most likely to stub out, because nothing in a naive
     * round-trip test touches them -- but they are part of the published surface
     * and used by real callers (git and rsync both change parameters mid-stream).
     *
     * The case compresses the first half, performs the operation, compresses the
     * rest, and records the whole output.  A stubbed implementation diverges in
     * the bytes, not just in a return code.
     */
    static void opStatechange(Buf out, String[] f) {
        byte[] src = corpus(argLong(f, 2, 0));
        String what = argStr(f, 3, "reset");
        long level = argLong(f, 4, 6);
        long argA = argLong(f, 5, 0);
        long argB = argLong(f, 6, 0);

        Deflater zs = new Deflater();
        int rc = zs.init2((int) level, Zlib.Z_DEFLATED, 15, 8, Zlib.Z_DEFAULT_STRATEGY);
        out.printf("init=%s\n", zret(rc));
        if (rc != Zlib.Z_OK) {
            return;
        }

        Buf comp = new Buf();
        byte[] obuf = new byte[16384];
        int half = src.length / 2;

        /* Phase one: compress the first half without finishing. */
        zs.nextIn = src;
        zs.nextInIndex = 0;
        zs.availIn = half;
        while (zs.availIn > 0) {
            zs.nextOut = obuf;
            zs.nextOutIndex = 0;
            zs.availOut = obuf.length;
            int drc = zs.deflate(Zlib.Z_NO_FLUSH);
            comp.put(obuf, 0, obuf.length - zs.availOut);
            if (drc != Zlib.Z_OK) {
                break;
            }
        }
        out.printf("phase1.total_in=%d\nphase1.total_out=%d\n", zs.totalIn, zs.totalOut);

        if (what.equals("pending")) {
            int[] pending = {0};
            int[] bits = {0};
            int prc = zs.pending(pending, bits);
            out.printf("pending.rc=%s\npending.bytes=%d\npending.bits=%d\n",
                    zret(prc), pending[0], bits[0]);
        } else if (what.equals("params")) {
            zs.nextOut = obuf;
            zs.nextOutIndex = 0;
            zs.availOut = obuf.length;
            int prc = zs.params((int) argA, (int) argB);
            comp.put(obuf, 0, obuf.length - zs.availOut);
            out.printf("params.rc=%s\nparams.level=%d\nparams.strategy=%d\n",
                    zret(prc), argA, argB);
        } else if (what.equals("prime")) {
            int prc = zs.prime((int) argA, (int) argB);
            out.printf("prime.rc=%s\nprime.bits=%d\nprime.value=%d\n",
                    zret(prc), argA, argB);
        } else if (what.equals("tune")) {
            int prc = zs.tune((int) argA, (int) argB, 128, 64);
            out.printf("tune.rc=%s\n", zret(prc));
        } else if (what.equals("getdict")) {
            byte[] dict = new byte[32768];
            int[] dlen = {dict.length};
            int prc = zs.getDictionary(dict, dlen);
            out.printf("getdict.rc=%s\ngetdict.len=%d\n", zret(prc), dlen[0]);
            if (prc == Zlib.Z_OK) {
                putBytes(out, "getdict", dict, dlen[0]);
            }
            /* A null buffer with a length out-parameter must report the length only. */
            int[] probeLen = {0};
            int nrc = zs.getDictionary(null, probeLen);
            out.printf("getdict.null.rc=%s\ngetdict.null.len=%d\n", zret(nrc), probeLen[0]);
        } else if (what.equals("copy")) {
            Deflater dup = new Deflater();
            int crc = zs.copy(dup);
            out.printf("copy.rc=%s\n", zret(crc));
            if (crc == Zlib.Z_OK) {
                /* Finish the copy independently and record its output: a copy that
                 * shares state with the original produces different bytes here. */
                Buf dupout = new Buf();
                dup.nextIn = src;
                dup.nextInIndex = half;
                dup.availIn = src.length - half;
                int drc;
                do {
                    dup.nextOut = obuf;
                    dup.nextOutIndex = 0;
                    dup.availOut = obuf.length;
                    drc = dup.deflate(Zlib.Z_FINISH);
                    dupout.put(obuf, 0, obuf.length - dup.availOut);
                } while (drc == Zlib.Z_OK);
                out.printf("copy.finish=%s\n", zret(drc));
                putBytes(out, "copy.out", dupout.p, dupout.n);
                out.printf("copy.total_in=%d\ncopy.total_out=%d\n",
                        dup.totalIn, dup.totalOut);
                dup.end();
            }
        } else if (what.equals("reset")) {
            int prc = zs.reset();
            out.printf("reset.rc=%s\nreset.total_in=%d\nreset.total_out=%d\n",
                    zret(prc), zs.totalIn, zs.totalOut);
            /* After a reset the stream must behave as freshly initialized, so the
             * output from here is directly comparable with a fresh deflate. */
            comp.n = 0;
        }

        /* Phase two: finish from wherever the operation left the stream. */
        zs.nextIn = src;
        zs.nextInIndex = half;
        zs.availIn = src.length - half;
        int drc;
        do {
            zs.nextOut = obuf;
            zs.nextOutIndex = 0;
            zs.availOut = obuf.length;
            drc = zs.deflate(Zlib.Z_FINISH);
            comp.put(obuf, 0, obuf.length - zs.availOut);
        } while (drc == Zlib.Z_OK);
        out.printf("finish=%s\n", zret(drc));
        putBytes(out, "comp", comp.p, comp.n);
        out.printf("final.total_in=%d\nfinal.total_out=%d\nfinal.adler=%d\n",
                zs.totalIn, zs.totalOut, zs.adler & 0xffffffffL);
        putMsg(out, "final", zs.msg);
        out.printf("end=%s\n", zret(zs.end()));
    }

    /* -------------------------------------------------------- op: gzheader
     *
     * setHeader / getHeader.  The gzip header is a wire format with fixed field
     * offsets, and the round trip through a GzHeader is the only way a caller
     * reads a member's name, comment or extra field.  A rewrite that treats the
     * header as opaque bytes fails here.
     *
     * MTIME is set explicitly rather than left to the clock, so the case is
     * reproducible: upstream never calls time() itself, and neither may a port.
     */
    static void opGzheader(Buf out, String[] f) {
        byte[] src = corpus(argLong(f, 2, 0));
        long variant = argLong(f, 3, 0);
        long level = argLong(f, 4, 6);

        byte[] extraField = new byte[64];
        for (int i = 0; i < extraField.length; i++) {
            extraField[i] = (byte) (i * 3 + 1);
        }

        GzHeader head = new GzHeader();
        head.time = 0x5f5e100;   /* fixed, not the clock */
        head.os = 3;
        head.text = (variant & 1) != 0 ? 1 : 0;
        if ((variant & 2) != 0) {
            head.name = cstr("payload.bin");
        }
        if ((variant & 4) != 0) {
            head.comment = cstr("a comment field with spaces and punctuation!");
        }
        if ((variant & 8) != 0) {
            head.extra = extraField;
            head.extraLen = extraField.length;
        }
        if ((variant & 16) != 0) {
            head.hcrc = 1;
        }
        if ((variant & 32) != 0) {
            head.time = 0;
            head.os = 255;
        }

        Deflater zs = new Deflater();
        int rc = zs.init2((int) level, Zlib.Z_DEFLATED, 15 + 16, 8,
                Zlib.Z_DEFAULT_STRATEGY);
        out.printf("init=%s\n", zret(rc));
        if (rc != Zlib.Z_OK) {
            return;
        }
        out.printf("setheader=%s\n", zret(zs.setHeader(head)));

        Buf comp = new Buf();
        byte[] obuf = new byte[16384];
        zs.nextIn = src;
        zs.nextInIndex = 0;
        zs.availIn = src.length;
        int drc;
        do {
            zs.nextOut = obuf;
            zs.nextOutIndex = 0;
            zs.availOut = obuf.length;
            drc = zs.deflate(Zlib.Z_FINISH);
            comp.put(obuf, 0, obuf.length - zs.availOut);
        } while (drc == Zlib.Z_OK);
        out.printf("deflate=%s\n", zret(drc));
        putBytes(out, "comp", comp.p, comp.n);
        zs.end();

        /* Read the header back through the public object. */
        Inflater iz = new Inflater();
        int irc = iz.init2(15 + 16);
        out.printf("inflate.init=%s\n", zret(irc));
        if (irc == Zlib.Z_OK) {
            GzHeader got = new GzHeader();
            byte[] nameBuf = new byte[128];
            byte[] commentBuf = new byte[256];
            byte[] extraBuf = new byte[128];
            got.name = nameBuf;
            got.nameMax = nameBuf.length;
            got.comment = commentBuf;
            got.commMax = commentBuf.length;
            got.extra = extraBuf;
            got.extraMax = extraBuf.length;
            out.printf("getheader=%s\n", zret(iz.getHeader(got)));

            Buf back = new Buf();
            iz.nextIn = comp.p;
            iz.nextInIndex = 0;
            iz.availIn = comp.n;
            int rc2;
            do {
                iz.nextOut = obuf;
                iz.nextOutIndex = 0;
                iz.availOut = obuf.length;
                rc2 = iz.inflate(Zlib.Z_NO_FLUSH);
                back.put(obuf, 0, obuf.length - iz.availOut);
            } while (rc2 == Zlib.Z_OK && iz.availIn > 0);
            out.printf("inflate.rc=%s\n", zret(rc2));
            out.printf("head.done=%d\nhead.text=%d\nhead.time=%d\nhead.xflags=%d\n"
                            + "head.os=%d\nhead.extra_len=%d\nhead.hcrc=%d\n",
                    got.done, got.text, got.time & 0xffffffffL, got.xflags, got.os,
                    got.extraLen, got.hcrc);
            out.printf("head.name=%s\n", got.name == null ? "(null)" : fromCstr(got.name));
            out.printf("head.comment=%s\n",
                    got.comment == null ? "(null)" : fromCstr(got.comment));
            if (got.extraLen != 0 && got.extraLen <= extraBuf.length) {
                out.puts("head.extra=");
                putHex(out, extraBuf, 0, got.extraLen);
                out.puts("\n");
            }
            out.printf("roundtrip=%s\n", sameBytes(back, src) ? "identical" : "DIFFERENT");
            iz.end();
        }
    }

    /* ----------------------------------------------------- op: inflatestate
     *
     * The inflate-side introspection and state surface: mark, codesUsed,
     * validate, undermine, syncPoint, getDictionary, copy, reset, reset2, prime,
     * sync.  Each is called on a stream that has been driven genuinely
     * mid-stream first, because every one of them answers a different question
     * about interior state and a stub that returns a plausible constant is only
     * caught by asking mid-stream.
     *
     *   blob what wbits arg
     */
    static void opInflatestate(Buf out, String[] f) {
        byte[] src = corpus(argLong(f, 2, 0));
        String what = argStr(f, 3, "reset");
        long wbits = argLong(f, 4, 15);
        long argA = argLong(f, 5, 0);

        /* A stream to work on, produced here. */
        DParams dp = new DParams();
        dp.level = 6;
        dp.windowBits = wbits;
        Buf comp = new Buf();
        State[] dst = new State[1];
        runDeflate(dp, src, comp, dst, null);

        Inflater iz = new Inflater();
        int rc = iz.init2((int) wbits);
        out.printf("init=%s\n", zret(rc));
        if (rc != Zlib.Z_OK) {
            return;
        }

        byte[] obuf = new byte[16384];
        Buf back = new Buf();
        /* Inflate roughly half the stream so the state is genuinely mid-stream. */
        int feed = comp.n / 2;
        iz.nextIn = comp.p;
        iz.nextInIndex = 0;
        iz.availIn = feed;
        int irc = Zlib.Z_OK;
        while (iz.availIn > 0 && irc == Zlib.Z_OK) {
            iz.nextOut = obuf;
            iz.nextOutIndex = 0;
            iz.availOut = obuf.length;
            irc = iz.inflate(Zlib.Z_NO_FLUSH);
            back.put(obuf, 0, obuf.length - iz.availOut);
        }
        out.printf("mid.rc=%s\nmid.total_in=%d\nmid.total_out=%d\n",
                zret(irc), iz.totalIn, iz.totalOut);

        if (what.equals("mark")) {
            /* The packing is contractual: callers unpack both halves, so both are
             * recorded.  C prints the whole value as a signed long, and -65536 for
             * an unusable state has to come back as -65536 here too. */
            long mark = iz.mark();
            out.printf("mark=%d\nmark.bits=%d\nmark.length=%d\n",
                    mark, mark >> 16, mark & 0xffff);
        } else if (what.equals("codesused")) {
            out.printf("codesUsed=%d\n", iz.codesUsed());
        } else if (what.equals("validate")) {
            int vrc = iz.validate((int) argA);
            out.printf("validate.rc=%s\nvalidate.check=%d\n", zret(vrc), argA);
        } else if (what.equals("undermine")) {
            int urc = iz.undermine((int) argA);
            out.printf("undermine.rc=%s\n", zret(urc));
        } else if (what.equals("syncpoint")) {
            out.printf("syncPoint=%d\n", iz.syncPoint());
        } else if (what.equals("getdict")) {
            byte[] dict = new byte[32768];
            int[] dlen = {dict.length};
            int grc = iz.getDictionary(dict, dlen);
            out.printf("getdict.rc=%s\ngetdict.len=%d\n", zret(grc), dlen[0]);
            if (grc == Zlib.Z_OK && dlen[0] != 0) {
                putBytes(out, "getdict", dict, dlen[0]);
            }
        } else if (what.equals("copy")) {
            Inflater dup = new Inflater();
            int crc = iz.copy(dup);
            out.printf("copy.rc=%s\n", zret(crc));
            if (crc == Zlib.Z_OK) {
                Buf dupout = new Buf();
                dup.nextIn = comp.p;
                dup.nextInIndex = feed;
                dup.availIn = comp.n - feed;
                int rc2 = Zlib.Z_OK;
                while (rc2 == Zlib.Z_OK) {
                    dup.nextOut = obuf;
                    dup.nextOutIndex = 0;
                    dup.availOut = obuf.length;
                    rc2 = dup.inflate(Zlib.Z_NO_FLUSH);
                    dupout.put(obuf, 0, obuf.length - dup.availOut);
                    if (dup.availIn == 0 && rc2 != Zlib.Z_STREAM_END) {
                        break;
                    }
                }
                out.printf("copy.rc2=%s\ncopy.total_out=%d\n", zret(rc2), dup.totalOut);
                putBytes(out, "copy.out", dupout.p, dupout.n);
                dup.end();
            }
        } else if (what.equals("reset")) {
            int prc = iz.reset();
            out.printf("reset.rc=%s\nreset.total_in=%d\n", zret(prc), iz.totalIn);
            back.n = 0;
            /* Re-inflate the whole stream from the reset state. */
            iz.nextIn = comp.p;
            iz.nextInIndex = 0;
            iz.availIn = comp.n;
            int rc2 = Zlib.Z_OK;
            while (rc2 == Zlib.Z_OK) {
                iz.nextOut = obuf;
                iz.nextOutIndex = 0;
                iz.availOut = obuf.length;
                rc2 = iz.inflate(Zlib.Z_NO_FLUSH);
                back.put(obuf, 0, obuf.length - iz.availOut);
                if (iz.availIn == 0 && rc2 != Zlib.Z_STREAM_END) {
                    break;
                }
            }
            out.printf("after_reset.rc=%s\n", zret(rc2));
            out.printf("after_reset.match=%s\n",
                    sameBytes(back, src) ? "identical" : "DIFFERENT");
        } else if (what.equals("reset2")) {
            int prc = iz.reset2((int) argA);
            out.printf("reset2.rc=%s\nreset2.wbits=%d\n", zret(prc), argA);
        } else if (what.equals("prime")) {
            int prc = iz.prime((int) argA, 0);
            out.printf("prime.rc=%s\n", zret(prc));
            /* Draining the primed bits must be reported by mark(). */
            out.printf("prime.mark=%d\n", iz.mark());
        } else if (what.equals("sync")) {
            int prc = iz.sync();
            out.printf("sync.rc=%s\nsync.total_in=%d\n", zret(prc), iz.totalIn);
        }

        putBytes(out, "out", back.p, back.n);
        putMsg(out, "mid", iz.msg);
        out.printf("end=%s\n", zret(iz.end()));
    }

    /* ------------------------------------------------------ op: syncrecover
     *
     * sync()'s actual job: resynchronize after damage by finding the next
     * full-flush marker.  This is separate from the `sync` case in
     * opInflatestate, which calls sync() on an undamaged stream that has no
     * marker in it -- a call that can only scan to the end and report
     * Z_DATA_ERROR, and therefore never exercises the search itself.
     *
     * The marker a Z_FULL_FLUSH leaves is five bytes: 00 00 00 ff ff (an empty
     * stored block, byte-aligned, then LEN=0000 NLEN=ffff).  The search walks the
     * input counting how much of `00 00 ff ff` it has, and the interesting branch
     * is the one taken on a zero byte that arrives when two zeroes are already
     * banked: that third zero is not a mismatch, it is the first zero of the
     * marker whose second zero it also is.  A search that resets its counter
     * there walks straight past every real marker, because every real marker has
     * that third zero in front of it.  Only a stream that contains one can tell
     * the two apart.
     *
     *   blob damage wbits chunk arg
     */
    static void opSyncrecover(Buf out, String[] f) {
        byte[] src = corpus(argLong(f, 2, 0));
        String damage = argStr(f, 3, "flip");
        long wbits = argLong(f, 4, 15);
        long chunk = argLong(f, 5, 4096);
        long arg = argLong(f, 6, 0);

        /* Full flushes every chunk, so the stream carries several markers. */
        DParams dp = new DParams();
        dp.level = 6;
        dp.windowBits = wbits;
        dp.inChunk = chunk > 0 ? chunk : 4096;
        dp.flushMode = Zlib.Z_FULL_FLUSH;
        dp.flushInterval = 1;
        Buf comp = new Buf();
        State[] dst = new State[1];
        int drc = runDeflate(dp, src, comp, dst, null);
        out.printf("deflate.rc=%s\ndeflate.len=%d\n", zret(drc), comp.n);

        /* Where the markers are.  Reported, because a rewrite whose full flush
         * emits a different marker fails here rather than somewhere downstream. */
        int markers = 0;
        int firstMarker = 0;
        for (int i = 0; i + 4 < comp.n; i++) {
            if (comp.p[i] == 0 && comp.p[i + 1] == 0 && comp.p[i + 2] == 0
                    && (comp.p[i + 3] & 0xff) == 0xff && (comp.p[i + 4] & 0xff) == 0xff) {
                if (markers == 0) {
                    firstMarker = i;
                }
                markers++;
            }
        }
        out.printf("markers=%d\nmarker.first=%d\n", markers, firstMarker);

        if (comp.n == 0) {
            return;
        }

        /* Damage.  Each kind is a different shape of loss a real archive suffers,
         * and each leaves the search starting from a different place. */
        int start = 0;                 /* where the damaged stream begins */
        if (damage.equals("flip")) {
            /* One bit inside the first block, so the header is intact and the
             * failure happens mid-symbol. */
            int at = comp.n > 40 ? 20 + (int) (arg % 16) : comp.n / 2;
            comp.p[at] ^= 0x40;
            out.printf("damage=flip at=%d\n", at);
        } else if (damage.equals("zero")) {
            /* A run of zeroes, which is also a run of near-markers: the search has
             * to carry its count across them and not fire early. */
            int at = comp.n > 64 ? 24 : comp.n / 4;
            int n = (int) (arg > 0 ? arg : 8);
            if (at + n > comp.n) {
                n = comp.n - at;
            }
            Arrays.fill(comp.p, at, at + n, (byte) 0);
            out.printf("damage=zero at=%d n=%d\n", at, n);
        } else if (damage.equals("behead")) {
            /* The head is gone entirely -- no zlib header, no first block.  This is
             * the case that has nothing but the marker to find, and the one a naive
             * search fails outright. */
            start = comp.n > 32 ? 12 + (int) (arg % 8) : 1;
            out.printf("damage=behead start=%d\n", start);
        } else if (damage.equals("prefix")) {
            /* A partial marker in front of the real one: 00 00 00 00 ff, then the
             * stream.  The count has to survive the extra zeroes and the false ff. */
            byte[] lure = {0x00, 0x00, 0x00, 0x00, (byte) 0xff};
            Buf lured = new Buf();
            lured.put(lure, 0, lure.length);
            lured.put(comp.p, 0, comp.n);
            comp = lured;
            out.printf("damage=prefix added=%d\n", lure.length);
        } else if (damage.equals("truncate")) {
            /* Cut after the first marker, so the recovery runs out of input rather
             * than reaching the end of the stream. */
            int keep = firstMarker != 0 ? firstMarker + 5 + (int) (arg % 64) : comp.n / 2;
            if (keep > comp.n) {
                keep = comp.n;
            }
            comp.n = keep;
            out.printf("damage=truncate keep=%d\n", keep);
        }

        Inflater iz = new Inflater();
        int rc = iz.init2((int) wbits);
        out.printf("init=%s\n", zret(rc));
        if (rc != Zlib.Z_OK) {
            return;
        }

        byte[] obuf = new byte[16384];
        Buf back = new Buf();
        iz.nextIn = comp.p;
        iz.nextInIndex = start;
        iz.availIn = comp.n - start;

        /* First pass: inflate until it stops, whatever the reason. */
        int irc = Zlib.Z_OK;
        while (irc == Zlib.Z_OK && iz.availIn > 0) {
            iz.nextOut = obuf;
            iz.nextOutIndex = 0;
            iz.availOut = obuf.length;
            irc = iz.inflate(Zlib.Z_NO_FLUSH);
            back.put(obuf, 0, obuf.length - iz.availOut);
            if (back.n > MAX_CASE_BYTES) {
                break;
            }
        }
        out.printf("first.rc=%s\nfirst.total_in=%d\nfirst.total_out=%d\n",
                zret(irc), iz.totalIn, iz.totalOut);
        putMsg(out, "first", iz.msg);
        out.printf("first.syncpoint=%d\n", iz.syncPoint());
        int beforeSync = back.n;

        /* The resynchronization, and how far it moved. */
        int srcRc = iz.sync();
        out.printf("sync.rc=%s\nsync.total_in=%d\nsync.avail_in=%d\n",
                zret(srcRc), iz.totalIn, iz.availIn);
        putMsg(out, "sync", iz.msg);

        /* Second pass: what comes back out after the resync.  A stream that
         * resumed at the wrong marker decodes to different bytes, not to
         * nothing. */
        if (srcRc == Zlib.Z_OK) {
            int rc2 = Zlib.Z_OK;
            while (rc2 == Zlib.Z_OK) {
                iz.nextOut = obuf;
                iz.nextOutIndex = 0;
                iz.availOut = obuf.length;
                rc2 = iz.inflate(Zlib.Z_NO_FLUSH);
                back.put(obuf, 0, obuf.length - iz.availOut);
                if (iz.availIn == 0 && rc2 != Zlib.Z_STREAM_END) {
                    break;
                }
                if (back.n > MAX_CASE_BYTES) {
                    break;
                }
            }
            out.printf("second.rc=%s\nsecond.total_out=%d\nsecond.gained=%d\n",
                    zret(rc2), iz.totalOut, back.n - beforeSync);
            putMsg(out, "second", iz.msg);
        }

        /* Recovered bytes, and whether they are a tail of the original.  Both
         * matter: the digest catches a wrong resume point, the suffix flag says
         * whether the recovery was coherent at all. */
        putBytes(out, "recovered", back.p, back.n);
        boolean suffix = back.n <= src.length && back.n > 0;
        if (suffix) {
            for (int i = 0; i < back.n; i++) {
                if (src[src.length - back.n + i] != back.p[i]) {
                    suffix = false;
                    break;
                }
            }
        }
        out.printf("recovered.is_suffix=%s\n", suffix ? "yes" : "no");
        out.printf("end=%s\n", zret(iz.end()));
    }

    /* ----------------------------------------------------------- op: error
     *
     * Corrupt a valid stream in a specific way and record exactly how the
     * library complains: the return code, the message string, and how many bytes
     * it consumed before noticing.  Error behavior is part of the interface --
     * callers branch on Z_DATA_ERROR versus Z_BUF_ERROR -- and it is where a
     * rewrite that "works" usually differs most.
     */
    static void opError(Buf out, String[] f) {
        byte[] src = corpus(argLong(f, 2, 0));
        String kind = argStr(f, 3, "flipbyte");
        long offset = argLong(f, 4, 0);
        long wbits = argLong(f, 5, 15);

        DParams dp = new DParams();
        dp.level = 6;
        dp.windowBits = wbits;
        Buf comp = new Buf();
        State[] dst = new State[1];
        runDeflate(dp, src, comp, dst, null);
        if (comp.n == 0) {
            out.puts("rc=Z_STREAM_ERROR\nnote=empty-stream\n");
            return;
        }

        /* Build the corrupted variant. */
        Buf bad = new Buf();
        bad.put(comp.p, 0, comp.n);
        int pos = (int) (offset < 0 ? 0 : offset);
        if (pos >= bad.n) {
            pos = bad.n - 1;
        }

        if (kind.equals("flipbyte")) {
            bad.p[pos] ^= (byte) 0xff;
        } else if (kind.equals("flipbit")) {
            bad.p[pos] ^= 0x01;
        } else if (kind.equals("truncate")) {
            bad.n = bad.n > pos ? bad.n - pos : 0;
        } else if (kind.equals("truncate-head")) {
            bad.n = pos;
        } else if (kind.equals("zero")) {
            bad.p[pos] = 0;
        } else if (kind.equals("append")) {
            byte[] junk = {(byte) 0xde, (byte) 0xad, (byte) 0xbe, (byte) 0xef,
                    0x00, 0x11, 0x22, 0x33};
            bad.put(junk, 0, junk.length);
        } else if (kind.equals("checksum")) {
            /* Corrupt the trailing adler32/crc32 only, so the compressed data
             * itself is valid and only the audit check fails. */
            if (bad.n >= 4) {
                bad.p[bad.n - 1] ^= 0x01;
            }
        } else if (kind.equals("header")) {
            bad.p[0] ^= 0x0f;
        } else if (kind.equals("empty")) {
            bad.n = 0;
        } else if (kind.equals("swap")) {
            if (bad.n >= 2) {
                int other = (pos + 1) % bad.n;
                byte t = bad.p[pos];
                bad.p[pos] = bad.p[other];
                bad.p[other] = t;
            }
        }

        out.printf("input.len=%d\ninput.sha=%s\n", bad.n, digestHex(bad.p, 0, bad.n));

        Inflater iz = new Inflater();
        int rc = iz.init2((int) wbits);
        if (rc != Zlib.Z_OK) {
            out.printf("init=%s\n", zret(rc));
            return;
        }
        byte[] obuf = new byte[16384];
        Buf back = new Buf();
        iz.nextIn = bad.p;
        iz.nextInIndex = 0;
        iz.availIn = bad.n;
        int irc = Zlib.Z_OK;
        long guard = 0;
        /* Drive to a terminal answer rather than stopping when the input runs
         * out.  A stream whose trailing checksum was corrupted consumes every
         * byte and only then reports Z_DATA_ERROR, so a loop that breaks on "no
         * input left" records Z_OK and grades nothing.  Once the input is
         * exhausted, inflate is called with availIn == 0 until it stops making
         * progress, which is how it reports the verdict on the stream as a
         * whole. */
        int drained = 0;
        while (guard++ < 100000) {
            iz.nextOut = obuf;
            iz.nextOutIndex = 0;
            iz.availOut = obuf.length;
            int beforeIn = iz.availIn;
            irc = iz.inflate(Zlib.Z_NO_FLUSH);
            int produced = obuf.length - iz.availOut;
            back.put(obuf, 0, produced);
            if (irc != Zlib.Z_OK) {
                break;
            }
            if (iz.availIn == 0) {
                /* No input left.  Allow exactly one more no-progress call to
                 * elicit the final code, then stop: a second one would spin. */
                if (produced == 0 && beforeIn == 0) {
                    if (drained++ != 0) {
                        break;
                    }
                }
            }
        }
        out.printf("rc=%s\nconsumed=%d\nproduced=%d\n", zret(irc), iz.totalIn,
                iz.totalOut);
        putMsg(out, "err", iz.msg);
        /* How much correct output was recovered before the error matters:
         * callers that stream to a consumer have already forwarded it. */
        int prefix = Math.min(back.n, src.length);
        int good = 0;
        while (good < prefix && back.p[good] == src[good]) {
            good++;
        }
        out.printf("valid_prefix=%d\nout.len=%d\n", good, back.n);
        out.printf("out.sha=%s\nend=%s\n", digestHex(back.p, 0, back.n),
                zret(iz.end()));
    }

    /* ------------------------------------------------------ op: inflateback
     *
     * inflateBack is a separate decompressor with a callback interface, used by
     * gzip itself.  It shares the inflate tables but none of the stream loop, so
     * it is a distinct code path that a rewrite can easily leave unimplemented
     * while every ordinary inflate case passes.
     *
     * C hands the read callback a pointer it may point directly into the
     * caller's buffer.  The Java form returns a byte[] instead, so the reader
     * here copies the slice it wants to hand over: the interface is what
     * changed, not the protocol -- an empty or null return still means "no more
     * input", and a non-zero write return still aborts.
     */
    static final class BackIn implements InflateBack.In {
        byte[] data;
        int pos;
        int chunk;

        @Override
        public byte[] read(Object desc) {
            if (pos >= data.length) {
                return null;
            }
            int take = data.length - pos;
            if (chunk > 0 && take > chunk) {
                take = chunk;
            }
            byte[] slice = Arrays.copyOfRange(data, pos, pos + take);
            pos += take;
            return slice;
        }
    }

    static final class BackOut implements InflateBack.Out {
        Buf out = new Buf();

        @Override
        public int write(Object desc, byte[] data, int len) {
            out.put(data, 0, len);
            return 0;
        }
    }

    static void opInflateback(Buf out, String[] f) {
        byte[] src = corpus(argLong(f, 2, 0));
        long inChunk = argLong(f, 3, 0);
        long wbits = argLong(f, 4, 15);
        int absBits = (int) (wbits < 0 ? -wbits : wbits);

        /* inflateBack consumes raw deflate, so the producer uses negative
         * windowBits. */
        DParams dp = new DParams();
        dp.level = 6;
        dp.windowBits = -absBits;
        Buf comp = new Buf();
        State[] dst = new State[1];
        int drc = runDeflate(dp, src, comp, dst, null);
        out.printf("deflate.rc=%s\ncomp.len=%d\n", zret(drc), comp.n);

        InflateBack zs = new InflateBack();
        byte[] window = new byte[1 << absBits];
        int rc = zs.init(absBits, window);
        out.printf("backinit=%s\n", zret(rc));
        if (rc == Zlib.Z_OK) {
            BackIn inState = new BackIn();
            inState.data = Arrays.copyOf(comp.p, comp.n);
            inState.chunk = (int) (inChunk > 0 ? inChunk : 0);
            BackOut outState = new BackOut();
            int brc = zs.inflateBack(inState, outState, null);
            out.printf("inflateBack=%s\n", zret(brc));
            putMsg(out, "back", zs.msg);
            out.printf("roundtrip=%s\n",
                    sameBytes(outState.out, src) ? "identical" : "DIFFERENT");
            putBytes(out, "out", outState.out.p, outState.out.n);
            out.printf("backend=%s\n", zret(zs.end()));
        }
    }

    /* ---------------------------------------------------------- op: gzfile
     *
     * The gz* API is a stdio-shaped layer over gzip streams: 30 of State A's 88
     * exported symbols live here, and they collapse into 26 GzFile methods.  It
     * has its own buffering, its own error reporting through error(), and a seek
     * that decompresses forward.  It also writes real files, so this op is the
     * only one that touches the filesystem -- into a scratch directory, with the
     * file removed afterwards.
     *
     * The bytes of the file itself are compared, which is where the gzip header
     * gets graded: an OS byte of 3, a zero MTIME, and no clock reads.
     */
    static void opGzfile(Buf out, String[] f) {
        byte[] src = corpus(argLong(f, 2, 0));
        String what = argStr(f, 3, "roundtrip");
        long level = argLong(f, 4, 6);
        long argA = argLong(f, 5, 0);
        long argB = argLong(f, 6, 0);

        Path path = Paths.get(scratchDir,
                String.format("gz-%s-%d-%d-%d.gz", what, level, argA, argB));
        try {
            Files.deleteIfExists(path);
        } catch (IOException e) {
            /* A leftover the next open will truncate anyway. */
        }

        String mode = "wb" + (level >= 0 && level <= 9 ? level : 6);
        GzFile gz = GzFile.open(path.toString(), mode);
        if (gz == null) {
            out.puts("gzopen=NULL\n");
            return;
        }
        out.printf("gzopen=ok\ngzdirect.write=%d\n", gz.direct());

        /* Write the payload, in chunks if asked, so write()'s buffering is
         * exercised rather than bypassed by one large call. */
        int step = argA > 0 ? (int) argA : (src.length != 0 ? src.length : 1);
        int written = 0;
        int wrc = 0;
        while (written < src.length) {
            int take = Math.min(src.length - written, step);
            wrc = gz.write(src, written, take);
            if (wrc <= 0) {
                break;
            }
            written += wrc;
            if (argB == 1) {
                gz.flush(Zlib.Z_SYNC_FLUSH);
            } else if (argB == 2) {
                gz.flush(Zlib.Z_FULL_FLUSH);
            }
        }
        out.printf("written=%d\nlast_write=%d\n", written, wrc);
        if (what.equals("printf")) {
            int prc = gz.printf("|%s|%d|%d|", new Object[]{"tail", 42, src.length});
            out.printf("gzprintf=%d\n", prc);
        }
        if (what.equals("putc")) {
            out.printf("gzputc=%d\n", gz.putc(0x41));
            out.printf("gzputs=%d\n", gz.puts("puts-tail\n"));
        }
        out.printf("gzoffset.write=%d\ngztell.write=%d\n", (int) gz.offset(), gz.tell());
        out.printf("gzclose=%s\n", zret(gz.close()));

        /* The compressed file's bytes are part of the comparison: open()'s
         * header, including its OS byte and zero mtime, must match the
         * reference. */
        try {
            byte[] raw = Files.readAllBytes(path);
            putBytes(out, "file", raw, raw.length);
        } catch (IOException e) {
            out.puts("file=UNREADABLE\n");
        }

        /* Read it back through whichever accessor the case names. */
        gz = GzFile.open(path.toString(), "rb");
        if (gz == null) {
            out.puts("reopen=NULL\n");
            quietDelete(path);
            return;
        }
        out.printf("reopen=ok\ngzdirect.read=%d\n", gz.direct());
        Buf back = new Buf();

        if (what.equals("gets")) {
            byte[] line = new byte[512];
            int lines = 0;
            while (gz.gets(line, line.length) != null) {
                /* C measures the returned string with strlen; the array carries
                 * the same NUL, so the length is read the same way. */
                int len = 0;
                while (len < line.length && line[len] != 0) {
                    len++;
                }
                back.put(line, 0, len);
                lines++;
                if (lines > 20000) {
                    break;
                }
            }
            out.printf("lines=%d\n", lines);
        } else if (what.equals("getc")) {
            int c;
            int count = 0;
            byte[] one = new byte[1];
            while ((c = gz.getc()) != -1) {
                one[0] = (byte) c;
                back.put(one, 0, 1);
                count++;
                /* Push one byte back and re-read it, every 1000 bytes. */
                if (count % 1000 == 0) {
                    gz.ungetc(c);
                    int again = gz.getc();
                    if (again != c) {
                        out.printf("ungetc.mismatch@%d\n", count);
                    }
                }
                if (count > 200000) {
                    break;
                }
            }
            out.printf("getc.count=%d\n", count);
        } else if (what.equals("seek")) {
            /* Seek forward, read, rewind, read again: seek() decompresses
             * forward and rewind() restarts the stream. */
            long target = src.length / 3;
            long landed = gz.seek(target, 0);
            out.printf("gzseek=%d\ngztell=%d\n", landed, gz.tell());
            byte[] tmp = new byte[4096];
            int got = gz.read(tmp, 0, tmp.length);
            out.printf("after_seek.read=%d\n", got);
            if (got > 0) {
                putBytes(out, "after_seek", tmp, got);
            }
            /* Sequenced deliberately, one call per statement, to match probe.c --
             * where these were sibling arguments to one bprintf and the compiler
             * was free to read the EOF flag before performing the rewind, which
             * it did.  Java's evaluation order is specified, so this half could
             * not reproduce the C half's answer and the case failed on undefined
             * behaviour in the oracle rather than on the port.  Both flags are
             * read now, in a stated order, on both halves. */
            int eofPre = gz.eof();
            int rewound = gz.rewind();
            int eofPost = gz.eof();
            out.printf("gzeof.pre_rewind=%d\ngzrewind=%d\ngzeof.mid=%d\n",
                    eofPre, rewound, eofPost);
            int total = 0;
            while ((got = gz.read(tmp, 0, tmp.length)) > 0) {
                back.put(tmp, 0, got);
                total += got;
            }
            out.printf("after_rewind.total=%d\n", total);
        } else if (what.equals("fread")) {
            byte[] tmp = new byte[4096];
            long items;
            while ((items = gz.fread(tmp, 4, tmp.length / 4)) > 0) {
                back.put(tmp, 0, (int) items * 4);
            }
            out.puts("gzfread.done=1\n");
        } else {
            byte[] tmp = new byte[4096];
            int rstep = argA > 0 && argA < tmp.length ? (int) argA : tmp.length;
            int got;
            while ((got = gz.read(tmp, 0, rstep)) > 0) {
                back.put(tmp, 0, got);
            }
            out.printf("last_read=%d\n", got);
        }

        int[] errnum = {0};
        String msg = gz.error(errnum);
        out.printf("gzeof=%d\ngzerror.num=%d\ngzerror.msg=%s\n", gz.eof(), errnum[0],
                msg == null ? "(null)" : msg);
        out.printf("gztell.read=%d\ngzoffset.read=%d\n", gz.tell(), gz.offset());
        gz.clearerr();
        out.printf("after_clearerr.eof=%d\n", gz.eof());
        out.printf("close_r=%s\n", zret(gz.closeRead()));

        putBytes(out, "out", back.p, back.n);
        out.printf("roundtrip=%s\n", sameBytes(back, src) ? "identical" : "DIFFERENT");
        quietDelete(path);
    }

    static void quietDelete(Path path) {
        try {
            Files.deleteIfExists(path);
        } catch (IOException e) {
            /* The scratch directory is discarded with the run. */
        }
    }

    /* ------------------------------------------------------------- op: abi
     *
     * The parts of the interface that are not behavior: the numeric values of
     * the public constants, the strings from version() and errorString(), the
     * compile-flag word, and what every entry point does when handed a stream it
     * never initialized or an argument it must refuse.
     *
     * The constants matter for the same reason in Java as in C: javac inlines a
     * `static final int` at its use site, so a renumbered constant is a break an
     * already-compiled consumer cannot see.
     *
     * probe.c's `layout` sub-case is absent on both sides.  sizeof and offsetof
     * have no JVM counterpart, and the question they asked -- is the
     * caller-visible structure what the contract says -- is asked by
     * structure.py, which reads the jar's classes back by reflection against the
     * contract.
     */
    static void opAbi(Buf out, String[] f) {
        String what = argStr(f, 2, "constants");

        if (what.equals("constants")) {
            out.printf("Z_NO_FLUSH=%d\n", Zlib.Z_NO_FLUSH);
            out.printf("Z_PARTIAL_FLUSH=%d\n", Zlib.Z_PARTIAL_FLUSH);
            out.printf("Z_SYNC_FLUSH=%d\n", Zlib.Z_SYNC_FLUSH);
            out.printf("Z_FULL_FLUSH=%d\n", Zlib.Z_FULL_FLUSH);
            out.printf("Z_FINISH=%d\n", Zlib.Z_FINISH);
            out.printf("Z_BLOCK=%d\n", Zlib.Z_BLOCK);
            out.printf("Z_TREES=%d\n", Zlib.Z_TREES);
            out.printf("Z_OK=%d\n", Zlib.Z_OK);
            out.printf("Z_STREAM_END=%d\n", Zlib.Z_STREAM_END);
            out.printf("Z_NEED_DICT=%d\n", Zlib.Z_NEED_DICT);
            out.printf("Z_ERRNO=%d\n", Zlib.Z_ERRNO);
            out.printf("Z_STREAM_ERROR=%d\n", Zlib.Z_STREAM_ERROR);
            out.printf("Z_DATA_ERROR=%d\n", Zlib.Z_DATA_ERROR);
            out.printf("Z_MEM_ERROR=%d\n", Zlib.Z_MEM_ERROR);
            out.printf("Z_BUF_ERROR=%d\n", Zlib.Z_BUF_ERROR);
            out.printf("Z_VERSION_ERROR=%d\n", Zlib.Z_VERSION_ERROR);
            out.printf("Z_NO_COMPRESSION=%d\n", Zlib.Z_NO_COMPRESSION);
            out.printf("Z_BEST_SPEED=%d\n", Zlib.Z_BEST_SPEED);
            out.printf("Z_BEST_COMPRESSION=%d\n", Zlib.Z_BEST_COMPRESSION);
            out.printf("Z_DEFAULT_COMPRESSION=%d\n", Zlib.Z_DEFAULT_COMPRESSION);
            out.printf("Z_FILTERED=%d\n", Zlib.Z_FILTERED);
            out.printf("Z_HUFFMAN_ONLY=%d\n", Zlib.Z_HUFFMAN_ONLY);
            out.printf("Z_RLE=%d\n", Zlib.Z_RLE);
            out.printf("Z_FIXED=%d\n", Zlib.Z_FIXED);
            out.printf("Z_DEFAULT_STRATEGY=%d\n", Zlib.Z_DEFAULT_STRATEGY);
            out.printf("Z_BINARY=%d\n", Zlib.Z_BINARY);
            out.printf("Z_TEXT=%d\n", Zlib.Z_TEXT);
            out.printf("Z_ASCII=%d\n", Zlib.Z_ASCII);
            out.printf("Z_UNKNOWN=%d\n", Zlib.Z_UNKNOWN);
            out.printf("Z_DEFLATED=%d\n", Zlib.Z_DEFLATED);
            out.printf("Z_NULL=%d\n", Zlib.Z_NULL);
            out.printf("MAX_WBITS=%d\n", Zlib.MAX_WBITS);
            out.printf("MAX_MEM_LEVEL=%d\n", Zlib.MAX_MEM_LEVEL);
            out.printf("ZLIB_VERSION=%s\n", Zlib.ZLIB_VERSION);
            out.printf("ZLIB_VERNUM=0x%x\n", Zlib.ZLIB_VERNUM);
            out.printf("ZLIB_VER_MAJOR=%d\nZLIB_VER_MINOR=%d\nZLIB_VER_REVISION=%d\n",
                    Zlib.ZLIB_VER_MAJOR, Zlib.ZLIB_VER_MINOR, Zlib.ZLIB_VER_REVISION);
            out.printf("ZLIB_VER_SUBREVISION=%d\n", Zlib.ZLIB_VER_SUBREVISION);
            return;
        }

        if (what.equals("runtime")) {
            out.printf("zlibVersion=%s\n", Zlib.version());
            out.printf("zlibCompileFlags=0x%x\n", Zlib.compileFlags());
            /* The compile-flag bit fields, decoded.  On a JVM there is no sizeof
             * to report, so the contract pins the word to what a caller reading
             * it must keep seeing: the same widths State A advertised. */
            long flags = Zlib.compileFlags();
            out.printf("flags.sizeof_uInt=%d\nflags.sizeof_uLong=%d\n",
                    flags & 0x3, (flags >> 2) & 0x3);
            out.printf("flags.sizeof_voidpf=%d\nflags.sizeof_z_off_t=%d\n",
                    (flags >> 4) & 0x3, (flags >> 6) & 0x3);
            out.printf("flags.debug=%d\nflags.asm=%d\nflags.winapi=%d\n",
                    (flags >> 8) & 1, (flags >> 9) & 1, (flags >> 10) & 1);
            out.printf("flags.builtin_memcpy=%d\nflags.dynamic_crc=%d\n",
                    (flags >> 16) & 1, (flags >> 17) & 1);
            out.printf("flags.no_gzfile=%d\nflags.no_gzip=%d\n",
                    (flags >> 20) & 1, (flags >> 21) & 1);
            out.printf("flags.pkzip_bug=%d\nflags.fastest=%d\n",
                    (flags >> 24) & 1, (flags >> 25) & 1);
            for (int code = 2; code >= -6; code--) {
                out.printf("zError(%d)=%s\n", code, Zlib.errorString(code));
            }
            /* crcTable must return a 256-entry table whose contents are fixed by
             * the CRC-32 polynomial; the first and last entries pin it, and the
             * sum catches a permuted one. */
            int[] table = Zlib.crcTable();
            out.printf("crc_table[0]=%d\ncrc_table[1]=%d\ncrc_table[255]=%d\n",
                    table[0] & 0xffffffffL, table[1] & 0xffffffffL,
                    table[255] & 0xffffffffL);
            long sum = 0;
            for (int i = 0; i < 256; i++) {
                sum += table[i] & 0xffffffffL;
            }
            out.printf("crc_table.sum=%d\n", sum);
            return;
        }

        if (what.equals("defensive")) {
            /* What every entry point does when the stream it is handed was never
             * initialized, and again after it was ended.  probe.c drives a
             * zeroed z_stream for this; the counterpart is a freshly constructed
             * object whose init was never called.  Both ask the same question of
             * the same code: the state check.
             *
             * Nothing here may throw.  An exception escaping into the probe is
             * recorded as a crash against exactly this case, which is the right
             * verdict: a caller that gets an unchecked exception where the
             * interface documents Z_STREAM_ERROR has been broken. */
            Deflater zs = new Deflater();
            out.printf("deflate=%s\n", zret(zs.deflate(Zlib.Z_NO_FLUSH)));
            out.printf("deflateEnd=%s\n", zret(zs.end()));
            out.printf("deflateReset=%s\n", zret(zs.reset()));
            out.printf("deflateResetKeep=%s\n", zret(zs.resetKeep()));
            out.printf("deflateParams=%s\n", zret(zs.params(6, 0)));
            out.printf("deflateSetDictionary=%s\n",
                    zret(zs.setDictionary(new byte[]{'x'}, 1)));
            int[] dlen = {0};
            out.printf("deflateGetDictionary=%s\n", zret(zs.getDictionary(null, dlen)));
            out.printf("deflatePrime=%s\n", zret(zs.prime(1, 0)));
            out.printf("deflateTune=%s\n", zret(zs.tune(8, 8, 8, 8)));
            out.printf("deflateSetHeader=%s\n", zret(zs.setHeader(null)));
            int[] pending = {0};
            int[] bits = {0};
            out.printf("deflatePending=%s\n", zret(zs.pending(pending, bits)));
            /* bound() answers even on an unusable stream: without parameters it
             * has to return the larger bound plus a wrapper, because a caller
             * sizing a buffer needs a number it can trust rather than an
             * error. */
            out.printf("deflateBound.0=%d\ndeflateBound.1000=%d\n",
                    zs.bound(0), zs.bound(1000));
            out.printf("deflateCopy=%s\n", zret(zs.copy(new Deflater())));

            Inflater iz = new Inflater();
            out.printf("inflate=%s\n", zret(iz.inflate(Zlib.Z_NO_FLUSH)));
            out.printf("inflateEnd=%s\n", zret(iz.end()));
            out.printf("inflateReset=%s\n", zret(iz.reset()));
            out.printf("inflateReset2=%s\n", zret(iz.reset2(15)));
            out.printf("inflateResetKeep=%s\n", zret(iz.resetKeep()));
            out.printf("inflateSetDictionary=%s\n",
                    zret(iz.setDictionary(new byte[]{'x'}, 1)));
            int[] idlen = {0};
            out.printf("inflateGetDictionary=%s\n", zret(iz.getDictionary(null, idlen)));
            out.printf("inflateSync=%s\n", zret(iz.sync()));
            out.printf("inflatePrime=%s\n", zret(iz.prime(1, 0)));
            out.printf("inflateGetHeader=%s\n", zret(iz.getHeader(null)));
            out.printf("inflateValidate=%s\n", zret(iz.validate(1)));
            out.printf("inflateUndermine=%s\n", zret(iz.undermine(1)));
            out.printf("inflateSyncPoint=%s\n", zret(iz.syncPoint()));
            /* Both of these report an unusable stream with a sentinel rather
             * than a code, and the sentinel is part of the contract: -65536 is
             * (-1 << 16), which unpacks to a back distance of -1. */
            out.printf("inflateMark=%d\n", iz.mark());
            out.printf("inflateCodesUsed=%d\n", iz.codesUsed());
            out.printf("inflateCopy=%s\n", zret(iz.copy(new Inflater())));
            InflateBack bs = new InflateBack();
            out.printf("inflateBackEnd=%s\n", zret(bs.end()));
            byte[] window = new byte[1 << 15];
            out.printf("inflateBackInit.nullwin=%s\n", zret(bs.init(15, null)));
            out.printf("inflateBackInit.wbits7=%s\n", zret(bs.init(7, window)));
            out.printf("inflateBackInit.wbits16=%s\n", zret(bs.init(16, window)));

            /* After a clean end, every call must refuse again: end() releases the
             * state and a stream that kept working afterwards would be using
             * memory it gave back. */
            Deflater d2 = new Deflater();
            int irc = d2.init2(6, Zlib.Z_DEFLATED, 15, 8, Zlib.Z_DEFAULT_STRATEGY);
            out.printf("after_end.init=%s\n", zret(irc));
            if (irc == Zlib.Z_OK) {
                out.printf("after_end.end=%s\n", zret(d2.end()));
                out.printf("after_end.deflate=%s\n", zret(d2.deflate(Zlib.Z_FINISH)));
                out.printf("after_end.reset=%s\n", zret(d2.reset()));
                out.printf("after_end.end2=%s\n", zret(d2.end()));
            }
            Inflater i2 = new Inflater();
            irc = i2.init2(15);
            out.printf("after_end.inflate_init=%s\n", zret(irc));
            if (irc == Zlib.Z_OK) {
                out.printf("after_end.inflate_end=%s\n", zret(i2.end()));
                out.printf("after_end.inflate=%s\n",
                        zret(i2.inflate(Zlib.Z_NO_FLUSH)));
                out.printf("after_end.inflate_reset=%s\n", zret(i2.reset()));
                out.printf("after_end.inflate_mark=%d\n", i2.mark());
            }

            /* Empty buffers with a zero length: the documented way to query a
             * checksum's initial value.  C passes a null pointer here; Java
             * passes a null array, which is the same statement. */
            out.printf("crc32.null=%d\n", Zlib.crc32(0, null, 0, 0) & 0xffffffffL);
            out.printf("adler32.null=%d\n", Zlib.adler32(0, null, 0, 0) & 0xffffffffL);
            out.printf("crc32.seeded=%d\n",
                    Zlib.crc32(12345, null, 0, 0) & 0xffffffffL);
            out.printf("adler32.seeded=%d\n",
                    Zlib.adler32(99999, null, 0, 0) & 0xffffffffL);

            /* The one-shot helpers must report a destination that is too small
             * rather than writing past it. */
            byte[] payload = new byte[64];
            byte[] small = new byte[4];
            int[] smallLen = {small.length};
            out.printf("compress.tight=%s\n",
                    zret(Zlib.compress(small, smallLen, payload, payload.length)));
            int[] zeroLen = {0};
            out.printf("compress.zero=%s\n",
                    zret(Zlib.compress(small, zeroLen, payload, payload.length)));
            int[] ulen = {small.length};
            out.printf("uncompress.garbage=%s\n",
                    zret(Zlib.uncompress(small, ulen, payload, payload.length)));

            /* A path that cannot be opened must come back as a null handle, not
             * as a handle that fails later. */
            out.printf("gzopen.missing=%s\n",
                    GzFile.open("/nonexistent/path/x.gz", "rb") == null ? "NULL" : "ok");
            out.printf("gzopen.badmode=%s\n",
                    GzFile.open("/nonexistent/path/x.gz", "qq") == null ? "NULL" : "ok");
            return;
        }

        if (what.equals("badargs")) {
            /* Out-of-range parameters must be rejected, not clamped: a caller
             * that passes level 10 has a bug and needs to hear about it.  This is
             * also why init is a method returning int rather than a constructor:
             * a constructor cannot report Z_STREAM_ERROR. */
            int[][] bad = {
                {10, 15, 8, 0}, {-2, 15, 8, 0}, {-100, 15, 8, 0},
                {6, 16, 8, 0}, {6, 7, 8, 0}, {6, 0, 8, 0}, {6, 1, 8, 0},
                {6, 48, 8, 0}, {6, 15, 0, 0}, {6, 15, 10, 0}, {6, 15, -1, 0},
                {6, 15, 8, 5}, {6, 15, 8, -1}, {6, 15, 8, 99},
                {6, -16, 8, 0}, {6, -7, 8, 0}, {6, 32, 8, 0},
            };
            for (int[] row : bad) {
                Deflater zs = new Deflater();
                int rc = zs.init2(row[0], Zlib.Z_DEFLATED, row[1], row[2], row[3]);
                out.printf("deflateInit2(%d,%d,%d,%d)=%s\n", row[0], row[1], row[2],
                        row[3], zret(rc));
                if (rc == Zlib.Z_OK) {
                    zs.end();
                }
            }
            int[] iwbits = {16, 7, 0, 1, 48, 49, -16, -7, 32, 47, 31, 8, -8, 15, 40};
            for (int wb : iwbits) {
                Inflater iz = new Inflater();
                int rc = iz.init2(wb);
                out.printf("inflateInit2(%d)=%s\n", wb, zret(rc));
                if (rc == Zlib.Z_OK) {
                    iz.end();
                }
            }
            /* An unsupported method must be refused. */
            for (int method = 0; method <= 9; method++) {
                if (method == Zlib.Z_DEFLATED) {
                    continue;
                }
                Deflater zs = new Deflater();
                int rc = zs.init2(6, method, 15, 8, 0);
                out.printf("method(%d)=%s\n", method, zret(rc));
                if (rc == Zlib.Z_OK) {
                    zs.end();
                }
            }
            /* A version string that does not match must be refused: this is the
             * mechanism that protects a caller compiled against a different
             * header, and it survives the port because a jar on the class path
             * can be swapped underneath a compiled consumer just as a shared
             * object can.  The size argument keeps its State A meaning through
             * Z_STREAM_SIZE. */
            out.printf("version.mismatch=%s\n",
                    zret(new Deflater().initRaw(6, "0.0.0", Zlib.Z_STREAM_SIZE)));
            out.printf("size.mismatch=%s\n",
                    zret(new Deflater().initRaw(6, Zlib.ZLIB_VERSION, 1)));
            out.printf("inflate.version.mismatch=%s\n",
                    zret(new Inflater().initRaw("0.0.0", Zlib.Z_STREAM_SIZE)));
            out.printf("inflate.size.mismatch=%s\n",
                    zret(new Inflater().initRaw(Zlib.ZLIB_VERSION, 1)));
            /* The major-version-only match that zlib documents as acceptable. */
            Deflater ok = new Deflater();
            int orc = ok.initRaw(6, "1.0.0", Zlib.Z_STREAM_SIZE);
            out.printf("version.major_only=%s\n", zret(orc));
            if (orc == Zlib.Z_OK) {
                ok.end();
            }
            return;
        }

        out.puts("unknown-abi-subop\n");
    }

    /* ----------------------------------------------------------- op: alloc
     *
     * The caller-supplied allocation hooks, which survive the port as
     * ZStream.Allocator / ZStream.Deallocator.  probe.c's counterpart explains at
     * length why nothing here counts allocations: a JVM port cannot route a
     * state *object* through a hook that returns primitive arrays, so its count
     * is necessarily different from the reference's and grading the number would
     * fail every correct port.  What is graded is what any honest implementation
     * agrees on -- the hook was used, the opaque reference arrived unchanged,
     * every block came back, the bytes are identical, and a refused allocation
     * becomes Z_MEM_ERROR rather than an exception.
     *
     * The allocator returns the array type the size argument names: 1 -> byte[],
     * 2 -> short[], 4 -> int[], 8 -> long[].  A library that asked for four-byte
     * items and got a byte[] would have to reinterpret it, which is exactly the
     * pointer arithmetic a JVM does not do, so the mapping is part of the
     * contract rather than a convenience.
     */
    static final class Counting implements ZStream.Allocator, ZStream.Deallocator {
        final Object magic;
        long calls;              /* hook entries, including the refused ones */
        long allocs;
        long frees;
        long failAfter;
        boolean opaqueOk = true;

        Counting(Object magic, long failAfter) {
            this.magic = magic;
            this.failAfter = failAfter;
        }

        @Override
        public Object allocate(Object opaque, int items, int size) {
            /* The opaque reference must arrive unchanged; a rewrite that drops it
             * breaks every caller that keeps an arena there.  Identity, not
             * equality: a copy would be a different object to the caller. */
            if (opaque != magic) {
                opaqueOk = false;
            }
            calls++;
            if (failAfter > 0 && calls >= failAfter) {
                return null;
            }
            Object block;
            switch (size) {
                case 1: block = new byte[items]; break;
                case 2: block = new short[items]; break;
                case 4: block = new int[items]; break;
                case 8: block = new long[items]; break;
                default: return null;   /* an item size the contract does not name */
            }
            allocs++;
            return block;
        }

        @Override
        public void release(Object opaque, Object address) {
            if (opaque != magic) {
                opaqueOk = false;
            }
            if (address != null) {
                frees++;
            }
        }
    }

    static void opAlloc(Buf out, String[] f) {
        byte[] src = corpus(argLong(f, 2, 0));
        long failAfter = argLong(f, 3, 0);
        long level = argLong(f, 4, 6);
        long wbits = argLong(f, 5, 15);
        long mem = argLong(f, 6, 8);
        boolean inflateSide = argLong(f, 7, 0) != 0;

        Object magic = new Object();
        Counting hooks = new Counting(magic, failAfter);

        Deflater zs = new Deflater();
        zs.allocator = hooks;
        zs.deallocator = hooks;
        zs.opaque = magic;

        int rc = zs.init2((int) level, Zlib.Z_DEFLATED, (int) wbits, (int) mem,
                Zlib.Z_DEFAULT_STRATEGY);
        /* opaque_ok is false when the hook was never called at all, matching
         * probe.c, where the flag starts zeroed and only an actual call can set
         * it.  An implementation that ignores the hooks must not be able to
         * claim the opaque reference survived them. */
        out.printf("init=%s\nused_hook=%s\nopaque_ok=%d\n", zret(rc),
                hooks.calls > 0 ? "yes" : "no",
                hooks.calls > 0 && hooks.opaqueOk ? 1 : 0);
        if (rc != Zlib.Z_OK) {
            /* A failed init must not have leaked: everything it took, it gave
             * back. */
            out.printf("outcome=init-failed\nleaked=%s\n",
                    hooks.allocs == hooks.frees ? "no" : "YES");
            return;
        }

        Buf comp = new Buf();
        byte[] obuf = new byte[16384];
        zs.nextIn = src;
        zs.nextInIndex = 0;
        zs.availIn = src.length;
        int drc;
        do {
            zs.nextOut = obuf;
            zs.nextOutIndex = 0;
            zs.availOut = obuf.length;
            drc = zs.deflate(Zlib.Z_FINISH);
            comp.put(obuf, 0, obuf.length - zs.availOut);
        } while (drc == Zlib.Z_OK);
        /* The compressed bytes, in full: the hooks must not change what the
         * compressor decides, so this is the same stream opDeflate would
         * produce. */
        out.printf("deflate=%s\n", zret(drc));
        putBytes(out, "comp", comp.p, comp.n);
        out.printf("end=%s\n", zret(zs.end()));
        out.printf("balanced=%s\noutcome=%s\n",
                hooks.allocs == hooks.frees ? "yes" : "NO",
                drc == Zlib.Z_STREAM_END ? "complete" : "incomplete");

        if (inflateSide) {
            Counting ihooks = new Counting(magic, failAfter);
            Inflater iz = new Inflater();
            iz.allocator = ihooks;
            iz.deallocator = ihooks;
            iz.opaque = magic;
            int irc = iz.init2((int) wbits);
            out.printf("inflate.init=%s\n", zret(irc));
            if (irc == Zlib.Z_OK) {
                Buf back = new Buf();
                /* A deliberately small output buffer, matching probe.c, and it
                 * is load-bearing here for the same reason: the window is the
                 * one allocation this side can route through the hook at all --
                 * `new Inflater()` is the state block C asks its hook for -- and
                 * inflate defers the window until the output spans more than one
                 * call.  Through a 16 KiB buffer nothing under 16 KiB ever
                 * needed one, so the flag was unanswerable.  See
                 * catalog.build_alloc. */
                byte[] ibuf = new byte[256];
                iz.nextIn = comp.p;
                iz.nextInIndex = 0;
                iz.availIn = comp.n;
                int rc2 = Zlib.Z_OK;
                while (rc2 == Zlib.Z_OK) {
                    iz.nextOut = ibuf;
                    iz.nextOutIndex = 0;
                    iz.availOut = ibuf.length;
                    int inBefore = iz.availIn;
                    rc2 = iz.inflate(Zlib.Z_NO_FLUSH);
                    int produced = ibuf.length - iz.availOut;
                    back.put(ibuf, 0, produced);
                    /* Stop on a call that neither consumed nor produced.  With a
                     * buffer this small, "input exhausted" is not "finished" --
                     * there is still buffered output to hand back -- so the old
                     * availIn test would have truncated the answer. */
                    if (produced == 0 && iz.availIn == inBefore) {
                        break;
                    }
                }
                /* Asked after the round trip, not after the init.  `new
                 * Inflater()` is the state block C allocates through the hook at
                 * init time, so nothing on this side can route it anywhere; the
                 * first allocation a JVM port has any business asking the hook
                 * for is the window, and inflate defers that to first need.
                 * Reading the flag at init would have graded where the memory
                 * lives rather than who supplied it.  probe.c moved the same
                 * line to the same place, for the same reason -- and neither
                 * half prints it on the init-failure path, because a refused
                 * inflateInit2 is a hook call only C makes.  See
                 * catalog.build_alloc. */
                /* Printed only when the round trip needed a window -- the one
                 * allocation the two halves can agree on.  The condition is the
                 * payload length, known to both before they start, so the
                 * outputs stay line-for-line identical. */
                if (src.length > ibuf.length) {
                    out.printf("inflate.used_hook=%s\n",
                            ihooks.calls > 0 ? "yes" : "no");
                }
                out.printf("inflate.rc=%s\ninflate.match=%s\n", zret(rc2),
                        sameBytes(back, src) ? "identical" : "DIFFERENT");
                out.printf("inflate.end=%s\n", zret(iz.end()));
                out.printf("inflate.balanced=%s\n",
                        ihooks.allocs == ihooks.frees ? "yes" : "NO");
            } else {
                out.printf("inflate.leaked=%s\n",
                        ihooks.allocs == ihooks.frees ? "no" : "YES");
            }
        }
    }

    /* ------------------------------------------------------- op: list-keys
     *
     * Not a case: the self-description structure.py asks for before it trusts a
     * linkage mode.  `--list-keys` prints the ops this probe knows, one per
     * line, and then a handful of values it could only have obtained from the
     * library -- so an empty or short answer means the program did not reach the
     * library at all, which is exactly what the two probe-linkage cases are
     * asking about.
     *
     * The reason it is not simply a version print: on the module path a jar can
     * resolve, launch and run right up to the first use of a package the
     * descriptor forgot to export, and on the class path a jar can be missing a
     * class nobody has touched yet.  Java resolves lazily, so "it started" is
     * not "it linked".  Every public type is therefore named as a class literal
     * below, which forces its load, and every concrete one is actually used, so
     * a jar that is missing a class or hiding a package fails here rather than
     * in whichever case happens to touch it first.
     *
     * probe.c carries the same mode printing the same lines in the same order.
     * The C half cannot force-resolve a class, and does not need to -- its
     * symbols are bound at link time -- so where this method resolves types, it
     * takes the addresses of the corresponding functions instead.  Nothing about
     * that difference reaches stdout: the two outputs are byte-identical, which
     * is what keeps the pair auditable.
     */
    static final String[] OPS = {
        "deflate", "inflate", "bound", "oneshot", "checksum", "statechange",
        "gzheader", "syncrecover", "inflatestate", "error", "inflateback",
        "gzfile", "abi", "alloc",
    };

    static void listKeys() {
        Buf out = new Buf();
        for (String op : OPS) {
            out.printf("op=%s\n", op);
        }

        /* Load every public type, including the four nested interfaces, whose
         * class literals are the only way to reach them without a use site. */
        Class<?>[] surface = {
            Zlib.class, ZStream.class, Deflater.class, Inflater.class,
            InflateBack.class, GzHeader.class, GzFile.class,
            ZStream.Allocator.class, ZStream.Deallocator.class,
            InflateBack.In.class, InflateBack.Out.class,
        };
        out.printf("types=%d\n", surface.length);
        for (Class<?> c : surface) {
            /* Named, so the array cannot be optimized away, and so a class that
             * loads under a different name is visible in the output. */
            out.printf("type=%s\n", c.getName());
        }

        /* Values only the library can produce.  These are recorded rather than
         * checked: a wrong checksum is the business of the checksum cases, which
         * grade it against the reference.  What is being established here is
         * that the calls returned at all. */
        byte[] check = "123456789".getBytes(StandardCharsets.ISO_8859_1);
        out.printf("version=%s\n", Zlib.version());
        out.printf("vernum=0x%x\n", Zlib.ZLIB_VERNUM);
        out.printf("flags=0x%x\n", Zlib.compileFlags());
        out.printf("zError=%s\n", Zlib.errorString(Zlib.Z_DATA_ERROR));
        out.printf("crc32=%d\n", Zlib.crc32(0, check, 0, check.length) & 0xffffffffL);
        out.printf("adler32=%d\n",
                Zlib.adler32(1, check, 0, check.length) & 0xffffffffL);
        out.printf("crc_table[8]=%d\n", Zlib.crcTable()[8] & 0xffffffffL);
        out.printf("compressBound=%d\n", Zlib.compressBound(check.length));

        /* A real round trip through the streaming engine, the one-shot helpers
         * and inflateBack: three separate entry paths, so a jar that carries
         * only the classes the class loader has seen so far cannot pass. */
        Deflater dz = new Deflater();
        int drc = dz.init2(6, Zlib.Z_DEFLATED, 15, 8, Zlib.Z_DEFAULT_STRATEGY);
        out.printf("deflateInit2=%s\n", zret(drc));
        Buf comp = new Buf();
        if (drc == Zlib.Z_OK) {
            out.printf("deflateBound=%d\n", dz.bound(check.length));
            byte[] obuf = new byte[256];
            dz.nextIn = check;
            dz.nextInIndex = 0;
            dz.availIn = check.length;
            int rc;
            /* Bounded, unlike the drain loops in the cases: nine bytes finish in
             * one call, so anything past a handful of iterations is an
             * implementation that never reports Z_STREAM_END.  Without the bound
             * that becomes a timeout, and a timeout is reported against the whole
             * linkage mode rather than naming what went wrong. */
            int spins = 0;
            do {
                dz.nextOut = obuf;
                dz.nextOutIndex = 0;
                dz.availOut = obuf.length;
                rc = dz.deflate(Zlib.Z_FINISH);
                comp.put(obuf, 0, obuf.length - dz.availOut);
            } while (rc == Zlib.Z_OK && ++spins < 16);
            out.printf("deflate=%s\ncomp.len=%d\n", zret(rc), comp.n);
            out.printf("deflateEnd=%s\n", zret(dz.end()));
        }
        if (comp.n > 0) {
            Inflater iz = new Inflater();
            int irc = iz.init2(15);
            out.printf("inflateInit2=%s\n", zret(irc));
            if (irc == Zlib.Z_OK) {
                byte[] back = new byte[64];
                iz.nextIn = comp.p;
                iz.nextInIndex = 0;
                iz.availIn = comp.n;
                iz.nextOut = back;
                iz.nextOutIndex = 0;
                iz.availOut = back.length;
                int rc = iz.inflate(Zlib.Z_FINISH);
                out.printf("inflate=%s\ninflate.total_out=%d\n", zret(rc),
                        iz.totalOut);
                out.printf("inflateEnd=%s\n", zret(iz.end()));
            }
        }

        int cap = (int) Zlib.compressBound(check.length);
        byte[] dest = new byte[cap != 0 ? cap : 1];
        int[] destLen = {cap};
        out.printf("compress=%s\n",
                zret(Zlib.compress(dest, destLen, check, check.length)));
        byte[] plain = new byte[check.length + 1];
        int[] plainLen = {check.length};
        out.printf("uncompress=%s\n",
                zret(Zlib.uncompress(plain, plainLen, dest, destLen[0])));

        InflateBack bz = new InflateBack();
        out.printf("inflateBackInit=%s\n", zret(bz.init(15, new byte[1 << 15])));
        out.printf("inflateBackEnd=%s\n", zret(bz.end()));

        GzHeader head = new GzHeader();
        head.os = 3;
        out.printf("gzheader.os=%d\n", head.os);
        out.printf("gzopen.missing=%s\n",
                GzFile.open("/nonexistent/probe/list-keys.gz", "rb") == null
                        ? "NULL" : "ok");

        try {
            rawOut.write(out.p, 0, out.n);
            rawOut.flush();
        } catch (IOException e) {
            die("write failed: " + e);
        }
    }

    /* ---------------------------------------------------------------- main */

    static void dispatch(String id, String[] f) {
        String op = f.length > 1 ? f[1] : "";
        Buf out = new Buf();

        switch (op) {
            case "deflate": opDeflate(out, f); break;
            case "inflate": opInflate(out, f); break;
            case "bound": opBound(out, f); break;
            case "oneshot": opOneshot(out, f); break;
            case "checksum": opChecksum(out, f); break;
            case "statechange": opStatechange(out, f); break;
            case "gzheader": opGzheader(out, f); break;
            case "syncrecover": opSyncrecover(out, f); break;
            case "inflatestate": opInflatestate(out, f); break;
            case "error": opError(out, f); break;
            case "inflateback": opInflateback(out, f); break;
            case "gzfile": opGzfile(out, f); break;
            case "abi": opAbi(out, f); break;
            case "alloc": opAlloc(out, f); break;
            default:
                out.printf("unknown-op=%s\n", op);
                emit(id, "badop", out);
                return;
        }

        emit(id, "ok", out);
    }

    public static void main(String[] args) throws Exception {
        /* Raw stdout, unwrapped: payloads are arbitrary bytes and a PrintStream
         * with a charset would rewrite them.  emit() flushes each record so a
         * case that kills the JVM cannot take the earlier records with it. */
        rawOut = System.out;

        /* Flags are recognized in any position and removed before the
         * positional arguments are read, matching probe.c, so that
         * `--list-keys` alone is not mistaken for a corpus directory. */
        boolean wantKeys = false;
        int positional = 0;
        String[] rest = new String[args.length];
        for (String arg : args) {
            if (arg.equals("--list-keys")) {
                wantKeys = true;
            } else {
                rest[positional++] = arg;
            }
        }
        if (positional > 0) {
            corpusDir = rest[0];
        }
        if (positional > 1) {
            scratchDir = rest[1];
        }
        if (wantKeys) {
            /* No corpus, no scratch directory, no stdin: the self-description
             * needs none of them, and creating a directory here would make the
             * linkage cases depend on the filesystem. */
            listKeys();
            return;
        }
        Files.createDirectories(Paths.get(scratchDir));

        InputStream in = System.in;
        ByteArrayOutputStream line = new ByteArrayOutputStream();
        int c;
        while (true) {
            c = in.read();
            if (c == -1 && line.size() == 0) {
                break;
            }
            if (c != -1 && c != '\n') {
                line.write(c);
                continue;
            }
            byte[] raw = line.toByteArray();
            line.reset();
            int len = raw.length;
            while (len > 0 && (raw[len - 1] == '\n' || raw[len - 1] == '\r')) {
                len--;
            }
            String text = new String(raw, 0, len, StandardCharsets.ISO_8859_1);
            if (!text.isEmpty() && text.charAt(0) != '#') {
                /* -1 keeps trailing empty fields, so a case with a deliberately
                 * empty final argument is not silently reshaped. */
                String[] fields = text.split("\t", -1);
                if (fields.length >= 2) {
                    dispatch(fields[0], fields);
                }
            }
            if (c == -1) {
                break;
            }
        }
        rawOut.flush();
    }

    /* A NUL-terminated byte array, which is what GzHeader.name and .comment hold:
     * the gzip format specifies NUL-terminated fields, so the convention belongs
     * to the wire format rather than to C. */
    static byte[] cstr(String s) {
        byte[] raw = s.getBytes(StandardCharsets.ISO_8859_1);
        byte[] outArr = new byte[raw.length + 1];
        System.arraycopy(raw, 0, outArr, 0, raw.length);
        return outArr;
    }

    /* Read back to the first NUL, matching what C's printf("%s") does with the
     * buffer inflateGetHeader filled. */
    static String fromCstr(byte[] b) {
        int end = 0;
        while (end < b.length && b[end] != 0) {
            end++;
        }
        return new String(b, 0, end, StandardCharsets.ISO_8859_1);
    }
}
