#!/usr/bin/env python3
"""The engine behind every module in this suite.

    driver.py --module build              # the six builds, once
    driver.py --module port-dataview      # a slice of the grading
    driver.py --self-check                # build-time validation, no trees needed

Every module here asks the same question of a different slice: given the six trees
`build` produced, do the keys named by this module's slice, on the targets named
by this module's slice, agree with what State A's x86-64 build printed?  The
slices are the SLICES table below and nothing else decides them.

Why one file rather than twelve
-------------------------------
The alternative is twelve scripts that each rebuild what they need, and the
failure mode of that shape is measured rather than hypothetical: a module that can
rebuild can rebuild *differently*, and then two rows in the same report disagree
about what the submission does.  `build` runs the whole matrix once, writes a
ledger, and every later module restores by name and fails loudly if its named
configuration is absent.

What this file deliberately cannot do
------------------------------------
Read the submission's source.  There is no helper here that opens a file in the
repository and looks for a string, and adding one would be a change of kind: the
question "is the byte order derived rather than asserted" belongs to stage 1,
which is a reader with both trees open, and the question "does the interpreter
compute the right answer" belongs here, which is an emulator and a diff.  A row
in this suite that asserted something about the text of a header would be
measuring the shape of a fix instead of its effect.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import targets as T  # noqa: E402

# --------------------------------------------------------------------------- #
# The slices
# --------------------------------------------------------------------------- #

#: The program whose every answer the specification fixes *and* whose answers
#: State A gets wrong on a big-endian target: DataView with an explicit
#: littleEndian argument.
PORT_PROGRAM = "dataview-explicit"

#: The programs whose admitted answers a correct port must not change.  Named
#: rather than derived as "everything else", because a program added later should
#: have to be classified by whoever adds it.
#:
#: `native-order` is in this list, which reads oddly and is right: the screen keeps
#: its `nat-` keys out of every graded row, so what a sheet module grades in that
#: program is only its four *bare* keys -- an 8-bit element view, a spec-fixed
#: byteOffset, an Atomics round trip and a sequence of Atomics operations -- none of
#: which depend on byte order, and each of which is the control for a `nat-` key
#: beside it.  Its order-dependent half is graded by the `native-order` module,
#: which does not compare targets at all.
SEMANTIC_PROGRAMS = ("typed-arrays", "numbers", "strings", "language", "dates",
                     "native-order")

#: Graded on all three targets by its own module, so it is excluded from the
#: preserve rows to keep the partition below exact.
BIGNUM_PROGRAM = "bignum-limbs"

#: What the preserve rows grade: everything the port rows grade, on the two
#: targets State A was already correct on.
PRESERVE_PROGRAMS = (PORT_PROGRAM,) + SEMANTIC_PROGRAMS

#: Every program, and the module that owns each (program, target) pair.
#:
#: The partition is exact and `--self-check` proves it: every admitted key is
#: graded on every target by exactly one module.  Overlap would not be wrong
#: arithmetically -- each module is its own weighted pool -- but it would make a
#: weight mean two different things, and "which row is this key in" is a question
#: a failure report has to be able to answer.
ALL_PROGRAMS = PRESERVE_PROGRAMS + (BIGNUM_PROGRAM,)

#: module id -> what it grades.
#:
#:   kind      how the module measures.  "sheet" grades collected answers against
#:             the oracle; the others run a purpose-built probe.
#:   programs  which programs' keys, for kind="sheet".
#:   targets   which targets.  The port rows name s390x alone and the preserve
#:             rows name the two that already worked; see suite.toml for why
#:             mixing them into one mean paid an unported tree 97%.
SLICES = {
    "build": {"kind": "build", "targets": T.TARGET_NAMES},

    # The port target.  The sheet rows stay s390x-only: the same programs on the
    # other two targets are `preserve-x86-64` and `preserve-armhf`, so all three
    # targets are covered and pooling them into one mean would only hide which one
    # moved -- that is what paid an unported tree 97% when this row was first
    # written, and splitting the rows is what fixed it.
    "port-dataview": {"kind": "sheet", "programs": (PORT_PROGRAM,),
                      "targets": (T.PORT_TARGET,)},
    "port-semantics": {"kind": "sheet", "programs": SEMANTIC_PROGRAMS,
                       "targets": (T.PORT_TARGET,)},

    # These two run on all three.  They have no `preserve-` counterpart -- no other
    # row launches a blob-carrying artifact or pulls a bignum through qjsc -- so
    # while they were s390x-only, the blob path on the two targets that already
    # worked was tested by nothing in this stage, and a submission that swapped the
    # writer unconditionally rather than behind a byte-order test broke both of them
    # and lost no weight anywhere.  Per-cell weight, not target choice, is what
    # keeps the port itself out of the scored pool now: see `RECORDED_ONLY`.
    "port-blob-transport": {"kind": "blob", "targets": T.TARGET_NAMES},
    "port-bignum-serialiser": {"kind": "bignum-ser", "targets": T.TARGET_NAMES},

    # Preservation.  The two targets State A was already right on.
    "preserve-x86-64": {"kind": "sheet", "programs": PRESERVE_PROGRAMS,
                        "targets": (T.ORACLE_TARGET,)},
    "preserve-armhf": {"kind": "sheet", "programs": PRESERVE_PROGRAMS,
                       "targets": (T.CONTROL_TARGET,)},

    # Everything else.
    "bignum-control": {"kind": "sheet", "programs": (BIGNUM_PROGRAM,),
                       "targets": T.TARGET_NAMES},
    "native-order": {"kind": "native", "targets": T.TARGET_NAMES},
    "upstream-suites": {"kind": "suites", "targets": T.TARGET_NAMES},
    "no-build-side-effects": {"kind": "side-effects", "targets": (T.ORACLE_TARGET,)},
    "screen-audit": {"kind": "apparatus", "targets": ()},
}

#: Modules whose checks decide the module on their own.  Declared here as well as
#: in suite.toml because the two say different things: suite.toml's `required`
#: gates the *stage* on the module's rate, and this gates the *module* on the
#: build it needs.  A module whose build is missing must not report a pass rate
#: over the handful of checks that noticed the absence.
BUILD_IS_REQUIRED = frozenset(SLICES) - {"screen-audit"}

#: The bignum constants, and why these ones.  See `run_bignum_ser`.
#:
#: (key, JS expression, measured to reach the raw limb loop)
#:
#: A constant discriminates only if some limb *above* the lowest nonzero limb of
#: libbf's normalised mantissa is not byte-palindromic, because the lowest one is
#: written by a shift-based loop that is byte-order clean and an all-0xff or
#: all-0xaa limb byte-swaps to itself.  That property is not readable off the
#: number: it depends on libbf's normalisation and on how the value was computed
#: -- `10n**50n` (117 significant bits) does not reach it and `fact(25)` (62) does.
#: So the set below was measured, not derived, and the `discriminates` column
#: records what was observed on a tree with only that defect.  Non-discriminating
#: constants are kept: they cost nothing, and a future change that makes one of
#: them start failing is information.
BIGNUM_CONSTANTS = (
    # Discriminating: a literal with a limb on the raw path that is not its own
    # byte reversal.  `_can_show_bignum_defect` decides this, and `--self-check`
    # holds every row here to its own third column.
    ("asym9",     "0x010203040506070809n",                      True),
    ("asym12",    "0x0102030405060708090a0b0cn",                True),
    ("asym16",    "0x0102030405060708090a0b0c0d0e0f10n",        True),
    ("asym17",    "0x0102030405060708090a0b0c0d0e0f1011n",      True),
    ("asym24",    "0x0102030405060708090a0b0c0d0e0f101112131415161718n", True),
    ("asymlo16",  "0x0102030405060708090a0b0c0d0e0f11n",        True),
    ("p64p1",     "0x10000000000000001n",                       True),
    ("p128p1",    "0x100000000000000000000000000000001n",       True),
    ("p192p1",    "0x1000000000000000000000000000000000000000000000001n", True),
    ("dec30",     "123456789012345678901234567890n",            True),
    ("dec40",     "1234567890123456789012345678901234567890n",  True),
    ("fact25",    "15511210043330985984000000n",                True),
    ("fact40",    "815915283247897734345611269596115894272000000000n", True),
    ("sum_asym",  "0x010203040506070809n + 0x0102030405060708090a0b0cn",   True),
    ("neg_asym16", "-(0x0102030405060708090a0b0c0d0e0f10n)",     True),

    # Controls at weight zero, in two kinds.  Wide literals whose every raw-path
    # limb is byte-palindromic, so the swap is invisible even though the value
    # does go through the serialiser:
    ("aabb",      "0xaaaaaaaaaaaaaaaabbbbbbbbbbbbbbbbn",        False),
    ("ones128",   "0xffffffffffffffffffffffffffffffffn",        False),
    # and the other direction: values built at run time from small literals, which
    # never reach the serialiser at all however wide the result.  Three of them are
    # the same value as a discriminating row above, so the pair states the mechanism
    # -- the literal fails on a State C and the computed form passes, and the row
    # that separates the two is the one about serialisation, not about arithmetic.
    ("expr-p64p1", "2n ** 64n + 1n",                            False),
    ("expr-fact25", "(() => { let a = 1n; for (let i = 2n; i <= 25n; i++) "
                    "a *= i; return a; })()",                   False),
    ("expr-fact40", "(() => { let a = 1n; for (let i = 2n; i <= 40n; i++) "
                    "a *= i; return a; })()",                   False),
    ("p64",       "2n ** 64n",                                  False),
    ("p200",      "2n ** 200n",                                 False),
    ("dec50",     "10n ** 50n",                                 False),
)

#: BigInt literals in a JavaScript expression: hex or decimal digits then `n`.
_BIGINT_LITERAL = re.compile(r"(?:0[xX][0-9a-fA-F_]+|[0-9][0-9_]*)n")


def _bigint_literals(expr):
    """Every BigInt literal in ``expr``, as ints."""
    out = []
    for tok in _BIGINT_LITERAL.findall(expr):
        body = tok[:-1].replace("_", "")
        out.append(int(body, 16) if body[:2].lower() == "0x" else int(body))
    return out


def _bignum_limbs(value, limb_bits):
    """libbf's mantissa limbs for ``value``, tab[0] lowest.

    The mantissa is left-aligned: tab[-1] carries the value's most significant
    bit, which is what makes the limb contents depend on the value's width and not
    just on its digits.
    """
    value = abs(value)
    if value == 0:
        return []
    nbits = value.bit_length()
    count = (nbits + limb_bits - 1) // limb_bits
    mant = value << (count * limb_bits - nbits)
    mask = (1 << limb_bits) - 1
    return [(mant >> (limb_bits * i)) & mask for i in range(count)]


def _literal_corrupts(value, limb_bits):
    """Would a byte-order mismatch change ``value`` when it is serialised?

    The writer skips leading zero limbs, writes the lowest nonzero one with a
    shift loop that is byte-order-clean after stripping its trailing zero bytes,
    and writes every limb above it with a raw fixed-width put in *native* order.
    The reader decides whether there was a partial limb from the byte count modulo
    the limb width -- so when no byte was stripped, the lowest limb goes through
    the raw path too.  A limb on the raw path survives a mismatch exactly when it
    equals its own byte reversal.
    """
    tab = _bignum_limbs(value, limb_bits)
    if not tab:
        return False
    low = 0
    while low < len(tab) and tab[low] == 0:
        low += 1
    if low >= len(tab):
        return False
    width = limb_bits // 8
    v, n1 = tab[low], width
    while (v & 0xff) == 0:
        n1 -= 1
        v >>= 8
    nbytes = (len(tab) - low - 1) * width + n1
    raw = list(range(low, len(tab)))
    if nbytes % width:
        raw = raw[1:]
    for j in raw:
        if tab[j] != int.from_bytes(tab[j].to_bytes(width, "little"), "big"):
            return True
    return False


def _can_show_bignum_defect(expr, limb_bits=64):
    """True when some BigInt literal in ``expr`` is corrupted by a raw-path swap.

    Two conditions, and both are properties of the code rather than of any
    submission.  First, only a *literal* reaches `JS_WriteBigNum`: the parser
    builds the bignum while compiling and the constant lands in the bytecode,
    whereas `2n ** 64n + 1n` is arithmetic on three single-limb literals that the
    interpreter performs after the blob has been read, so the wide result is never
    serialised at all.  Second, the literal has to have a limb on the raw path
    that is not byte-palindromic.

    This is why the table above cannot be written by eye.  `10n ** 50n` is wider
    than `0x010203040506070809n` and cannot show the defect; `0xaaaa...bbbb...n`
    is a literal that does reach the serialiser and still cannot show it, because
    both its limbs are their own reversal.  Checked against a correct and a
    known-broken big-endian build: 15 rows separate them, 7 do not, and this
    predicate agrees with the measurement on all 22.
    """
    return any(_literal_corrupts(x, limb_bits) for x in _bigint_literals(expr))


# --------------------------------------------------------------------------- #
# Emitting
# --------------------------------------------------------------------------- #

#: Keys a module declares as controls, derived from the tables above rather than
#: restated, keyed by the module that owns them.  `Emitter.add` clamps these to
#: weight 0 whatever a call site passes.
#:
#: This exists because of a defect that measurement found and review did not.
#: `run_bignum_ser`'s main loop reads the third column of BIGNUM_CONSTANTS and
#: weights each key by it; its "the round trip did not run" path walked the same
#: table with the column bound to `_d` and emitted every key at the default
#: weight 1.0.  On an unported tree, where the s390x build cannot read a blob its
#: own host wrote, that put all eight controls into the scored pool.  It cost that
#: tree nothing -- everything in the row failed anyway -- but a control whose
#: weight depends on whether the harness got far enough to run it is not a
#: control, and the next submission to fail the harness for an unrelated reason
#: would have been graded on eight keys that cannot be failed for a byte-order
#: reason.
#:
#: The fourth defect of this shape: a table carries a classification column and
#: one reader of the table ignores it.  `--self-check` already holds the column
#: itself to the mechanism, which says nothing about whether a reader honours it.
#: So the invariant moves off the call sites, which have to remember, and into the
#: one place every check passes through, which cannot forget.
DECLARED_CONTROLS = {
    "port-bignum-serialiser": frozenset(
        key for key, _expr, discriminating in BIGNUM_CONSTANTS
        if not discriminating),
}


class Emitter:
    """Accumulates checks and writes the JSON the behavioural runner reads.

    Two conventions, both load-bearing.

    ``weight=0`` means *not scored*: a control, a diagnostic, or an observation
    about the original tree.  It means not reported either -- swerefactor deletes a
    weight-0 row when it collects the module's ``behavioural.json``
    (``behavioural.py``, which counts them into ``unscored_observations``), because
    checks inside a module divide its weight equally and a row declared zero would
    otherwise take a full share of it.  So the module still runs the control and
    still logs it; the row is simply absent downstream.  ``skip`` is not the
    alternative: a skip stays in the denominator and scores 0, so recording a
    control as one would charge every submission for it.

    A key its module declared a control in ``DECLARED_CONTROLS`` gets ``weight=0``
    here regardless of what the call site asked for, and the clamp is reported.
    Weight is the one field where a call site's mistake is invisible in the output
    -- a wrong summary reads wrong, a wrong weight just quietly changes a
    denominator -- so it is not left to a call site to get right.

    ``required=True`` means the check decides its module: if it fails, the
    module's rate is zero however many others passed.  Used for exactly one thing
    here -- the build this module needs exists -- because a module that grades a
    tree that was never built should report nothing, not a rate over the checks
    that observed the absence.
    """

    def __init__(self, module):
        self.module = module
        self.checks = []
        self.notes = []
        self.metadata = {}
        self.clamped = []
        self.started = time.time()

    def add(self, check_id, verdict, summary="", weight=1.0, detail="",
            required=False, **extra):
        weight = float(weight)
        controls = DECLARED_CONTROLS.get(self.module, ())
        if check_id.rsplit("/", 1)[-1] in controls:
            #: Declared a control by the table it came from, so its verdict is
            #: reported and does not move the score -- on every path, including
            #: the ones that fail before reaching the code that knows this.
            if weight:
                self.clamped.append(check_id)
            weight = 0.0
            extra.setdefault("control", True)
        entry = {"id": check_id, "unit": self.module, "verdict": verdict,
                 "summary": summary[:400], "weight": weight}
        if required:
            entry["required"] = True
        if detail:
            entry["detail"] = detail[:4000]
        if extra:
            entry["metadata"] = extra
        self.checks.append(entry)
        return entry

    def note(self, text):
        self.notes.append(text)

    def write(self, path):
        payload = {
            "schema": "swerefactor.stage-result/1",
            "stage": "behavioural",
            "unit": self.module,
            "duration_sec": round(time.time() - self.started, 3),
            "checks": self.checks,
            "notes": self.notes,
            "metadata": self.metadata,
        }
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
        scored = [c for c in self.checks if c["weight"] > 0]
        bad = sum(1 for c in scored if c["verdict"] not in ("pass", "skip"))
        print("[%s] %d/%d scored checks passed (%d reported unscored), wrote %s"
              % (self.module, len(scored) - bad, len(scored),
                 len(self.checks) - len(scored), path))
        if self.clamped:
            #: A clamp that fires means some path emitted a declared control at a
            #: scoring weight.  The score is right either way, so this is not a
            #: failure -- but it is a call site that disagrees with its own table,
            #: and a silent correction would let it stay that way.
            print("[%s] %d declared control(s) reached the emitter at a scoring "
                  "weight and were clamped to 0: %s"
                  % (self.module, len(self.clamped),
                     ", ".join(sorted(set(self.clamped))[:8])))
        return 1 if bad else 0


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #

#: Build products a submission may have left in its tree.  Discarded from the
#: snapshot before anything is built, so every measurement is of something this
#: run compiled.  A shipped `.o` could have been produced by any compiler on any
#: host, which is the one thing a byte-order port must not be measured through.
#: Reported in metadata rather than scored -- shipping build output is untidy, not
#: a byte-order defect, and stage 1 is the reader that judges intent.
DISCARD_SUFFIXES = (".o", ".a", ".so", ".obj", ".lib", ".d", ".gcda", ".gcno")
DISCARD_DIRS = (".obj", "build", "_build", "out", ".git", "__pycache__",
                ".cache", "node_modules")


def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# The frozen expectations
# --------------------------------------------------------------------------- #
#
# Everything State A has to say was decided when this image was built, by
# lib/freeze.py, from an archive whose digest was checked twice.  A grading run
# reads it and never recomputes it.
#
# The reason is not tidiness.  Building State A next to a submission means running
# the submission's `make` as root in the same container while the answers are being
# decided, and a `Makefile` is a program: it can write to another directory, put a
# compiler earlier on PATH, or edit an answer sheet collected a minute before.  None
# of that takes ingenuity, only a rule with a recipe.  Freezing removes the class.
# It also halves the stage: three builds instead of six.

ASSETS = os.environ.get("SWEREFACTOR_ASSETS", "/opt/assets")
ASSET_SCHEMA = "swerefactor-pf03-frozen-v2"

_FROZEN = {}


class MissingAssets(Exception):
    """The frozen expectations are absent or are not the ones this file grades with.

    Never a submission's fault, and never recovered from by recomputing: an
    expectation this driver derives for itself is one nothing else in the report
    shares.
    """


def frozen(name):
    """Read one file out of the frozen blob, with its manifest digest checked.

    ``name`` is "screen", "labels", "oracle", "manifest", or "answers/<target>".
    """
    if name in _FROZEN:
        return _FROZEN[name]
    manifest = _FROZEN.get("manifest")
    if manifest is None:
        path = os.path.join(ASSETS, "manifest.json")
        if not os.path.isfile(path):
            raise MissingAssets(
                "no frozen expectations at %s. This image was built without "
                "lib/freeze.py, or SWEREFACTOR_ASSETS points somewhere else." % path)
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        if manifest.get("schema") != ASSET_SCHEMA:
            raise MissingAssets("frozen assets declare schema %r; this driver "
                                "grades against %r"
                                % (manifest.get("schema"), ASSET_SCHEMA))
        _FROZEN["manifest"] = manifest
        if name == "manifest":
            return manifest
    rel = ("answers/original-%s.json" % name.split("/", 1)[1]
           if name.startswith("answers/") else "%s.json" % name)
    path = os.path.join(ASSETS, rel)
    if not os.path.isfile(path):
        raise MissingAssets("the frozen assets have no %s" % rel)
    want = (manifest.get("files") or {}).get(rel)
    got = _digest(path)
    if want and got != want:
        raise MissingAssets(
            "%s has digest %s and the manifest records %s. Something wrote to the "
            "frozen assets after the image was built." % (rel, got[:16], want[:16]))
    with open(path, encoding="utf-8") as fh:
        _FROZEN[name] = json.load(fh)
    return _FROZEN[name]


def snapshot_submission(src, dest, emit):
    """Copy the delivered tree, dropping anything that was already compiled."""
    if os.path.exists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    dropped = []

    def ignore(directory, names):
        skip = set()
        for name in names:
            full = os.path.join(directory, name)
            rel = os.path.relpath(full, src)
            if os.path.isdir(full) and name in DISCARD_DIRS:
                skip.add(name)
                dropped.append(rel + "/")
            elif name.endswith(DISCARD_SUFFIXES):
                skip.add(name)
                dropped.append(rel)
        return skip

    shutil.copytree(src, dest, symlinks=True, ignore=ignore)
    if dropped:
        emit.metadata["discarded_build_products"] = sorted(dropped)[:200]
        emit.note("%d pre-built file(s) were dropped from the submission "
                  "snapshot; everything measured here was compiled in this run"
                  % len(dropped))
    return dest


def inventory(root):
    """Every regular file under root, relative, with size and digest."""
    out = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in DISCARD_DIRS]
        for name in files:
            full = os.path.join(base, name)
            if os.path.islink(full):
                continue
            rel = os.path.relpath(full, root)
            try:
                out[rel] = (os.path.getsize(full), _digest(full))
            except OSError:
                continue
    return out


# --------------------------------------------------------------------------- #
# The build module
# --------------------------------------------------------------------------- #

class MissingBuild(Exception):
    """A module's named configuration is not in the ledger.

    Raised rather than rebuilt.  Two builds of one submission can differ -- a
    generated file left behind, a rule that only fires the first time -- and then
    two rows of one report disagree about what the submission does, with nothing
    in the report saying which build each row saw.
    """


def load_builds(emit, needed):
    """Restore the named configurations the build module published.

    ``needed`` is (role, target) pairs.  A missing one is a required failure with
    the ledger's own account of what went wrong, so the report says "s390x never
    built, here is its make log" instead of "47 checks failed".
    """
    ledger = T.read_shared_json("builds")
    if ledger is None:
        raise MissingBuild(
            "the build module published no ledger at %s; it runs first and every "
            "module after it reads what it produced"
            % os.path.join(T.SHARED, "builds.json"))
    builds = T.restore(ledger)
    emit.metadata["ledger_builds"] = sorted(builds)
    missing = []
    for role, target in needed:
        name = "%s-%s" % (role, target)
        build = builds.get(name)
        if build is None:
            missing.append("%s: not in the ledger" % name)
        elif not build.built:
            missing.append(build.failure_summary() or "%s: not built" % name)
    if missing:
        raise MissingBuild("; ".join(missing))
    return builds


def run_build(emit, module="build"):
    """Three builds of the submission, three answer sheets, and the ledger.

    ``module`` is unused and present because `main` dispatches every kind the same
    way, ``handler(emit, module)``.  A handler with its own arity is a module that
    raises `TypeError` the first time it is dispatched -- which is how this
    parameter came to be here, and why `_self_check` now checks the signatures.

    Scored, and every build is `required`: a tree that does not compile for all three
    targets has nothing behavioural left to measure, and the stage should say so with
    the make log attached rather than report a rate over the checks that noticed the
    absence.

    State A is not built here.  Its answers, its screen and its labels were frozen
    into this image before any submission existed; this module reads the manifest,
    confirms the blob is the one it grades against, and publishes what it found for
    the rows that follow.
    """
    os.makedirs(T.SHARED, exist_ok=True)
    emit.metadata["shared_dir"] = T.SHARED

    manifest = frozen("manifest")
    emit.metadata["frozen"] = {
        "schema": manifest.get("schema"),
        "frozen_at_utc": manifest.get("frozen_at_utc"),
        "archive_sha256": (manifest.get("provenance") or {}).get("archive_sha256"),
        "counts": manifest.get("counts"),
    }
    emit.add("expectations/frozen", "pass",
             "%d admitted keys, frozen %s from an archive with digest %s"
             % ((manifest.get("counts") or {}).get("admitted", 0),
                manifest.get("frozen_at_utc"),
                ((manifest.get("provenance") or {}).get("archive_sha256")
                 or "?")[:16]),
             weight=0.0)

    #: Every file the blob names is read once here, so a tampered asset fails in the
    #: first module with the file named, not in the middle of a grading row.
    for name in (["screen", "labels", "oracle"]
                 + ["answers/%s" % t for t in T.TARGET_NAMES]):
        frozen(name)
    emit.add("expectations/intact", "pass",
             "every frozen file matches the digest in the manifest", weight=0.0)

    if not os.path.isdir(T.REPO):
        emit.add("submission-present", "fail",
                 "no submission tree at %s" % T.REPO, required=True)
        return 1
    snapshot_submission(T.REPO, os.path.join(T.SNAPSHOTS, "submission"), emit)

    builds = {}
    for target in T.TARGET_NAMES:
        build = T.Build("submission", target)
        build.execute()
        builds[build.name] = build
        if build.built:
            emit.add("build/%s" % build.name, "pass",
                     "make produced %d goal(s)" % len(build.goals),
                     required=True,
                     seconds=round(build.steps["make"].seconds, 1))
        else:
            emit.add("build/%s" % build.name, "fail", build.failure_summary(),
                     required=True,
                     detail=(build.step("make")
                             or build.step("prepare")).tail(60))
            continue
        T.write_answers(T.collect_answers(build))

    ledger = T.persist(builds, os.path.join(T.SHARED, "logs"))
    T.write_shared_json("builds", ledger)

    #: The submission's own labels, for the diagnostic half of `native-order`.
    sheets = {t: T.read_answers("submission", t) for t in T.TARGET_NAMES}
    T.write_labels({"submission": T.classify(
        {t: s for t, s in sheets.items() if s is not None})})

    # Each target really is the architecture it claims to be.  A Makefile edited
    # to drop CROSS_PREFIX builds cleanly and produces three x86-64 trees, every
    # one of which passes every behavioural probe on this machine.
    for target in T.TARGET_NAMES:
        build = builds.get("submission-%s" % target)
        if build is None or not build.built:
            continue
        kind = build.artifact_kind("qjs") or ""
        want = T.TARGETS[target].elf_machine or ""
        emit.add("artifact-is-%s" % target,
                 "pass" if want and want in kind else "fail",
                 "qjs reports %r; expected an ELF for %s" % (kind[:120], want),
                 required=True)
    return 0


# --------------------------------------------------------------------------- #
# Grading collected answers
# --------------------------------------------------------------------------- #

def read_screen_or_fail(emit):
    """The frozen screen, or a named failure.

    No module reconstructs it.  A module that can rebuild the screen can rebuild a
    *different* screen, and then two rows of one report disagree about what the
    expectation was with nothing in the report saying so.
    """
    scr = frozen("screen")
    emit.metadata["screen_source"] = os.path.join(ASSETS, "screen.json")
    return scr


# --------------------------------------------------------------------------- #
# What State A itself carried
# --------------------------------------------------------------------------- #

#: Every scored row in this suite compares the submission against original/x86-64,
#: and on the port target that comparison *is* the port requirement.  Which means
#: such a cell asks the submission for a capability State A does not have -- by
#: premise, because removing the little-endian assumption is the task.  Charging for
#: it makes the reference tree unable to pass the stage that was built out of its own
#: answers, and a stage the reference fails is measuring the premise instead of the
#: submission.
#:
#: So the rule is the campaign's: a cell whose subject is a capability State A lacks
#: on that target is reported with a real verdict at weight 0.0 and owned by a named
#: stage-1 gate; a cell State A itself carried stays scored.  What survives here is
#: preservation and collateral damage -- everything the port was supposed to leave
#: alone, on all three targets -- which is what a behavioural stage can actually
#: measure by comparison.  Whether the port happened is read by stage 1, which has
#: six required gates for it, and attacked by stage 3.
#:
#: The distinction is never guessed and never hand-maintained.  It is read out of the
#: frozen blob, sealed at image build time before any submission existed:
#:
#:   sheet rows    original/<target>'s own answer to the key, against the admitted
#:                 expectation;
#:   blob rows     whether original/<target> ran the artifact and projected the
#:                 x86-64 value            (oracle["blob_baseline"]);
#:   bignum rows   whether original/<target> completed the round trip and read the
#:                 constant back unchanged (oracle["bignum_baseline"]);
#:   native rows   whether original/<target> answered the key the answer its own
#:                 declared layout implies.
#:
#: "State A rates 1.0 on every scored check" is therefore true by construction, and a
#: cell returns to the scored pool the moment State A can carry it -- which is what
#: keeps this from being an exemption list that rots.
RECORDED_ONLY = ("reported, not charged: original/%s does not carry this either, so "
                 "the cell asks for the port itself rather than for something the "
                 "port had to preserve. Owned by stage 1's %s gate.")

#: The same idea for the two rows that *launch* something instead of reading a sheet,
#: where "does State A carry this" has a second, better answer available at grading
#: time: does the submission itself carry it?
#:
#: A sheet cell is a value State A either printed or did not.  A blob artifact and a
#: bignum round trip are operations, and an operation has a third outcome: it can be
#: performed and give the wrong answer.  State A on s390x never gets there -- the
#: artifacts die with exit 1 or SIGSEGV and the round trip returns nothing, because a
#: big-endian reader rejects a little-endian blob before any value exists -- so those
#: cells cannot be charged to a submission that is equally unported.  But a
#: submission that *did* get the operation running has claimed the capability, and
#: then the value it produced is fair to hold it to.
#:
#: That distinction is the whole reason this row can still see a port that fixed the
#: bytecode container and left the mantissa loop alone: such a build completes the
#: round trip and reads back byte-reversed limbs, which is a wrong answer to a
#: question it just demonstrated it can ask.  Gating on "State A carried it" alone
#: would have made that defect invisible here and left it to stage 1 -- which is a
#: real loss, because this row is the only place in the stage that reaches those four
#: limb blocks at all.
CONDITIONAL_ONLY = ("reported, not charged: original/%s does not carry this and this "
                    "build does not complete the %s either, so no value exists to "
                    "compare. Charged as soon as either one completes it -- a "
                    "submission that gets this far is held to the numbers it reads. "
                    "Owned by stage 1's %s gate.")

#: Which stage-1 gate answers for each recorded-only family.
OWNED_BY = {
    "sheet": "byte_order_handling_is_complete",
    "blob": "bootstrap_handles_endianness",
    "bignum": "byte_order_handling_is_complete",
    "native": "byte_order_handling_is_complete",
}

_ORIGINAL_FLAT = {}


def original_answers(target):
    """original/<target>'s sealed answers, flattened to {(program, key): value}."""
    if target not in _ORIGINAL_FLAT:
        sheet = frozen("answers/%s" % target) or {}
        flat = {}
        for stem, prog in (sheet.get("programs") or {}).items():
            for key, value in (prog.get("answers") or {}).items():
                flat[(stem, key)] = value
        _ORIGINAL_FLAT[target] = flat
    return _ORIGINAL_FLAT[target]


