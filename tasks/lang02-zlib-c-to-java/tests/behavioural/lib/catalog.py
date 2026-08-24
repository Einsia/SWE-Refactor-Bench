#!/usr/bin/env python3
"""Case catalog for lang02-zlib-c-to-java.

This module enumerates what the evaluation measures.  A *case* is one distinct
product behavior exercised end to end through a shipped entry point: the
installed jar through its published `org.zlib` API, the jar's manifest and
module descriptor, or its class files read as class files.  Changing a parameter
does not create a case; changing the code path does.  Deflating one payload at
levels 1 and 9 are two cases because they run two different match strategies,
while the same payload at four output-buffer sizes is one case with four
assertions.

Every behavioural case is graded by differential comparison against the pinned C
reference, built from source inside the verifier image.  No expected output is
written by hand, so the catalog cannot drift from what the reference does, and
zlib's quirks are graded as behavior rather than repaired as bugs.

Because the reference is C and the submission is a jar, the two sides cannot
share a binary the way a C-to-Rust migration can: nothing links `probe.c`
against `zlib.jar`.  So the instrument is a *matched pair* -- `probe/probe.c`
against the reference install, `probe/Probe.java` against the submission jar --
and the contract between them is that neither may carry a case the other cannot
answer, because the C run is the oracle.  Two cases are shaped by that rule and
are marked where they appear: `abi` has no `layout` sub-case (sizeof and
offsetof have no JVM counterpart; structure.py asks that question by reflection
instead) and `alloc` records no allocation counts (a state object cannot be
routed through a hook that hands back primitive arrays).

The two grading configs are the two documented CMake configurations,
`BUILD_SHARED_LIBS=ON` and `=OFF`.  For a jar the option means nothing, and that
is exactly why it is graded: a build that errors on an option it has no use for
has broken its interface, and both configurations must still produce the
complete install inventory and a jar that behaves identically.  Where a
distinction between *consumer* linkage forms matters -- the jar resolved as a
named module on the module path, or flattened onto the class path where
`module-info` is ignored -- it is a separate case rather than a config, because
it is a property of how the consumer runs, not of how the library was built.

Kinds:
  probe  -- a line of the probe protocol, run against both the reference and the
            submission and compared byte for byte
  cli    -- an argv for an installed consumer entry point (the verifier's own
            consumer, compiled against the install tree and run both ways)
  driver -- one of upstream's own test programs, ported by the migration and run
            as a process, compared against the reference program's output
  struct -- a structural fact about the build, install tree, jar, module
            descriptor, class files or API shape, evaluated by the build and
            structure modules
  guard  -- a migration-audit gate, evaluated by the audit module

`struct` and `guard` cases are declared here so the catalog is the single
inventory of what the task measures; their logic lives in the modules that own
the evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

# Both link modes.  Named for what the consumer does, not for a CMake switch.
BOTH = ("static", "shared")

# Deflate levels.  0 is stored-only, 1 switches to deflate_fast, 4 and up use
# deflate_slow with lazy matching, and -1 is the documented default alias for 6.
LEVELS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, -1)

# Strategies.  Each one replaces a different part of the block decision:
# FILTERED restricts match distances, HUFFMAN_ONLY skips matching entirely,
# RLE limits distances to one, and FIXED forbids dynamic Huffman trees.
STRATEGIES = (0, 1, 2, 3, 4)
STRATEGY_NAMES = {0: "default", 1: "filtered", 2: "huffman", 3: "rle", 4: "fixed"}

# windowBits.  8..15 is zlib-wrapped, -8..-15 is raw deflate, +16 selects the
# gzip wrapper, and 15+32 asks inflate to auto-detect the wrapper.
WBITS_DEFLATE = (9, 10, 11, 12, 13, 14, 15, -9, -12, -15, 25, 27, 31)
WBITS_INFLATE = (8, 9, 15, -9, -15, 31, 47)

# memLevel picks the hash-table and pending-buffer sizes.  1 is the smallest
# legal value and forces the compressor through its tightest buffering path.
MEMLEVELS = (1, 2, 4, 6, 8, 9)

# Flush schedules, as (Z_ constant, chunk interval).
FLUSHES = (
    ("noflush", 0, 0),
    ("partial", 1, 2),
    ("sync", 2, 3),
    ("full", 3, 5),
    ("block", 5, 4),
)

# Buffer sizes swept as assertions inside a streaming case.
IN_CHUNKS = (1, 2, 7, 64, 1024, 65536)
OUT_CHUNKS = (1, 2, 7, 64, 1024, 65536)

# Flush modes that emit an empty stored block cannot terminate against an output
# buffer too small to hold one.  Measured against the reference: Z_NO_FLUSH and
# Z_BLOCK are independent of out_chunk at every size; Z_PARTIAL_FLUSH cannot
# terminate at out_chunk 1; Z_SYNC_FLUSH and Z_FULL_FLUSH cannot terminate below
# out_chunk 6, because each output-bound return sets zlib's last_flush to -1,
# which disarms the repeated-flush guard so the next call appends another
# five-byte empty block that again does not fit.  That is zlib's contract rather
# than a defect, but a case the reference cannot finish grades nothing, so the
# catalog holds flushing schedules to out_chunk 0 or >= 6.
MIN_OUT_CHUNK_FLUSHING = 6


def out_chunks_for(flush_mode: int) -> tuple[int, ...]:
    """Output buffer sizes that terminate under this flush mode."""
    if flush_mode in (0, 5):  # Z_NO_FLUSH, Z_BLOCK
        return OUT_CHUNKS
    return tuple(c for c in OUT_CHUNKS if c == 0 or c >= MIN_OUT_CHUNK_FLUSHING)


def stride(items, count: int) -> list:
    """Evenly spaced deterministic subset, preserving order.

    Stride rather than sample: the choice follows from the inputs, needs no PRNG
    to reproduce, and spreads coverage across a family instead of clustering at
    its start.
    """
    items = list(items)
    if count >= len(items):
        return items
    if count <= 0:
        return []
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


class Catalog:
    def __init__(self, corpus: dict) -> None:
        self.docs = corpus["documents"]
        self.by_family: dict[str, list[int]] = {}
        for entry in self.docs:
            self.by_family.setdefault(entry["family"], []).append(entry["index"])
        self.cases: list[dict] = []
        self._ids: set[str] = set()

    # -- registration -----------------------------------------------------

    def add(
        self,
        case_id: str,
        family: str,
        kind: str,
        *,
        fields: list | None = None,
        argv: list[str] | None = None,
        check: str | None = None,
        params: dict | None = None,
        configs: tuple[str, ...] = BOTH,
        weight: float = 1.0,
        note: str = "",
    ) -> None:
        if case_id in self._ids:
            raise SystemExit(f"duplicate case id: {case_id}")
        self._ids.add(case_id)
        case = {
            "id": case_id,
            "family": family,
            "kind": kind,
            "configs": list(configs),
            "weight": weight,
        }
        if fields is not None:
            # A case may carry several assertions: the same behavior probed at
            # several buffer sizes is one case, and it passes only if every
            # assertion matches.
            rows = fields if fields and isinstance(fields[0], list) else [fields]
            case["asserts"] = [[str(x) for x in row] for row in rows]
        if argv is not None:
            case["argv"] = [str(x) for x in argv]
        if check is not None:
            case["check"] = check
        if params:
            case["params"] = params
        if note:
            case["note"] = note
        self.cases.append(case)

    def probe(self, case_id: str, family: str, fields: list, **kw) -> None:
        self.add(case_id, family, "probe", fields=fields, **kw)

    # -- helpers ----------------------------------------------------------

    def doc(self, index: int) -> dict:
        return self.docs[index]

    def name(self, index: int) -> str:
        return self.docs[index]["id"]

    def all_indices(self) -> list[int]:
        return [d["index"] for d in self.docs]

    def deflate_row(
        self,
        case_key: str,
        blob: int,
        *,
        level: int = 6,
        strategy: int = 0,
        wbits: int = 15,
        memlevel: int = 8,
        in_chunk: int = 0,
        out_chunk: int = 0,
        flush: int = 0,
        interval: int = 0,
        dict_blob: int = -1,
    ) -> list:
        return [
            case_key, "deflate", blob, level, strategy, wbits, memlevel,
            in_chunk, out_chunk, flush, interval, dict_blob,
        ]

    # -- A. compression levels ---------------------------------------------

    def build_levels(self) -> None:
        """Every level over a cross-section of the corpus.

        The level selects the match search: 0 stores, 1-3 run deflate_fast with
        no lazy evaluation, and 4-9 run deflate_slow, where a longer match found
        one position later displaces the match already in hand.  The levels also
        differ in max_chain and max_lazy, so the same payload takes a different
        set of matches at each one.  Grading the compressed bytes rather than a
        round trip is what makes these cases mean anything: a rewrite that
        searches correctly but breaks ties differently still decompresses.
        """
        spread = stride(self.all_indices(), 24)
        for index in spread:
            for level in LEVELS:
                self.probe(
                    f"deflate/level/{level:+d}/{self.name(index)}",
                    "compress-level",
                    self.deflate_row(f"{index}-L{level}", index, level=level),
                    weight=1.5 if level in (1, 6, 9) else 1.0,
                )

    def build_strategies(self) -> None:
        """Every strategy over the payload shapes each one exists to serve.

        Z_FILTERED and Z_RLE are not general-purpose settings; they were added
        for data whose byte values change smoothly, where a short nearby match
        beats a long far one.  The `structured` family supplies exactly that
        shape, and `runs` supplies what Z_RLE reduces to a distance of one.
        Z_HUFFMAN_ONLY must emit no match at all, which is checkable from the
        output alone, and Z_FIXED must never emit a dynamic block.
        """
        targets = (
            stride(self.by_family["structured"], 8)
            + stride(self.by_family["runs"], 8)
            + stride(self.by_family["periodic"], 8)
            + stride(self.by_family["text"], 4)
            + stride(self.by_family["random"], 4)
            + stride(self.by_family["histogram"], 6)
        )
        for index in targets:
            for strategy in STRATEGIES:
                for level in (1, 6, 9):
                    self.probe(
                        f"deflate/strategy/{STRATEGY_NAMES[strategy]}/"
                        f"L{level}/{self.name(index)}",
                        f"compress-strategy-{STRATEGY_NAMES[strategy]}",
                        self.deflate_row(
                            f"{index}-S{strategy}-L{level}", index,
                            level=level, strategy=strategy,
                        ),
                    )

    def build_windows(self) -> None:
        """windowBits, covering all three wrapper formats and the small windows.

        A window smaller than the payload forces matches to be dropped once
        their distance leaves the window, so the boundary is observable in the
        output.  The wrapper selection is a separate axis: 15 writes a two-byte
        zlib header and a big-endian Adler-32 trailer, negative writes nothing
        at all, and +16 writes a ten-byte gzip header and a little-endian CRC-32
        with a length.  A rewrite that hardcodes one wrapper fails the others.
        """
        # Payloads that straddle the small windows, so the window edge matters.
        targets = stride(self.by_family["sizes"], 10) + stride(
            self.by_family["periodic"], 6
        ) + stride(self.by_family["text"], 4)
        for index in targets:
            for wbits in WBITS_DEFLATE:
                self.probe(
                    f"deflate/window/{wbits:+d}/{self.name(index)}",
                    "compress-window",
                    self.deflate_row(f"{index}-W{wbits}", index, wbits=wbits),
                )

    def build_memlevels(self) -> None:
        """memLevel, which sizes the hash table and the pending buffer.

        At memLevel 1 the hash table is small enough that distinct strings
        collide often, so the match finder misses matches a bigger table would
        find -- and misses them deterministically.  It also shrinks the pending
        buffer, which changes when the compressor is forced to flush a block.
        Both effects are visible in the bytes.
        """
        targets = stride(self.all_indices(), 10)
        for index in targets:
            for mem in MEMLEVELS:
                for level in (1, 9):
                    self.probe(
                        f"deflate/memlevel/{mem}/L{level}/{self.name(index)}",
                        "compress-memlevel",
                        self.deflate_row(
                            f"{index}-M{mem}-L{level}", index,
                            level=level, memlevel=mem,
                        ),
                    )

    def build_matchcraft(self) -> None:
        """Payloads whose only match is one exact length at one exact distance.

        These are the sharpest cases in the catalog.  Each payload is built so
        that a single (length, distance) pair is the only match available, which
        pins one row of the length code table and one row of the distance code
        table, including the extra-bits count.  A rewrite with one wrong entry
        in `trees.c` fails exactly the cases that use that entry and passes the
        rest, so the failure names the bug.
        """
        for index in self.by_family["matchcraft"]:
            self.probe(
                f"deflate/match/{self.name(index)}",
                "compress-match",
                self.deflate_row(f"{index}-mc", index, level=9),
                weight=2.0,
            )
        # The same payloads at level 1, where deflate_fast takes the first match
        # instead of weighing the next position.
        for index in self.by_family["matchcraft"]:
            self.probe(
                f"deflate/match-fast/{self.name(index)}",
                "compress-match",
                self.deflate_row(f"{index}-mcf", index, level=1),
                weight=1.5,
            )
        # And at level 6, which is what Z_DEFAULT_COMPRESSION resolves to and
        # therefore what almost every caller actually gets.  Levels 1 and 9 sit
        # at the ends of the configuration table -- deflate_fast with the
        # shortest chains, deflate_slow with the longest -- and a table whose
        # middle rows are wrong agrees with the reference at both ends.  The
        # default has to be graded on the payloads that pin one match exactly.
        for index in self.by_family["matchcraft"]:
            self.probe(
                f"deflate/match-default/{self.name(index)}",
                "compress-match",
                self.deflate_row(f"{index}-mcd", index, level=6),
                weight=2.0,
            )
        # The rest of the configuration table, on a subset.  Each level names a
        # different (good_length, max_lazy, nice_length, max_chain) row, and a
        # row transcribed wrong fails only its own level.
        for level in (2, 3, 4, 5, 7, 8):
            for index in stride(self.by_family["matchcraft"], 3):
                self.probe(
                    f"deflate/match-L{level}/{self.name(index)}",
                    "compress-match",
                    self.deflate_row(f"{index}-mc{level}", index, level=level),
                )

    def build_histograms(self) -> None:
        """Alphabet size and skew, which drive Huffman tree construction.

        A one-symbol alphabet has no tree to build, 256 uniform symbols make
        every code the same length, and a steep geometric skew pushes the
        natural code length past the 15-bit limit so the tree has to be
        flattened by the code-length limiting pass.  That pass is subtle and
        rarely exercised by round-trip tests, because a wrong limit still
        decodes -- it just decodes to a different tree than the reference wrote.
        """
        for index in self.by_family["histogram"]:
            for level in (6, 9):
                self.probe(
                    f"deflate/histogram/L{level}/{self.name(index)}",
                    "compress-histogram",
                    self.deflate_row(f"{index}-h{level}", index, level=level),
                    weight=1.5,
                )
        # Static-tree competition: at these shapes the fixed tree sometimes wins,
        # and choosing between static, dynamic and stored is a per-block decision.
        for index in stride(self.by_family["histogram"], 12):
            self.probe(
                f"deflate/histogram/fixed/{self.name(index)}",
                "compress-histogram",
                self.deflate_row(f"{index}-hf", index, level=6, strategy=4),
            )

    # -- B. streaming: chunking and flush schedules -------------------------

    def build_chunking(self) -> None:
        """Feeding the same payload in different sized pieces.

        With Z_NO_FLUSH the compressed output must not depend on how the input
        was split, because the compressor is free to hold data until it has
        enough to decide.  That independence is a real invariant and it is
        checked here as one case with one assertion per buffer size: a rewrite
        that lets its internal buffering leak into the byte stream produces
        different output for a one-byte feed than for a whole-buffer feed, and
        this is the family that catches it.
        """
        for index in stride(self.all_indices(), 20):
            rows = [
                self.deflate_row(
                    f"{index}-in{c}", index, in_chunk=c, out_chunk=65536
                )
                for c in IN_CHUNKS
            ]
            self.probe(
                f"stream/in-chunk/{self.name(index)}",
                "stream-chunking",
                rows,
                weight=2.0,
                note="Z_NO_FLUSH output must not depend on the input split",
            )
        for index in stride(self.all_indices(), 20):
            rows = [
                self.deflate_row(f"{index}-out{c}", index, out_chunk=c)
                for c in out_chunks_for(0)
            ]
            self.probe(
                f"stream/out-chunk/{self.name(index)}",
                "stream-chunking",
                rows,
                weight=2.0,
                note="Z_NO_FLUSH output must not depend on the output buffer size",
            )
        # Both axes at once, on the payloads whose size straddles the buffers.
        for index in stride(self.by_family["sizes"], 8):
            rows = [
                self.deflate_row(f"{index}-{i}-{o}", index, in_chunk=i, out_chunk=o)
                for i in (1, 7, 1024)
                for o in (1, 7, 1024)
            ]
            self.probe(
                f"stream/grid/{self.name(index)}",
                "stream-chunking",
                rows,
                weight=1.5,
            )

    def build_flushes(self) -> None:
        """Flush schedules, which are visible in the output by design.

        A flush ends the current block and aligns the stream so a decoder can
        resume, which means the schedule the caller chose is recorded in the
        bytes.  Z_SYNC_FLUSH and Z_FULL_FLUSH append an empty stored block;
        Z_FULL_FLUSH additionally resets the match window, so matches never
        reach back across it; Z_PARTIAL_FLUSH leaves the stream mid-byte; and
        Z_BLOCK ends the block without aligning.  Each is separate bookkeeping
        and each can be got wrong on its own while the stream still decodes.
        """
        for label, mode, interval in FLUSHES:
            if mode == 0:
                continue  # covered as the baseline in build_chunking
            for index in stride(self.all_indices(), 14):
                rows = [
                    self.deflate_row(
                        f"{index}-{label}-{c}", index,
                        in_chunk=c, out_chunk=65536,
                        flush=mode, interval=interval,
                    )
                    for c in IN_CHUNKS
                ]
                self.probe(
                    f"stream/flush/{label}/{self.name(index)}",
                    f"stream-flush-{label}",
                    rows,
                    weight=1.5,
                )
        # Small output buffers against each schedule, restricted to the sizes at
        # which the drain protocol provably terminates (see MIN_OUT_CHUNK_FLUSHING).
        for label, mode, interval in FLUSHES:
            for index in stride(self.all_indices(), 8):
                rows = [
                    self.deflate_row(
                        f"{index}-{label}-o{c}", index,
                        in_chunk=64, out_chunk=c,
                        flush=mode, interval=interval,
                    )
                    for c in out_chunks_for(mode)
                ]
                self.probe(
                    f"stream/flush-out/{label}/{self.name(index)}",
                    f"stream-flush-{label}",
                    rows,
                )
        # Z_FULL_FLUSH resets the window, so a payload that repeats across the
        # reset must not match back through it.  The `periodic` family repeats on
        # a fixed stride, which is the shape that exposes a missing reset.
        for index in self.by_family["periodic"]:
            self.probe(
                f"stream/full-reset/{self.name(index)}",
                "stream-flush-full",
                self.deflate_row(
                    f"{index}-fr", index, level=9,
                    in_chunk=1024, flush=3, interval=2,
                ),
                weight=1.5,
            )

    def build_dictionaries(self) -> None:
        """deflateSetDictionary, which primes the window before any input.

        A dictionary lets the first bytes of the payload match against data the
        decompressor must be given separately, and it changes the zlib header:
        the FDICT bit is set and the dictionary's Adler-32 is written into the
        stream.  Inflate then reports Z_NEED_DICT and refuses to continue until
        the caller supplies the same bytes.  The interesting boundary is a
        dictionary longer than the window, where only the tail is kept.
        """
        dicts = self.by_family["dictionary"]
        payloads = stride(self.by_family["text"], 6) + stride(
            self.by_family["source"], 6
        ) + stride(self.by_family["structured"], 4)
        for d in dicts:
            for index in payloads:
                self.probe(
                    f"dict/deflate/{self.name(d)}/{self.name(index)}",
                    "dictionary",
                    self.deflate_row(
                        f"{index}-d{d}", index, level=6, dict_blob=d
                    ),
                )
        # Raw deflate with a dictionary: no header to carry the Adler-32, so the
        # decompressor has to be told out of band and the bytes differ.
        for d in stride(dicts, 6):
            for index in stride(payloads, 6):
                self.probe(
                    f"dict/raw/{self.name(d)}/{self.name(index)}",
                    "dictionary",
                    self.deflate_row(
                        f"{index}-dr{d}", index, wbits=-15, dict_blob=d
                    ),
                )

    # -- C. decompression ---------------------------------------------------

    def build_inflate(self) -> None:
        """The decompressor, graded on its own.

        The compressed input is produced inside the probe by a fixed recipe, so
        these cases grade inflate independently of deflate: a submission whose
        compressor is byte-perfect but whose decompressor mis-decodes a distance
        code fails here and nowhere else.  The recorded payload is the recovered
        bytes plus the end state, so a stream that decodes to the right data but
        leaves total_in wrong still fails.
        """
        for index in stride(self.all_indices(), 30):
            self.probe(
                f"inflate/basic/{self.name(index)}",
                "inflate",
                [f"{index}-inf", "inflate", index, 15, 15, 0, 0, 6],
                weight=1.5,
            )
        # Wrapper handling on the inflate side, including 47 (auto-detect).
        for index in stride(self.all_indices(), 10):
            for dw, iw in ((15, 15), (-15, -15), (31, 31), (31, 47), (15, 47)):
                self.probe(
                    f"inflate/window/{dw:+d}-{iw:+d}/{self.name(index)}",
                    "inflate-window",
                    [f"{index}-w{dw}-{iw}", "inflate", index, dw, iw, 0, 0, 6],
                )
        # Buffer chunking on the inflate side.  Unlike deflate, the recovered
        # bytes must be identical at every chunk size -- inflate has no freedom
        # to choose -- so any variation is a defect and this is one case with one
        # assertion per size.
        for index in stride(self.all_indices(), 16):
            rows = [
                [f"{index}-ic{i}-oc{o}", "inflate", index, 15, 15, i, o, 6]
                for i in (1, 7, 1024)
                for o in (1, 7, 1024)
            ]
            self.probe(
                f"inflate/chunking/{self.name(index)}",
                "inflate-chunking",
                rows,
                weight=2.0,
                note="recovered bytes must be identical at every buffer size",
            )
        # Every level's output through inflate: the decoder has to handle stored,
        # static and dynamic blocks, and which of those appear depends on level.
        for index in stride(self.all_indices(), 8):
            for level in (0, 1, 6, 9):
                self.probe(
                    f"inflate/level/{level}/{self.name(index)}",
                    "inflate",
                    [f"{index}-il{level}", "inflate", index, 15, 15, 0, 0, level],
                )

    def build_inflateback(self) -> None:
        """inflateBack, the callback-driven decoder used by gzip itself.

        This is a separate code path from inflate: the caller owns the window,
        input arrives through an in() callback and output leaves through an out()
        callback, and the loop never returns to the caller between them.  It is
        also the path most likely to be skipped in a rewrite, because nothing in
        a round-trip test touches it.
        """
        for index in stride(self.all_indices(), 18):
            self.probe(
                f"inflateback/basic/{self.name(index)}",
                "inflateback",
                [f"{index}-ib", "inflateback", index, 0, 15],
                weight=1.5,
            )
        for index in stride(self.all_indices(), 8):
            for chunk in (1, 7, 64, 4096):
                self.probe(
                    f"inflateback/chunk{chunk}/{self.name(index)}",
                    "inflateback",
                    [f"{index}-ib{chunk}", "inflateback", index, chunk, 15],
                )

    def build_errors(self) -> None:
        """Corrupted streams, where the error and its position are the answer.

        A decompressor is judged as much by what it rejects as by what it
        accepts.  Each mutation targets a different layer: `header` breaks the
        two-byte zlib header check, `checksum` leaves the data intact and
        corrupts only the trailer so the failure must come at the very end,
        `flipbit` invalidates a Huffman code so the failure must come at the bit
        that broke, and `truncate` ends the stream early so inflate must ask for
        more input rather than declare corruption.  The graded payload includes
        the return code, the message text, and how much was consumed and
        produced before the failure -- a rewrite that detects corruption late
        still fails, because zlib detects it at a specific byte.
        """
        modes = (
            "flipbyte", "flipbit", "truncate", "truncate-head", "zero",
            "append", "checksum", "header", "empty", "swap",
        )
        for mode in modes:
            for index in stride(self.all_indices(), 12):
                self.probe(
                    f"error/{mode}/{self.name(index)}",
                    f"error-{mode}",
                    [f"{index}-e{mode}", "error", index, mode, 0, 15],
                    weight=1.5,
                )
        # The same mutations against the gzip wrapper, which has a different
        # header, a different trailer, and a length field the zlib format lacks.
        for mode in ("header", "checksum", "truncate", "append"):
            for index in stride(self.all_indices(), 8):
                self.probe(
                    f"error/gzip-{mode}/{self.name(index)}",
                    f"error-{mode}",
                    [f"{index}-eg{mode}", "error", index, mode, 0, 31],
                )
        # And against raw deflate, which has no header or trailer at all: the
        # only defense is the bitstream itself.
        for mode in ("flipbit", "truncate", "zero"):
            for index in stride(self.all_indices(), 8):
                self.probe(
                    f"error/raw-{mode}/{self.name(index)}",
                    f"error-{mode}",
                    [f"{index}-er{mode}", "error", index, mode, 0, -15],
                )

    # -- D. checksums and one-shot helpers ----------------------------------

    def build_checksums(self) -> None:
        """crc32 and adler32, including the _z and combine variants.

        These are the most frequently reimplemented parts of zlib and the
        easiest to get subtly wrong: a table built with the polynomial bits in
        the wrong order gives a plausible-looking checksum that is wrong for
        every input, and a combine that mishandles a zero length breaks only the
        callers that concatenate an empty piece.  Splitting the input sweeps the
        incremental path, where a wrong initial value is otherwise invisible.
        """
        for index in stride(self.all_indices(), 40):
            self.probe(
                f"checksum/oneshot/{self.name(index)}",
                "checksum",
                [f"{index}-ck", "checksum", index, 0, 0],
                weight=1.5,
            )
        for index in stride(self.all_indices(), 16):
            for split in (1, 3, 7, 64, 1024, 65536):
                self.probe(
                    f"checksum/split{split}/{self.name(index)}",
                    "checksum",
                    [f"{index}-ck{split}", "checksum", index, split, 0],
                )
        # A non-zero seed is what an incremental caller actually passes, and it
        # is where a wrong pre/post-conditioning of the CRC shows up.
        for index in stride(self.all_indices(), 10):
            for seed in (1, 0xFFFFFFFF, 0x12345678):
                self.probe(
                    f"checksum/seed{seed:08x}/{self.name(index)}",
                    "checksum",
                    [f"{index}-cs{seed}", "checksum", index, 0, seed],
                )

    def build_oneshot(self) -> None:
        """compress, compress2, uncompress and uncompress2.

        These are the entry points most applications call.  uncompress2 reports
        how much input it consumed, which matters to any caller whose buffer
        holds more than one stream, and a destination buffer one byte short must
        produce Z_BUF_ERROR rather than a truncated result.  Both are contracts
        that a round-trip test cannot see.
        """
        for index in stride(self.all_indices(), 24):
            self.probe(
                f"oneshot/compress/{self.name(index)}",
                "oneshot",
                [f"{index}-os", "oneshot", index, -2, 0, 0],
                weight=1.5,
            )
        for index in stride(self.all_indices(), 12):
            for level in (0, 1, 6, 9):
                self.probe(
                    f"oneshot/compress2/L{level}/{self.name(index)}",
                    "oneshot",
                    [f"{index}-os{level}", "oneshot", index, level, 0, 0],
                )
        # A destination shorter than the result: the documented failure mode.
        for index in stride(self.all_indices(), 10):
            for slack in (1, 2, 16, 256):
                self.probe(
                    f"oneshot/short-{slack}/{self.name(index)}",
                    "oneshot-short",
                    [f"{index}-osh{slack}", "oneshot", index, 6, slack, 0],
                )
        # Trailing bytes after a complete stream: uncompress2 must report how
        # much it used and leave the rest, uncompress must not care.
        for index in stride(self.all_indices(), 10):
            for trail in (1, 8, 64):
                self.probe(
                    f"oneshot/trailing-{trail}/{self.name(index)}",
                    "oneshot-trailing",
                    [f"{index}-ost{trail}", "oneshot", index, 6, 0, trail],
                )

    def build_bounds(self) -> None:
        """deflateBound and compressBound, which callers allocate against.

        A bound that is too small makes a correct caller overflow its buffer, so
        the value is part of the contract rather than a hint.  The numbers are
        compared exactly rather than merely checked for sufficiency: a rewrite
        returning a wildly generous bound would break callers that size a fixed
        buffer from it, and one returning a tight bound computed differently
        would break the callers zlib's own arithmetic protects.
        """
        for index in stride(self.all_indices(), 20):
            for level in (0, 1, 6, 9):
                self.probe(
                    f"bound/L{level}/{self.name(index)}",
                    "bound",
                    [f"{index}-b{level}", "bound", index, level, 15, 8],
                )
        # The bound depends on windowBits and memLevel too, because the header
        # size and the worst-case stored-block overhead both change.
        for index in stride(self.all_indices(), 8):
            for wbits in (9, 15, -15, 31):
                for mem in (1, 8, 9):
                    self.probe(
                        f"bound/w{wbits:+d}-m{mem}/{self.name(index)}",
                        "bound",
                        [f"{index}-bw{wbits}m{mem}", "bound", index, 6, wbits, mem],
                    )

    # -- E. stream state manipulation ---------------------------------------

    def build_deflate_state(self) -> None:
        """The mid-stream state operations on the compressor.

        Each of these reaches into a live stream and is a distinct contract.
        deflateCopy must duplicate the window, the hash chains and the pending
        buffer so that both copies continue identically -- the deepest structural
        test of a rewrite's state layout, because nothing else forces the whole
        state to be reproducible.  deflateParams changes level or strategy in
        flight, which must flush what is already matched before the new settings
        take effect.  deflatePrime injects raw bits ahead of the stream,
        deflateTune overrides the match heuristics, deflatePending reports what
        is buffered, and deflateGetDictionary reads back the sliding window.
        """
        ops = ("copy", "reset", "params", "prime", "tune", "pending", "getdict")
        weights = {"copy": 2.5, "params": 2.0, "getdict": 1.5}
        for what in ops:
            for index in stride(self.all_indices(), 14):
                self.probe(
                    f"dstate/{what}/{self.name(index)}",
                    f"dstate-{what}",
                    [f"{index}-ds{what}", "statechange", index, what, 6, 0, 0],
                    weight=weights.get(what, 1.0),
                )
        # deflateParams across every level transition that changes the match
        # engine: entering and leaving deflate_fast is the transition that has to
        # flush, and 0 <-> non-zero switches stored mode on and off.
        for src_level in (0, 1, 3, 6, 9):
            for dst_level in (0, 1, 6, 9):
                if src_level == dst_level:
                    continue
                for index in stride(self.all_indices(), 3):
                    self.probe(
                        f"dstate/params/L{src_level}-L{dst_level}/{self.name(index)}",
                        "dstate-params",
                        [
                            f"{index}-dp{src_level}-{dst_level}", "statechange",
                            index, "params", src_level, dst_level, 0,
                        ],
                        weight=1.5,
                    )
        # deflatePrime with each bit count from 1 to 16: the boundary at 8 and 16
        # is where the pending-bit buffer spills into a whole byte.
        for bits in (1, 2, 3, 7, 8, 9, 15, 16):
            for index in stride(self.all_indices(), 3):
                self.probe(
                    f"dstate/prime/{bits}bits/{self.name(index)}",
                    "dstate-prime",
                    [f"{index}-dpr{bits}", "statechange", index, "prime", 6, bits, 0],
                )

    def build_inflate_state(self) -> None:
        """The mid-stream state operations on the decompressor.

        inflateMark reports the bit position, which is the only way a caller can
        resume at a sub-byte offset.  inflateCopy must clone a decoder mid-block,
        including the code tables built so far.  inflateSync must find the next
        sync point after damage, which is what makes a corrupted archive
        partially recoverable.  inflateReset2 changes the wrapper on a live
        stream.  inflateCodesUsed and inflateValidate are the newer entry points
        a rewrite is most likely to stub out, and inflateUndermine deliberately
        relaxes the checks.
        """
        ops = (
            "mark", "copy", "reset", "reset2", "prime", "sync", "syncpoint",
            "codesused", "validate", "undermine", "getdict",
        )
        weights = {"copy": 2.5, "sync": 2.0, "mark": 1.5, "getdict": 1.5}
        for what in ops:
            for index in stride(self.all_indices(), 12):
                self.probe(
                    f"istate/{what}/{self.name(index)}",
                    f"istate-{what}",
                    [f"{index}-is{what}", "inflatestate", index, what, 15, 0],
                    weight=weights.get(what, 1.0),
                )
        # The same operations against the gzip and raw wrappers, because the
        # header state machine is what reset2 and sync have to reason about.
        for what in ("reset2", "sync", "mark", "copy"):
            for wbits in (-15, 31, 47):
                for index in stride(self.all_indices(), 4):
                    self.probe(
                        f"istate/{what}/w{wbits:+d}/{self.name(index)}",
                        f"istate-{what}",
                        [
                            f"{index}-isw{what}{wbits}", "inflatestate",
                            index, what, wbits, 0,
                        ],
                    )

    def build_sync_recovery(self) -> None:
        """inflateSync doing the job it exists for: resyncing after damage.

        The `istate-sync` cases above call inflateSync on an intact stream with
        no full-flush marker in it, so the call can only scan to the end and
        report Z_DATA_ERROR -- it never exercises the search.  These cases
        compress with Z_FULL_FLUSH so the stream carries markers, then damage it
        and recover.

        The marker is five bytes, `00 00 00 ff ff`, and the search has to carry
        its match count across the third zero: that zero is both a mismatch
        against the 0xff it wanted and the first zero of the marker it is
        standing on.  A search that restarts from nothing there walks past every
        real marker in the stream, which is a rewrite that returns the right
        codes on undamaged input and cannot recover a damaged archive at all.

        `behead` and `prefix` are the sharp ones: the first has nothing but the
        marker to find, and the second puts a partial marker in front of a real
        one.
        """
        damages = ("flip", "zero", "behead", "prefix", "truncate")
        weights = {"behead": 2.5, "prefix": 2.5, "zero": 1.5}
        for damage in damages:
            for index in stride(self.all_indices(), 6):
                self.probe(
                    f"sync/{damage}/{self.name(index)}",
                    "sync-recover",
                    [f"{index}-sr{damage}", "syncrecover", index, damage, 15, 4096, 0],
                    weight=weights.get(damage, 1.0),
                )
        # The wrappers, where recovery has to contend with a header state machine
        # and a trailing checksum that no longer matches.
        for damage in ("behead", "prefix", "zero"):
            for wbits in (-15, 31):
                for index in stride(self.all_indices(), 10):
                    self.probe(
                        f"sync/{damage}/w{wbits:+d}/{self.name(index)}",
                        "sync-recover",
                        [
                            f"{index}-srw{damage}{wbits}", "syncrecover",
                            index, damage, wbits, 4096, 0,
                        ],
                        weight=1.5,
                    )
        # Flush spacing, which decides how many markers there are and how far
        # apart, and the offset argument, which moves the damage within a block.
        for chunk in (512, 8192):
            for damage in ("behead", "prefix"):
                for index in stride(self.all_indices(), 14):
                    self.probe(
                        f"sync/{damage}/c{chunk}/{self.name(index)}",
                        "sync-recover",
                        [
                            f"{index}-src{damage}{chunk}", "syncrecover",
                            index, damage, 15, chunk, 0,
                        ],
                    )
        for arg in (1, 3, 7):
            for index in stride(self.all_indices(), 18):
                self.probe(
                    f"sync/behead/off{arg}/{self.name(index)}",
                    "sync-recover",
                    [f"{index}-sro{arg}", "syncrecover", index, "behead", 15, 4096, arg],
                )

    def build_gzheader(self) -> None:
        """deflateSetHeader and inflateGetHeader.

        The gzip header is a wire format with fixed field offsets and optional
        variable-length members: an extra field with a two-byte length, a
        NUL-terminated file name, a NUL-terminated comment, and an optional
        header CRC.  Reading a member back requires actually parsing the format,
        so a rewrite that treats the header as opaque bytes round-trips its own
        output and fails here.  The variants sweep which members are present,
        because each combination shifts the offsets of everything after it.
        """
        for variant in range(12):
            for index in stride(self.all_indices(), 6):
                self.probe(
                    f"gzheader/v{variant:02d}/{self.name(index)}",
                    "gzheader",
                    [f"{index}-gh{variant}", "gzheader", index, variant, 6],
                    weight=1.5,
                )

    def build_gzfile(self) -> None:
        """The stdio-shaped gz* API, which is a second product inside zlib.

        gzopen, gzread, gzwrite, gzprintf, gzgets, gzgetc, gzseek and gztell are
        a buffered file layer with their own semantics: gzgets stops at a
        newline and NUL-terminates, gzseek on a read stream is implemented by
        decompressing forward and must still report the right offset, gzprintf
        goes through a format buffer with its own size limit, and gzungetc has to
        push back into the read buffer.  This layer is where an otherwise
        complete rewrite most often diverges, because it is the part that is not
        about compression at all.
        """
        ops = ("roundtrip", "gets", "getc", "seek", "fread", "printf", "putc")
        weights = {"roundtrip": 2.0, "seek": 2.0, "gets": 1.5}
        for what in ops:
            for index in self.by_family["gzfile"]:
                self.probe(
                    f"gz/{what}/{self.name(index)}",
                    f"gzfile-{what}",
                    [f"{index}-gz{what}", "gzfile", index, what, 6, 0, 0],
                    weight=weights.get(what, 1.0),
                )
        # Beyond the dedicated family: a cross-section of the whole corpus
        # through the write-then-read path, at every level including 0.
        for index in stride(self.all_indices(), 14):
            for level in (0, 1, 6, 9):
                self.probe(
                    f"gz/level{level}/{self.name(index)}",
                    "gzfile-roundtrip",
                    [f"{index}-gzl{level}", "gzfile", index, "roundtrip", level, 0, 0],
                )
        # Buffer sizes on the gz layer: gzbuffer changes the internal buffer, and
        # a read size that straddles it exercises the refill path.
        for index in stride(self.by_family["gzfile"], 6):
            for size in (1, 2, 8, 61, 8192):
                self.probe(
                    f"gz/read-size{size}/{self.name(index)}",
                    "gzfile-fread",
                    [f"{index}-gzr{size}", "gzfile", index, "fread", 6, size, 0],
                )
        # Seeking to specific offsets, including backwards, which forces a restart
        # of the decompression from the beginning of the stream.
        for index in stride(self.by_family["gzfile"], 6):
            for off in (0, 1, 100, 4096, 65536):
                self.probe(
                    f"gz/seek{off}/{self.name(index)}",
                    "gzfile-seek",
                    [f"{index}-gzs{off}", "gzfile", index, "seek", 6, off, 0],
                    weight=1.5,
                )

    def build_alloc(self) -> None:
        """The custom allocator hook, and what happens when it fails.

        z_stream carries zalloc, zfree and opaque, so a caller can supply its own
        allocator -- and zlib promises to route every allocation through it.  A
        port that quietly allocates on its own still works, until a consumer
        that must account for its own memory uses it.  The failure injection is the
        sharper half: a refused allocation must produce Z_MEM_ERROR and leave
        nothing leaked.

        What both halves record is the port-independent set, and that is a
        deliberate weakening.  A JVM implementation cannot ask a hook that returns
        primitive arrays for a state *object*, so it will legitimately request a
        different number of blocks than C does, and a count would fail every
        correct port.  What is recorded instead: that the hook was used at all,
        that the opaque reference survived the round trip, that nothing was left
        outstanding, that the bytes are identical, and that a refusal becomes
        Z_MEM_ERROR.

        `used_hook` on the inflate side is the same reasoning carried further, and
        it is worth stating because it is the shape of every allocator question in
        this port.  C's inflateInit2 allocates the state block through the hook
        before it returns, and defers the window to first need.  A JVM port has no
        state block to ask for -- `new Inflater()` is the state -- so the only
        allocation it can route anywhere is that deferred window.  Hence: the flag
        is read after the round trip rather than after the init; the round trip
        runs through a 256-byte output buffer, because inflate allocates a window
        only when the output spans more than one call; and the flag is printed only
        when the payload is longer than that buffer, since a payload that fits in
        one call needs no window and there is nothing for either half to report.
        What is left is a question both halves answer the same way: the window came
        from the caller's allocator.  Neither half prints the flag on the
        init-failure path at all.

        That is also why fail_after is only ever 0 or 1.  "Fail the first
        allocation" is a question every implementation answers the same way;
        "fail the fifth" depends on how many blocks it asks for, which is the
        very thing that legitimately differs.  It makes the fail_after=1 case
        doubly load-bearing: an implementation that ignored the hooks entirely
        would sail through a case whose whole point is that allocation was
        refused, so `used_hook` is graded next to the return code.
        """
        for index in stride(self.all_indices(), 12):
            self.probe(
                f"alloc/deflate/{self.name(index)}",
                "alloc",
                [f"{index}-al", "alloc", index, 0, 6, 15, 8, 0],
                weight=1.5,
            )
            self.probe(
                f"alloc/inflate/{self.name(index)}",
                "alloc",
                [f"{index}-ali", "alloc", index, 0, 6, 15, 8, 1],
                weight=1.5,
            )
        # Refusing the first allocation.  See the docstring: an Nth-allocation
        # sweep is not portable, so this is the one refusal point every
        # implementation must agree on.  It is graded on both sides of the library
        # because the two initialisers allocate differently.
        for index in stride(self.all_indices(), 10):
            self.probe(
                f"alloc/fail1/deflate/{self.name(index)}",
                "alloc-fail",
                [f"{index}-af1", "alloc", index, 1, 6, 15, 8, 0],
                weight=2.0,
            )
            self.probe(
                f"alloc/fail1/inflate/{self.name(index)}",
                "alloc-fail",
                [f"{index}-afi1", "alloc", index, 1, 6, 15, 8, 1],
                weight=2.0,
            )
        # memLevel and windowBits change the sizes requested.  The counts are not
        # recorded, but the hook must still be the thing that supplies them: these
        # cases pin that the plumbing survives every state size, including the
        # smallest legal one, where a port is most tempted to take a shortcut and
        # allocate a fixed-size block of its own.
        for mem in (1, 4, 8, 9):
            for wbits in (9, 15):
                for index in stride(self.all_indices(), 2):
                    self.probe(
                        f"alloc/m{mem}-w{wbits}/{self.name(index)}",
                        "alloc",
                        [f"{index}-am{mem}w{wbits}", "alloc", index, 0, 6, wbits, mem, 0],
                    )

    def build_abi_surface(self) -> None:
        """The in-process contract: constants, runtime metadata, argument checking.

        The constants are a contract a source rebuild hides and a consumer does
        not: code that switched on Z_STREAM_END keeps working only if the value
        is unchanged, and a port that renumbers them compiles clean everywhere
        and breaks every caller that stored one.  `runtime` reads the metadata a
        consumer can query at run time -- the version, the compile flags, the
        error strings, the CRC table -- and `defensive` and `badargs` pin the
        documented error returns that callers branch on, which is where a port
        most often substitutes an exception for a return code.

        There is no `layout` sub-case.  The C version measured sizeof(z_stream)
        and the offset of every public field, which is the sharpest ABI question
        available and has no JVM counterpart at all.  It is not dropped: it moves
        to structure.py, which checks the shape of all 115 contract members by
        reflection against the installed jar.  That is strictly more coverage
        than two structs, and it is asked of the artifact rather than of a
        program compiled beside it.
        """
        for what in ("constants", "runtime", "defensive", "badargs"):
            self.probe(
                f"abi/{what}",
                "abi-surface",
                [f"abi-{what}", "abi", what],
                weight=4.0 if what == "constants" else 2.0,
            )

    def build_compose(self) -> None:
        """Composed pipelines over the composition corpus.

        The `compose` family holds payloads assembled from several shapes in one
        buffer, which is what real data looks like: a header, a table, a run of
        text, a block of binary.  A compressor that handles each shape correctly
        in isolation can still choose block boundaries badly when they are
        adjacent, and that decision is what these grade.
        """
        for index in self.by_family["compose"]:
            self.probe(
                f"compose/deflate/{self.name(index)}",
                "compose",
                self.deflate_row(f"{index}-cp", index, level=6),
            )
        for index in stride(self.by_family["compose"], 32):
            for level in (1, 9):
                self.probe(
                    f"compose/L{level}/{self.name(index)}",
                    "compose",
                    self.deflate_row(f"{index}-cp{level}", index, level=level),
                )
        for index in stride(self.by_family["compose"], 24):
            self.probe(
                f"compose/inflate/{self.name(index)}",
                "compose",
                [f"{index}-cpi", "inflate", index, 15, 15, 0, 0, 6],
            )
        for index in stride(self.by_family["compose"], 16):
            self.probe(
                f"compose/gzip/{self.name(index)}",
                "compose",
                self.deflate_row(f"{index}-cpg", index, level=6, wbits=31),
            )

    # -- F. installed consumer entry points ---------------------------------

    def build_cli(self) -> None:
        """Entry points a downstream consumer uses, run as processes.

        zlib installs no executable, so there is no CLI of its own to grade, and
        after the migration it installs no pkg-config file either -- a `.pc` file
        advertising `-I`, `-L` and `-lz` describes a C ABI that no longer exists.
        What replaces them is not a metadata query but the real acceptance test:
        the verifier's own consumer, compiled against the *installed jar* and run
        as a process.

        It is run two ways, and the pair is the point.  On the module path the jar
        must resolve as the named module `org.zlib` and export what the consumer
        imports; on the class path `module-info` is ignored entirely and the same
        code must still work.  A descriptor that forgets to export a package
        passes one and fails the other, and neither run alone would say so.  This
        is a property of how a consumer runs rather than of how the library was
        built, which is why it is two cases and not two configs.

        The reference side of all of these is the C consumer, whose records are
        keyed by case id, so both linkage forms are compared against the same
        frozen reference behavior.

        The gzip interoperability cases go further and require the system gzip to
        accept what the library wrote, and to have its output read back.  Those
        cannot be passed by a rewrite that is merely self-consistent: the other
        implementation is not participating in the migration.
        """
        # The consumer's own sub-commands.  Every one of these is a thing a
        # downstream project actually does with zlib, not a synthetic probe.
        consumer_cases = (
            ("version", ["version"], 3.0),
            ("compress", ["compress"], 3.0),
            ("gzip", ["gzip"], 3.0),
            ("checksum", ["checksum"], 2.0),
            ("stream", ["stream"], 2.0),
            ("gzfile", ["gzfile"], 2.0),
            ("flags", ["flags"], 2.0),
            ("errors", ["errors"], 1.0),
        )
        for name, args, weight in consumer_cases:
            self.add(
                f"cli/consumer/{name}",
                "consumer-module",
                "cli",
                argv=["@consumer"] + args,
                weight=weight,
                note="a program built against the installed jar, module path",
            )
        # The same consumer, class path.  Weighted lower per case only because the
        # module-path run already proved the behavior; what these add is that the
        # jar is still usable by a consumer that never opted into modules, which
        # is most of the ecosystem.
        for name, args, weight in consumer_cases:
            self.add(
                f"cli/consumer-classpath/{name}",
                "consumer-classpath",
                "cli",
                argv=["@consumer-cp"] + args,
                weight=max(1.0, weight - 1.0),
                note="the same program with the jar on the class path",
            )
        # Interoperability with the system gzip, which is an independent
        # implementation of the same wire format.
        for name, args, weight in (
            ("gzip-reads-ours", ["interop-write"], 4.0),
            ("we-read-gzip", ["interop-read"], 4.0),
            ("gzip-reads-gzfile", ["interop-gzfile"], 3.0),
            ("concatenated", ["interop-concat"], 2.0),
        ):
            self.add(
                f"cli/interop/{name}",
                "consumer-interop",
                "cli",
                argv=["@consumer"] + args,
                weight=weight,
                note="the format is checked against an outside implementation",
            )

    def build_drivers(self) -> None:
        """The two test drivers upstream ships, run as programs.

        `test/example` and `test/minigzip` are built by the graded CMake --
        ZLIB_BUILD_EXAMPLES defaults to ON and the graded configure passes it
        explicitly -- and `example` is registered with add_test.  They are not
        installed, so they are not part of the release inventory, but they are
        part of what the build produces and their output is a published behavior:
        `example` prints a fixed self-test transcript that names the version and
        the compile flags, and `minigzip` is a complete gzip-format command line
        tool built on the gz* layer.  The migration has to port both; the contract
        names them `org.zlib.test.Example` and `org.zlib.test.MiniGzip` and
        requires each to be reachable as an executable file in the build
        directory, since the harness execs a path.  A shell wrapper around `java`
        is the expected shape, which is why the argv here is unchanged from the
        C-to-C form: `@example` resolves to whatever the build put there.

        There is no `example64` case.  Upstream builds a second copy of the driver
        with `-D_FILE_OFFSET_BITS=64` to prove the large-file variants of the gz*
        entry points work; on the JVM `long` is 64-bit unconditionally and there
        is no second variant to build, so the contract lists two drivers rather
        than four.  The coverage is not lost -- the 64-bit offset behavior is
        graded through the gzfile probe family and `gzoffset`/`gztell` -- but the
        duplicate program has nothing left to distinguish it.

        These are the most end-to-end cases in the catalog.  Everything else
        reaches the library through code the verifier owns, which the submission
        never sees; here the submission's own program is what runs, and its bytes
        on stdout are compared against the reference program's.

        Five modes, because minigzip's paths differ materially:

        `stdout`    compress a corpus payload to stdout, comparing the gzip
                    stream byte for byte.  The level and strategy flags are
                    exercised here, since each maps to a character in the mode
                    string minigzip hands to gzdopen.
        `gzin`      decompress a stream the *system* gzip produced.  The input is
                    not the library's own output, so a self-consistent rewrite
                    gets no help from it.
        `roundtrip` pipe compress into decompress and require the original bytes
                    back, which catches a stream that is well-formed but wrong.
        `file`      the named-file path, which uses gzopen and the .gz suffix
                    logic rather than gzdopen on a descriptor.
        `example`   run the self-test driver and compare its whole transcript.
        """
        self.add(
            "driver/example/example",
            "driver-example",
            "driver",
            argv=["@example"],
            params={"mode": "example"},
            weight=5.0,
            note="upstream's self-test driver, ported and built by the graded build",
        )
        # Levels and strategies on one mid-sized payload, so the flag is the only
        # thing that varies between these cases.
        flagged = self.doc(167)
        for level in range(1, 10):
            self.add(
                f"driver/minigzip/level-{level}",
                "driver-level",
                "driver",
                argv=["@minigzip", f"-{level}"],
                params={"mode": "stdout", "doc": flagged["index"]},
                weight=1.5,
                note=f"minigzip -{level} on {flagged['id']}",
            )
        for flag, label in (("-f", "filtered"), ("-h", "huffman"), ("-r", "rle")):
            self.add(
                f"driver/minigzip/strategy-{label}",
                "driver-strategy",
                "driver",
                argv=["@minigzip", flag],
                params={"mode": "stdout", "doc": flagged["index"]},
                weight=1.5,
                note=f"minigzip {flag} selects the {label} strategy",
            )
        for doc in stride(self.docs, 24):
            self.add(
                f"driver/minigzip/stdout-{doc['id']}",
                "driver-stdout",
                "driver",
                argv=["@minigzip"],
                params={"mode": "stdout", "doc": doc["index"]},
                weight=1.0,
                note="default level, stdout",
            )
        for doc in stride(self.docs, 20):
            self.add(
                f"driver/minigzip/gzin-{doc['id']}",
                "driver-gzin",
                "driver",
                argv=["@minigzip", "-d"],
                params={"mode": "gzin", "doc": doc["index"], "gzip_level": 6},
                weight=2.0,
                note="decompress a stream the system gzip produced",
            )
        for doc in stride(self.docs, 16):
            self.add(
                f"driver/minigzip/roundtrip-{doc['id']}",
                "driver-roundtrip",
                "driver",
                argv=["@minigzip"],
                params={"mode": "roundtrip", "doc": doc["index"]},
                weight=1.5,
                note="compress then decompress must return the input",
            )
        for doc in stride(self.docs, 4):
            self.add(
                f"driver/minigzip/roundtrip9-{doc['id']}",
                "driver-roundtrip",
                "driver",
                argv=["@minigzip", "-9"],
                params={"mode": "roundtrip", "doc": doc["index"]},
                weight=1.5,
                note="roundtrip at maximum level",
            )
        for doc in stride(self.docs, 6):
            self.add(
                f"driver/minigzip/file-{doc['id']}",
                "driver-file",
                "driver",
                argv=["@minigzip"],
                params={"mode": "file", "doc": doc["index"]},
                weight=2.0,
                note="the named-file path, including the .gz suffix handling",
            )

    # -- G. structure: build, install tree, ABI, package metadata ------------

    STRUCT_CASES: tuple[tuple[str, str, tuple[str, ...], float, str], ...] = (
        # Configure, build, install.  Unlike the C-to-C form, these are two real
        # builds: the contract defines a BUILD_SHARED_LIBS=ON configuration and an
        # =OFF one, and each must configure, build and install completely.  The
        # option is meaningless for a jar, which is why it is graded -- a build
        # that fails on a switch it has no use for has broken its interface.  The
        # checks below that describe a one-off property of the build system rather
        # than of a configuration are pinned to one config so they run once.
        ("build/configure", "configure", BOTH, 3.0,
         "cmake configure with no C compiler able to compile the repository"),
        ("build/compile", "compile", BOTH, 4.0,
         "build to completion with the C compilers shimmed to refuse"),
        ("build/install", "install", BOTH, 3.0, ""),
        ("build/warnings", "build-warnings", BOTH, 1.0,
         "build log free of hard errors and unresolved-reference warnings"),
        ("build/examples-flag", "examples-flag", ("static",), 1.0,
         "ZLIB_BUILD_EXAMPLES=ON must not break the configure"),
        ("build/reconfigure-clean", "reconfigure", ("static",), 1.0,
         "a second configure in a used build dir must succeed"),
        ("build/rebuild-idempotent", "rebuild", ("static",), 1.0,
         "an immediate second build must do nothing and stay green"),
        ("build/version-reported", "cmake-version", ("static",), 2.0,
         "the project version reported by cmake is still 1.3.1"),
        ("build/out-of-source", "outofsource", ("static",), 1.0,
         "the documented out-of-source build works"),
        ("build/skip-install-flags", "skip-install", ("static",), 1.0,
         "SKIP_INSTALL_* options still suppress what they name"),
        ("build/prefix-honored", "prefix", ("static",), 2.0,
         "CMAKE_INSTALL_PREFIX places everything under the prefix"),
        ("build/cache-options", "cache-options", ("static",), 2.0,
         "the documented cache options still exist with their upstream defaults"),
        ("build/ctest-registration", "ctest", ("static",), 1.0,
         "`example` is still registered with add_test and ctest lists it"),
        ("build/javac-invoked", "javac-invoked", ("static",), 2.0,
         "the build actually compiles Java sources rather than staging a prebuilt jar"),
        ("build/config-agreement", "config-agreement", ("static",), 3.0,
         "both configurations install a jar with the same public surface"),

        # Install inventory.  The release contract is now two entries and a great
        # many absences.  `share/java/zlib-1.3.1.jar` is the artifact and
        # `share/java/zlib.jar` is the stable name that stands in for the old
        # `libz.so.1` symlink -- a downstream build that hardcodes one path needs
        # the unversioned name to keep working across releases, which is the same
        # service the SONAME link performed.  The absences carry as much weight:
        # an install tree that still ships a `.so`, a `.a`, a header or a `.pc`
        # file is advertising a C ABI that no longer exists, and a consumer that
        # believes the advertisement fails at link time rather than here.
        ("install/jar-versioned", "inv-jar", BOTH, 4.0,
         "share/java/zlib-1.3.1.jar, the artifact"),
        ("install/jar-stable-link", "inv-jar-link", BOTH, 3.0,
         "share/java/zlib.jar -> zlib-1.3.1.jar, the stable name"),
        ("install/jar-is-zip", "inv-jar-readable", BOTH, 3.0,
         "the artifact is a well-formed zip a JDK tool can open"),
        ("install/no-native-lib", "inv-no-native", BOTH, 4.0,
         "no .so/.a/.dylib/.dll/.jnilib anywhere under the prefix"),
        ("install/no-headers", "inv-no-headers", BOTH, 3.0,
         "no zlib.h or zconf.h: there is no C ABI left to declare"),
        ("install/no-pkgconfig", "inv-no-pc", BOTH, 2.0,
         "no .pc file, whose -I/-L/-lz a Java consumer cannot use"),
        ("install/no-manpage", "inv-no-man", BOTH, 1.0,
         "no man3 page documenting C prototypes"),
        ("install/no-loose-classes", "inv-no-loose-classes", BOTH, 2.0,
         "no .class files outside the jar, which would be a second copy"),
        ("install/no-extra", "inv-no-extra", BOTH, 2.0,
         "no unexpected files added to the install tree"),
        ("install/no-source-leak", "inv-no-source-leak", BOTH, 2.0,
         "no .java/.c/.o/.class artifacts installed"),
        ("install/prefix-contained", "inv-prefix-contained", BOTH, 2.0,
         "every installed path is under the prefix"),
        ("install/manifest", "inv-manifest", ("static",), 1.0,
         "install_manifest.txt lists what was installed"),

        # The jar as a container.  What the ELF section did for a shared object
        # this does for the artifact: it reads what was actually shipped rather
        # than asking the library about itself.  A jar that carries the test
        # drivers, a second copy of the classes, a signature block or a Main-Class
        # is a different product from the one the contract describes, however
        # correctly its compression works.
        ("jar/entries-required", "jar-entries", BOTH, 4.0,
         "every class the contract names is present in the jar"),
        ("jar/entries-forbidden", "jar-forbidden", BOTH, 4.0,
         "no test classes, sources, native libraries or nested jars"),
        ("jar/packages", "jar-packages", BOTH, 3.0,
         "only org.zlib: no shaded dependency smuggled into the artifact"),
        ("jar/manifest-attributes", "jar-manifest", BOTH, 3.0,
         "the manifest carries the pinned attributes and version"),
        ("jar/no-main-class", "jar-no-main", BOTH, 2.0,
         "a library declares no Main-Class"),
        ("jar/no-signature", "jar-unsigned", BOTH, 2.0,
         "no META-INF signature block, which would pin the jar to one signer"),
        ("jar/compression", "jar-compression", BOTH, 1.0,
         "entries use a documented compression method"),
        ("jar/no-duplicate-entries", "jar-no-dup", BOTH, 2.0,
         "no entry name appears twice, which resolves unpredictably"),

        # The module descriptor.  This is the direct successor to zlib.map: both
        # are a machine-readable declaration of exactly what the outside world may
        # reach, and in both cases the declaration is the contract rather than a
        # build detail.  A missing export is invisible on the class path and fatal
        # on the module path, which is why the consumer runs both ways.
        ("module/descriptor-present", "mod-present", BOTH, 4.0,
         "module-info.class at the root of the jar"),
        ("module/name", "mod-name", BOTH, 4.0, "the module is named org.zlib"),
        ("module/exports", "mod-exports", BOTH, 4.0,
         "exactly the packages the contract exports, qualified as it says"),
        ("module/no-extra-exports", "mod-no-extra-exports", BOTH, 3.0,
         "nothing else is exported, whatever the implementation needed"),
        ("module/requires", "mod-requires", BOTH, 3.0,
         "no dependency beyond java.base"),
        ("module/forbidden-requires", "mod-forbidden-requires", BOTH, 3.0,
         "no requires on a module that would supply another zlib"),
        ("module/not-open", "mod-not-open", BOTH, 2.0,
         "the module is not open, which would defeat its own encapsulation"),
        ("module/describe-agrees", "mod-describe", BOTH, 2.0,
         "`jar --describe-module` agrees with the compiled descriptor"),
        ("module/validate", "mod-validate", BOTH, 2.0,
         "the module resolves in a real module graph"),

        # The API, read by reflection against the installed jar.  This is where
        # the layout probe went.  The C version measured sizeof(z_stream) and two
        # field offsets; this checks the name, kind, modifiers, parameter types and
        # return type of all 115 contract members, which is the same question --
        # can a consumer compiled against the published interface still reach it --
        # asked of considerably more of the surface.
        ("api/types-present", "api-types", BOTH, 4.0,
         "all seven public types exist with the declared kind and modifiers"),
        ("api/members-present", "api-members", BOTH, 4.0,
         "every declared member exists with the declared signature"),
        ("api/no-extra-public", "api-no-extra", BOTH, 3.0,
         "no public member beyond the contract: the surface did not widen"),
        ("api/constants-values", "api-constants", BOTH, 4.0,
         "the 39 int constants and ZLIB_VERSION hold their pinned values"),
        ("api/constants-final", "api-constants-final", BOTH, 2.0,
         "the constants are static final, not mutable fields"),
        ("api/stream-fields", "api-stream-fields", BOTH, 4.0,
         "ZStream's public fields exist with the declared types"),
        ("api/nested-hooks", "api-nested-hooks", BOTH, 3.0,
         "the Allocator and Deallocator hook interfaces are present"),
        ("api/crc-table-defensive", "api-crc-table", BOTH, 2.0,
         "crcTable() hands out a copy, not the live table"),
        ("api/no-checked-exceptions", "api-exceptions", BOTH, 2.0,
         "no checked exception beyond what the contract declares"),
        # The one question that exists only because the target is a JVM: C's
        # inflateBackInit_ cannot see how long the caller's window is, so it does
        # not check.  A Java implementation cannot help seeing it, and the contract
        # requires it to refuse a short window with the documented return code
        # rather than let an array bound decide.  Graded here rather than in the
        # probe because probe.c has no way to ask it.
        ("api/inflateback-short-window", "api-back-window", BOTH, 3.0,
         "a window shorter than 1 << windowBits is refused, not overrun"),

        # The class files themselves, read as class files.  These are the
        # structural half of the "did the C really leave" question -- the
        # audit gates own the mandatory version -- and the place where a port
        # that delegated to java.util.zip is caught: the delegation is a constant
        # pool entry, and no amount of correct output hides it.
        ("classfile/major-version", "cf-major", BOTH, 3.0,
         "compiled for the pinned bytecode version"),
        ("classfile/all-parse", "cf-parse", BOTH, 3.0,
         "every class in the jar parses as a class file"),
        ("classfile/no-native-methods", "cf-no-native", BOTH, 4.0,
         "no method carries ACC_NATIVE: there is no C left to call"),
        ("classfile/no-forbidden-types", "cf-no-forbidden-types", BOTH, 4.0,
         "no reference to java.util.zip, Unsafe or the FFM API"),
        ("classfile/no-forbidden-strings", "cf-no-forbidden-strings", BOTH, 3.0,
         "the same names do not appear as strings for reflection to resolve"),
        ("classfile/no-process-spawn", "cf-no-process", BOTH, 4.0,
         "no Runtime.exec or ProcessBuilder reference"),
        ("classfile/no-library-load", "cf-no-load", BOTH, 4.0,
         "no System.load or loadLibrary reference"),
        ("classfile/deps-closed", "cf-deps", BOTH, 3.0,
         "the constant pool references only java.base and org.zlib"),

        # The instrument and the consumer must actually reach the artifact, both
        # linkage ways.  A failure here is not a behavioral finding, it is the
        # verifier telling itself the jar is unusable.
        ("link/probe-modulepath", "probe-modulepath", BOTH, 4.0,
         "the differential probe compiles and runs with the jar as a module"),
        ("link/probe-classpath", "probe-classpath", BOTH, 4.0,
         "the same probe runs with the jar on the class path"),
        ("link/consumer-compiles", "consumer-compiles", BOTH, 4.0,
         "a consumer compiles against the installed jar alone"),

        # Source-level structure of the release contract.  What the migration
        # replaced -- C gone, Java present and proportionate -- is graded in
        # audit.py instead, as mandatory gates, and only there: grading it in
        # both places would weight one migration fact twice, once as a gate that
        # zeroes the whole score and once as structural credit, so a submission
        # that left a C file behind would be charged for it in a way no rubric
        # could explain.  A mandatory fact belongs to the gates; these cases are
        # about something else -- what a downstream consumer still finds in the
        # tree after the rewrite.
        #
        # None of these is about State A.  State A is C: it produces no jar, so it
        # fails most of the structural suite by construction, and that is what the
        # State-A rejection run exists to confirm.  The run that must come out
        # clean is the reference State B, because the mutation sweep measures every
        # mutant against it -- a case that already fails on the reference is a case
        # that can never detect a mutation.
        #
        # zlib.h is the interesting one.  It is not installed -- shipping it would
        # advertise an ABI that is gone -- but it must stay in the source tree,
        # because it is the specification the port was written against and the only
        # human-readable statement of what each entry point promises.  A migration
        # that deletes its own specification has removed the evidence that the
        # behavior was preserved on purpose.
        ("source/spec-header-kept", "src-spec-header", ("static",), 3.0,
         "zlib.h stays in the tree as the behavioural specification"),
        ("source/module-info-declares", "src-module-info", ("static",), 3.0,
         "module-info.java declares what zlib.map declared"),
        ("source/cmake-targets", "src-cmake-targets", ("static",), 2.0,
         "the zlib and zlibstatic target names survive"),
        ("source/cmake-no-dangling", "src-cmake-dangling", ("static",), 2.0,
         "CMakeLists references no file the migration removed"),
        ("source/license", "src-license", ("static",), 1.0,
         "LICENSE and the license notices survive the migration"),
        ("source/docs", "src-docs", ("static",), 1.0,
         "the shipped documentation is still present"),
    )

    def build_struct(self) -> None:
        for case_id, check, configs, weight, note in self.STRUCT_CASES:
            self.add(
                case_id, "structure", "struct",
                check=check, configs=configs, weight=weight, note=note,
            )

    # -- H. migration audit (anti-cheat) ---------------------------------

    # These are gates, not score.  The behavioural score is awarded only when every
    # mandatory gate holds.  A submission that keeps the C around, shells out to
    # another implementation, links the system zlib, or special-cases the
    # evaluation has not performed the migration, however well it compresses.
    GUARD_CASES: tuple[tuple[str, str, bool, str], ...] = (
        # G1 -- the old implementation has left the source closure.
        ("guard/no-c-sources", "no-c-sources", True,
         "no .c/.cc/.cpp/.S/.inc translation unit anywhere in the repository"),
        ("guard/no-private-headers", "no-private-headers", True,
         "the private C headers (deflate.h, inflate.h, trees.h, ...) are gone"),
        ("guard/no-c-in-build", "no-c-in-build", True,
         "CMake declares no C/C++ sources for either library target"),
        ("guard/java-present", "java-present", True,
         "a substantial Java implementation exists in the repository"),
        ("guard/java-is-primary", "java-is-primary", True,
         "the installed jar is produced from the repository's Java sources"),
        ("guard/no-c-fallback", "no-c-fallback", True,
         "no C implementation kept behind a CMake option or feature flag"),

        # G2 -- no C toolchain participated in producing the artifacts.
        ("guard/compiler-shim-clean", "compiler-shim-clean", True,
         "the shimmed C/C++ drivers were never asked to compile a repository file"),
        ("guard/no-compile-syscalls", "no-compile-syscalls", True,
         "no cc1/cc1plus/as invocation observed during the build"),
        ("guard/no-prebuilt-objects", "no-prebuilt-objects", True,
         "no .o/.a/.so/.class/.jar checked into the submission"),
        ("guard/no-embedded-reference", "no-embedded-reference", True,
         "no copy of the reference library is shipped inside the repository"),
        ("guard/no-vendored-zlib", "no-vendored-zlib", True,
         "no vendored zlib, miniz, zlib-ng or libdeflate source in the tree"),

        # G3 -- provenance of the shipped artifact, read from the class files.
        ("guard/class-java-provenance", "class-java-provenance", True,
         "the shipped classes carry javac provenance and Java source names"),
        ("guard/no-c-provenance", "no-c-provenance", True,
         "no class names a C translation unit as its source"),
        # The central gate of this task.  Every other route to a shortcut costs
        # the submitter something -- a checked-in binary, a spawned process, a
        # loaded library -- but the JDK ships a complete, correct, bit-compatible
        # deflate in java.util.zip, three lines away and free.  A port that calls
        # it produces the reference's exact bytes on every behavioural case in this
        # catalog and has performed no migration whatsoever.  There is no
        # behavioral test that separates the two, which is precisely why this is a
        # mandatory gate read out of the constant pool rather than a scored case.
        ("guard/no-jdk-deflate", "no-jdk-deflate", True,
         "the artifact does not delegate to java.util.zip or java.util.jar"),
        ("guard/no-native-methods", "no-native-methods", True,
         "no native method: the C is not being reached through JNI"),
        ("guard/no-unsafe-ffm", "no-unsafe-ffm", True,
         "no sun.misc.Unsafe and no java.lang.foreign downcall"),
        ("guard/deps-whitelist", "deps-whitelist", True,
         "no dependency on a module or class outside java.base"),
        ("guard/no-system-zlib", "no-system-zlib", True,
         "the artifacts do not load or exec the system zlib"),
        ("guard/no-library-load", "no-library-load", True,
         "no System.load or loadLibrary of an outside implementation"),
        ("guard/no-exec-helpers", "no-exec-helpers", True,
         "the library spawns no helper process while under test"),
        ("guard/no-file-access", "no-file-access", True,
         "the compression paths read no file outside the caller's buffers"),

        # G4 -- one implementation on one default path.
        ("guard/single-implementation", "single-implementation", True,
         "no second implementation kept behind a flag, feature or environment"),
        ("guard/no-env-dispatch", "no-env-dispatch", True,
         "behavior does not change with environment variables"),
        ("guard/no-verifier-awareness", "no-verifier-awareness", True,
         "no branch keyed on test paths, case ids, or the verifier's presence"),
        ("guard/default-path", "default-path", True,
         "the graded artifacts are what a default build produces"),
        ("guard/no-network", "no-network", True,
         "the build performs no network access"),
        ("guard/no-corpus-answers", "no-corpus-answers", True,
         "no table of expected outputs keyed by input digest or length"),
        ("guard/no-oversized-tables", "no-oversized-tables", True,
         "no data blob large enough to hold precomputed answers"),

        # G5 -- the release contract is intact.
        ("guard/version-unchanged", "version-unchanged", True,
         "the library still reports version 1.3.1 and vernum 0x1310"),
        ("guard/compile-flags-unchanged", "compile-flags-unchanged", True,
         "zlibCompileFlags still describes the pinned type sizes"),
        ("guard/no-api-widening", "no-api-widening", True,
         "no public member beyond the pinned contract surface"),
        ("guard/module-descriptor-intact", "module-descriptor-intact", True,
         "module-info exports exactly what zlib.map made visible, and no more"),
        ("guard/api-shape-unchanged", "api-shape-unchanged", True,
         "the declared types, signatures and constant values are unchanged"),
        # No advisory entry in this tuple, and `driver.py`'s partition check is why
        # it cannot gain one casually: PROVENANCE_GATES and SEMANTIC_GATES must
        # cover exactly what this tuple declares, so a guard here with no stage-1
        # gate to defer to fails the image build, and `check-task.py --only
        # gate-map` fails it on the host for the same reason.  The `advisory &
        # PROVENANCE_GATES` leak check in driver.py therefore guards an empty set;
        # it stays as the statement that a measured guard cannot be advisory.
    )

    def build_guards(self) -> None:
        for case_id, check, mandatory, note in self.GUARD_CASES:
            self.add(
                case_id, "audit", "guard",
                check=check,
                configs=BOTH,
                weight=0.0,
                params={"mandatory": mandatory},
                note=note,
            )

    # -- assembly ------------------------------------------------------------

    def build_all(self) -> None:
        self.build_levels()
        self.build_strategies()
        self.build_windows()
        self.build_memlevels()
        self.build_matchcraft()
        self.build_histograms()
        self.build_chunking()
        self.build_flushes()
        self.build_dictionaries()
        self.build_inflate()
        self.build_inflateback()
        self.build_errors()
        self.build_checksums()
        self.build_oneshot()
        self.build_bounds()
        self.build_deflate_state()
        self.build_inflate_state()
        self.build_sync_recovery()
        self.build_gzheader()
        self.build_gzfile()
        self.build_alloc()
        self.build_abi_surface()
        self.build_compose()
        self.build_cli()
        self.build_drivers()
        self.build_struct()
        self.build_guards()


# The catalog is generated, so a mistake in one generator could silently shrink
# coverage.  These floors are asserted when the verifier image is built: the image
# does not build if the catalog comes up short.
MIN_TOTAL_CASES = 800
MIN_BEHAVIOURAL_CASES = 650
MIN_GUARD_CASES = 30
MIN_STRUCT_CASES = 50
MIN_FAMILIES = 40


def build_catalog(corpus: dict) -> dict:
    catalog = Catalog(corpus)
    catalog.build_all()
    cases = catalog.cases

    by_kind: dict[str, int] = {}
    by_family: dict[str, int] = {}
    assertions = 0
    executions = 0
    for case in cases:
        by_kind[case["kind"]] = by_kind.get(case["kind"], 0) + 1
        by_family[case["family"]] = by_family.get(case["family"], 0) + 1
        rows = len(case.get("asserts", [1]))
        assertions += rows
        executions += rows * len(case["configs"])

    # `driver` counts as behavioural alongside `probe` and `cli`: those cases run
    # the submission's own example and minigzip binaries and compare their
    # transcripts and produced files against the reference's, which is a
    # behavioral comparison and is scored as one.  Only `struct` and `guard` sit
    # outside the behavioural score.
    behavioural = (
        by_kind.get("probe", 0) + by_kind.get("cli", 0) + by_kind.get("driver", 0)
    )
    if len(cases) < MIN_TOTAL_CASES:
        raise SystemExit(f"catalog has {len(cases)} cases, floor is {MIN_TOTAL_CASES}")
    if behavioural < MIN_BEHAVIOURAL_CASES:
        raise SystemExit(f"only {behavioural} behavioural cases, floor is {MIN_BEHAVIOURAL_CASES}")
    if by_kind.get("guard", 0) < MIN_GUARD_CASES:
        raise SystemExit(f"only {by_kind.get('guard', 0)} guard cases")
    if by_kind.get("struct", 0) < MIN_STRUCT_CASES:
        raise SystemExit(f"only {by_kind.get('struct', 0)} structural cases")
    if len(by_family) < MIN_FAMILIES:
        raise SystemExit(f"only {len(by_family)} families, floor is {MIN_FAMILIES}")

    manifest = {
        "schema": "swerefactor-catalog-v1",
        "task": "lang02-zlib-c-to-java",
        "corpus_digest": corpus["digest"],
        "counts": {
            "total": len(cases),
            "behavioural": behavioural,
            "assertions": assertions,
            "executions": executions,
            "by_kind": by_kind,
            "by_family": by_family,
        },
        "cases": cases,
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["digest"] = hashlib.sha256(payload).hexdigest()
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="build the lang02 case catalog")
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()

    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    manifest = build_catalog(corpus)
    if args.out:
        args.out.write_text(
            json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
    counts = manifest["counts"]
    print(
        f"cases: {counts['total']}  behavioural: {counts['behavioural']}"
        f"  assertions: {counts['assertions']}  executions: {counts['executions']}"
    )
    for kind, count in sorted(counts["by_kind"].items()):
        print(f"  kind {kind:8s} {count:6d}")
    if args.summary:
        for family, count in sorted(counts["by_family"].items()):
            print(f"  family {family:28s} {count:6d}")
    print(f"catalog digest: {manifest['digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
