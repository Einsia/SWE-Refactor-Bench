#!/usr/bin/env python3
"""Runs one behavioural module of lang02-zlib-c-to-java.

The grading engine for this task -- the corpus generator, the case catalog, the
differential probe pair, the CMake driver, the class-file reader -- predates the
modular suite and is unchanged by it.  What changed is who calls it: instead of
one process that builds, measures and scores in a fixed order, each module is its
own process that asks this driver for one slice of the catalog.

The slices are not arbitrary.  ``catalog.py`` already tags every case with a
family, and the families already group the way a reviewer of a zlib port would
group them: the deflate parameters, the flush protocol, the gzip file layer, the
inflate state machine.  MODULES below is that grouping made explicit, and the
mapping is checked to be total -- a family nobody claims is a case that would
silently stop being graded, so it fails the image build instead.

What has to be shared is the build, because it costs minutes and seventeen
modules need its output.  The `build` module runs first, installs both published
configurations into $SRB_SUITE_WORK, compiles the Java half of the differential
pair against the installed jar, and writes build.json; everything after it reads
that file.  Nothing else crosses the process boundary.

The one Java-specific thing about that handover is what gets written down.  A
downstream module cannot be handed the jar and told to compile the probe itself:
javac copies a `static final int` into the class file at the use site, so a probe
compiled twice against two jars is two different programs, and which one answered
a case would depend on which module ran it.  So the build module compiles it once
per linkage mode and publishes the whole `java -p <jar> --add-modules org.zlib
-cp <classes> Probe` argv.  Downstream modules run that argv verbatim.

Usage:
    driver.py --module build                # the shared build and the probe
    driver.py --module deflate-params       # slice of the catalog
    driver.py --module structure            # release contract, jar, module-info
    driver.py --module provenance           # the gates with an artifact to point at
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build as buildmod  # noqa: E402
import audit  # noqa: E402
import oracle  # noqa: E402
import structure  # noqa: E402
import vlib  # noqa: E402
from vlib import Log  # noqa: E402

CONFIGS = buildmod.CONFIGS

#: The configuration every behavioural case is measured in.
#:
#: Both configurations are built and both are graded structurally, because a
#: submission that only ever tried one usually cannot install the other.  The
#: behavioural comparison runs in one of them, and it is `shared` because that is
#: the configuration upstream's CMakeLists installs unconditionally.  A jar has no
#: static-link mode for a second configuration to differ in: what "static" means
#: for this task is a second CMake configuration, not a second artifact.
BEHAVIOURAL_CONFIG = "shared"
BEHAVIOURAL_MODE = buildmod.MODE_MODULE

# Directories a submission may leave behind that must not be reused.  Rebuilding
# from source is the point: an installed tree or a stale build/ could have been
# produced by anything, including a build with network access or a JDK this
# container does not have.
#
# `classes/` and `bin/` are deliberately absent.  Both are plausible names for a
# directory of *sources* in a Java project, and discarding a submission's sources
# would fail it for the verifier's guess about a layout.  What protects the run
# from a stale `classes/` is that the graded build compiles into its own build
# directory, not that this list deleted it.
DISCARD_DIRS = ("build", "_build", "cmake-build", "cmake-build-debug",
                "cmake-build-release", "out", "install", "node_modules",
                ".git", ".hg", ".svn", "__pycache__",
                ".gradle", ".mvn", "target", ".m2", ".idea", ".settings")

# --------------------------------------------------------------------------- #
# Which catalog families each module owns.
#
# Every behavioural family in catalog.py appears exactly once.  `_check_total`
# proves it, so adding a family without claiming it is a build failure rather than
# a quiet loss of coverage.
#
# The grouping is by what a defect in it would mean, not by case count.  A
# submission can get every compression level right and still have no flush
# protocol; it can round-trip every payload and still not implement
# `deflateSetDictionary`.  Those are different claims about the migration, so they
# are different modules and they fail separately.
# --------------------------------------------------------------------------- #
MODULES: dict[str, tuple[str, ...]] = {
    # A. the deflate parameters that select a code path: level, window, memLevel
    "deflate-params":  ("compress-level", "compress-window", "compress-memlevel"),
    # B. the five strategies, plus the payloads built to exercise match finding
    #    and the literal/length histogram Z_FIXED and Z_HUFFMAN_ONLY react to
    "deflate-strategy": ("compress-strategy-default", "compress-strategy-filtered",
                         "compress-strategy-huffman", "compress-strategy-rle",
                         "compress-strategy-fixed", "compress-match",
                         "compress-histogram"),
    # C. the flush protocol and chunked I/O: where a stream implementation that
    #    works on one buffer stops working
    "streaming":       ("stream-chunking", "stream-flush-partial",
                        "stream-flush-sync", "stream-flush-full",
                        "stream-flush-block", "stream-flush-noflush"),
    # D. preset dictionaries, both halves
    "dictionary":      ("dictionary",),
    # E. inflate: the decompressor, its window sizes, its chunking, inflateBack
    "inflate":         ("inflate", "inflate-window", "inflate-chunking",
                        "inflateback"),
    # F. corrupt input: what the reference returns, and where it stops
    "errors":          ("error-flipbyte", "error-flipbit", "error-truncate",
                        "error-truncate-head", "error-zero", "error-append",
                        "error-checksum", "error-header", "error-empty",
                        "error-swap"),
    # G. recovery after damage -- inflateSync and the resynchronisation cases
    "recovery":        ("sync-recover",),
    # H. the convenience layer: compress/uncompress, compressBound, and the two
    #    checksums with their combine functions
    "oneshot":         ("oneshot", "oneshot-short", "oneshot-trailing", "bound",
                        "checksum"),
    # I. the deflate stream object: copy, reset, params, prime, tune, pending
    "deflate-state":   ("dstate-copy", "dstate-reset", "dstate-params",
                        "dstate-prime", "dstate-tune", "dstate-pending",
                        "dstate-getdict"),
    # J. the inflate stream object, including the diagnostic entry points
    "inflate-state":   ("istate-mark", "istate-copy", "istate-reset",
                        "istate-reset2", "istate-prime", "istate-sync",
                        "istate-syncpoint", "istate-codesused",
                        "istate-validate", "istate-undermine", "istate-getdict"),
    # K. the gzip layer: header fields, and the whole gzFile API
    "gzip":            ("gzheader", "gzfile-roundtrip", "gzfile-gets",
                        "gzfile-getc", "gzfile-seek", "gzfile-fread",
                        "gzfile-printf", "gzfile-putc"),
    # L. long sequences of API calls in one stream, composed at freeze time
    "compose":         ("compose",),
    # M. the pluggable allocator, including allocation failure
    "memory":          ("alloc", "alloc-fail"),
    # N. the public surface as a downstream program sees it: the pinned
    #    constants, the version at run time, the defensive paths, bad arguments,
    #    and the verifier's own Java consumer in both linkage modes
    "api-surface":     ("abi-surface", "consumer-module", "consumer-classpath",
                        "consumer-interop"),
    # O. the submission's own example and minigzip, built by the graded CMake
    "drivers":         ("driver-example", "driver-level", "driver-strategy",
                        "driver-stdout", "driver-gzin", "driver-roundtrip",
                        "driver-file"),
}

#: Families reached by case *kind* rather than by family name.
#:
#: A structural case is not a payload being compressed -- it is a question about
#: the installed tree, and it is selected by kind so that adding a structural case
#: to the catalog does not also require naming its family here.
KIND_MODULES = {"structure": "struct", "provenance": "guard"}

#: Families no behavioural module claims because stage 1 owns them outright.
#:
#: Empty for this task: every family in the catalog is behavioural, structural or
#: a guard, and the guards are split between this stage and stage 1 by *check*
#: rather than by family.  The name is kept because `_check_total` reads it and a
#: future family that moved wholesale to stage 1 would go here.
STAGE_ONE_FAMILIES: tuple[str, ...] = ()

# --------------------------------------------------------------------------- #
# The measured half of the audit question.
#
# Two kinds of claim, and the line between the two lists is not "mechanical vs
# semantic" -- it is whether the gate has an *artifact* to point at.
#
#   * A gate that observes something about the delivered jar, or about a JVM
#     running it, is cheap to trust.  Whether the shimmed C driver was ever asked
#     to compile is a line in a ledger.  Whether a class file's constant pool
#     refers to `java/util/zip/Deflater` is a `CONSTANT_Class` entry.  Whether the
#     library answers differently under a hostile environment is two runs and a
#     byte comparison.  None of these is a regex guessing at intent, and none can
#     be argued with, so they are here -- measured, because "the .pc prefix is
#     wrong" and "there is no Java in this repository" are both unpaid and should
#     not read the same in the report.
#
#   * A gate that judges is where a pile of patterns would be doing semantic work
#     it cannot do.  "Is the C really gone, or was it transliterated statement for
#     statement into Java that does the same pointer arithmetic on a byte[]?" is
#     not a question about file extensions.  "Is this a migration of *this*
#     repository, or is it jzlib with the package renamed?" is not a question a
#     `.java` file count answers -- jzlib is a pure-Java zlib port that would pass
#     every provenance gate in this list.  Those are stage 1's, where a model
#     reads both trees.
#
# So a claim about the *repository* is not this module's to make.  Walking the
# submitted tree for `.class` and `.jar` files, grepping build files for a fetch,
# measuring file sizes, hashing LICENSE, hashing the three RFCs -- grading any of
# those in the stage that compares two builds would put "a stale class file is
# checked in" and "the library does not reproduce the reference on unseen input"
# on the same footing.  They are stage 1's, with a read-only scan doing the
# reading.
# --------------------------------------------------------------------------- #
PROVENANCE_GATES: tuple[str, ...] = (
    # -- the build compiled no C -----------------------------------------
    "compiler-shim-clean",      # the shimmed drivers were never asked to compile
    "no-compile-syscalls",      # no cc1/cc1plus/as ran by another path
    # -- the artifact was produced by javac from Java ---------------------
    "class-java-provenance",    # the shipped classes carry a .java SourceFile
    "no-c-provenance",          # no class names a C translation unit as its source
    # -- the bytes were produced by this repository's own code ------------
    "no-jdk-deflate",           # java.util.zip is not the implementation
    "no-native-methods",        # no JNI: the C is not being reached through a stub
    "no-unsafe-ffm",            # no sun.misc.Unsafe, no java.lang.foreign downcall
    "deps-whitelist",           # nothing outside java.base is required
    "no-system-zlib",           # the artifacts do not load or exec the system zlib
    "no-library-load",          # no System.load / loadLibrary
    "no-exec-helpers",          # the library spawns no helper process under test
    "no-file-access",           # the compression paths open no file of their own
    "no-corpus-answers",        # no stored answer table, in source or in the jar
    # -- one implementation, reached by default ---------------------------
    "single-implementation",    # one jar per configuration
    "no-env-dispatch",          # the answer does not change with the environment
    "default-path",             # the graded artifacts are what a default build makes
    # -- the release contract, measured on the install --------------------
    "version-unchanged",        # still 1.3.1 / 0x1310 on every published surface
    "compile-flags-unchanged",  # zlibCompileFlags still describes the pinned sizes
    "no-api-widening",          # no public member beyond the pinned surface
    "module-descriptor-intact",  # module-info exports what zlib.map made visible
    "api-shape-unchanged",      # declared types, signatures and constants unchanged
)

#: Which stage 1 gate answers each guard this stage stopped measuring.
#:
#: Two namespaces meet here and they are not the same one.  The keys are catalog
#: guard names -- this file's own vocabulary, partitioned with PROVENANCE_GATES
#: above and checked against the catalog in `_check_total`.  The values are gate
#: ids declared in `tests/evaluation.toml`, a file in a different build context
#: that no image containing this one can read.
#:
#: Written as a mapping rather than as a list with prose about who covers what.
#: It called six of these "stage 1 gates of the same name" when one of the six is
#: one, and it named `no-embedded-reference` as an answerer when that is another
#: key in this same table.  A deferral naming a gate that does not exist is a
#: guard measured in neither stage, which is the one outcome the split was
#: supposed to make impossible.  The references here are checked by
#: `tests/check-task.py --only gate-map`; see that file for why the check runs on
#: the host and not in any image.
#:
#: Several guards share an answerer, which is most of the reason for deferring
#: them.  The closure question is one question to a reader and was five checks to
#: a scanner, and putting it to a reviewer five times would count one defect five
#: times.  Guards deferred *and* still measured here are the two in OVERLAP_GATES,
#: and they are the only ones allowed to be.
#:
#: Not every stage 1 gate appears on the right: `drivers-are-ported` has no
#: counterpart here, and `default-path` is measured here under its own name while
#: also being declared there.  Both are fine and neither is a deferral -- this
#: table only claims that a guard this stage dropped is answered somewhere, not
#: that the two stages ask the same set of questions.
STAGE1_ANSWERS: dict[str, str] = {
    # The closure question: C gone, Java there instead, and no way back to the C.
    # Not answerable from an artifact -- a repository that kept deflate.c beside a
    # complete Java port builds a perfectly good jar, and the jar cannot say the C
    # is still there.
    "no-c-sources":          "no-c-implementation",
    "no-private-headers":    "no-c-implementation",
    "no-c-fallback":         "no-c-implementation",
    "java-present":          "java-is-real",
    "java-is-primary":       "java-is-real",
    # What the build is arranged to do, as opposed to what it did on this machine.
    # `no-network` folds in: the same gate asks what the build fetches.
    "no-c-in-build":         "no-c-in-build",
    "no-network":            "no-c-in-build",
    # A correct implementation that is not this one.  See the jzlib note above.
    "no-vendored-zlib":      "no-foreign-port",
    # A checked-in .class/.jar/.so/.o, and a checked-in reference to build from.
    "no-embedded-reference": "no-embedded-artifacts",
    "no-prebuilt-objects":   "no-embedded-artifacts",
    # Whether a table the jar legally contains is a Huffman table or memorised
    # output.  Answering it means reading the table's consumers.
    "no-oversized-tables":   "no-answer-tables",
    "no-corpus-answers":     "no-answer-tables",
    # Measured here as `no-env-dispatch`, a byte comparison between two runs.  The
    # gate's real question is whether a branch is keyed on a test path or a case
    # id, and that means reading the branch and deciding what it is for.
    "no-verifier-awareness": "no-verifier-awareness",
    # The central one, asked twice on purpose: see OVERLAP_GATES.
    "no-jdk-deflate":        "no-jdk-compression",
    # An entry here is a guard this stage declares and does not measure, paired with
    # the stage 1 gate that answers it.  Both halves have to be real: the partition
    # check above requires the catalog's guard declarations and these deferrals to
    # cover each other exactly, so a guard with no answerer -- or an answerer that
    # is not a declared gate -- fails the image build, and `check-task.py --only
    # gate-map` fails it on the host.  An advisory answerer is not enough either.
    # `required = false` puts a gate outside the conjunction stage 1 builds its
    # verdict from, so deferring to one would be a guard measured in neither stage.
}

#: The guards this stage does not measure, in the vocabulary of the catalog.
#:
#: Derived rather than written out: a guard added to the table above is deferred by
#: that edit alone, and a guard in one and not the other is not expressible.
SEMANTIC_GATES: tuple[str, ...] = tuple(STAGE1_ANSWERS)

#: Gates deliberately asked in both stages.
#:
#: Normally a gate in both lists is a bug -- it would be measured and judged, and
#: the same defect would cost a submission twice.  `no-jdk-deflate` is the
#: exception, and it is the exception for a reason specific to this task.
#:
#: The JDK's own deflate is bit-compatible with zlib's.  A submission that deletes
#: the C, writes seven classes whose public surface matches the contract, and
#: forwards every call to `java.util.zip.Deflater` passes *every behavioural case
#: in this suite* -- byte for byte, at every level, strategy and window size --
#: because it is the same algorithm.  No behavioural case can separate it from an
#: honest port.  It is three lines of work and it is the whole task not done.
#:
#: So it is asked twice, with two different instruments.  Here it is measured on
#: the artifact: constant-pool type references, string constants that could be fed
#: to `Class.forName`, and a differential run under a class loader that refuses to
#: load `java.util.zip`.  In stage 1 a model reads the source for the same thing,
#: which is the stronger instrument against obfuscation -- a name assembled from
#: two halves at run time is invisible to a constant-pool scan and obvious to a
#: reader.  Neither subsumes the other, and the cost of missing it is the whole
#: benchmark, so both run.
#:
#: This is not double-scoring.  Stage 1 gates are unscored: they are pass/fail, and
#: a failure is a zero for the submission rather than a deduction.  Here it is one
#: check of twenty-one in a required module, which by the runner's own rules zeroes
#: the stage.  The two mechanisms agree on the verdict and neither adds a penalty
#: to the other.
#:
#: `no-corpus-answers` is the second, and it splits the same way for the same
#: reason.  Measured here, it is a question about artifacts: does the jar carry a
#: resource entry, and does any string constant in the constant pool look like a
#: path into the verifier's corpus.  In stage 1 it is the question those two
#: instruments cannot ask -- given a 40 KiB table of shorts that the jar legally
#: contains, is it a Huffman table or is it output memorised per input.  The
#: measured half cannot read the table's consumers and the reading half cannot see
#: inside the archive, so neither is redundant.
OVERLAP_GATES: tuple[str, ...] = ("no-jdk-deflate", "no-corpus-answers")

#: Checks recorded here and scored nowhere in this stage, because stage 1 owns them.
#:
#: The criterion this stage runs on is that an expectation is scored only if the
#: oracle can answer it, and the oracle is State A -- the C zlib every expected
#: value in this suite was computed from.  Seventeen of the twenty-one gates meet
#: that bar: a C library spawns no helper process, loads no library at run time,
#: opens no file on the compression path, ships no answer table, does not change its
#: answer with the environment, is one implementation per prefix, and still reports
#: 1.3.1.  Those questions are about what a compression library may do, not about
#: which compiler built it, and the C answers them the same way the Java must.
#:
#: These four cannot be answered by it, and not because the instrument is awkward --
#: because their subject *is* the retired toolchain:
#:
#:   * `compiler-shim-clean` and `no-compile-syscalls` ask whether a C compiler ran.
#:     State A's build cannot be configured without one.
#:   * `class-java-provenance` asks whether the shipped classes name a .java file as
#:     their source, and `no-c-provenance` whether any of them names a .c.  State A
#:     ships no classes at all, and what it does ship came out of eighteen .c files.
#:
#: Charging for them here would mean the reference scores less than full marks on
#: its own corpus, which makes the corpus unfalsifiable as a measurement: a
#: submission's shortfall could no longer be read as a behavioural difference.
#:
#: Nothing is given up by not charging.  Stage 1 asks all four, required, before
#: this image runs, and a required gate failing there is a zero for the whole
#: submission -- reached before this image is pulled, and without the stage having
#: to be run to establish it.  `no-c-in-build`
#: owns the two compiler questions ("nothing in the build compiles C, and nothing
#: fetches"), `java-is-real` and `no-c-implementation` own the two provenance ones
#: ("is what replaced them a Java implementation rather than the same C somewhere
#: else").  They still run here, still appear in the report tagged `[recorded, not
#: scored]`, and still put their evidence in front of a reader.
#:
#: `no-jdk-deflate` is deliberately *not* here, though it is the check a reader
#: reaches for first.  Its subject is java.util.zip -- a shortcut inside the target
#: platform, not the retired one -- and "the compressed bytes come from this
#: repository's own Java" is a requirement instruction.md states about State B.  A
#: requirement stated to the agent is not detection of a migration; it is part of
#: the deliverable, and it stays scored and stays blocking for the reason
#: `run_provenance` gives.  Same for `no-corpus-answers`.
RECORDED_ONLY_GATES: frozenset[str] = frozenset({
    "compiler-shim-clean",
    "no-compile-syscalls",
    "class-java-provenance",
    "no-c-provenance",
})


def _check_total(catalog: dict) -> None:
    """Fail unless every case in the catalog is reachable from exactly one module.

    Three ways coverage can be lost, and this rejects all three:

      * a family no module claims -- the cases still exist, still get built, and
        are never run, so the stage grades a smaller suite than it reports;
      * a family two modules claim -- the cases run twice and count twice, which
        moves weight onto whatever those two modules happen to cover;
      * a family this file names that the catalog does not have -- a rename
        upstream, which looks identical to a module that simply found no work.

    The last one is why this compares in both directions.  A module whose families
    have all been renamed produces an empty slice, an empty slice is a module with
    no checks, and a module with no checks is an `error` -- which the suite reports
    as a broken verifier rather than as a failed submission, and that verdict is
    right but arrives an hour late.  Here it arrives at image build time.

    Guards are counted by their own arithmetic, not by family: they all live in
    family `audit`, and they are split between two stages by check name, so
    "one module owns family audit" is not the invariant.  The invariant is that
    the two lists partition the catalog's guards, with the deliberate overlap
    named.
    """
    cases = catalog["cases"]
    claimed: dict[str, str] = {}
    for module, families in MODULES.items():
        for family in families:
            if family in claimed:
                raise SystemExit(
                    f"family {family!r} is claimed by two modules: "
                    f"{claimed[family]!r} and {module!r}")
            claimed[family] = module
    for family in STAGE_ONE_FAMILIES:
        if family in claimed:
            raise SystemExit(
                f"family {family!r} is both a stage 1 family and claimed by "
                f"module {claimed[family]!r}")
        claimed[family] = "<stage 1>"

    kinds = {v for v in KIND_MODULES.values()}
    present = {c["family"] for c in cases if c["kind"] not in kinds}
    unclaimed = sorted(present - set(claimed))
    if unclaimed:
        raise SystemExit(f"families no module claims: {', '.join(unclaimed)}")
    # A stage 1 family is expected to be absent from the behavioural catalog -- that
    # is what moving it there means -- so it is not a phantom.
    phantom = sorted(f for f in set(claimed) - present
                     if claimed[f] != "<stage 1>")
    if phantom:
        raise SystemExit(
            "modules claim families the catalog does not declare "
            f"(renamed upstream?): {', '.join(phantom)}")

    # The arithmetic, stated twice from two directions.  Summing the per-module
    # slice sizes and comparing against the catalog's own behavioural count catches
    # a case whose kind is behavioural but whose family was matched by nothing --
    # which the set comparison above cannot see, because it compares names.
    by_family: dict[str, int] = {}
    for case in cases:
        if case["kind"] in kinds:
            continue
        by_family[case["family"]] = by_family.get(case["family"], 0) + 1
    covered = sum(n for f, n in by_family.items() if claimed.get(f) not in (None, "<stage 1>"))
    expect = int(catalog["counts"]["behavioural"])
    if covered != expect:
        raise SystemExit(
            f"modules cover {covered} behavioural cases, catalog declares {expect}")

    # Guards: the two lists must partition them, and the intersection must be
    # exactly the set this file admits to asking twice.
    declared = {c["check"] for c in cases if c["kind"] == "guard"}
    both = set(PROVENANCE_GATES) & set(SEMANTIC_GATES)
    if both != set(OVERLAP_GATES):
        raise SystemExit(
            "guard checks in both lists must be exactly OVERLAP_GATES; "
            f"in both: {sorted(both)}, declared: {sorted(OVERLAP_GATES)}")
    if set(OVERLAP_GATES) - declared:
        raise SystemExit(
            f"OVERLAP_GATES names checks the catalog does not declare: "
            f"{sorted(set(OVERLAP_GATES) - declared)}")
    union = set(PROVENANCE_GATES) | set(SEMANTIC_GATES)
    if union != declared:
        raise SystemExit(
            "the two guard lists do not partition the catalog's guards; "
            f"missing: {sorted(declared - union)}, "
            f"invented: {sorted(union - declared)}")

    # A guard the catalog itself marks advisory must not end up scored here.  The
    # catalog says `mandatory = false` for LICENSE and the RFC fixtures because a
    # hash of a document is evidence about tidiness, not about a migration; a
    # scored check inside a required module is the opposite of advisory, so this
    # asserts the two agree rather than trusting that they happen to.
    advisory = {c["check"] for c in cases
                if c["kind"] == "guard" and not c["params"].get("mandatory", True)}
    leaked = sorted(advisory & set(PROVENANCE_GATES))
    if leaked:
        raise SystemExit(
            "guards the catalog marks advisory are in the measured list, where "
            f"they would be scored: {', '.join(leaked)}")
    if not RECORDED_ONLY_GATES <= set(PROVENANCE_GATES):
        raise SystemExit(
            "RECORDED_ONLY_GATES must be checks this stage actually runs, or a "
            "typo silently un-scores nothing and leaves the real gate scored: "
            f"{sorted(RECORDED_ONLY_GATES - set(PROVENANCE_GATES))}")
    # No check for a gate named both blocking and unscored: nothing can zero the
    # module, so the pair is not expressible, and a weight-0 gate means exactly
    # one thing.  The check below is the one that matters.
    if len(RECORDED_ONLY_GATES) >= len(PROVENANCE_GATES):
        raise SystemExit(
            "every measured gate is recorded-only: this module would have no "
            "scored check, and the scorer reads that as rate 0.0")

    # Every deferral names an answerer.  The half of that claim checkable from
    # inside this image is that the name is there and is a gate id in shape; that
    # such a gate is declared in evaluation.toml is checked on the host, by
    # tests/check-task.py --only gate-map, because this image cannot see that
    # file.
    #
    # Worth checking here anyway: a guard dropped from PROVENANCE_GATES and added
    # to the mapping with an empty or misspelled answerer reads as deferred and is
    # measured by nobody, and the partition check above passes either way -- it
    # only looks at the keys.
    unanswered = sorted(g for g in SEMANTIC_GATES if not STAGE1_ANSWERS[g].strip())
    if unanswered:
        raise SystemExit(
            f"deferred guards with no stage 1 gate named: {', '.join(unanswered)}")
    malformed = sorted({a for a in STAGE1_ANSWERS.values()
                        if " " in a or a != a.strip() or not a.islower()})
    if malformed:
        raise SystemExit(
            f"answerers that are not bare gate ids: {', '.join(malformed)}")


class Assets:
    """The frozen grading inputs, verified to be the ones the image was built with.

    Everything this stage grades against was computed once, at image build time,
    from the pristine C library: the corpus, the catalog, the expectations.  At
    grading time they are read and never recomputed, because recomputing them
    would mean a C zlib had to be present -- and a working reference sitting beside
    the submission is both the answer key and the thing the submission was supposed
    to replace.  Nothing under `reference/` survives into this image; the manifest
    records those paths for auditability and they exist only in the build
    container.

    The four digests are checked against what the manifest recorded, and the check
    is not about tampering by the submission, which never runs as a user that can
    write here.  It is about the two things that do go wrong in practice: an image
    rebuilt from a changed corpus generator with a cached catalog layer, and a
    `docker cp` during development that replaced one file of a matched set.  Either
    produces a run where the answers no longer describe the corpus, and every
    failure lands on the submission looking like a verdict.  A mismatch is a broken
    verifier, so it aborts rather than scores.
    """

    def __init__(self, root: Path, log: Log) -> None:
        self.root = root
        self.log = log
        self.manifest = vlib.read_json(root / "verifier-manifest.json")
        if self.manifest.get("schema") != "swerefactor-verifier-manifest-v1":
            raise SystemExit(
                f"unexpected manifest schema: {self.manifest.get('schema')!r}")
        self.contract = vlib.read_json(root / "source-contract.json")
        self.catalog = vlib.read_json(root / "catalog.json")
        self.corpus_meta = vlib.read_json(root / "corpus" / "corpus.json")
        # The directory that *contains* blobs/, not blobs/ itself: oracle.collect
        # resolves payload paths under it, the same way freeze.py handed it over.
        self.corpus_dir = root / "corpus"
        self.baseline = root / "baseline"
        self.probe_src = root / "probe"
        self.surface_src = root / "surface"
        self.shim = root / "shim" / "ccshim.py"
        self.consumer_java = root / "Consumer.java"
        self.consumer_c = root / "consumer.c"
        self.store = oracle.ExpectationStore(
            root / "oracle" / "expectations.json",
            root / "oracle" / "expectations.bin",
        )
        digest = self.store.load()
        want = self.manifest["digests"]
        got = {"catalog": self.catalog["digest"],
               "corpus": self.corpus_meta["digest"],
               "oracle": digest}
        for key, expected in sorted(want.items()):
            if got.get(key) != expected:
                raise SystemExit(
                    f"verifier asset mismatch for {key}: image recorded "
                    f"{expected}, found {got.get(key)}. The grading inputs are "
                    f"not the frozen ones; refusing to score.")
        # A fourth relation, and the one the three digests above cannot state: the
        # expectations know which catalog they were frozen against.  Two matched
        # halves of different pairs each verify internally and answer different
        # questions.
        if self.store.meta.get("catalog_digest") != self.catalog["digest"]:
            raise SystemExit("the expectations were frozen against a different "
                             "catalog; refusing to score")
        for required in (self.probe_src / "Probe.java", self.probe_src / "probe.c",
                         self.surface_src / "Surface.java", self.shim,
                         self.consumer_java, self.consumer_c):
            if not required.is_file():
                raise SystemExit(f"frozen verifier input missing: {required}")
        log.write(
            f"assets: {self.catalog['counts']['total']} cases, "
            f"{self.corpus_meta['count']} payloads, "
            f"{len(self.store.entries)} expectations, oracle {digest[:12]}")

    def slice(self, families: tuple[str, ...] = (), kind: str = "") -> dict:
        """A catalog holding only the cases one module owns.

        The same shape as the whole catalog, so `oracle.collect` and the graders
        take it unmodified -- which is what keeps the module split from being a
        rewrite of the engine.

        Three behavioural kinds are kept, not two: `probe` is the differential
        pair, `driver` is the submission's own example and minigzip run under the
        same comparison, and `cli` is minigzip driven through a shell.  Keeping
        only `probe` would drop 103 cases into the gap between "not selected" and
        "not graded" while `_check_total`'s arithmetic still added up, because that
        counts by family and those cases are in families this file names.
        """
        if kind:
            cases = [c for c in self.catalog["cases"] if c["kind"] == kind]
        else:
            wanted = set(families)
            cases = [c for c in self.catalog["cases"]
                     if c["family"] in wanted
                     and c["kind"] in ("probe", "driver", "cli")]
        return {**self.catalog, "cases": cases}


# --------------------------------------------------------------------------- #
# The shared build
# --------------------------------------------------------------------------- #

BUILD_STATE = "build.json"


def snapshot(source: Path, dest: Path, log: Log) -> int:
    """Copy the submission into the suite's own scratch, minus the discards.

    The graded build runs on the copy, never on the delivered tree.  Two reasons,
    and the second is the one that matters here: a CMake build writes into its
    source directory when told to configure in place, and the tree the provenance
    module reads has to be the tree as submitted -- a `.class` file deleted by the
    build would otherwise disappear from the evidence stage 1 also reads.
    """
    if not source.is_dir():
        raise SystemExit(f"no submission at {source}")
    if dest.exists():
        shutil.rmtree(dest)
    count = vlib.copy_tree(source, dest, skip_names=set(DISCARD_DIRS))
    log.write(f"snapshot: {count} files -> {dest}")
    return count


def run_build(assets: Assets, work: Path, submitted: Path, emit: Emitter,
              log: Log) -> int:
    """Configure, build and install both configurations, then publish the ledger.

    This module owns everything expensive.  It runs first because every other
    module reads what it writes, and it is `required` in suite.toml because a
    submission that does not build has nothing for the rest of the stage to
    measure -- seventeen modules would each independently discover the same
    missing jar and report it as seventeen findings.

    Five build probes run here too, and they are graded here rather than in the
    structure module even though they are structural questions.  Each one is
    another configure-and-build: reconfiguring in place, building out of source,
    installing to a second prefix, rebuilding to check nothing rebuilds, running
    the submission's own ctest.  They cost what the main build costs, and a module
    that had to re-run them would double the stage's wall clock.

    What crosses to the other modules is `build.json`, and the argv it carries is
    the point.  javac copies a `static final int` into the class file at the use
    site, so a probe compiled against one jar is a different program from the same
    source compiled against another; compiling it once here and publishing the
    exact `java` invocation is what makes the fifteen case modules agree about what
    they ran.
    """
    repo = work / "repo"
    emit.metadata["snapshot"] = snapshot(submitted, repo, log)

    shim_dir = work / "shim"
    if vlib.shim_enforced():
        buildmod.install_shim(assets.shim, shim_dir, log)
    else:
        # Self-test only.  With the shim off, State A -- which is C -- builds, and
        # that is how the whole grading path gets exercised end to end before any
        # submission exists.  A grading run never takes this branch, and a run that
        # did says so in its own notes rather than only in this comment.
        shim_dir.mkdir(parents=True, exist_ok=True)
        emit.notes.append(
            "SELF-TEST RUN: the compiler shim was disabled, so C compilation was "
            "permitted. This is not a valid grading result.")
        # The same statement as a field, because the note is prose in a list a
        # scorer does not read.  A grading run must not carry this key at all, and
        # selftest.py asserts both directions -- present when it asked for the
        # self-test, absent otherwise.  It is the one artifact that distinguishes a
        # report full of skips because there was nothing to grade from a report full
        # of skips for any other reason.
        emit.metadata["selftest"] = True
        log.write("shim disabled (SRB_SHIM=off): self-test mode")

    state: dict[str, Any] = {
        "schema": "swerefactor.build-ledger/1",
        "repo": str(repo),
        "shim": str(shim_dir),
        "behavioural_config": BEHAVIOURAL_CONFIG,
        "configs": {},
    }
    for config in CONFIGS:
        builder = buildmod.Builder(repo, work, shim_dir, log, config=config)
        outcome = builder.run_all()
        if outcome.installed:
            # The supplementary probes only mean anything once a normal build has
            # succeeded; on a broken tree they produce noise that reads like seven
            # more findings.  Each records into outcome.extra under the keys
            # structure.py reads, so they run here -- where the build already is --
            # and are graded there.
            #
            # The order is not arbitrary: probe_skip_install reuses the build
            # directory probe_altprefix leaves behind, so altprefix runs first.
            builder.probe_rebuild()
            builder.probe_reconfigure()
            builder.probe_cache()
            builder.probe_ctest()
            builder.probe_outofsource()
            builder.probe_altprefix()
            builder.probe_skip_install()
        for step, ok in (("configure", outcome.configured),
                         ("compile", outcome.built),
                         ("install", outcome.installed)):
            emit.add(f"{config}/{step}", "pass" if ok else "fail",
                     f"cmake {step} of the {config} configuration",
                     required=(config == BEHAVIOURAL_CONFIG),
                     detail="" if ok else _step_detail(outcome, step))
        _emit_single_owner(config, outcome, emit, log)
        state["configs"][config] = {
            "prefix": str(outcome.prefix),
            "build_dir": str(outcome.build_dir),
            "scratch": str(outcome.scratch) if outcome.scratch else "",
            "installed": bool(outcome.installed),
            # persist() writes the streams to disk and records their paths, so the
            # modules downstream read the same logs this one saw rather than a
            # clipped copy: check_build_warnings searches the compile log for
            # javac's own diagnostics, and a needle elided from the middle of a
            # clipped log would read as a clean build.
            "outcome": outcome.persist(work / "state"),
        }
        log.write(f"build[{config}]: configured={outcome.configured} "
                  f"built={outcome.built} installed={outcome.installed}")

    # -- the handover the C form did not need ------------------------------
    #
    # javac copies a `static final int` into the class file at the use site.  A
    # probe compiled against one jar is therefore a different program from the same
    # source compiled against another, and if each module compiled its own, which
    # program answered a case would depend on which module ran it.  So it is
    # compiled here, once per linkage mode, and the whole argv is published.
    #
    # Both modes, because api-surface grades them separately: a jar can work on the
    # module path and not as a plain classpath entry -- a missing Automatic-Module-
    # Name, a split package, a service file that only the module system reads --
    # and that is a real difference to a consumer.
    state["probe"] = {}
    state["consumer"] = {}
    behavioural = state["configs"].get(BEHAVIOURAL_CONFIG) or {}
    jar = Path(behavioural["prefix"]) / buildmod.JAR_RELPATH if behavioural else None
    scratch = Path(behavioural["scratch"]) if behavioural.get("scratch") else work / "scratch"
    if _selftest_c_install(behavioural):
        # The self-test, and only ever the self-test.  See _selftest_c_install for
        # why this branch cannot be reached by a graded submission.
        _publish_c_probe(assets, state, behavioural, scratch, emit, log)
    elif not behavioural.get("installed") or jar is None or not jar.is_file():
        emit.add("probe/compile", "fail",
                 "the differential probe could not be compiled: the "
                 f"{BEHAVIOURAL_CONFIG} configuration installed no jar at "
                 f"{buildmod.JAR_RELPATH}",
                 required=True)
        log.write("build: no installed jar; the case modules will report their "
                  "cases not run")
    else:
        state["jar"] = str(jar)
        for mode in (buildmod.MODE_MODULE, buildmod.MODE_CLASSPATH):
            # The Probe.java file, not the directory it lives in: this one goes
            # straight to javac as a source argument.  structure.py's own
            # _java_sources() resolves either form, which is why the two callers
            # can be handed different things and both be right.
            result, class_dir = buildmod.compile_java_probe(
                assets.probe_src / "Probe.java", jar, scratch, log,
                config=BEHAVIOURAL_CONFIG, mode=mode)
            ok = bool(result is not None and result.ok)
            emit.add(f"probe/compile/{mode}", "pass" if ok else "fail",
                     f"the differential probe compiles against the installed jar "
                     f"on the {mode} path",
                     required=(mode == BEHAVIOURAL_MODE),
                     detail="" if ok or result is None
                            else result.tail(lines=40, limit=2000))
            if not ok:
                continue
            state["probe"][mode] = buildmod.java_command(
                "Probe", mode=mode, jar=jar, class_dir=class_dir,
                module_name=buildmod.MODULE_NAME)
        module_argv, classpath_argv = build_consumer_commands(
            assets, jar, scratch, log)
        state["consumer"][buildmod.MODE_MODULE] = module_argv or []
        state["consumer"][buildmod.MODE_CLASSPATH] = classpath_argv or []
        for mode, argv in ((buildmod.MODE_MODULE, module_argv),
                           (buildmod.MODE_CLASSPATH, classpath_argv)):
            emit.add(f"consumer/compile/{mode}", "pass" if argv else "fail",
                     f"the verifier's Java consumer compiles against the installed "
                     f"jar on the {mode} path")

    vlib.write_json(work / BUILD_STATE, state)
    log.write(f"build ledger written to {work / BUILD_STATE}")
    return 0


def _selftest_c_install(behavioural: dict) -> bool:
    """Did this run install a C zlib, with the shim off, on purpose?

    Both halves are required and neither is a heuristic about the submission.

    Whether the shim is enforced is set by whoever invoked the stage, never by the
    tree being graded -- vlib.shim_enforced() is the one definition, and it is
    enforced for every graded run.  The C library shape -- an installed
    `include/zlib.h` next to a `lib/libz.so` -- is read off the install rather than
    off the source, so it says what the build produced rather than what the
    repository looks like.

    Under enforcement a repository whose build compiles C cannot install anything at
    all: ccshim.py refuses every compile of a repository source, so `libz.so` is
    never produced and this returns False on the first clause anyway.  That is what
    keeps a self-test path from becoming a way to be graded as C -- a submission
    would have to be handed a disabled shim by the harness, and the harness does
    not offer that.

    A run that takes this branch is already labelled: run_build appended the
    SELF-TEST note to the module's own result when it disabled the shim.
    """
    if vlib.shim_enforced():
        return False
    if not behavioural.get("installed"):
        return False
    prefix = Path(behavioural["prefix"])
    if not (prefix / "include" / "zlib.h").is_file():
        return False
    libdir = prefix / "lib"
    return any((libdir / name).exists()
               for name in ("libz.so", "libz.so.1", "libz.a"))


def _publish_c_probe(assets: Assets, state: dict, behavioural: dict,
                     scratch: Path, emit: Emitter, log: Log) -> None:
    """Compile the C halves of both instruments against a C install and publish.

    This is the branch that makes the self-test a measurement rather than a claim.
    suite.toml has always said `SRB_SHIM=off` exists "for the self-test that proves
    State A scores 1.0", but nothing compiled an instrument State A could answer:
    the ledger only ever carried a `java ... Probe` argv, so State A reached
    run_slice with no probe and every behavioural case was recorded `error --
    installed no usable jar`.  4,062 cases said "the submission is broken" about
    the library every one of their expectations was frozen from.

    What is published is the same shape the Java path publishes -- a command prefix
    per linkage mode, in the same ledger fields -- so run_slice, oracle.collect and
    the graders are untouched and cannot tell the two apart.  That is deliberate: an
    instrument that took a different path through the comparison would prove
    something about the path rather than about State A.

    Both modes get the same binary, for the reason freeze.py states when it passes
    `consumer_cp=[probe]`: a C library has one linkage, so the same program answers
    both sentinels.
    """
    prefix = Path(behavioural["prefix"])
    scratch.mkdir(parents=True, exist_ok=True)
    probe_bin = scratch / "probe-selftest"
    compiled = buildmod.compile_probe(
        assets.probe_src / "probe.c", prefix, probe_bin, log,
        label="selftest-probe")
    ok = bool(compiled is not None and compiled.ok)
    emit.add("probe/compile", "pass" if ok else "fail",
             "the differential probe compiles against the installed C library "
             "(self-test)",
             required=True,
             detail="" if ok or compiled is None
                    else compiled.tail(lines=40, limit=2000))
    if not ok:
        log.write("selftest: the C probe did not compile; cases will be not run")
        return
    for mode in (buildmod.MODE_MODULE, buildmod.MODE_CLASSPATH):
        state["probe"][mode] = [str(probe_bin)]
        emit.add(f"probe/compile/{mode}", "pass",
                 f"the differential probe is runnable for the {mode} sentinel "
                 f"(self-test: one C binary answers both)",
                 required=(mode == BEHAVIOURAL_MODE))

    consumer_bin = scratch / "consumer-selftest"
    built = buildmod.compile_consumer_c(
        assets.consumer_c, prefix, consumer_bin, log, label="selftest-consumer")
    cok = bool(built is not None and built.ok)
    for mode in (buildmod.MODE_MODULE, buildmod.MODE_CLASSPATH):
        if cok:
            state["consumer"][mode] = [str(consumer_bin)]
        emit.add(f"consumer/compile/{mode}", "pass" if cok else "fail",
                 f"the verifier's consumer compiles against the installed C "
                 f"library on the {mode} path (self-test)")
    log.write(f"selftest: published C probe {probe_bin} for both linkage modes")


def build_consumer_commands(
    assets: Assets, jar: Path, scratch: Path, log: Log
) -> tuple[list[str] | None, list[str] | None]:
    """Compile the verifier's own consumer against the installed jar, both ways.

    The consumer is a downstream program: it does what a project that depends on
    this library does, which is `import org.zlib`, call the documented entry
    points, and nothing else.  It is compiled twice because the two linkage modes
    fail differently, and the `consumer-module` and `consumer-classpath` families
    grade them as separate claims.

    Returns the run argv for each mode, or None where the compile failed.  None
    means the cases in that family are reported *not run* rather than failed --
    a consumer the verifier could not compile is not evidence about the
    submission's behaviour, only about its packaging, and the packaging is already
    graded by the check that recorded the failure.
    """
    out: list[list[str] | None] = []
    for mode in (buildmod.MODE_MODULE, buildmod.MODE_CLASSPATH):
        class_dir = scratch / f"consumer-{mode}"
        result = buildmod.java_compile(
            [assets.consumer_java], class_dir, log,
            label=f"consumer-{mode}", mode=mode, jar=jar)
        if result is None or not result.ok:
            log.write(f"consumer ({mode}) did not compile; its cases will be "
                      f"reported not run")
            out.append(None)
            continue
        out.append(buildmod.java_command(
            "Consumer", mode=mode, jar=jar, class_dir=class_dir))
    return out[0], out[1]


def _emit_single_owner(config: str, outcome: buildmod.BuildOutcome,
                       emit: Emitter, log: Log) -> None:
    """One target per artifact, measured on the generated build system.

    The graded build is `cmake --build <dir> --parallel` (build.py:363, and
    instruction.md documents that exact argv).  A tree that gives two custom
    targets the same custom-command OUTPUT gets the recipe copied into both
    `build.make` files, and make may then run it twice at once.  Whether that
    corrupts the artifact is a matter of timing: measured on this submission, the
    same unchanged tree passed no scored check three times and ~97.5% of them
    four times over seven runs, because the jar recipe renames a temp into place
    and the loser finds its temp gone.

    So this is not graded on whether the build failed -- that question already has
    an answer, and the answer is a coin flip.  It is graded on the property, read
    out of the generated makefiles right after configure, where it is stable.  The
    contract is stated in instruction.md (`one target owns the rule`), which is what
    lets this be scored at all: it measures a documented requirement rather than
    guessing at fragility.

    Skipped, not failed, when configure did not run: there is no build system to
    read, the configure check already carries that verdict, and a second failure
    for one cause reads as two findings.
    """
    if not outcome.configured:
        emit.add(f"{config}/single-owner", "skip",
                 f"configure did not produce a build system for the {config} "
                 f"configuration, so its rules could not be read")
        return
    dupes = buildmod.duplicated_recipes(outcome.build_dir)
    if not dupes:
        emit.add(f"{config}/single-owner", "pass",
                 f"every artifact in the {config} configuration is built by "
                 f"exactly one target")
        log.write(f"single-owner[{config}]: no duplicated recipes")
        return
    named = "; ".join(f"{out} is built by {', '.join(sorted(targets))}"
                      for out, targets in sorted(dupes.items()))
    emit.add(
        f"{config}/single-owner", "fail",
        # Leads with the defect and the fix, because the renderer keeps the front
        # of a summary: report.py prints summary then detail's first six lines.
        f"two targets build one artifact, so `cmake --build --parallel` may run "
        f"the same recipe twice concurrently and corrupt it: {named}. Give the "
        f"rule one owning target and have the other add_dependencies() on that "
        f"target rather than DEPENDS on the same output.",
        required=(config == BEHAVIOURAL_CONFIG),
        detail=(
            f"Read from {outcome.build_dir}/CMakeFiles/*/build.make after "
            f"configure.\n"
            + "\n".join(f"  {out}  <- {', '.join(sorted(targets))}"
                        for out, targets in sorted(dupes.items()))
            + "\nEach listed target carries its own recipe for that output. The "
              "graded build passes --parallel, so make may start both copies at "
              "once; a recipe that stages a temp file and renames it into place "
              "loses the temp to whichever copy finishes first. This is reported "
              "whether or not the race fired during this run, because whether it "
              "fires is timing."),
        evidence=[{"path": str(outcome.build_dir / "CMakeFiles"),
                   "note": "generated build system"}])
    log.write(f"single-owner[{config}]: {len(dupes)} duplicated recipe(s): {named}")


def _step_detail(outcome: buildmod.BuildOutcome, step: str) -> str:
    result = getattr(outcome, step, None)
    if result is None:
        return "the step did not run because an earlier one failed"
    return result.tail(lines=40, limit=2000)


def read_build(work: Path) -> dict:
    """The ledger, or a hard stop.

    A module that reads no ledger cannot tell "the build failed" from "the build
    module never ran", and those need opposite verdicts: the first is the
    submission's, the second is the harness's.  The build module writes a ledger
    even when nothing in it built, so an absent file is unambiguously the second --
    and raising here reports it as a module error, which the scorer records as a
    broken verifier rather than as a zero for the submission.
    """
    path = work / BUILD_STATE
    if not path.is_file():
        raise SystemExit(
            f"no build ledger at {path}: the build module did not run, or ran in a "
            f"different work directory. This is a harness fault, not a submission "
            f"failure.")
    state = vlib.read_json(path)
    if state.get("schema") != "swerefactor.build-ledger/1":
        raise SystemExit(f"unexpected build ledger schema: {state.get('schema')!r}")
    return state


def restore_outcomes(state: dict) -> dict[str, buildmod.BuildOutcome]:
    """Rebuild the BuildOutcome objects the structural checks read.

    Both configurations, always -- including one that did not install.  A missing
    outcome and an outcome that records a failed install are different inputs to
    structure.py: the first makes its checks raise KeyError, the second makes them
    say what went wrong.
    """
    return {config: buildmod.BuildOutcome.restore(blob["outcome"])
            for config, blob in (state.get("configs") or {}).items()}


# --------------------------------------------------------------------------- #
# The behavioural modules
# --------------------------------------------------------------------------- #


def run_slice(assets: Assets, state: dict, module: str, emit: Emitter,
              work: Path, log: Log) -> int:
    """Run and grade the cases one module owns.

    Every module takes this path, and none of them knows which cases it has: it
    reads its own id, asks for that slice, and runs it.  The alternative -- a
    module per family with its own script -- would put the case selection in
    eighteen places and let them disagree with the catalog.

    The comparison itself is unchanged from the single-process form.  The probe
    answers a case, the frozen expectation says what the C library answered, and
    the two are compared as bytes.  What changed is only where the probe argv comes
    from: the ledger, rather than a compile this process did.
    """
    families = MODULES[module]
    catalog = assets.slice(families=families)
    if not catalog["cases"]:
        raise SystemExit(
            f"module {module!r} selected no cases from families "
            f"{', '.join(families)}: the catalog and MODULES disagree")

    probe_argv = (state.get("probe") or {}).get(BEHAVIOURAL_MODE) or []
    consumer = (state.get("consumer") or {}).get(BEHAVIOURAL_MODE) or None
    consumer_cp = (state.get("consumer") or {}).get(buildmod.MODE_CLASSPATH) or None
    config = state["configs"].get(BEHAVIOURAL_CONFIG) or {}

    if not probe_argv:
        # Nothing to run.  Every case is recorded, individually, as not run --
        # never as skipped.  A skip leaves the denominator, so a submission that
        # installed no jar would score 1.0 over nothing; and never as passed, for
        # the obvious reason.  `error` is the verdict that stays in the denominator
        # and says why.
        return record_all_unrun(
            catalog, emit,
            f"the {BEHAVIOURAL_CONFIG} configuration installed no usable jar, so "
            f"the differential probe could not be run")

    ctx = oracle.CollectContext(
        label=module,
        prefix=Path(config["prefix"]),
        build_dir=Path(config["build_dir"]),
        # Command *prefixes*, not paths.  A submission's probe is
        # `java -p <jar> --add-modules org.zlib -cp <classes> Probe`, which is not
        # an exec'able file, and the executor appends each case's arguments to it.
        probe_command=list(probe_argv),
        consumer=list(consumer) if consumer else None,
        scratch=work / "scratch",
        # Stated rather than defaulted: None means "report those cases not run".
        # Defaulting it to the module-path argv would silently grade the classpath
        # family against a module-path consumer and report that as a pass.
        consumer_cp=list(consumer_cp) if consumer_cp else None,
    )
    records, diagnostics = oracle.collect(catalog, ctx, assets.corpus_dir, log)
    grade_records(catalog, records, assets.store, emit, log,
                  abandoned=frozenset(diagnostics.get("abandoned") or ()))
    _emit_diagnostics(diagnostics, emit, log)
    return 0


def record_all_unrun(catalog: dict, emit: Emitter, why: str) -> int:
    """Every case in the slice, recorded as an error with a reason.

    Not `skip`, and the distinction decides the score.  A skip leaves the
    denominator: a module whose cases were all skipped has no scored checks, and a
    module with no scored checks divides an empty weight -- producing 0.0 by
    accident rather than on purpose, or 1.0 if anyone ever changes that branch.  An
    error stays in the denominator and carries the reason, which is what "the jar
    was never built" should look like in a report.
    """
    for case in catalog["cases"]:
        emit.add(case["id"], "error", why, weight=float(case.get("weight", 1.0)),
                 family=case["family"], kind=case["kind"])
    emit.notes.append(why)
    return 0


def grade_records(catalog: dict, records: dict, store: oracle.ExpectationStore,
                  emit: Emitter, log: Log,
                  abandoned: set[str] | frozenset[str] = frozenset()) -> None:
    """Compare each case's observed bytes against the frozen expectation.

    One check per case, and a case's keys come from `oracle.keys_for` rather than
    from anything reconstructed here -- the same function the freeze used, so a
    case cannot be graded against a key that was never frozen, and a key that was
    frozen cannot go ungraded.

    A case with several assertions passes only when every one of them matches.
    Three deflate calls at three levels over one payload is one claim about the
    compressor, and three-quarters of a claim is not three-quarters of a pass --
    the rate this stage publishes counts cases, so where a case boundary is drawn
    is what the rate can say.

    A case with no frozen expectation is an `error`, not a `fail`.  It should be
    unreachable: `Assets` refuses to start unless the store was frozen against this
    exact catalog, and `oracle.freeze` aborts on a record matching no case.  If it
    happens anyway, the cause is on this side of the line, and a submission must not
    be marked wrong for it.

    `abandoned` names keys the executor never attempted, because the module's
    deadline arrived first.  Also an `error` rather than a `fail`, and for the same
    reason: nothing was asked of the submission, so nothing about it was observed.
    The weight stays in the denominator either way -- an unattempted case earns no
    credit -- but the verdict has to say which of the two happened, because one is a
    fact about the submission and the other is a fact about the clock.
    """
    missing = 0
    unattempted = 0
    for case in catalog["cases"]:
        keys = oracle.keys_for(case)
        weight = float(case.get("weight", 1.0))
        failures: list[str] = []
        absent: list[str] = []
        skipped: list[str] = []
        diff = ""
        for key in keys:
            if key in abandoned:
                skipped.append(key)
                continue
            expectation = store.get(key)
            if expectation is None:
                absent.append(key)
                continue
            observed = records.get(key)
            if observed is None:
                failures.append(f"{key}: the submission produced no result")
                continue
            status, payload = observed
            if status != expectation.status:
                failures.append(
                    f"{key}: status {status!r}, reference {expectation.status!r}")
                continue
            if vlib.sha256_bytes(payload) == expectation.digest:
                continue
            failures.append(f"{key}: output differs")
            # One diff per case, not per key.  A case can hold dozens of
            # assertions, and a report that inlined every one of them would be
            # megabytes of hex for a single wrong byte.
            if not diff:
                diff = vlib.unified_diff(store.payload(key), payload, limit=24)
        summary = case.get("note") or case["id"]
        if absent:
            missing += 1
            emit.add(case["id"], "error",
                     f"no frozen expectation for {', '.join(absent[:4])}",
                     weight=weight, family=case["family"], kind=case["kind"],
                     asserts=len(keys))
            continue
        # After `absent`, and after `failures` has been collected but before it is
        # acted on: a case some of whose assertions ran is unattempted as a case,
        # because a case passes only when every assertion matches and the missing
        # ones cannot be assumed to -- unless one of the assertions that *did* run
        # already failed.  Then the case is settled and saying "not attempted" would
        # be a false statement about the submission, and would move a real defect
        # into the count this module reports as clock damage.
        if skipped and not failures:
            unattempted += 1
            emit.add(case["id"], "error",
                     "not attempted: the module's budget ran out before this case",
                     weight=weight, family=case["family"], kind=case["kind"],
                     asserts=len(keys), unattempted_asserts=len(skipped))
            continue
        detail = ""
        if failures:
            detail = "; ".join(failures[:6])
            if len(failures) > 6:
                detail += f" (+{len(failures) - 6} more)"
            if diff:
                detail = (detail + "\n" + diff).strip()
        emit.add(case["id"], "pass" if not failures else "fail", summary,
                 weight=weight, detail=detail,
                 family=case["family"], kind=case["kind"], asserts=len(keys))
    if missing:
        log.write(f"WARNING: {missing} case(s) had no frozen expectation; this is "
                  f"a verifier fault and they were recorded as errors")
    if unattempted:
        note = (f"{unattempted} case(s) were not attempted: the module's budget ran "
                f"out before them, so they score zero and keep their weight")
        log.write(f"WARNING: {note}")
        # In the notes as well as the log, because the log stays inside the
        # container and the notes reach the report.  A module that published a
        # partial measurement has to say so where the score is read.
        emit.notes.append(note)


def run_structure(assets: Assets, state: dict, emit: Emitter, work: Path,
                  log: Log) -> int:
    """Grade the installed tree: the jar, the module descriptor, the release facts.

    71 cases, none of which compresses anything.  They ask whether what the build
    installed is a Java library a downstream project can consume: does the jar exist
    at the published path, does `module-info.class` export what `zlib.map` made
    visible, is the class file version 61, does the pkg-config file read back, did
    the seven build probes behave.

    This module compiles its own probe rather than reading the ledger's argv, and
    that is not an inconsistency with the fifteen case modules.  `_probe_linkage`
    exists to find out *whether* a probe can be compiled against the jar at all, in
    each mode, with its own cache -- it is the measurement, so taking the answer
    from a build that already succeeded would measure nothing.
    """
    outcomes = restore_outcomes(state)
    if BEHAVIOURAL_CONFIG not in outcomes:
        raise SystemExit(
            f"the build ledger has no {BEHAVIOURAL_CONFIG!r} outcome; the build "
            f"module did not complete")
    expectations = structure.Expectations.load(assets.contract, assets.baseline)
    catalog = assets.slice(kind="struct")
    evaluator = structure.StructureEvaluator(
        Path(state["repo"]),
        outcomes,
        expectations,
        assets.contract,
        work / "scratch",
        # A directory here, not a file: _java_sources() globs it, and the surface
        # enumerator is several classes.
        assets.probe_src,
        assets.surface_src,
        log,
    )
    outs = evaluator.evaluate(catalog["cases"])
    skipped = 0
    for out in outs:
        detail = out.detail
        if out.diff:
            detail = (detail + "\n" + out.diff).strip()
        # `skip` only ever for the reference self-test, and only for a case whose
        # evidence does not exist -- structure.py sets `not_applicable` and leaves it
        # empty while grading, whatever it found.  See vlib.NotApplicable for what
        # the two states mean and vlib.c_self_test for why a submission cannot reach
        # this one.  A skip leaves the denominator; a failure does not.
        if out.not_applicable:
            skipped += 1
            emit.add(out.case_id, "skip", out.not_applicable[:200],
                     weight=out.weight, detail=out.not_applicable,
                     family=out.family, kind=out.kind)
            continue
        emit.add(out.case_id, "pass" if out.passed else "fail", out.detail[:200],
                 weight=out.weight, detail=detail,
                 family=out.family, kind=out.kind)
    log.write(f"structure: {sum(1 for o in outs if o.passed)}/{len(outs)} passed"
              + (f", {skipped} not applicable to a C install" if skipped else ""))
    if skipped:
        emit.notes.append(
            f"{skipped} of {len(outs)} structural cases ask about the Java "
            f"delivery, which this run does not have: it is the reference "
            f"self-test of the corpus, not a submission")
        emit.metadata["selftest_cases_not_applicable"] = skipped
    return 0


def run_provenance(assets: Assets, state: dict, emit: Emitter, work: Path,
                   log: Log) -> int:
    """Grade the gates that have a built artifact to point at.

    21 of the catalog's 35 guards.  Of the other 14, a model reads both trees in
    stage 1; two more are read there *as well as* measured here, so 16 guards reach
    stage 1 in total.  The split and its reasoning are in PROVENANCE_GATES above,
    and which stage 1 gate answers each deferral is in STAGE1_ANSWERS.

    Seventeen of the 21 are scored.  The remaining four are measured, reported and
    priced at zero, because their subject is which toolchain produced the artifact --
    a question State A cannot answer about itself and stage 1 already answers,
    required, before this image runs.  RECORDED_ONLY_GATES names them and argues it.

    Two of the 21 -- `no-jdk-deflate` and `no-exec-helpers`, the two that mean "a
    foreign engine produced these bytes" -- cost their share of this module like the
    other nineteen, and are read by no rule of their own.  They are also the one
    cheat weight alone underprices: a submission delegating to java.util.zip is
    bit-identical to the reference, so it passes every other case here and gives all
    six adversaries nothing to find.  Stage 1 is where that is answered.  It reads
    for the same two questions with a reviewer and fails the submission outright,
    before this image is pulled.
    """
    outcomes = restore_outcomes(state)
    expectations = structure.Expectations.load(assets.contract, assets.baseline)
    catalog = assets.slice(kind="guard")
    selected = [c for c in catalog["cases"] if c["check"] in set(PROVENANCE_GATES)]
    unknown = sorted(set(PROVENANCE_GATES) -
                     {c["check"] for c in catalog["cases"]})
    if unknown:
        raise SystemExit(
            f"PROVENANCE_GATES names checks the catalog does not declare: "
            f"{', '.join(unknown)}")
    if len(selected) != len(PROVENANCE_GATES):
        raise SystemExit(
            f"selected {len(selected)} guard cases for "
            f"{len(PROVENANCE_GATES)} gate names: the catalog declares a check "
            f"more than once")
    auditor = audit.IntegrityAuditor(
        # The repository, as well as the outcomes.  Unlike the C form, several of
        # these gates read source: `no-native-methods` is a `native` keyword,
        # `deps-whitelist` is what module-info requires, and both are questions
        # about what was written as much as about what was built.
        Path(state["repo"]),
        outcomes,
        assets.baseline,
        expectations,
        assets.contract,
        work / "scratch",
        assets.probe_src,
        log,
    )
    outs = auditor.evaluate(selected)
    skipped = 0
    for out in outs:
        # `required` is decided here, from the check's name, and not from the
        # gate's own `mandatory` flag.  Every gate in the catalog was mandatory
        # when the catalog was the whole score; two of them still are, and the
        # engine's job is to find out what is true rather than to decide what a
        # truth costs.
        #
        # weight=1.0, stated rather than read off the case.  The catalog gives
        # every guard weight 0.0, because a gate has no pass rate -- it was a
        # tripwire and the score was elsewhere.  Here seventeen are scored, equally:
        # no ordering among them is defensible enough to encode as a number, and
        # inheriting the catalog's 0.0 would make the whole module weightless and its
        # rate 0.0 by division.
        #
        # The other four are recorded at 0.0 on purpose -- see RECORDED_ONLY_GATES.
        # They run, they report, and a failure is visible in the evidence; they just
        # are not priced here, because their subject is which toolchain built the
        # artifact and stage 1 already fails a submission outright for it.
        recorded_only = out.check in RECORDED_ONLY_GATES
        # `skip` only for the reference self-test, and only for a gate whose
        # evidence does not exist.  audit.py records `not_applicable` on every
        # run -- so a graded report says "there is no installed jar" instead of
        # asserting something about behaviour that was never observed -- but the
        # verdict only changes here, and only when auditor.self_test is set.
        #
        # No `required=` on either call.  The scorer does not read the field on a
        # behavioural check, so passing it would write a flag into behavioural.json
        # that nothing honours -- which is how a rule that does not run gets read
        # back as one that does.
        if auditor.self_test and out.not_applicable:
            skipped += 1
            emit.add(out.gate_id, "skip", out.not_applicable[:200],
                     weight=0.0 if recorded_only else 1.0,
                     detail=out.not_applicable,
                     evidence=out.evidence[:20], check=out.check)
            continue
        emit.add(out.gate_id, "pass" if out.passed else "fail",
                 (out.detail[:200] or out.check) +
                 (" [recorded, not scored: stage 1 owns this]"
                  if recorded_only else ""),
                 weight=0.0 if recorded_only else 1.0,
                 detail=out.detail,
                 evidence=out.evidence[:20], check=out.check)
    log.write(f"provenance: {sum(1 for o in outs if o.passed)}/{len(outs)} passed"
              + (f", {skipped} with no jar to inspect" if skipped else ""))
    if skipped:
        emit.notes.append(
            f"{skipped} of {len(outs)} gates read the delivered jar, which this run "
            f"does not have: it is the reference self-test of the corpus, not a "
            f"submission")
        emit.metadata["selftest_gates_not_applicable"] = skipped
    emit.metadata["gates_measured"] = len(selected)
    emit.metadata["gates_recorded_not_scored"] = sorted(RECORDED_ONLY_GATES)
    # The deferrals with their answerers, not just their names.  A reader of this
    # result who wants to know where `no-vendored-zlib` was decided should not have
    # to infer it from two files in different build contexts, and a deferral whose
    # answerer was dropped is visible here rather than only in an absence.
    emit.metadata["gates_deferred_to_stage_1"] = {
        guard: STAGE1_ANSWERS[guard] for guard in sorted(SEMANTIC_GATES)}
    return 0


def _emit_diagnostics(diagnostics: dict, emit: Emitter, log: Log) -> None:
    """Surface what the executor could not do, without scoring it twice.

    Crashes, missing drivers and unrun cases are already reflected in the cases
    themselves -- a case whose probe crashed did not match its expectation, so it
    failed.  These go into the module's metadata instead of becoming checks of
    their own: a second check for the same event would count it twice, and the
    reason a whole family failed is exactly what a reader needs and what a
    per-case detail line does not show.
    """
    for name in ("crashes", "missing_drivers", "not_run", "abandoned"):
        value = diagnostics.get(name)
        if value:
            emit.metadata[name] = value
            log.write(f"diagnostics[{name}]: "
                      f"{len(value) if hasattr(value, '__len__') else value}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _read_toml(path: Path) -> dict:
    """One TOML file, through whichever reader this interpreter has.

    `tomllib` is stdlib from 3.11 and the image is pinned to 3.11.2, but the host
    that runs the authoring checks is on 3.10, and a self-check that only runs
    inside a built image is one that never runs while it is being written.  `tomli`
    is the 3.10 spelling of the same parser.
    """
    try:
        import tomllib as reader  # type: ignore[import-not-found]
    except ModuleNotFoundError:  # pragma: no cover - 3.10 and older
        try:
            import tomli as reader  # type: ignore[no-redef]
        except ModuleNotFoundError as exc:
            raise SystemExit(
                f"no TOML reader available to check {path}: {exc}") from exc
    with path.open("rb") as handle:
        return reader.load(handle)


def check_guard_module_declared(suite_toml: Path) -> None:
    """Fail unless suite.toml declares the module this file routes guard cases to.

    Was `check_blocking_gates_block`, which asserted the other half of a pair: that
    the module owning the two blocking gates was `required = true` one directory up,
    since without the marking a named check cost this module's weight and nothing
    else.  Both halves are retired -- no check zeroes a module, no module zeroes the
    stage, and `config.py` now refuses a suite.toml that sets `required` at all -- so
    what is left is the claim that still means something: this file sends the guard
    cases to a module id, and that module has to exist.  Without it the cases are
    routed nowhere and the module simply reports nothing, which reads as a suite that
    ran and found no problems.

    Separate from `_self_check` so it can be called without a catalog: `_self_check`
    loads /opt/assets first and only exists inside the built image, and a check that
    can only run in an image is one that never runs while it is being written.
    `tests/check-task.py --only figures` calls this, and so does the mutation suite.
    """
    owner = next(m for m, k in KIND_MODULES.items() if k == "guard")
    if not suite_toml.is_file():
        raise SystemExit(f"no suite.toml at {suite_toml}")
    # `module`, singular, is the key `config.Suite.load` reads.  This asked for
    # `modules` first and got an empty list, which is one `return` away from a check
    # that passes because it looked at nothing -- so an empty parse is its own
    # failure, worded as a fault on this side rather than as a missing module.
    modules = _read_toml(suite_toml).get("module") or []
    if not modules:
        raise SystemExit(
            f"{suite_toml} parsed to no modules at all -- this check is reading the "
            f"wrong key, not finding a broken suite")
    marked = [m for m in modules if m.get("id") == owner]
    if not marked:
        raise SystemExit(
            f"suite.toml declares no {owner!r} module, but this file routes the "
            f"guard cases to it")
    # And it has to count.  A weight-0 module runs, reports, and contributes nothing;
    # for the module holding every provenance guard that is the whole anti-cheat axis
    # priced at nothing, which is the failure the retired marking was reaching for.
    if not float(marked[0].get("weight") or 0.0) > 0:
        raise SystemExit(
            f"suite.toml gives the {owner!r} module weight "
            f"{marked[0].get('weight')!r} -- it carries every provenance guard this "
            f"stage runs, and at weight 0 they are measured and priced at nothing")


def _self_check(assets_root: Path) -> int:
    """Everything about this file that can be checked without a submission.

    Run at image build time, on the built image, so a suite whose module map has
    drifted from its catalog fails the build instead of grading eighteen modules
    against a catalog that no longer matches them.  The failures it catches are the
    quiet ones: a family nobody claims still gets generated and never run, and a
    renamed check still produces a green module with fewer cases in it.
    """
    log = Log()
    assets = Assets(assets_root, log)
    _check_total(assets.catalog)

    # The module ids this file answers to, against the catalog they slice.  A
    # module that selects nothing is the failure that arrives an hour late
    # otherwise, as "the module wrote no checks" -- a verifier error reported
    # after the build has already been paid for.
    empty: list[str] = []
    total = 0
    for module in MODULES:
        n = len(assets.slice(families=MODULES[module])["cases"])
        total += n
        if n == 0:
            empty.append(module)
        print(f"  {module:16s} {n:5d} cases")
    for module, kind in sorted(KIND_MODULES.items()):
        n = len(assets.slice(kind=kind)["cases"])
        if n == 0:
            empty.append(module)
        print(f"  {module:16s} {n:5d} cases (kind={kind})")
    if empty:
        raise SystemExit(f"modules that select no cases: {', '.join(empty)}")

    declared = int(assets.catalog["counts"]["behavioural"])
    if total != declared:
        raise SystemExit(
            f"the fifteen case modules select {total} cases; the catalog declares "
            f"{declared} behavioural")

    # Every gate this stage measures must have a handler.  audit.py answers a
    # check named `no-jdk-deflate` with a method named `gate_no_jdk_deflate`, and a
    # missing one is reported per-case at grading time as "no handler implemented"
    # -- a fail against the submission for a gap on this side of the line.
    missing = [name for name in PROVENANCE_GATES
               if not hasattr(audit.IntegrityAuditor,
                              f"gate_{name.replace('-', '_')}")]
    if missing:
        raise SystemExit(
            f"gates with no handler in audit.py: {', '.join(missing)}")
    struct_missing = [
        c["check"] for c in assets.slice(kind="struct")["cases"]
        if not hasattr(structure.StructureEvaluator,
                       f"check_{c['check'].replace('-', '_')}")]
    if struct_missing:
        raise SystemExit(
            f"structural checks with no handler in structure.py: "
            f"{', '.join(sorted(set(struct_missing)))}")

    check_guard_module_declared(HERE.parent / "suite.toml")

    # Both numbers, because "16 deferred" and "14 are stage 1's only" are both true
    # of the same partition and reading either alone as the other is a wrong count
    # of a real thing: 21 + 16 - 2 = 35 guards, the two overlaps being asked twice.
    only_1 = len(set(SEMANTIC_GATES) - set(PROVENANCE_GATES))
    print(f"self-check ok: {total} behavioural cases over {len(MODULES)} modules, "
          f"{len(PROVENANCE_GATES)} gates measured "
          f"({len(PROVENANCE_GATES) - len(RECORDED_ONLY_GATES)} scored + "
          f"{len(RECORDED_ONLY_GATES)} recorded only), "
          f"{len(SEMANTIC_GATES)} deferred to stage 1 ({only_1} of them measured "
          f"only there), answered by {len(set(STAGE1_ANSWERS.values()))} stage 1 "
          f"gate(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", help="the module id to run")
    parser.add_argument("--self-check", action="store_true",
                        help="check the module map against the frozen catalog "
                             "and exit; used at image build time")
    parser.add_argument("--assets", type=Path,
                        default=Path(os.environ.get("SWEREFACTOR_ASSETS",
                                                    "/opt/assets")))
    parser.add_argument("--repo", type=Path,
                        default=Path(os.environ.get("SRB_REPO", "/workspace/repo")))
    # Two work directories, and they are not interchangeable.  --suite-work is
    # shared by every module and is where the build ledger lands; --work is this
    # module's own and is wiped between modules.  Writing the ledger to the latter
    # would make every module after `build` read a file that is not there.
    parser.add_argument("--suite-work", type=Path,
                        default=Path(os.environ.get("SRB_SUITE_WORK", "/tmp/suite")))
    parser.add_argument("--work", type=Path,
                        default=Path(os.environ.get("SRB_WORK", "/tmp/module")))
    parser.add_argument("--result", type=Path,
                        default=Path(os.environ.get("SRB_RESULT", "result.json")))
    args = parser.parse_args(argv)

    if args.self_check:
        return _self_check(args.assets)
    if not args.module:
        parser.error("--module is required (or --self-check)")

    module = args.module
    known = set(MODULES) | set(KIND_MODULES) | {"build"}
    if module not in known:
        parser.error(f"unknown module {module!r}; known: {', '.join(sorted(known))}")

    args.work.mkdir(parents=True, exist_ok=True)
    args.suite_work.mkdir(parents=True, exist_ok=True)
    log = Log(args.work / f"{module}.log")
    emit = Emitter(module)
    try:
        assets = Assets(args.assets, log)
        _check_total(assets.catalog)
        emit.metadata["catalog_digest"] = assets.catalog["digest"]
        if module == "build":
            run_build(assets, args.suite_work, args.repo, emit, log)
        elif module == "structure":
            run_structure(assets, read_build(args.suite_work), emit, args.work, log)
        elif module == "provenance":
            run_provenance(assets, read_build(args.suite_work), emit, args.work, log)
        else:
            run_slice(assets, read_build(args.suite_work), module, emit,
                      args.work, log)
    except SystemExit as exc:
        # This file refusing to grade: a missing ledger, a catalog that does not
        # match the module map, an asset digest mismatch.  Reported as the module's
        # own error carrying the reason, because exiting non-zero with no result
        # file reaches the report as "the module wrote no checks" and loses the one
        # sentence that says why.
        reason = str(exc.code) if exc.code not in (0, None) else "refused to grade"
        log.write(f"ERROR: {reason}")
        emit.notes.append(reason)
        emit.write(args.result, status="error")
        return 1
    except Exception as exc:  # noqa: BLE001
        # A module that dies writes what it has.  Every case graded before the
        # raise keeps its verdict; without this the submission is charged for all
        # of them over one unhandled error in the verifier, and the report cannot
        # tell that apart from a repository that never built.
        import traceback
        log.write(f"module {module} raised: {type(exc).__name__}: {exc}")
        emit.notes.append(f"{type(exc).__name__}: {exc}")
        emit.metadata["traceback"] = traceback.format_exc()[-4000:]
        emit.write(args.result)
        return 1
    finally:
        log.close()
    return emit.write(args.result)


class Emitter:
    """Accumulates checks and writes the JSON the behavioural runner reads.

    The verdict is a string, not a boolean, because four of them are meaningful and
    two of them are not failures.  `skip` leaves the denominator -- it is the
    suite's decision that a check does not apply, and a check that does not apply
    must not cost credit.  `error` stays in it, because "the thing under test was
    never produced" is the usual cause and dropping it would let a submission that
    built nothing score 1.0 over an empty denominator.

    Ids are written unprefixed.  The suite namespaces them with the module id when
    it merges the modules, so a check called `configure/shared` here is
    `build/configure/shared` in the report; writing the prefix by hand would double
    it.  Ids also have to be unique within a module: a duplicate is dropped rather
    than merged, which would quietly shrink the denominator.
    """

    def __init__(self, module: str) -> None:
        self.module = module
        self.checks: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.metadata: dict[str, Any] = {}
        self.started = time.time()
        self._seen: set[str] = set()

    def add(self, check_id: str, verdict: str, summary: str = "", *,
            weight: float = 1.0, required: bool = False, detail: str = "",
            evidence: list | None = None, **extra: Any) -> None:
        if check_id in self._seen:
            # Not silently tolerated.  The suite would drop the duplicate and the
            # module would report fewer cases than it ran, which is the one failure
            # mode that looks like a smaller suite rather than a bug.
            raise SystemExit(f"{self.module}: duplicate check id {check_id!r}")
        self._seen.add(check_id)
        entry: dict[str, Any] = {
            "id": check_id,
            "verdict": verdict,
            "summary": summary,
            "weight": float(weight),
        }
        if required:
            entry["required"] = True
        if detail:
            entry["detail"] = detail
        if evidence:
            # A top-level field on Check, not metadata: the report reads grounded
            # evidence from here and confirms the paths it names really exist.
            # Passed through **extra it would land in metadata and never be read.
            entry["evidence"] = [e if isinstance(e, dict) else {"note": str(e)}
                                 for e in evidence]
        if extra:
            entry["metadata"] = extra
        self.checks.append(entry)

    def write(self, path: Path, *, status: str = "ok") -> int:
        payload = {
            "schema": "swerefactor.stage-result/1",
            "stage": "behavioural",
            "unit": self.module,
            "status": status,
            "duration_sec": round(time.time() - self.started, 3),
            "checks": self.checks,
            "notes": self.notes,
            "metadata": self.metadata,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        failed = sum(1 for c in self.checks
                     if c["verdict"] not in ("pass", "skip"))
        print(f"[{self.module}] {len(self.checks) - failed}/{len(self.checks)} "
              f"passed, wrote {path}")
        return 1 if failed else 0


# Last in the file, deliberately.  `main` runs at import time under this guard and
# calls into every class and helper above it, so the guard has to be the final
# statement -- placed before `Emitter`, as it was, every module died with
# `NameError: name 'Emitter' is not defined` before it could run a case, while
# `--self-check` returned earlier and never touched the name.  That is why the
# image build stayed green while the stage could not run at all.
if __name__ == "__main__":
    raise SystemExit(main())