def baseline(kind, target):
    """What original/<target> carried for a non-sheet row, out of the frozen blob.

    Returns {} when the blob predates the baseline -- an older image grades every
    cell, which is the previous behaviour and fails loudly rather than quietly
    handing out credit.
    """
    table = frozen("oracle").get("%s_baseline" % kind) or {}
    return table.get(target) or {}


def run_sheet(emit, module):
    """One check per (program, key, target) in this module's slice.

    The expectation is always original/x86-64's answer, whatever target is being
    graded.  On x86-64 and armhf that makes the row a preservation requirement; on
    s390x it makes it the port requirement -- and where it is the port requirement
    and State A's own s390x build answers the key wrongly, the cell is reported at
    weight 0.0 rather than scored, for the reason written above `RECORDED_ONLY`.

    Measured: that is 41 of the 47 `dataview-explicit` keys on s390x, each an exact
    byte reversal of the x86-64 answer, and nothing else in this suite -- the other
    six dataview keys and all 348 keys of the six semantic programs are answered
    correctly by State A on s390x, so they stay scored and a submission that damages
    them on the way through still pays for it.
    """
    slice_ = SLICES[module]
    programs, targets = slice_["programs"], slice_["targets"]
    scr = read_screen_or_fail(emit)
    admitted = {p: keys for p, keys in scr["admitted"].items() if p in programs}
    emit.metadata["programs"] = list(programs)
    emit.metadata["targets"] = list(targets)
    emit.metadata["admitted_keys"] = sum(len(v) for v in admitted.values())

    unknown = [p for p in programs if p not in scr["admitted"]]
    if unknown:
        # A program this module claims produced no admitted key at all. Left
        # unscored: it means the screen or the program changed, not that the
        # submission did something.
        emit.add("slice-has-keys", "error",
                 "no admitted keys for %s" % ", ".join(unknown), weight=0.0)

    load_builds(emit, [("submission", t) for t in targets])
    graded = recorded = 0
    for target in targets:
        sheet = T.read_answers("submission", target)
        if sheet is None:
            emit.add("answers/%s" % target, "fail",
                     "the build module recorded no answer sheet for "
                     "submission-%s" % target, required=True)
            continue
        origin = original_answers(target)
        report = T.grade_sheet(admitted, sheet)
        for program in sorted(admitted):
            row = report.get(program) or {}
            for key in sorted(admitted[program]):
                cid = "%s/%s/%s" % (target, program, key)
                expected = admitted[program][key]
                #: Did State A's own build for this target answer this key the way
                #: the expectation requires?  Where it did not, the cell is the port
                #: itself and is reported rather than charged.
                carried = origin.get((program, key)) == expected
                weight = 1.0 if carried else 0.0
                extra = "" if carried else \
                    " -- " + RECORDED_ONLY % (target, OWNED_BY["sheet"])
                if key in (row.get("differ") or {}):
                    actual = row["differ"][key]["actual"]
                    emit.add(cid, "fail",
                             "expected %r, got %r%s"
                             % (expected[:80], actual[:80], extra), weight=weight,
                             detail="" if carried else
                             "original/%s prints %r here"
                             % (target, (origin.get((program, key)) or
                                         "<absent>")[:200]))
                elif key in (row.get("missing") or []):
                    emit.add(cid, "fail",
                             "the program did not print this key (exit %s)%s"
                             % (row.get("rc"), extra), weight=weight,
                             detail=(row.get("output_tail") or "")[:1200])
                else:
                    emit.add(cid, "pass", "%s%s" % (expected[:80], extra),
                             weight=weight)
                if carried:
                    graded += 1
                else:
                    recorded += 1
            if row.get("duplicate_keys"):
                emit.add("%s/%s/no-duplicate-keys" % (target, program), "fail",
                         "printed twice: %s" % row["duplicate_keys"], weight=0.0)
    emit.metadata["checks_graded"] = graded
    emit.metadata["checks_recorded_only"] = recorded
    return 0


