#!/usr/bin/env python3
"""Builds the verifier-private payload corpus for lang02-zlib-c-to-java.

Every behavioural case in this task is "push these bytes through this production
entry point and compare the result against the reference, byte for byte".  For a
compression library the input is the whole experiment: deflate's output is a
function of the match finder, the lazy-match heuristic, the block-splitting
decision and the Huffman tree builder, and each of those only shows itself on
input shaped to provoke it.  A corpus of ordinary text would grade about a third
of the code.

So the families below are organized by the mechanism they attack:

* **Boundaries.** Sizes and periods sitting exactly on MIN_MATCH (3), MAX_MATCH
  (258), the 32 KiB window, and the internal buffer sizes.  Off-by-one errors in
  a rewritten match finder live here and nowhere else.
* **Histograms.** Payloads with 1, 2, 3, ... 256 distinct symbols, plus skewed
  distributions, so the Huffman builder is exercised from its degenerate
  single-symbol tree up to a full alphabet against the 15-bit length limit.
* **Block-splitting.** Alternating compressible and incompressible runs, which
  is what makes deflate choose between stored, static and dynamic blocks.
* **Reality.** Files from the pinned source tree itself: real C, real headers,
  a real ChangeLog.  Deterministic because the tree is pinned.

Determinism: everything is either read from the pinned tree or produced by a
fixed-seed generator, so the frozen expectations stay valid forever.

Non-disclosure: the corpus never ships in the agent workspace.  A solver cannot
enumerate it, so hard-coding answers is not a strategy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

CORPUS_SCHEMA = "swerefactor-corpus-v2"
TASK = "lang02-zlib-c-to-java"

# Deflate's own constants.  Named here because the whole point of the boundary
# families is to land on them exactly.
MIN_MATCH = 3
MAX_MATCH = 258
WINDOW = 32768

class Stream:
    """A seeded generator, so the corpus is a pure function of the seed.

    SHA-256 in counter mode rather than `random`: the stdlib PRNG's stream is a
    promise about an implementation, and this corpus has to reproduce byte for
    byte on any Python that can run the verifier.
    """

    def __init__(self, seed: str) -> None:
        self.seed = seed.encode("utf-8")
        self.counter = 0
        self.buffer = b""

    def bytes(self, count: int) -> bytes:
        while len(self.buffer) < count:
            block = hashlib.sha256(self.seed + self.counter.to_bytes(8, "big")).digest()
            self.buffer += block
            self.counter += 1
        out, self.buffer = self.buffer[:count], self.buffer[count:]
        return out

    def below(self, bound: int) -> int:
        if bound <= 1:
            return 0
        return int.from_bytes(self.bytes(4), "big") % bound

    def pick(self, items):
        return items[self.below(len(items))]


# ---------------------------------------------------------------------------
# Family: sizes
#
# One payload per interesting length.  Deflate's stored-block path, its
# emit-final-block path and its flush bookkeeping all key off total length, and
# the sizes around 0, 1 and the internal 16 KiB pending buffer are where a
# rewrite forgets to drain something.
# ---------------------------------------------------------------------------

SIZE_POINTS = [
    0, 1, 2, 3, 4, 5, 6, 7, 8, 15, 16, 17, 31, 32, 33, 63, 64, 65,
    127, 128, 129, 255, 256, 257, 258, 259, 260, 511, 512, 513,
    1023, 1024, 1025, 2047, 2048, 2049, 4095, 4096, 4097,
    8191, 8192, 8193, 16383, 16384, 16385, 32767, 32768, 32769,
    65535, 65536, 65537, 131071, 131072,
]


def size_payloads() -> list[tuple[str, bytes]]:
    """Same generator, every length: isolates length from content."""
    stream = Stream("swerefactor/lang02/sizes/v1")
    base = stream.bytes(131072 + 8)
    out = []
    for size in SIZE_POINTS:
        out.append((f"size-{size:06d}", base[:size]))
    return out


# ---------------------------------------------------------------------------
# Family: runs
#
# A single repeated byte is the maximally compressible input and drives the
# match finder to MAX_MATCH on every step.  The lengths straddle MAX_MATCH and
# its multiples, where a rewritten emit loop mis-splits a long match.
# ---------------------------------------------------------------------------


def run_payloads() -> list[tuple[str, bytes]]:
    out = []
    for size in (1, 2, 3, 4, 257, 258, 259, 260, 515, 516, 517, 1024, 4096, 65536):
        out.append((f"run-zero-{size:06d}", b"\x00" * size))
    for size in (3, 258, 259, 1000, 32768, 40000):
        out.append((f"run-ff-{size:06d}", b"\xff" * size))
    for size in (3, 258, 1024, 33000):
        out.append((f"run-a-{size:06d}", b"A" * size))
    # A run that changes byte exactly once, at each of several offsets: the
    # match has to be cut at the right place.
    for cut in (1, 2, 3, 4, 257, 258, 259):
        body = bytearray(b"x" * 600)
        body[cut] = ord("y")
        out.append((f"run-break-{cut:04d}", bytes(body)))
    return out


# ---------------------------------------------------------------------------
# Family: periodic
#
# Period p with total length n gives a known match structure: every position
# after the first period matches at distance p.  Periods on and around
# MIN_MATCH, MAX_MATCH and the window bound test distance encoding across all
# 30 distance codes; period 1 degenerates to a run.
# ---------------------------------------------------------------------------


def periodic_payloads() -> list[tuple[str, bytes]]:
    stream = Stream("swerefactor/lang02/periodic/v1")
    out = []
    periods = [1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 24, 31, 32, 33, 48, 64, 96,
               127, 128, 129, 192, 256, 257, 258, 259, 384, 512, 1024, 2048,
               4096, 8192, 16384, 32767, 32768]
    for period in periods:
        unit = stream.bytes(period)
        total = max(period * 3, 4096)
        if period >= 8192:
            total = period * 3
        body = (unit * (total // period + 1))[:total]
        out.append((f"period-{period:05d}", body))
    # Two interleaved periods: the match finder has to choose between two
    # candidate distances, which is what the lazy-match heuristic is for.
    for a, b in ((3, 7), (5, 256), (17, 258), (64, 4096), (258, 32768)):
        ua, ub = stream.bytes(a), stream.bytes(b)
        chunk = ua * 40 + ub * 8
        out.append((f"period-mix-{a:05d}-{b:05d}", (chunk * 4)[:24576]))
    return out


# ---------------------------------------------------------------------------
# Family: histogram
#
# Alphabet size drives the literal/length Huffman tree.  One distinct symbol is
# the degenerate tree zlib has a special case for; 256 distinct symbols with a
# skew steep enough to want a >15-bit code forces the length-limiting pass.
# ---------------------------------------------------------------------------


def histogram_payloads() -> list[tuple[str, bytes]]:
    stream = Stream("swerefactor/lang02/histogram/v1")
    out = []
    for alphabet in (1, 2, 3, 4, 5, 7, 8, 9, 15, 16, 17, 31, 32, 33, 63, 64,
                     65, 127, 128, 129, 200, 255, 256):
        symbols = bytes(range(alphabet)) if alphabet <= 256 else bytes(range(256))
        body = bytearray()
        for _ in range(8192):
            body.append(symbols[stream.below(len(symbols))])
        out.append((f"hist-flat-{alphabet:03d}", bytes(body)))
    # Geometric skew: symbol i appears about 2^-i as often as symbol 0.  This is
    # the shape that produces very long codes.
    for spread in (4, 8, 16, 64, 256):
        body = bytearray()
        while len(body) < 12288:
            value = 0
            while value < spread - 1 and stream.below(2):
                value += 1
            body.append(value % 256)
        out.append((f"hist-skew-{spread:03d}", bytes(body)))
    # Every byte value exactly once, in order and reversed: a flat histogram
    # with no matches at all, the case where a dynamic tree costs more than a
    # static one.
    out.append(("hist-identity", bytes(range(256))))
    out.append(("hist-identity-rev", bytes(reversed(range(256)))))
    out.append(("hist-identity-x64", bytes(range(256)) * 64))
    return out


# ---------------------------------------------------------------------------
# Family: text
#
# Word-structured input is what deflate was tuned for, and it is the only family
# where the lazy-match heuristic and the strategy parameter visibly disagree with
# each other.  Built from a fixed word list rather than prose so it stays
# reproducible without shipping a text file.
# ---------------------------------------------------------------------------

WORDS = (
    "the of and to in a is that it for as with was on be by not this have from "
    "or one had but what all were when we there can an your which their said if "
    "will each about how up out them then she many some so these would other into "
    "has more her two like him see time could no make than first been its who now "
    "people my made over did down only way find water long little very after "
    "called just where most know get through back much before also around another "
    "came come work three word must because does part even place well such here "
    "take why things help put years different away again off went old number great "
    "tell men say small every found still between name should home big give air "
    "line set own under read last never us left end along while might next sound "
    "below saw something thought both few those always looked show large often "
    "together asked house don world going want school important until form food "
    "keep children feet land side without boy once animals life enough took four "
    "head above kind began almost live page got earth need far hand high year "
    "mother light country father let night following came want show also"
).split()


def text_payloads() -> list[tuple[str, bytes]]:
    stream = Stream("swerefactor/lang02/text/v1")
    out = []
    for size in (64, 256, 1024, 4096, 16384, 65536, 131072):
        pieces: list[str] = []
        total = 0
        while total < size:
            word = stream.pick(WORDS)
            pieces.append(word)
            total += len(word) + 1
            if stream.below(12) == 0:
                pieces.append("\n")
        body = " ".join(pieces).encode("ascii")[:size]
        out.append((f"text-{size:06d}", body))
    # Highly redundant prose: the same sentence many times, so long matches at
    # long distances dominate.  Distances here exceed the 8 KiB default hash
    # chain depth, which is where memLevel and windowBits start to matter.
    sentence = b"The quick brown fox jumps over the lazy dog near the riverbank. "
    for count in (2, 8, 64, 512, 2048):
        out.append((f"text-repeat-{count:05d}", sentence * count))
    # Two alternating paragraphs: a match candidate at a large distance and a
    # closer worse one, which is exactly the lazy-match decision.
    para_a = b"alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu\n" * 40
    para_b = b"alpha beta gamma delta epsilon ZETA eta theta iota kappa lambda mu\n" * 40
    out.append(("text-alternate", para_a + para_b + para_a + para_b))
    return out


# ---------------------------------------------------------------------------
# Family: source
#
# Real files from the pinned tree.  Compression of source code is zlib's most
# common real workload, and reading these from the repository means the family
# is deterministic without being synthetic.
# ---------------------------------------------------------------------------

SOURCE_FILES = [
    ("deflate-c", "deflate.c"),
    ("inflate-c", "inflate.c"),
    ("trees-c", "trees.c"),
    ("zlib-h", "zlib.h"),
    ("zconf-h", "zconf.h"),
    ("gzread-c", "gzread.c"),
    ("infback-c", "infback.c"),
    ("crc32-c", "crc32.c"),
    ("changelog", "ChangeLog"),
    ("readme", "README"),
    ("cmakelists", "CMakeLists.txt"),
    ("zlib-map", "zlib.map"),
    ("makefile-in", "Makefile.in"),
    ("configure", "configure"),
    ("example-c", "test/example.c"),
    ("infcover-c", "test/infcover.c"),
    ("faq", "FAQ"),
    ("zlib-3", "zlib.3"),
]


def source_payloads(repo: Path) -> list[tuple[str, bytes]]:
    out = []
    for name, relative in SOURCE_FILES:
        path = repo / relative
        if not path.is_file():
            raise SystemExit(f"corpus source file missing from pinned tree: {relative}")
        out.append((f"src-{name}", path.read_bytes()))
    # The concatenation of the three biggest sources: a large input with genuine
    # cross-file redundancy, which is the case that needs the full window.
    big = b"".join(
        (repo / rel).read_bytes() for rel in ("deflate.c", "inflate.c", "trees.c")
    )
    out.append(("src-concat", big))
    # The same file twice: every byte of the second copy is a match at a
    # distance larger than the default window can reach for the tail.
    doubled = (repo / "deflate.c").read_bytes()
    out.append(("src-doubled", doubled + doubled))
    return out


# ---------------------------------------------------------------------------
# Family: random
#
# Incompressible input is the stored-block path and the "compressed is bigger
# than the input" case that deflateBound exists to size.  Lengths straddle the
# stored-block ceiling of 65535.
# ---------------------------------------------------------------------------


def random_payloads() -> list[tuple[str, bytes]]:
    stream = Stream("swerefactor/lang02/random/v1")
    out = []
    for size in (1, 2, 3, 7, 16, 100, 1000, 4096, 16384, 65534, 65535,
                 65536, 65537, 131072):
        out.append((f"rand-{size:06d}", stream.bytes(size)))
    # Random over a restricted alphabet: compressible enough for a dynamic tree
    # but with no usable matches.
    for bits in (1, 2, 4):
        mask = (1 << bits) - 1
        raw = stream.bytes(8192)
        out.append((f"rand-bits{bits}", bytes(b & mask for b in raw)))
    return out


# ---------------------------------------------------------------------------
# Family: mixed
#
# Alternating compressible and incompressible segments.  This is the family that
# grades the block-splitting decision: deflate must close a dynamic block and
# open a stored one at the right byte, and a rewrite that always emits one block
# type produces different -- still valid -- output, which this catches.
# ---------------------------------------------------------------------------


def mixed_payloads() -> list[tuple[str, bytes]]:
    stream = Stream("swerefactor/lang02/mixed/v1")
    out = []
    for zeros, noise, reps in (
        (16, 16, 64), (256, 256, 16), (1024, 64, 8), (64, 1024, 8),
        (4096, 4096, 4), (32768, 1024, 2), (1024, 32768, 2),
    ):
        body = bytearray()
        for _ in range(reps):
            body += b"\x00" * zeros
            body += stream.bytes(noise)
        out.append((f"mix-{zeros:05d}-{noise:05d}", bytes(body)))
    # Compressible head, incompressible tail, and the reverse: the transition
    # happens exactly once, at a known offset.
    for head in (1, 100, 8192, 65535):
        out.append((f"mix-head-{head:05d}", b"q" * head + stream.bytes(8192)))
        out.append((f"mix-tail-{head:05d}", stream.bytes(8192) + b"q" * head))
    return out


# ---------------------------------------------------------------------------
# Family: structured
#
# Binary layouts with regular stride: fixed-width records, counters, bitmaps,
# floats.  Real binary data is neither text nor random, and Z_FILTERED exists
# precisely for this shape.  A rewrite that ignores the strategy parameter agrees
# with the reference on text and disagrees here.
# ---------------------------------------------------------------------------


def structured_payloads() -> list[tuple[str, bytes]]:
    stream = Stream("swerefactor/lang02/structured/v1")
    out = []
    # Little-endian counters at several widths: the low byte varies, the high
    # bytes are almost constant.
    for width in (2, 4, 8):
        body = bytearray()
        for value in range(4096):
            body += value.to_bytes(width, "little")
        out.append((f"struct-counter{width}", bytes(body)))
    # Fixed-width records with a constant tag and a varying field.
    for stride in (4, 8, 16, 32, 64):
        body = bytearray()
        for index in range(2048):
            body += b"\xab\xcd"
            body += (index % 251).to_bytes(2, "big")
            body += stream.bytes(max(stride - 4, 0))
        out.append((f"struct-record{stride:02d}", bytes(body)))
    # A sparse bitmap: mostly zero with isolated set bits, the classic
    # Z_FILTERED / Z_RLE input.
    for density in (1, 8, 64):
        body = bytearray(16384)
        for _ in range(16384 // density):
            body[stream.below(len(body))] = 1 << stream.below(8)
        out.append((f"struct-bitmap{density:02d}", bytes(body)))
    # A gradient: each byte one more than the last.  No exact matches at all,
    # but a very skewed distribution of differences.
    out.append(("struct-gradient", bytes((i * 7) % 256 for i in range(16384))))
    out.append(("struct-sawtooth", bytes(range(64)) * 256))
    return out


# ---------------------------------------------------------------------------
# Family: utf8
#
# Multibyte text.  zlib is byte-oriented and must not care, which is the point:
# these payloads have high-bit bytes in structured positions, and any rewrite
# that accidentally treats a byte as signed produces different matches here.
# ---------------------------------------------------------------------------

UTF8_SAMPLES = [
    ("ascii-mix", "Plain ASCII with punctuation: (a+b)*c == d! #42 & 'quotes'."),
    ("latin", "Приветствие: naïve café résumé Ångström Straße"),
    ("cjk", "压缩算法在实际系统中的应用与性能分析,以及无损压缩的边界条件。"),
    ("cjk-repeat", "压缩" * 400),
    ("jp", "日本語のテキストを圧縮する場合の挙動について検証する。"),
    ("ko", "한국어 텍스트 압축 동작을 검증하기 위한 샘플 문자열입니다."),
    ("emoji", "\U0001f600\U0001f680\U0001f4a1❤️\U0001f3f3️‍\U0001f308" * 60),
    ("combining", "éàôüñ" * 200),
    ("rtl", "العربية والعبرية עברית مختلطة مع ASCII text"),
    ("boundary-2byte", "".join(chr(c) for c in range(0x80, 0x800, 7))),
    ("boundary-3byte", "".join(chr(c) for c in range(0x800, 0xD000, 211))),
    ("boundary-4byte", "".join(chr(c) for c in range(0x10000, 0x30000, 1013))),
]


def utf8_payloads() -> list[tuple[str, bytes]]:
    out = [(f"utf8-{name}", text.encode("utf-8")) for name, text in UTF8_SAMPLES]
    # Invalid UTF-8: high bytes in sequences that no decoder accepts.  zlib must
    # treat these as ordinary bytes, so a port that routes payload data through a
    # String, a Reader, or any charset decoder fails here and only here -- Java's
    # decoders substitute U+FFFD silently rather than raising, which is exactly the
    # kind of corruption a round-trip test against itself cannot see.
    out.append(("utf8-invalid-lone", bytes([0xC3, 0x28, 0xA0, 0xA1, 0xE2, 0x28, 0xA1]) * 512))
    out.append(("utf8-invalid-surrogate", bytes([0xED, 0xA0, 0x80, 0xED, 0xBF, 0xBF]) * 512))
    out.append(("utf8-invalid-overlong", bytes([0xF0, 0x82, 0x82, 0xAC, 0xC0, 0x80]) * 512))
    out.append(("utf8-all-high", bytes(range(128, 256)) * 64))
    return out


# ---------------------------------------------------------------------------
# Family: dictionary
#
# Payloads paired with the preset dictionaries used by the deflateSetDictionary
# cases.  A dictionary changes the output of the first block only, so these are
# short on purpose: a long payload would drown the effect.
# ---------------------------------------------------------------------------

DICTIONARIES = [
    ("dict-empty", b""),
    ("dict-short", b"zlib"),
    ("dict-words", b"the quick brown fox jumps over the lazy dog"),
    ("dict-html", b"<html><head><title></title></head><body><div class=\"\"></div></body></html>"),
    ("dict-json", b"{\"id\":,\"name\":\"\",\"values\":[],\"nested\":{\"flag\":true,\"count\":0}}"),
    ("dict-source", b"static void deflate_stored(deflate_state *s, int flush) { unsigned len; }"),
    ("dict-exact-window", None),   # filled below: exactly 32768 bytes
    ("dict-over-window", None),    # 40000 bytes: zlib keeps only the last 32768
]


def dictionary_payloads() -> list[tuple[str, bytes]]:
    stream = Stream("swerefactor/lang02/dictionary/v1")
    out: list[tuple[str, bytes]] = []
    for name, body in DICTIONARIES:
        if body is None:
            size = WINDOW if name.endswith("exact-window") else 40000
            body = stream.bytes(size)
        out.append((name, body))
    # The payloads these dictionaries are meant to compress.
    out.append(("dictdata-words", b"the quick brown fox jumps over the lazy dog again and again"))
    out.append(("dictdata-html", b"<html><head><title>Page</title></head><body><div class=\"main\">Hi</div></body></html>"))
    out.append(("dictdata-json", b"{\"id\":7,\"name\":\"widget\",\"values\":[1,2,3],\"nested\":{\"flag\":true,\"count\":9}}"))
    out.append(("dictdata-source", b"static void deflate_stored(deflate_state *s, int flush) { unsigned len = 0; }"))
    out.append(("dictdata-none", b"completely unrelated payload with no dictionary overlap whatsoever"))
    return out


# ---------------------------------------------------------------------------
# Family: matchcraft
#
# Payloads built to land a match of an exact length at an exact distance.  These
# are the unit tests of the match finder: `craft(3, 1)` is the shortest match at
# the shortest distance, `craft(258, 32768)` is the longest at the longest, and
# every combination in between checks one cell of the length/distance code
# tables.  Getting a single extra-bits count wrong in trees.c breaks exactly one
# of these.
# ---------------------------------------------------------------------------


def craft(length: int, distance: int, filler: bytes) -> bytes:
    """A payload whose only match is `length` bytes at `distance`."""
    head = filler[:distance]
    if len(head) < distance:
        head = (filler * (distance // len(filler) + 1))[:distance]
    body = bytearray(head)
    # Repeat the first `length` bytes of the head, so the match is unambiguous.
    body += head[:length] if length <= distance else (head * (length // distance + 1))[:length]
    # A distinct tail so the match cannot run past its intended end.
    body += b"\x7f\x7e\x7d"
    return bytes(body)


def matchcraft_payloads() -> list[tuple[str, bytes]]:
    stream = Stream("swerefactor/lang02/matchcraft/v1")
    filler = stream.bytes(WINDOW + 64)
    out = []
    lengths = [3, 4, 5, 6, 7, 8, 10, 11, 12, 15, 16, 19, 23, 24, 31, 32, 35,
               43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 257, 258]
    for length in lengths:
        out.append((f"craft-len-{length:03d}", craft(length, max(length, 512), filler)))
    distances = [1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 16, 17, 24, 25, 32, 33, 48, 49,
                 64, 65, 96, 97, 128, 129, 192, 193, 256, 257, 384, 385, 512,
                 513, 768, 769, 1024, 1025, 1536, 2048, 3072, 4096, 6144, 8192,
                 12288, 16384, 24576, 32767, 32768]
    for distance in distances:
        out.append((f"craft-dist-{distance:05d}", craft(MIN_MATCH, distance, filler)))
        if distance >= MAX_MATCH:
            out.append((f"craft-max-{distance:05d}", craft(MAX_MATCH, distance, filler)))
    return out


# ---------------------------------------------------------------------------
# Family: gzfile
#
# Payloads for the gz* stream API, which reaches code the raw deflate path never
# does: line buffering, ungetc, seek-by-skip, and the multi-member reader.
# Sizes here straddle the gz layer's own 8 KiB internal buffer.
# ---------------------------------------------------------------------------


def gzfile_payloads() -> list[tuple[str, bytes]]:
    stream = Stream("swerefactor/lang02/gzfile/v1")
    out = []
    for size in (0, 1, 8191, 8192, 8193, 16384, 65536):
        out.append((f"gz-flat-{size:06d}", stream.bytes(size)))
    # Line-structured data for gzgets: lines shorter and longer than the buffer
    # it will be read into, plus a file with no trailing newline.
    lines = b"".join(b"line %04d with some padding text\n" % i for i in range(400))
    out.append(("gz-lines", lines))
    out.append(("gz-lines-nonl", lines[:-1]))
    out.append(("gz-longline", b"x" * 9000 + b"\n" + b"short\n"))
    out.append(("gz-crlf", b"alpha\r\nbeta\r\ngamma\r\n" * 100))
    out.append(("gz-nul", b"before\x00after\x00\x00end\n" * 200))
    out.append(("gz-onlynl", b"\n" * 1000))
    out.append(("gz-text", b"".join(w.encode() + b" " for w in WORDS) * 4))
    return out


# ---------------------------------------------------------------------------
# Family: compose
#
# Concatenations of members of the other families, so a case can exercise a
# transition the individual families do not contain: a run followed by noise
# followed by structured data, all inside one deflate stream.
# ---------------------------------------------------------------------------

COMPOSE_COUNT = 96
COMPOSE_SEED = "swerefactor/lang02/compose/v1"


def compose_payloads(pool: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    stream = Stream(COMPOSE_SEED)
    # Only reasonably sized members, so a composite stays under ~96 KiB.
    usable = [body for _, body in pool if 0 < len(body) <= 16384]
    out = []
    for index in range(COMPOSE_COUNT):
        parts = [stream.pick(usable) for _ in range(2 + stream.below(5))]
        out.append((f"compose-{index:04d}", b"".join(parts)))
    return out


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build(repo: Path, out: Path) -> dict:
    """Assemble every family and write the corpus plus its index."""
    families: list[tuple[str, list[tuple[str, bytes]]]] = [
        ("sizes", size_payloads()),
        ("runs", run_payloads()),
        ("periodic", periodic_payloads()),
        ("histogram", histogram_payloads()),
        ("text", text_payloads()),
        ("source", source_payloads(repo)),
        ("random", random_payloads()),
        ("mixed", mixed_payloads()),
        ("structured", structured_payloads()),
        ("utf8", utf8_payloads()),
        ("dictionary", dictionary_payloads()),
        ("matchcraft", matchcraft_payloads()),
        ("gzfile", gzfile_payloads()),
    ]
    pool = [item for _, items in families for item in items]
    families.append(("compose", compose_payloads(pool)))

    blobs = out / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    entries = []
    seen: set[str] = set()
    index = 0
    for family, items in families:
        for name, body in items:
            if name in seen:
                raise SystemExit(f"duplicate corpus id {name}")
            seen.add(name)
            (blobs / f"{index:05d}.bin").write_bytes(body)
            entries.append(
                {
                    "index": index,
                    "id": name,
                    "family": family,
                    "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
            )
            index += 1

    manifest = {
        "schema": CORPUS_SCHEMA,
        "task": TASK,
        "compose_seed": COMPOSE_SEED,
        "count": len(entries),
        "total_bytes": sum(e["bytes"] for e in entries),
        "families": {
            family: sum(1 for e in entries if e["family"] == family)
            for family, _ in families
        },
        "documents": entries,
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["digest"] = hashlib.sha256(payload).hexdigest()
    (out / "corpus.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    manifest = build(args.repo, args.out)
    print(f"corpus payloads: {manifest['count']}")
    for family, count in sorted(manifest["families"].items()):
        print(f"  {family:12s} {count:5d}")
    print(f"corpus bytes:  {manifest['total_bytes']}")
    print(f"corpus digest: {manifest['digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