# --------------------------------------------------------------------------- #
# The blob transport
# --------------------------------------------------------------------------- #

#: Every artifact in this tree that carries bytecode written by one binary and
#: read by another, with how to run it.
#:
#:   (check id, kind, argv tail, stdin)
#:
#: `kind` says which build member to launch: an executable produced by compiling a
#: generated .c file, or the interpreter itself, which links the compiled repl and
#: qjscalc blobs into its own image.
#: The artifacts that carry a blob written by one byte order and read by another.
#:
#: `\q` on the interactive two is load-bearing, and it was learned by running them.
#: Upstream's repl.js installs a level-triggered read handler and reads with
#: `std.in.read`, which returns 0 at EOF rather than signalling it -- so a piped
#: `1+1\n` makes a *correct* build print 2 and then spin on an exhausted descriptor
#: forever.  It was the freeze step that noticed, because State A on x86-64 is the
#: first thing it runs.  An unported s390x build segfaults immediately, so a suite
#: that only ever tried the broken side would have shipped with the timeout in it.
#:
#: `qjs-eval` carries no blob it needs: `-e` evaluates its argument and never loads
#: the repl.  It rides along as this row's control.  Measured on State A/s390x, it
#: prints 42 while the other four fail, which is what makes those four failures
#: attributable to the blob boundary rather than to a build that cannot run.
BLOB_ARTIFACTS = (
    ("examples-hello",  "binary", "examples/hello",   (), None),
    ("examples-testfib", "binary", "examples/test_fib", (), None),
    ("qjs-repl",        "qjs",    None,               (), "1+1\n\\q\n"),
    ("qjs-eval",        "qjs",    None,               ("-e", "print(40+2)"), None),
    ("qjs-qjscalc",     "qjs",    None,               ("--qjscalc",), "2^10\n\\q\n"),
)

#: Terminal rendering, removed before comparing.
_ANSI = (re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"),   # OSC
         re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]"),           # CSI
         re.compile(r"\x1b[@-Z\\-_]"))                       # two-character
_BANNER = re.compile(r"^(?:qjs|QuickJS|QJSCalc)\b")


def blob_projection(text):
    """What an artifact computed, with the line editor's own output dropped.

    The interactive artifacts print a terminal session: colour, cursor movement,
    and a character-by-character echo of the input as it is typed.  Comparing that
    whole would make repl.js's prompt rendering part of the contract, and nothing
    about a byte-order port should have to reproduce an escape sequence.

    So: strip the escapes, then drop the banner and the prompt lines.  What remains
    is what the blob evaluated -- measured, the five artifacts project to
    'Hello World', 'Hello World\\nfib(10)= 55', '2', '42' and '1024'.

    The three failure modes stay visible.  A reader that rejects the blob exits
    nonzero before printing anything; a reader that misreads the body prints an
    error naming the integer it misread ("ReferenceError: '1627389952' is not
    defined", State A/s390x, measured); a reader that crashes returns a signal.  The
    caller checks the exit status first, so none of those reach this function.
    """
    for pat in _ANSI:
        text = pat.sub("", text)
    keep = [ln.strip() for ln in text.split("\n")
            if ln.strip() and not _BANNER.match(ln.strip())]
    return "\n".join(keep)


def _run_blob_artifact(build, spec):
    _cid, kind, rel, tail, stdin = spec
    if kind == "binary":
        path = build.path(rel)
        if not os.path.exists(path):
            return None
        return build.run_native_binary(path, key="blob:" + rel)
    argv = build.target.launch([build.qjs] + list(tail))
    return T.run("blob:qjs" + ("".join(tail) or "-stdin"), argv, build.root,
                 build.target.run_timeout, stdin_text=stdin)


def run_blob(emit, module):
    """Run each blob-carrying artifact and compare what it computed with State A.

    The same rule as every other row: what the artifact computes on the port target
    must be what State A computed on x86-64.  Nothing here parses a bytecode header
    or asserts a version constant -- the outcomes the header produces are *visible
    in the output*, which is why the row can be a comparison instead of a format
    check.  Measured on State A/s390x, where the port is missing:

      correct              the blob loads and the artifact prints its answer;
      writer never swapped  a clean "invalid version" and a nonzero exit;
      reader never switched the version check passes by coincidence and the body is
                           misread.  examples/hello and examples/test_fib exit 1
                           with "ReferenceError: '1627389952' is not defined" -- an
                           integer read out of the blob at the wrong end -- and
                           `qjs` interactive takes SIGSEGV on the repl blob.

    Exit status is checked before the value, so a crash and a wrong answer are
    distinct findings rather than one "differs".

    The comparison is on `blob_projection(output)`, not on the raw bytes: the two
    interactive artifacts print a line-editor session, and requiring a submission to
    reproduce repl.js's escape sequences would be an implementation detail dressed
    up as behaviour.

    This row runs on all three targets, and a cell is charged when the frozen
    baseline says original/<target> carried the artifact *or* when this build ran it
    successfully.  An artifact State A carried is a preservation requirement; one it
    did not is the port itself, and charging an equally unported submission for it
    would mean the reference tree cannot pass a stage built out of its own answers.
    Measured on State A, that splits the fifteen cells 11 scored / 4 recorded -- the
    four on s390x that need a blob written by a little-endian host and read by a
    big-endian reader, which is exactly the boundary the task exists to fix.

    The second half of that rule matters as much as the first.  Those four cells stop
    being free the moment a submission gets the artifact running: exit status is
    checked before the value, so "it launched" and "it printed the right thing" are
    separate findings, and a build that loads the blob and then computes from
    misread bytes fails here rather than being excused.  See CONDITIONAL_ONLY.

    Running x86-64 and armhf here is not decoration, and it is not free credit: the
    host and target compilers being the same binary is what makes those cells a
    *preservation* requirement rather than a trivial one.  A submission that swaps
    the writer unconditionally instead of behind a byte-order test now breaks the
    blob path on the two little-endian targets and pays for it here.  Before this row
    reached them, that submission lost nothing anywhere in the stage.
    """
    targets = SLICES[module]["targets"]
    builds = load_builds(emit, [("submission", t) for t in targets])
    expected = dict(frozen("oracle").get("blob") or {})
    emit.add("expectations/blob", "pass" if expected else "fail",
             "%d frozen artifact outputs from State A on %s"
             % (len(expected), T.ORACLE_TARGET), weight=0.0)

    for target in targets:
        build = builds["submission-%s" % target]
        carried = baseline("blob", target)
        for spec in BLOB_ARTIFACTS:
            cid = "%s/%s" % (target, spec[0])
            if spec[0] not in expected:
                emit.add(cid, "skip", "no expectation for this artifact",
                         weight=0.0)
                continue
            ok_in_a = bool(carried.get(spec[0]))
            step = _run_blob_artifact(build, spec)
            #: Charged when State A carried the cell, and also when this build ran the
            #: artifact at all.  State A's s390x artifacts exit 1 or take SIGSEGV
            #: before printing, so they stay recorded; a submission that got the
            #: artifact running is held to what it printed.  See CONDITIONAL_ONLY.
            ran = step is not None and step.ok
            weight = 1.0 if (ok_in_a or ran) else 0.0
            extra = "" if weight else \
                " -- " + CONDITIONAL_ONLY % (target, "%s run" % spec[2],
                                             OWNED_BY["blob"])
            if step is None:
                emit.add(cid, "fail", "the build did not produce %s%s"
                         % (spec[2], extra), weight=weight)
                continue
            got = blob_projection(step.out)
            if not step.ok:
                emit.add(cid, "fail",
                         "exit %d%s%s" % (step.rc,
                                          " (timed out)" if step.timed_out else "",
                                          extra),
                         weight=weight, detail=step.tail(30))
            elif got == expected[spec[0]]:
                emit.add(cid, "pass", "%r%s" % (got[:80], extra), weight=weight)
            else:
                emit.add(cid, "fail",
                         "computed a different value than original/x86-64" + extra,
                         weight=weight,
                         detail="expected:\n%s\n\ngot:\n%s"
                                % (expected[spec[0]][:900], got[:900]))
    return 0


# --------------------------------------------------------------------------- #
# The bignum serialiser
# --------------------------------------------------------------------------- #

#: The rule that pulls a blob through `qjsc` the way the build itself does.
#:
#: This row needs to write bignums with the host's compiler and read them back with
#: the target's interpreter, and the flags for that are the submission's business,
#: not this driver's.  Asking `make` for the recipe it would run to produce
#: `qjscalc.c` returns whatever the submission decided -- `-x` behind a
#: `TARGET_BIGENDIAN` test, an unconditional flag, a differently-named variable, a
#: generator script -- and the flags in that command are the ones a blob written for
#: this target is supposed to carry.
RECIPE_GOAL = "qjscalc.c"
RECIPE_TOKENS = re.compile(r"(?:^|\s)(-x|-flto|-fbignum|-m|-M|-c|-N\s+\S+|-D\s*\S+)")

#: If the build has no such rule, these are tried in order and the first that
#: reproduces the oracle's values is accepted.  A submission that reaches the right
#: answer through a mechanism this driver never heard of still passes, and metadata
#: records which path was taken.
FALLBACK_FLAGS = (("-x",), (), ("--bswap",), ("-b",))

#: The baseline's key for "original/<target> completed the round trip at all".  A
#: constant cannot be carried by a build that never got a blob back, so this gates
#: the whole target rather than one row of it.
ROUNDTRIP_CELL = "__roundtrip__"


def attempt_bignum_roundtrip(build, ref, target):
    """Pull the bignum constants through this build's own blob path.

    Shared with `lib/freeze.py`, which needs this answer for State A on every target
    in order to seal the baseline the row grades against.  One code path on purpose:
    a baseline measured by a different procedure than the graded run is a baseline
    that can disagree with it, and the disagreement would read as a submission
    defect.

    Returns a dict: `values` (or None if no flag set got a blob back), the last
    `step`, the `recipe_flags` make printed, the `flags` actually used, `via`
    ("make -n" or "fallback"), `source`, and `recipe_ok`.
    """
    recipe_flags, source = _recipe_flags(build)
    out = {"values": None, "step": None, "recipe_flags": recipe_flags,
           "flags": recipe_flags, "via": None, "source": source,
           "recipe_ok": None}
    if recipe_flags is not None:
        values, step = _bignum_roundtrip(build, recipe_flags, BIGNUM_CONSTANTS,
                                         target)
        out.update(values=values, step=step, via="make -n",
                   recipe_ok=values is not None)
        if values is not None:
            return out
    #: The build printed no usable rule, or its own flags got no blob back.  Try the
    #: known flag sets and accept the first that reproduces the frozen values -- a
    #: submission that reaches the right answer by a mechanism this driver never
    #: heard of still passes.
    for candidate in FALLBACK_FLAGS:
        values, step = _bignum_roundtrip(build, candidate, BIGNUM_CONSTANTS, target)
        out["step"] = step
        if values is not None and all(values.get(k) == v for k, v in ref.items()):
            out.update(values=values, flags=list(candidate), via="fallback")
            return out
    out["values"] = None
    return out


def _bignum_cell(carried, target, key, discriminates, completed):
    """(weight, summary suffix) for one bignum constant on one target.

    `completed` is whether *this* build got a blob back at all.  See
    CONDITIONAL_ONLY: it is the second way a cell earns its weight, and the reason
    this row can still fail a build whose round trip runs and returns reversed limbs.
    """
    if not discriminates:
        return 0.0, ""            # a declared control; the emitter clamps it anyway
    if not carried.get(key) and not completed:
        return 0.0, " -- " + CONDITIONAL_ONLY % (target, "bignum round trip",
                                                 OWNED_BY["bignum"])
    return 1.0, ""


def _recipe_flags(build):
    """Ask make what it would run to regenerate a blob, and take its flags.

    Returns (flags, source) where source is either the printed recipe or the name of
    the fallback.  `make -n` after removing the product prints the command without
    running it; a submission whose build does not have that rule at all returns None
    and the caller falls back to trying flag sets.
    """
    product = build.path(RECIPE_GOAL)
    saved = None
    if os.path.exists(product):
        with open(product, "rb") as fh:
            saved = fh.read()
        os.unlink(product)
    try:
        step = T.run("recipe", build.make_argv(["-n", RECIPE_GOAL]), build.root,
                     T.RUN_TIMEOUT)
    finally:
        if saved is not None:
            with open(product, "wb") as fh:
                fh.write(saved)
    if not step.ok:
        return None, "make -n exited %d" % step.rc
    for line in step.out.splitlines():
        if "qjsc" not in line or "-c" not in line:
            continue
        flags = [m.group(1).strip() for m in RECIPE_TOKENS.finditer(line)]
        keep = [f for f in flags if f not in ("-c", "-m", "-M") and
                not f.startswith("-N") and not f.startswith("-o")]
        return keep, line.strip()[:400]
    return None, "no qjsc rule in the printed recipe"


BIGNUM_HARNESS = r"""
/* Written by the grader, compiled to bytecode by the host, read by the target. */
%s
"""


def _bignum_source(constants):
    lines = []
    for key, expr, _disc in constants:
        lines.append("print(%r + ': ' + (%s));" % (key, expr))
    return BIGNUM_HARNESS % "\n".join(lines)


#: The scratch files the round trip writes into the tree it is grading.
BIGNUM_SCRATCH = "srb_bignum"


def _bignum_cleanup(build):
    """Remove the round trip's own scratch files from the tree it ran in.

    The row writes `srb_bignum.js` into the submission's build root, compiles it to
    `srb_bignum.c` and links a binary beside them.  Left behind, those are three
    files the tree did not have before grading -- and `no-build-side-effects`
    rebuilds from a pristine snapshot and compares product sets, so the grader's own
    droppings read as the submission's side effects.  Measured: that is exactly what
    happened the first time this row ran on x86-64, which is the target that row
    inspects, and it cost an honest tree the check.

    A probe that writes into the tree under test cleans up after itself, whether or
    not another row happens to look.
    """
    try:
        names = os.listdir(build.root)
    except OSError:
        return
    for name in names:
        if name.startswith(BIGNUM_SCRATCH):
            try:
                os.unlink(os.path.join(build.root, name))
            except OSError:
                pass


def _bignum_roundtrip(build, flags, constants, prefix):
    """Compile the bignum program to a blob, then read the blob back.

    `qjsc -c` emits a C array; compiling and running it is what `examples/hello`
    does, so this reuses the same path rather than inventing a loader.  The scratch
    files are removed on every exit path; see `_bignum_cleanup`.
    """
    try:
        src = os.path.join(build.root, BIGNUM_SCRATCH + ".js")
        with open(src, "w") as fh:
            fh.write(_bignum_source(constants))
        gen = os.path.join(build.root, BIGNUM_SCRATCH + ".c")
        step = build.run_host_qjsc(list(flags) + ["-fbignum", "-e", "-o", gen, src],
                                   key=prefix + ":qjsc")
        if not step.ok:
            return None, step
        step = build.compile_generated(os.path.join(build.root, BIGNUM_SCRATCH), gen,
                                       key=prefix + ":cc")
        if not step.ok:
            return None, step
        step = build.run_native_binary(os.path.join(build.root, BIGNUM_SCRATCH),
                                       key=prefix + ":run")
        if not step.ok:
            return None, step
        return T.parse_answers(step.out), step
    finally:
        _bignum_cleanup(build)


def run_bignum_ser(emit, module):
    """Serialise arbitrary-precision integers on the port target and read them back.

    Why this row exists at all: the mantissa loop in the bignum writer does not go
    through the same put-integer helpers as the rest of the stream.  It writes limbs
    with the raw fixed-width writers, so the stream-level swap the rest of the
    format gets never touches them, and the only thing that orders a limb is a
    compile-time conditional.  A submission that fixes the transport and stops -- a
    real and complete-looking State C -- passes every other row here and fails this
    one.

    Where the constants come from: `_can_show_bignum_defect`, which decides from the
    writer's own structure whether a given expression can show the defect at all.
    Two conditions have to hold together, and neither is obvious by eye.  The value
    must appear as a BigInt *literal*, because only a literal is built while
    compiling and serialised into the bytecode -- `2n ** 64n + 1n` is arithmetic on
    three single-limb literals that runs after the blob has been read, so its
    64-bit-wide result never passes through the writer.  And the literal must have a
    limb on the raw path that is not its own byte reversal, because the lowest
    nonzero limb goes out through a shift loop that is already order-clean and a
    palindromic limb swaps to itself.

    Both halves cost this row weight before they were understood.  Five constants
    shipped declared as discriminating while being expressions: a submission that
    fixed the transport and left the serialiser alone passed them and collected a
    third of this module's weight for arithmetic that has nothing to do with the
    port.  `--self-check` now holds every row to its third column at both limb
    widths, so the table cannot drift from the mechanism again.

    The controls say the same thing from the other side.  `aabb` and `ones128` are
    wide literals that do reach the writer and still cannot show the defect, because
    every limb they occupy is its own reversal.  `expr-p64p1`, `expr-fact25` and
    `expr-fact40` are the same values as three discriminating rows, computed instead
    of written: on a submission that has left the serialiser unported the literal
    fails and the computed form passes, which is the difference between this row and
    port-semantics stated in the results rather than in a comment.

    Like the blob row, this runs on all three targets, and a cell is charged when the
    frozen baseline says original/<target> carried it *or* this build completed the
    round trip itself.  Both halves are needed and neither is arbitrary.

    On s390x State A completes no round trip at all -- the host writes a
    little-endian blob and the big-endian reader rejects it on the version byte
    before any constant is printed -- so an equally unported submission is not
    charged for a value that does not exist.  x86-64 and armhf do carry it, at two
    different limb widths, so those cells are the check that a submission which
    reached into the writer did not break the two platforms that already worked.

    The second half is what keeps this row's reason for existing.  A build that fixed
    the bytecode container and left the mantissa loop alone completes the round trip
    and reads back byte-reversed limbs: it has demonstrated it can ask the question,
    so the answer is charged, and this row fails it on s390x.  Gating on the frozen
    baseline alone would have made State C -- the exact defect this row was
    constructed to catch, and the only one in the stage that reaches those four limb
    blocks -- invisible here.  See CONDITIONAL_ONLY.
    """
    targets = SLICES[module]["targets"]
    builds = load_builds(emit, [("submission", t) for t in targets])
    ref = dict(frozen("oracle").get("bignum") or {})
    emit.add("expectations/bignum", "pass" if ref else "fail",
             "%d frozen constant values, written and read on %s in State A"
             % (len(ref), T.ORACLE_TARGET), weight=0.0)
    if not ref:
        for key, _expr, _d in BIGNUM_CONSTANTS:
            for target in targets:
                emit.add("%s/%s" % (target, key), "skip", "no expectation",
                         weight=0.0)
        return 0

    for target in targets:
        build = builds["submission-%s" % target]
        carried = baseline("bignum", target)
        run = attempt_bignum_roundtrip(build, ref, target)
        got, step, source = run["values"], run["step"], run["source"]
        #: Whether *this* build got a blob back at all.  Read together with the frozen
        #: baseline it decides what this target's cells are charged for, and it is the
        #: half that lets this row still fail a build whose round trip runs and hands
        #: back reversed limbs.  See CONDITIONAL_ONLY.
        completed = got is not None
        rt_scored = bool(carried.get(ROUNDTRIP_CELL)) or completed
        rt_weight = 1.0 if rt_scored else 0.0
        rt_extra = "" if rt_scored else \
            " -- " + CONDITIONAL_ONLY % (target, "bignum round trip",
                                         OWNED_BY["bignum"])
        if run["recipe_flags"] is not None:
            emit.add("%s/recipe" % target,
                     "pass" if run["recipe_ok"] else "fail",
                     "flags taken from the build's own rule: %s"
                     % (" ".join(run["recipe_flags"]) or "(none)"),
                     weight=0.0, detail=source)
        emit.metadata.setdefault("blob_flags", {})[target] = {
            "flags": list(run["flags"] or []), "via": run["via"] or "none",
            "recipe": source}
        if got is not None and run["via"] == "fallback":
            emit.note("%s: the build printed no usable recipe (%s); flags %r "
                      "reproduced the expected values"
                      % (target, source, run["flags"]))
        if got is None:
            emit.add("%s/roundtrip" % target, "fail",
                     "no flag set completed a bignum round trip on this build"
                     + rt_extra, weight=rt_weight,
                     detail=(step.tail(30) if step is not None else source))
            for key, _expr, discriminates in BIGNUM_CONSTANTS:
                weight, extra = _bignum_cell(carried, target, key, discriminates,
                                             completed)
                emit.add("%s/%s" % (target, key), "fail",
                         "the round trip did not run" + extra,
                         weight=weight, control=not discriminates)
            continue
        emit.add("%s/roundtrip" % target, "pass",
                 "%d values read back from a blob written on the host" % len(got),
                 weight=rt_weight)
        for key, _expr, discriminates in BIGNUM_CONSTANTS:
            cid = "%s/%s" % (target, key)
            weight, extra = _bignum_cell(carried, target, key, discriminates,
                                         completed)
            if key not in ref:
                emit.add(cid, "skip", "the original did not print this key",
                         weight=0.0)
            elif key not in got:
                emit.add(cid, "fail",
                         "not printed by the target's read-back" + extra,
                         weight=weight)
            elif got[key] == ref[key]:
                emit.add(cid, "pass", "%s%s" % (got[key][:120], extra),
                         weight=weight, control=not discriminates)
            else:
                emit.add(cid, "fail",
                         "differs from the value written on x86-64" + extra,
                         weight=weight, control=not discriminates,
                         detail="expected: %s\ngot:      %s"
                                % (ref[key][:400], got[key][:400]))
    return 0


# --------------------------------------------------------------------------- #
# The target's own byte order
# --------------------------------------------------------------------------- #

#: How each native key's little-endian answer becomes its big-endian answer.
#:
#: A `nat-` key is an observation a big-endian target is *supposed* to answer
#: differently, so it cannot be graded by comparing targets.  What can be graded is
#: the relation between the two answers, because the layout that relates them is
#: fixed by the language and not by the port: an ArrayBuffer's fields sit at spec'd
#: offsets with spec'd widths, and changing byte order permutes the bytes *within a
#: field* and nothing else.
#:
#:   ("fields", (w1, w2, ...))  a comma-separated byte list; reverse each field of
#:                              that many bytes in place, left to right
#:   ("elements", None)         a comma-separated list of whole values read back
#:                              through a narrower view; the list order reverses
#:   ("swap", other_key)        this key and `other_key` exchange answers, because
#:                              one of them asked for the target's order explicitly
#:                              and the other asked for its opposite
#:   ("order", None)            the literal the anchor prints
#:
#: Every row here was measured against a correct big-endian build and a correct
#: little-endian one; nothing in it was derived from the submission.
NATIVE_LAYOUT = {
    "nat-order": ("order", None),
    "nat-u16-0102": ("fields", (2,)),
    "nat-u32-01020304": ("fields", (4,)),
    "nat-i32-minus2": ("fields", (4,)),
    "nat-i16-minus2": ("fields", (2,)),
    "nat-f32-one": ("fields", (4,)),
    "nat-f32-minusone": ("fields", (4,)),
    "nat-f64-onepointfive": ("fields", (8,)),
    "nat-f64-one": ("fields", (8,)),
    "nat-bi64": ("fields", (8,)),
    "nat-bu64": ("fields", (8,)),
    "nat-atomics": ("fields", (4,)),
    # A 16-byte buffer written through three views at spec'd offsets: a Uint16Array
    # at 2, a Float32Array at 8, a Uint32Array at 12.  The untouched bytes are zero
    # and reversing them is the identity, so the layout is stated in full anyway.
    "nat-builtin442": ("fields", (2, 2, 4, 4, 4)),
    # A Uint32Array of two elements at byteOffset 4 in a 16-byte buffer, printed to
    # 12 bytes: four untouched, then the two elements.
    "nat-slice": ("fields", (4, 4, 4)),
    "nat-u32-as-u16": ("elements", None),
    "nat-f64-as-u32": ("elements", None),
    "nat-alias-u32-then-dv-be": ("swap", "nat-alias-u32-then-dv-le"),
    "nat-alias-u32-then-dv-le": ("swap", "nat-alias-u32-then-dv-be"),
    "nat-alias-f64-then-dv-be": ("swap", "nat-alias-f64-then-dv-le"),
    "nat-alias-f64-then-dv-le": ("swap", "nat-alias-f64-then-dv-be"),
}

#: A native key may legitimately report that the feature is missing.
NATIVE_UNAVAILABLE = "unavailable"


def _reverse_fields(value, widths):
    """Reverse each field of a comma-separated byte list, or None if it does not fit."""
    parts = [p.strip() for p in value.split(",")]
    if sum(widths) and len(parts) % sum(widths):
        return None
    out, i = [], 0
    while i < len(parts):
        for w in widths:
            if i + w > len(parts):
                return None
            out.extend(reversed(parts[i:i + w]))
            i += w
    return ",".join(out)


def _expected_native(key, le_answers, big_endian):
    """What this key must print on a target of the given order.

    `le_answers` is one correct little-endian build's answers for the same program.
    On a little-endian target the expectation is that answer unchanged; on a
    big-endian one it is that answer transformed by the spec'd layout.  Returns
    (expected, reason) or (None, why not).
    """
    if key not in le_answers:
        return None, "the reference build did not print it"
    ref = le_answers[key]
    if ref.strip() == NATIVE_UNAVAILABLE:
        return NATIVE_UNAVAILABLE, "the reference reports the feature absent"
    if not big_endian:
        return ref, "same order as the reference"
    kind, arg = NATIVE_LAYOUT[key]
    if kind == "order":
        return "big", "the anchor names the target's order"
    if kind == "fields":
        got = _reverse_fields(ref, arg)
        if got is None:
            return None, ("the reference answer %r does not divide into %s-byte "
                          "fields" % (ref[:60], arg))
        return got, "each %s-byte field reversed" % (arg,)
    if kind == "elements":
        return ",".join(reversed([p.strip() for p in ref.split(",")])), \
            "the element order reverses"
    if kind == "swap":
        if arg not in le_answers:
            return None, "the reference did not print %s" % arg
        return le_answers[arg], "exchanges answers with %s" % arg
    return None, "no layout for this key"


def run_native(emit, module):
    """Grade the target's own byte order, without grading it against another target.

    A big-endian machine is *supposed* to show a Float64Array's bytes reversed
    through a Uint8Array.  Comparing that with x86-64 would fail every correct port
    and pass a submission that byte-swapped its typed arrays into agreement, so the
    screen keeps these keys out of every other row.  What is gradable is the
    *relation*: the buffer's fields sit where the language says they sit, so a change
    of byte order permutes bytes within a field and does nothing else.  This row
    applies that permutation to a correct little-endian answer and requires the
    target to match.

    That makes it a real check on a partial swap: a submission that reordered 32-bit
    accesses and left 64-bit ones alone satisfies `nat-u32-01020304` and fails
    `nat-f64-one`, and one that reached past an element width into an 8-bit view
    fails the invariant control in the same program.

    Which cells are charged for is decided the same way as every other row: by
    whether original/<target> already answers the key the answer its own declared
    layout implies.  Measured, that leaves 56 of the 60 cells scored and records the
    other four -- the `nat-alias-*-then-dv-*` pairs on s390x, which store through a
    typed array and read back through a DataView that was *told* an order, so they
    inherit the DataView defect wholesale and are the port rather than a native
    property.  They are the same four keys the prefix audit below refuses to ask
    State A about, for the same reason, and both places say so.

    It also audits the prefix, twice over and from two different authorities.

    The prefix is earned from `NATIVE_LAYOUT`: a `nat-` key is one whose declared
    big-endian expectation differs from the little-endian reference answer.  That is
    a property of the declaration and the oracle, both fixed when the image was
    built, and it is deliberately not asked of State A's own three builds -- four of
    these keys read a typed-array store back through a DataView that was told an
    order, so State A on s390x takes the little-endian branch of the very `#ifdef`
    this task is about and prints the x86-64 answer.  Asking State A whether those
    keys vary by order gets `no`, from the bug, and reports `fail` on four
    correctly-declared keys of a correct submission.

    The other direction is asked of the submission, where it is not self-referential:
    a *bare* key that the submission answers differently on the big-endian target is
    a value the port left order-dependent, which is the bug, named.
    """
    targets = SLICES[module]["targets"]
    load_builds(emit, [("submission", t) for t in targets])
    sheets = {}
    for target in targets:
        sheet = T.read_answers("submission", target)
        if sheet is None:
            raise MissingBuild("no answer sheet for submission-%s; the build module "
                               "publishes it" % target)
        sheets[target] = sheet
    reference = frozen("answers/%s" % T.ORACLE_TARGET)

    def native_of(sheet):
        out = {}
        for stem, prog in (sheet.get("programs") or {}).items():
            for key, value in (prog.get("answers") or {}).items():
                if key.startswith(T.NATIVE_PREFIX):
                    out[key] = value
        return out

    ref_native = native_of(reference)
    unknown = sorted(set(ref_native) - set(NATIVE_LAYOUT))
    emit.add("layout/covers-every-native-key",
             "pass" if not unknown else "fail",
             "all %d native keys have a declared layout" % len(ref_native)
             if not unknown else
             "%d native keys have no declared layout" % len(unknown),
             weight=0.0, detail="\n".join(unknown))

    recorded = 0
    for target in targets:
        big = T.TARGETS[target].big_endian
        got_native = native_of(sheets[target])
        orig_native = native_of(frozen("answers/%s" % target) or {})
        for key in sorted(set(ref_native) & set(NATIVE_LAYOUT)):
            cid = "%s/%s" % (target, key)
            want, why = _expected_native(key, ref_native, big)
            if want is None:
                emit.add(cid, "skip", why, weight=0.0)
                continue
            #: Does State A's own build for this target already answer this key the
            #: way its layout implies?  Where it does not, the cell is the port.
            carried = orig_native.get(key) == want
            weight = 1.0 if carried else 0.0
            extra = ""
            if not carried:
                recorded += 1
                extra = " -- " + RECORDED_ONLY % (target, OWNED_BY["native"])
            if key not in got_native:
                emit.add(cid, "fail", "not printed by this build" + extra,
                         weight=weight)
            elif got_native[key].strip() == NATIVE_UNAVAILABLE and \
                    want != NATIVE_UNAVAILABLE:
                emit.add(cid, "fail",
                         "this build reports the feature absent; the reference "
                         "printed %s%s" % (want[:80], extra), weight=weight)
            elif got_native[key] == want:
                emit.add(cid, "pass", "%s (%s)%s"
                         % (got_native[key][:80], why, extra), weight=weight)
            else:
                emit.add(cid, "fail",
                         "not the answer this order implies (%s)%s" % (why, extra),
                         weight=weight,
                         detail="reference (%s, little-endian): %s\nexpected here:"
                                "  %s\ngot:            %s\noriginal/%s:    %s"
                                % (T.ORACLE_TARGET, ref_native[key][:200],
                                   want[:200], got_native[key][:200], target,
                                   (orig_native.get(key) or "<absent>")[:200]))
    emit.metadata["native_recorded_only"] = recorded

    #: Does each nat- key's declaration actually make it order-dependent?  Asked of
    #: NATIVE_LAYOUT against the oracle's answers, which is where the expectation
    #: comes from in the rows above.  A key whose big-endian expectation equals the
    #: little-endian answer is a key the prefix excused from every other module for
    #: nothing: it is invariant, and it belongs in the general population.
    unearned = []
    for key in sorted(set(ref_native) & set(NATIVE_LAYOUT)):
        if ref_native[key].strip() == NATIVE_UNAVAILABLE:
            continue
        want, _why = _expected_native(key, ref_native, True)
        if want is not None and want == ref_native[key]:
            unearned.append("%s: %s on both orders" % (key, want[:60]))
    emit.add("labels/nat-prefix-earned",
             "pass" if not unearned else "fail",
             "every nat- key's declared layout makes it order-dependent"
             if not unearned else
             "%d nat- key(s) have the same answer on both orders" % len(unearned),
             weight=0.0, detail="\n".join(unearned[:60]) +
             "\n\nreported, not charged: an unearned prefix is this suite's fault. "
             "Asked of the declaration and the oracle, never of State A's own "
             "s390x build -- State A takes the wrong branch of exactly the #ifdef "
             "these keys probe, so it answers 'invariant' from the bug.")

    labels = T.read_labels() or {}
    sub = labels.get("submission") or {}
    if not sub:
        emit.add("labels/available", "skip",
                 "no submission labels; the build module publishes them",
                 weight=0.0)
        return 0
    #: A bare key whose answer varies across the submission's own three targets is a
    #: value the port left order-dependent.  Asked as an absolute this fails on State
    #: A by 41 keys -- the DataView keys, whose order dependence *is* the defect --
    #: so it is asked as a delta against the leak set sealed from State A instead.
    #: What is charged is what the submission introduced; the inherited 41 are
    #: reported beside it and belong to stage 1, which has a gate for them.
    #:
    #: The delta is the honest form of this check even setting State A aside: an
    #: absolute reading cannot distinguish "you broke something" from "you have not
    #: finished", and only the first is a regression a behavioural stage can charge
    #: for by comparison.
    def leaks_of(labelled):
        out = []
        for stem, keys in sorted((labelled or {}).items()):
            if not isinstance(keys, dict):
                continue
            for key, label in sorted(keys.items()):
                if not key.startswith(T.NATIVE_PREFIX) and label == "order-varying":
                    out.append("%s/%s" % (stem, key))
        return out

    leaked = leaks_of(sub)
    #: The baseline comes out of the frozen blob, not out of `labels` -- the runtime
    #: file the build module writes carries only the submission's own classification,
    #: so reading the original from there silently yields an empty baseline and every
    #: inherited leak reads as newly introduced.  Measured on State A: 41 of them.
    inherited = set(leaks_of((frozen("labels") or {}).get("original") or {}))
    introduced = [k for k in leaked if k not in inherited]
    still_open = [k for k in leaked if k in inherited]

    emit.add("labels/no-new-order-dependence",
             "pass" if not introduced else "fail",
             "no key varies by byte order that original/%s did not"
             % T.ORACLE_TARGET if not introduced
             else "%d key(s) newly vary by byte order without declaring it"
                  % len(introduced),
             detail="\n".join(introduced[:60]))
    emit.add("labels/inherited-order-dependence",
             "pass" if not still_open else "fail",
             "State A's %d order-varying keys are all resolved" % len(inherited)
             if not still_open
             else "%d of State A's %d order-varying keys still vary"
                  % (len(still_open), len(inherited)),
             weight=0.0, detail="\n".join(still_open[:60]) +
             "\n\n" + RECORDED_ONLY % (T.PORT_TARGET, OWNED_BY["native"]))
    emit.metadata["label_counts"] = {
        "unearned_nat_prefix": len(unearned),
        "order_varying_total": len(leaked),
        "order_varying_introduced": len(introduced),
        "order_varying_inherited_open": len(still_open),
        "order_varying_inherited_baseline": len(inherited)}
    return 0


# --------------------------------------------------------------------------- #
# Upstream's own suites
# --------------------------------------------------------------------------- #

#: Upstream's test files, in the order they are run.
UPSTREAM_SUITES = (
    "tests/test_bjson.js",
    "tests/test_builtin.js",
    "tests/test_closure.js",
    "tests/test_loop.js",
    "tests/test_language.js",
    "tests/test_std.js",
    "tests/test_qjscalc.js",
)

#: The one suite that needs an interpreter flag to run at all.
SUITE_FLAGS = {"tests/test_qjscalc.js": ("--qjscalc",)}


def _run_upstream(build, rel):
    path = build.path(rel)
    if not os.path.exists(path):
        return None
    return build.run_js(rel, qjs_args=SUITE_FLAGS.get(rel, ()),
                        key="upstream:" + os.path.basename(rel))


#: The four-outcome table of `run_suites`, as a decision on its own.  Split out of
#: the loop below so `--self-check` can exercise all four branches without a build
#: tree or a frozen baseline, for the reason the clamp probe exists: the defect this
#: replaced was a branch whose message read "not charged" while its weight was left
#: at the 1.0 default, and it survived review and a full grading run.  Nothing had
#: ever pushed a case through it and looked at the number that came out.
#:
#: Returns (verdict, weight, improved).  Only one of the four costs anything.
def _upstream_verdict(ref_ok, got_ok):
    if ref_ok:
        #: State A passes here, so this cell is in the scored pool: the submission
        #: either kept it or broke it, and breaking it is the one charged outcome.
        return ("pass", 1.0, False) if got_ok else ("fail", 1.0, False)
    #: State A fails here, on this host, for reasons that are the host's or
    #: upstream's -- `os.isatty(0)` under a pipe, an upstream assertion with a
    #: little-endian byte string in it.  No submission can pass it or is asked to,
    #: so the cell is recorded and not charged either way.  A submission that does
    #: pass it is recorded as an improvement and still not paid for one, because the
    #: pool has to be the same size for everyone.
    return ("pass", 0.0, bool(got_ok))


def run_suites(emit, module):
    """Run upstream's tests and grade each against the same suite in State A.

    The baseline is measured in this run, on the same target, rather than frozen,
    because most of what these suites say here is about the host: test_std.js fails
    everywhere at `os.isatty(0)` under a pipe, and test_builtin.js fails on the
    big-endian target in *both* trees at an upstream assertion with a little-endian
    byte string written into it.  A frozen pass list would charge every submission
    for both.

    So each cell is graded as a comparison, and only one of the four outcomes is
    charged:

      State A passes, submission passes   pass, weight 1
      State A passes, submission fails    fail, weight 1   <- the only cost
      State A fails, submission fails     pass, weight 0   (decided by the host)
      State A fails, submission passes    pass, weight 0, recorded as an improvement

    The scored pool is therefore exactly the suites State A passes on this target,
    and it is sealed: `ref_ok` is read from the frozen baseline, so a submission can
    lose a cell here but cannot move one out of the pool.
    """
    targets = SLICES[module]["targets"]
    builds = load_builds(emit, [("submission", t) for t in targets])
    baseline = frozen("oracle").get("upstream") or {}
    for target in targets:
        sub = builds["submission-%s" % target]
        want = baseline.get(target) or {}
        for rel in UPSTREAM_SUITES:
            cid = "%s/%s" % (target, os.path.basename(rel))
            ref = want.get(rel)
            got = _run_upstream(sub, rel)
            if ref is None:
                emit.add(cid, "skip", "no frozen baseline for this suite",
                         weight=0.0)
                continue
            ref_ok = ref.get("rc") == 0 and not ref.get("timed_out")
            if got is None:
                emit.add(cid, "fail", "not present in the submission",
                         detail="State A has %s" % rel)
                continue
            verdict, weight, improved = _upstream_verdict(ref_ok, got.ok)
            if ref_ok and got.ok:
                emit.add(cid, verdict, "passes in State A and here", weight=weight)
            elif ref_ok:
                emit.add(cid, verdict,
                         "passes in State A, exits %d here%s"
                         % (got.rc, " (timed out)" if got.timed_out else ""),
                         weight=weight, detail=got.tail(30))
            elif improved:
                emit.add(cid, verdict,
                         "fails in State A (exit %s), passes here" % ref.get("rc"),
                         weight=weight, improved=True, detail=ref.get("tail", ""))
            else:
                #: Recorded, not charged, and the weight has to say so as well as the
                #: message: this cell's outcome was decided by State A on this host --
                #: test_std.js at `os.isatty(0)` under a pipe, test_builtin.js at an
                #: upstream assertion with a little-endian byte string in it -- and no
                #: submission can pass it or is asked to.  Left at weight 1.0 it was
                #: five free marks in every report, this row's rate reading 21/21 on a
                #: tree that fixed nothing.  The scored pool is now exactly the suites
                #: State A passes on this target, which is sealed in the frozen
                #: baseline: a submission can lose a cell here but cannot remove one.
                emit.add(cid, verdict,
                         "fails in State A too (baseline: exit %s) -- not charged"
                         % ref.get("rc"), weight=weight,
                         baseline_rc=ref.get("rc"), submission_rc=got.rc,
                         detail="State A:\n%s\n\nsubmission:\n%s"
                                % (ref.get("tail", ""), got.tail(12)))
    return 0


# --------------------------------------------------------------------------- #
# The build leaves the tree alone
# --------------------------------------------------------------------------- #

#: The generated sources that carry bytecode: written into the tree by running the
#: build's own bytecode compiler.  State A ships neither, which is the point -- a
#: submission that ships one has frozen a blob written by whatever machine produced
#: it, and the check below is whether the build can produce it again.
GENERATED_BLOB_SOURCES = ("repl.c", "qjscalc.c")


def run_side_effects(emit, module):
    """Build twice from two fresh copies and compare what each build left behind.

    Two failures this catches, both of which make the byte-order question depend on
    which machine last ran `make` rather than on the tree:

      a build that generates a source file and leaves it in the tree, so the second
      build reads the first one's output;

      a pre-generated blob shipped in the tree with no rule that regenerates it, so
      the bytes in it were written by whatever host produced them.

    The second is the one that matters here, and it is measured the only way that
    does not involve reading the Makefile: delete the product, build again, and see
    whether it comes back.  A tree that ships `repl.c` and cannot rebuild it fails,
    whatever its rules say.
    """
    target = SLICES[module]["targets"][0]
    builds = load_builds(emit, [("submission", target)])
    build = builds["submission-%s" % target]
    pristine = os.path.join(T.SNAPSHOTS, "submission")
    if not os.path.isdir(pristine):
        raise MissingBuild("no pristine snapshot at %s; the build module writes it"
                           % pristine)

    before = inventory(pristine)
    after = inventory(build.root)
    produced = sorted(set(after) - set(before))
    emit.metadata["files_produced_by_build"] = len(produced)
    emit.add("build/produced-something", "pass" if produced else "fail",
             "the build created %d files" % len(produced),
             detail="\n".join(produced[:80]))

    shipped = [f for f in GENERATED_BLOB_SOURCES if f in before]
    emit.metadata["blob_sources_shipped"] = shipped
    emit.add("tree/blob-sources-not-shipped", "pass" if not shipped else "fail",
             "the tree ships no pre-generated bytecode source" if not shipped else
             "the tree ships %s" % ", ".join(shipped),
             weight=0.0,
             detail="reported, not charged: shipping one is only a fault if the "
                    "build cannot regenerate it, which is the next check")

    #: The load-bearing half: build a second time from the pristine snapshot with
    #: every generated blob source removed, and require them back.
    second = T.Build("submission", target,
                     root=os.path.join(T.BUILD_ROOT,
                                       "submission-%s-rebuild" % target))
    if not second.prepare_sources():
        emit.add("rebuild/prepare", "error", "could not copy the snapshot again",
                 weight=0.0, detail=second.step("prepare").tail(10))
        return 0

    for rel in GENERATED_BLOB_SOURCES:
        p = second.path(rel)
        if os.path.isfile(p):
            os.unlink(p)
    emit.add("rebuild/starts-clean", "pass",
             "no pre-generated bytecode source in the second build's tree",
             weight=0.0,
             detail="removed if present: %s" % ", ".join(GENERATED_BLOB_SOURCES))

    second.execute()
    step = second.step("make")
    emit.add("rebuild/completes", "pass" if step and step.ok else "fail",
             "a second build from a fresh copy completes" if step and step.ok else
             "a second build from a fresh copy failed",
             detail="" if step is None else step.tail(40))
    if step is None or not step.ok:
        return 0

    absent = [rel for rel in GENERATED_BLOB_SOURCES
              if not os.path.isfile(second.path(rel))]
    emit.add("rebuild/regenerates-blob-sources",
             "pass" if not absent else "fail",
             "the build wrote every bytecode source itself" if not absent else
             "%d bytecode source(s) the build did not produce: %s"
             % (len(absent), ", ".join(absent)),
             detail="a blob the build cannot produce carries the byte order of "
                    "whatever machine wrote it, and no amount of correct code in "
                    "this tree changes that")

    #: And the two builds must agree on what they produced.
    second_after = inventory(second.root)
    second_produced = set(second_after) - set(before)
    only_first = sorted(set(produced) - second_produced)
    only_second = sorted(second_produced - set(produced))
    emit.add("rebuild/same-products",
             "pass" if not (only_first or only_second) else "fail",
             "both builds produced the same set of files" if
             not (only_first or only_second) else
             "the two builds produced different file sets (%d/%d)"
             % (len(only_first), len(only_second)),
             detail="only in the first:\n%s\n\nonly in the second:\n%s"
                    % ("\n".join(only_first[:40]), "\n".join(only_second[:40])))

    #: Then the answer that matters: the rebuilt tree must behave the same.
    sheet = T.collect_answers(second)
    original = T.read_answers("submission", target)
    if original is None:
        emit.add("rebuild/same-answers", "skip", "no first-build answer sheet",
                 weight=0.0)
        return 0
    differ = []
    for stem, prog in sorted((original.get("programs") or {}).items()):
        first = {k: v for k, v in (prog.get("answers") or {}).items()
                 if not k.startswith("__")}
        again = {k: v for k, v in
                 (((sheet.get("programs") or {}).get(stem) or {}).get("answers")
                  or {}).items() if not k.startswith("__")}
        for key in sorted(first):
            if again.get(key) != first[key]:
                differ.append("%s/%s: %r -> %r"
                              % (stem, key, first[key][:60],
                                 (again.get(key) or "<absent>")[:60]))
    emit.add("rebuild/same-answers", "pass" if not differ else "fail",
             "a second build from a fresh copy answers identically" if not differ
             else "%d answers changed between two builds of the same tree"
                  % len(differ),
             detail="\n".join(differ[:60]))
    shutil.rmtree(second.root, ignore_errors=True)
    return 0


# --------------------------------------------------------------------------- #
# The apparatus checks itself
# --------------------------------------------------------------------------- #

def _suite_metadata():
    """The counts this suite's own suite.toml claims, or {} if it cannot be read.

    The suite-level ``[metadata]`` table, which the shared loader carries through
    verbatim.  Read here with tomllib rather than through ``swerefactor.config``
    because this file runs under ``python3 -I`` and does not otherwise need the
    harness on sys.path -- and because an unreadable suite.toml should make the
    apparatus row say so, not raise on an import.
    """
    path = os.path.join(T.SUITE, "suite.toml")
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return {}
    try:
        import tomllib
        data = tomllib.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return data.get("metadata") or {}


def run_apparatus(emit, module):
    """Check the grader, not the submission.

    Four properties, all of which held when this suite was written and any of which
    could stop holding through a change to it:

      the screen on disk is the one the build module published, byte for byte;
      its admitted count is the number suite.toml tells a reader it is;
      every program printed each of its keys exactly once on every target;
      nothing wrote to the frozen assets after the image was built.

    The last is why `frozen()` checks every file against the manifest digest before
    returning it, and why no module recomputes an expectation it cannot find: a
    module that can rebuild the screen can rebuild a *different* screen, and two rows
    of one report would then disagree about what the expectation was with nothing in
    the report saying so.  This row is the check that the discipline held in a real
    run.

    Its weight is nearly nominal and it cannot fail on a submission's account.  If it
    fails, this suite is broken, and the report should say so where a person will see
    it rather than quietly scoring a tree against an expectation nobody can name.
    """
    manifest = frozen("manifest")
    screen = frozen("screen")
    emit.add("assets/manifest-schema", "pass",
             "frozen assets declare %s" % manifest.get("schema"))
    emit.metadata["frozen_at_utc"] = manifest.get("frozen_at_utc")

    #: Every file the manifest names, re-digested here rather than trusted.
    bad = []
    for rel, want in sorted((manifest.get("files") or {}).items()):
        path = os.path.join(ASSETS, rel)
        if not os.path.isfile(path):
            bad.append("%s: absent" % rel)
        elif _digest(path) != want:
            bad.append("%s: digest changed" % rel)
    emit.add("assets/unmodified", "pass" if not bad else "fail",
             "all %d frozen files match the manifest"
             % len(manifest.get("files") or {}) if not bad else
             "%d frozen file(s) do not match the manifest" % len(bad),
             detail="\n".join(bad))

    #: The mode bits the image build set, read from stat rather than from
    #: `os.access`.  Grading runs as root and root satisfies W_OK whatever the mode
    #: says, so the access-based version of this check could not pass in the
    #: container it ships in: it read `fail` on all six files of a correct image and
    #: said so in a row a reader sees before any grading row.  What stat answers is
    #: the question worth asking -- did something chmod the frozen assets after the
    #: build -- and that has an answer root cannot fake.
    mutable = []
    for rel in sorted(manifest.get("files") or {}):
        try:
            mode = os.stat(os.path.join(ASSETS, rel)).st_mode
        except OSError as exc:
            mutable.append("%s: %s" % (rel, exc))
            continue
        if mode & 0o222:
            mutable.append("%s: mode %o" % (rel, mode & 0o777))
    emit.add("assets/read-only", "pass" if not mutable else "fail",
             "every frozen file still carries the mode the image build set"
             if not mutable else
             "%d frozen file(s) have a write bit set" % len(mutable),
             weight=0.0,
             detail="\n".join(mutable[:40]) +
                    "\n\nreported, not charged: the digest check above is the "
                    "boundary, and a chmod without a write is not a fault. This "
                    "row is here so an image whose assets were unsealed says so.")

    admitted = sum(len(v) for v in (screen.get("admitted") or {}).values())
    native = sum(len(v) for v in (screen.get("native") or {}).values())
    excluded = sum(len(v) for v in (screen.get("excluded") or {}).values())
    missing = sum(len(v) for v in (screen.get("missing") or {}).values())
    emit.metadata["screen"] = {"admitted": admitted, "native": native,
                               "excluded": excluded, "missing": missing}

    declared = _suite_metadata()
    for name, got in (("admitted_keys", admitted), ("native_keys", native),
                      ("excluded_keys", excluded)):
        want = declared.get(name)
        cid = "metadata/%s" % name.replace("_", "-")
        if want is None:
            emit.add(cid, "skip", "suite.toml does not declare %s" % name,
                     weight=0.0)
        elif int(want) == got:
            emit.add(cid, "pass", "suite.toml says %d and the screen has %d"
                     % (int(want), got))
        else:
            emit.add(cid, "fail",
                     "suite.toml says %d, the screen has %d" % (int(want), got),
                     detail="a reader of this file would be told the wrong number")

    #: No key printed twice, in the frozen sheets or in the submission's.
    dupes = []
    for target in T.TARGET_NAMES:
        for label, sheet in (("State A", frozen("answers/%s" % target)),
                             ("submission", T.read_answers("submission", target))):
            if sheet is None:
                continue
            for stem, prog in sorted((sheet.get("programs") or {}).items()):
                d = (prog.get("answers") or {}).get("__duplicate_keys__")
                if d:
                    dupes.append("%s %s %s: %s" % (label, target, stem, d))
    emit.add("answers/no-duplicate-keys", "pass" if not dupes else "fail",
             "every program printed each key once" if not dupes else
             "%d program runs printed a key twice" % len(dupes),
             detail="\n".join(dupes[:40]))

    #: And there is no second copy of the screen anywhere a module could have read
    #: instead.  The shared work area is written by the modules themselves, so a
    #: screen appearing there means some module recomputed one.
    stray = []
    for base, _dirs, files in os.walk(T.SHARED) if os.path.isdir(T.SHARED) else []:
        for name in files:
            if name.startswith("screen") and name.endswith(".json"):
                stray.append(os.path.relpath(os.path.join(base, name), T.SHARED))
    emit.add("screen/not-reconstructed", "pass" if not stray else "fail",
             "no module wrote a screen of its own" if not stray else
             "%d screen file(s) in the shared work area: %s"
             % (len(stray), ", ".join(sorted(stray)[:6])),
             detail="the graded screen is %s and is read-only"
                    % os.path.join(ASSETS, "screen.json"))
    return 0


# --------------------------------------------------------------------------- #
# The driver checks itself, at image build time
# --------------------------------------------------------------------------- #

KINDS = {
    "build": run_build,
    "sheet": run_sheet,
    "blob": run_blob,
    "bignum-ser": run_bignum_ser,
    "native": run_native,
    "suites": run_suites,
    "side-effects": run_side_effects,
    "apparatus": run_apparatus,
}


def _undefined_names(path):
    """Names read inside a function that nothing in this file can bind.

    Third in a row of defects that `py_compile` accepts and grading discovers.  A
    name is decidable without running anything, so it is checked here.

    Deliberately flow-insensitive and conservative: a name counts as bound if it
    is assigned, imported, a parameter, a comprehension or except or with target,
    or a module-level or builtin name -- anywhere in its function or an enclosing
    one, in any branch.  That accepts some genuinely unsafe code (a name bound
    only on the other side of an `if`), and reports nothing that is merely
    unusual.  What it catches is the rename that missed a use, which is the
    failure that keeps happening.
    """
    import ast
    import builtins

    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)

    module_names = set(dir(builtins))
    #: Bound by the import machinery rather than by any statement here.
    module_names.update(("__file__", "__name__", "__doc__", "__spec__",
                         "__loader__", "__package__", "__builtins__", "__debug__"))
    for node in tree.body:
        for sub in ast.walk(node):
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                module_names.add(sub.name)
            elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                module_names.add(sub.id)
            elif isinstance(sub, ast.alias):
                module_names.add((sub.asname or sub.name).split(".")[0])
            elif isinstance(sub, ast.Global):
                module_names.update(sub.names)

    def bound_by(fn):
        """Every name `fn`'s own body can bind, not descending into nested defs."""
        names = set()
        for arg in list(fn.args.posonlyargs) + list(fn.args.args) + \
                list(fn.args.kwonlyargs):
            names.add(arg.arg)
        for arg in (fn.args.vararg, fn.args.kwarg):
            if arg is not None:
                names.add(arg.arg)
        stack = list(fn.body)
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
                continue                      # its locals are its own
            if isinstance(node, ast.Lambda):
                continue
            if isinstance(node, ast.ClassDef):
                names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif isinstance(node, ast.alias):
                names.add((node.asname or node.name).split(".")[0])
            elif isinstance(node, ast.ExceptHandler) and node.name:
                names.add(node.name)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                names.update(node.names)
            stack.extend(ast.iter_child_nodes(node))
        return names

    problems = []
    functions = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    #: Enclosing scopes, by source span: a nested def sees its parent's locals.
    spans = [(f, f.lineno, max(getattr(x, "lineno", f.lineno)
                               for x in ast.walk(f))) for f in functions]
    for fn in functions:
        visible = set(module_names) | bound_by(fn)
        for other, lo, hi in spans:
            if other is not fn and lo <= fn.lineno <= hi:
                visible |= bound_by(other)
        stack, reads = list(fn.body), []
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue                      # walked as its own function
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                reads.append(node)
            stack.extend(ast.iter_child_nodes(node))
        for node in reads:
            if node.id not in visible:
                problems.append("%s: line %d reads %r, which nothing binds"
                                % (fn.name, node.lineno, node.id))
    return sorted(set(problems))


def _self_check():
    """Prove the properties this file's design depends on, before any grading.

    Run at image build time, so a suite that cannot be graded coherently fails to
    build rather than producing a number.  The load-bearing one is the partition: if
    two sheet modules claim the same (program, target) pair, a key's weight means two
    things at once and no report can say which row it belongs to.  If no module
    claims a pair, a program runs on every target and is graded nowhere.
    """
    problems, notes = [], []

    for module in sorted(SLICES):
        kind = SLICES[module]["kind"]
        if kind not in KINDS:
            problems.append("module %s has kind %r with no implementation"
                            % (module, kind))
        for target in SLICES[module]["targets"]:
            if target not in T.TARGETS:
                problems.append("module %s names unknown target %r"
                                % (module, target))

    #: Every handler is called as handler(emit, module).  Checked because one of
    #: them was written to take only `emit`, and nothing before grading noticed:
    #: `--self-check` walked the tables, the twelve run.sh symlinks resolved, the
    #: image built clean, and the first real run turned the `required` build module
    #: into a TypeError -- which zeroed the stage for a submission that was fine.
    #: A missing implementation is a name lookup; a wrong signature is not.
    for kind, handler in sorted(KINDS.items()):
        try:
            sig = inspect.signature(handler)
            sig.bind(None, None)
        except TypeError as exc:
            problems.append("handler for kind %r is not callable as "
                            "handler(emit, module): %s" % (kind, exc))

    scanned = 0
    for source in (__file__, T.__file__):
        if not source or not os.path.exists(source):
            continue
        scanned += 1
        for line in _undefined_names(source):
            problems.append("%s: %s" % (os.path.basename(source), line))
    notes.append("%d source file(s) have no unbound local names" % scanned)

    #: Every sheet module's (program, target) pairs, and the partition over them.
    claimed = {}
    for module, spec in sorted(SLICES.items()):
        if spec["kind"] != "sheet":
            continue
        for program in spec["programs"]:
            for target in spec["targets"]:
                claimed.setdefault((program, target), []).append(module)
    for pair, owners in sorted(claimed.items()):
        if len(owners) > 1:
            problems.append("(%s, %s) is graded by %s" % (pair[0], pair[1],
                                                          " and ".join(owners)))
    for program in ALL_PROGRAMS:
        for target in T.TARGET_NAMES:
            if (program, target) not in claimed:
                problems.append("(%s, %s) is graded by no module"
                                % (program, target))
    notes.append("%d (program, target) pairs, each claimed once"
                 % len(claimed))

    #: The programs this file names must be the programs on disk.
    on_disk = tuple(stem for stem, _ in T.program_files())
    if on_disk:
        for stem in on_disk:
            if stem not in ALL_PROGRAMS:
                problems.append("program %s.js is on disk and in no module" % stem)
        for stem in ALL_PROGRAMS:
            if stem not in on_disk:
                problems.append("program %s is named here and not on disk" % stem)
        notes.append("%d programs on disk, all accounted for" % len(on_disk))
    else:
        notes.append("no programs directory visible; the disk check was skipped")

    #: Every native key the reference program prints must have a declared layout,
    #: and every declared layout must reduce a plausible answer.
    for key, (kind, arg) in sorted(NATIVE_LAYOUT.items()):
        if kind == "fields":
            probe = ",".join(str(i) for i in range(sum(arg)))
            if _reverse_fields(probe, arg) is None:
                problems.append("layout for %s cannot reverse its own width" % key)
        elif kind == "swap":
            if NATIVE_LAYOUT.get(arg, (None, None))[1] != key:
                problems.append("%s swaps with %s, which does not swap back"
                                % (key, arg))
        elif kind not in ("order", "elements"):
            problems.append("layout for %s has unknown kind %r" % (key, kind))
    notes.append("%d native keys have a declared, self-consistent layout"
                 % len(NATIVE_LAYOUT))

    #: The bignum table must have discriminating constants and declared controls,
    #: and no duplicate keys.
    keys = [k for k, _e, _d in BIGNUM_CONSTANTS]
    if len(set(keys)) != len(keys):
        problems.append("duplicate key in BIGNUM_CONSTANTS")
    disc = sum(1 for _k, _e, d in BIGNUM_CONSTANTS if d)
    if disc < 8:
        problems.append("only %d discriminating bignum constants; the row cannot "
                        "carry its weight" % disc)

    #: Every row's third column, against the mechanism.  Five rows once claimed to
    #: discriminate and could not: they were expressions, and an expression is
    #: arithmetic the interpreter does after the blob is read, so no wide value ever
    #: reaches the serialiser.  A broken port passed them and collected a third of
    #: this module's weight for it.  Checked at both limb widths because which limbs
    #: a value occupies depends on the width, and the suite does not choose it.
    for key, expr, declared in BIGNUM_CONSTANTS:
        for limb_bits in (64, 32):
            able = _can_show_bignum_defect(expr, limb_bits)
            if able != declared:
                problems.append(
                    "bignum constant %s declares discriminating=%s and at "
                    "LIMB_BITS=%d it %s show the defect"
                    % (key, declared, limb_bits,
                       "can" if able else "cannot"))
    notes.append("%d bignum constants, %d discriminating and %d controls, each "
                 "agreeing with the serialiser's own reach at both limb widths"
                 % (len(keys), disc, len(keys) - disc))

    #: A control that is the same value as a discriminating row states the mechanism
    #: rather than restating the arithmetic, so at least one pair must exist.
    paired = [k for k, _e, d in BIGNUM_CONSTANTS
              if not d and k.startswith("expr-") and k[5:] in set(
                  x for x, _e2, d2 in BIGNUM_CONSTANTS if d2)]
    if not paired:
        problems.append("no control shares a value with a discriminating row; "
                        "nothing states that serialisation is what differs")
    notes.append("%d control(s) pair with a discriminating row: %s"
                 % (len(paired), ", ".join(paired)))

    #: The clamp in `Emitter.add`, exercised in both directions.  A gate that is
    #: never seen to fire is indistinguishable from one that cannot: the defect it
    #: exists for -- a path emitting a declared control at the default weight --
    #: was in this file, past review, until grading a tree showed eight controls in
    #: the scored pool.  So a declared control is pushed through at weight 1.0 and
    #: must come out at 0 and be reported, and a discriminating key is pushed
    #: through the same call and must keep its weight.
    controls = sorted(DECLARED_CONTROLS.get("port-bignum-serialiser", ()))
    disc_keys = [k for k, _e, d in BIGNUM_CONSTANTS if d]
    if not controls or not disc_keys:
        problems.append("cannot exercise the control clamp: the table has %d "
                        "control(s) and %d discriminating row(s)"
                        % (len(controls), len(disc_keys)))
    else:
        probe = Emitter("port-bignum-serialiser")
        held = probe.add("s390x/%s" % controls[0], "fail", "clamp probe",
                         weight=1.0)
        kept = probe.add("s390x/%s" % disc_keys[0], "fail", "clamp probe",
                         weight=1.0)
        if held["weight"] != 0.0:
            problems.append("a declared control emitted at weight 1.0 stayed at "
                            "%r; the clamp does not fire" % held["weight"])
        elif not probe.clamped:
            problems.append("the clamp changed a control's weight and reported "
                            "nothing, so a call site that disagrees with the "
                            "table stays invisible")
        if kept["weight"] != 1.0:
            problems.append("a discriminating key was clamped to %r; the clamp "
                            "matches keys it must not" % kept["weight"])
        #: Same call, one module over: the clamp is scoped by module, so a key of
        #: this name in another module must pass through untouched.
        elsewhere = Emitter("bignum-control").add(
            "s390x/%s" % controls[0], "fail", "clamp probe", weight=1.0)
        if elsewhere["weight"] != 1.0:
            problems.append("module %r clamped %r, which it does not declare"
                            % ("bignum-control", controls[0]))
        notes.append("the control clamp holds %s at 0, leaves %s at 1.0, and does "
                     "not reach another module's key of the same name"
                     % (controls[0], disc_keys[0]))

    #: `upstream-suites`' four outcomes, each pushed through and the weight read
    #: back.  Same reason as the clamp probe above: the defect this table replaced
    #: was a branch that said "not charged" in its message and carried the default
    #: weight of 1.0, which made every run hand out five marks for suites State A
    #: fails on this host -- the row read 21/21 on a tree that had fixed nothing.
    #: The bug was invisible because no test had ever asked the branch for a number.
    #: Stated as the invariant rather than four literals: a cell is charged exactly
    #: when State A passes it, whatever the submission did.
    for ref_ok in (True, False):
        for got_ok in (True, False):
            verdict, weight, improved = _upstream_verdict(ref_ok, got_ok)
            want_weight = 1.0 if ref_ok else 0.0
            want_verdict = "fail" if (ref_ok and not got_ok) else "pass"
            if weight != want_weight:
                problems.append(
                    "upstream cell (State A %s, submission %s) has weight %r, "
                    "expected %r: a cell is charged exactly when State A passes it"
                    % ("passes" if ref_ok else "fails",
                       "passes" if got_ok else "fails", weight, want_weight))
            if verdict != want_verdict:
                problems.append(
                    "upstream cell (State A %s, submission %s) reports %r, "
                    "expected %r"
                    % ("passes" if ref_ok else "fails",
                       "passes" if got_ok else "fails", verdict, want_verdict))
            if improved != (not ref_ok and got_ok):
                problems.append(
                    "upstream cell (State A %s, submission %s) marks improved=%r"
                    % ("passes" if ref_ok else "fails",
                       "passes" if got_ok else "fails", improved))
    notes.append("upstream-suites charges 1 of its 4 outcomes -- State A passes and "
                 "the submission does not -- and records the other three at weight 0")

    #: Blob artifacts must name a runnable member.
    for cid, kind, rel, _tail, _stdin in BLOB_ARTIFACTS:
        if kind == "binary" and not rel:
            problems.append("blob artifact %s is a binary with no path" % cid)
        if kind not in ("binary", "qjs"):
            problems.append("blob artifact %s has unknown kind %r" % (cid, kind))
    notes.append("%d blob-carrying artifacts declared" % len(BLOB_ARTIFACTS))

    for line in notes:
        print("[self-check] %s" % line)
    for line in problems:
        print("[self-check] PROBLEM: %s" % line)
    print("[self-check] %d problem(s)" % len(problems))
    return 1 if problems else 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="the engine behind every module in this suite")
    parser.add_argument("--module", help="the module id to run")
    parser.add_argument("--result", help="where to write the JSON result "
                                         "(default: $SRB_RESULT)")
    parser.add_argument("--self-check", action="store_true",
                        help="prove this file's invariants and exit")
    args = parser.parse_args(argv)

    if args.self_check:
        return _self_check()
    module = args.module or os.environ.get("SRB_MODULE_ID")
    if not module:
        parser.error("no --module and no SRB_MODULE_ID")
    if module not in SLICES:
        parser.error("unknown module %r; known: %s"
                     % (module, ", ".join(sorted(SLICES))))
    result = args.result or os.environ.get("SRB_RESULT") or \
        os.path.join(os.environ.get("SRB_WORK", "."), "result.json")

    emit = Emitter(module)
    emit.metadata["kind"] = SLICES[module]["kind"]
    emit.metadata["targets"] = list(SLICES[module]["targets"])
    handler = KINDS[SLICES[module]["kind"]]
    try:
        handler(emit, module)
    except MissingBuild as exc:
        #: This module needs a build another module makes, and it is not there.
        #: Reported rather than reconstructed: a module that rebuilds what it is
        #: missing grades against an expectation no other row shares.
        emit.add("apparatus/build-available", "fail", str(exc)[:400],
                 required=True,
                 detail="this module reads a build the build module publishes; it "
                        "does not make its own")
        emit.note("stopped early: %s" % exc)
    except MissingAssets as exc:
        #: The frozen expectations are absent or altered.  Not a submission's fault
        #: and not gradable: reported as an error with the file named, so a person
        #: reads "this image was built wrong" instead of a rate.
        emit.add("apparatus/expectations-available", "error", str(exc)[:400],
                 required=True,
                 detail="every expectation this suite grades against was frozen "
                        "into the image by lib/freeze.py before any submission "
                        "existed. Nothing here recomputes one.")
        emit.note("stopped early: %s" % exc)
    except Exception:
        emit.add("apparatus/driver-error", "error",
                 "the driver raised while running this module", required=True,
                 detail=traceback.format_exc()[-3500:])
        emit.note("stopped early: the driver raised; see the detail on "
                  "apparatus/driver-error")
    return emit.write(result)


if __name__ == "__main__":
    sys.exit(main())
