#!/usr/bin/env python3
"""Mutation testing for the verifier: does the case catalog actually detect?

A suite that passes on the reference proves only that it is self-consistent.  A
suite worth grading with also has to *fail* when behavior changes -- and for a
compression library the changes that matter are mostly invisible to a round-trip
test.  Almost every mutant below still produces a stream that inflates back to
its input; what it does not produce is the reference's bytes.  That is the whole
premise of grading this task byte-for-byte, so it is the premise that gets
tested here.

What is mutated is the *reference State B*, the Java port -- not State A's C.
That is the difference from the C-to-C form of this file, and it is the only
thing that can be mutated: the expectations were frozen from State A's C, and a
mutant in the C would move the oracle rather than the answer, which tests
nothing.  Mutating the Java asks the question the grader actually needs answered:
if a submission got this one detail wrong, would any case say so?

Each mutant is a realistic porting mistake: a shorter match chain at the default
level, a different TOO_FAR cutoff, one less byte of slack in a bound, the head of
an oversized dictionary instead of its tail, an off-by-one in the length table, a
resync that restarts from zero.  Each is graded exactly as a submission would be,
and each must be caught -- by the specific families named with it, not merely by
something somewhere.  A mutant no case detects is a hole in the catalog, and the
exit status says so.

Three controls run alongside them.  `control-nmax` changes Adler-32's batch size
to another multiple of 16, `control-cast-parens` parenthesizes a cast operand,
and `control-static-tie` flips a comparison whose guarded assignment is already a
no-op: all three are semantically invisible, and a suite that flags any of them
is over-fitted to the reference's source rather than to its behavior.

Run with --shim off.  The shim exists to catch a submission that compiles C, and
every tree here is Java by construction, so leaving it on would only add a gate
that cannot fire.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Mutant:
    name: str
    path: str
    old: str
    new: str
    expect: str
    # Families that must report a failure.  Detection alone is too weak a claim:
    # a mutant that broke something unrelated would satisfy it while the family
    # it was written to exercise still passed, and the hole would stay hidden.
    families: tuple[str, ...] = ()
    control: bool = False
    count: int = 1
    # Which graded fraction the mutant is expected to move.  Most move behavior;
    # the packaging ones move only the structural phase, and one moves only the
    # audit gates.
    phase: str = "behavioural"


# Every `old` string below was verified to occur exactly `count` times in the
# reference State B.  The count is re-asserted at injection time, so a mutant that
# stopped applying (because the source drifted) fails loudly instead of quietly
# reporting an undetected mutation and blaming the catalog.
MUTANTS: list[Mutant] = [
    # --- the deflate engine, at the level nearly everything uses --------------
    Mutant(
        "level6-chain-half", "java/org/zlib/Deflater.java",
        "{0, 4, 8, 32, 16, 32, 128, 256, 1024, 4096};",
        "{0, 4, 8, 32, 16, 32,  64, 256, 1024, 4096};",
        "the match chain length at level 6, so the default level finds "
        "different matches and emits different bytes",
        # Deliberately not `compress-match`.  Those payloads come from
        # `craft()`, which builds data whose only match is one length at one
        # distance, so the hash chain for the matching position holds a single
        # entry and max_chain is never the binding constraint -- they pin the
        # length/distance code tables, not the depth of the search.  A shortened
        # chain shows up instead wherever many positions collide in one bucket,
        # which is the general corpus at the default level.  `too-far-8192`
        # below does claim the family, and does fail it, because a crafted
        # distance past 4096 is exactly what that constant rejects.
        families=("compress-level", "compose", "oneshot"),
    ),
    Mutant(
        "too-far-8192", "java/org/zlib/Deflater.java",
        "private static final int TOO_FAR = 4096;",
        "private static final int TOO_FAR = 8192;",
        "the distance past which the slow deflate rejects a length-3 match",
        families=("compress-level", "compress-match"),
    ),
    # --- checksums, which every wrapper carries ------------------------------
    Mutant(
        "adler-base", "java/org/zlib/internal/Checksums.java",
        "private static final int BASE = 65521;",
        "private static final int BASE = 65520;",
        "the Adler-32 modulus: the checksum itself, every zlib trailer, and "
        "every FDICT identifier",
        families=("checksum", "compress-level", "dictionary"),
    ),
    Mutant(
        "crc32-combine-shift", "java/org/zlib/internal/Checksums.java",
        "return (multmodp(x2nmodp(len2, 3), (int) crc1) ^ (int) crc2) & 0xffffffffL;",
        "return (multmodp(x2nmodp(len2, 4), (int) crc1) ^ (int) crc2) & 0xffffffffL;",
        "crc32Combine's length scaling, which is only visible if the "
        "combination operators are exercised rather than just crc32 itself",
        families=("checksum",),
    ),
    # --- the bounds, which callers size their buffers from -------------------
    Mutant(
        "compress-bound-slack", "java/org/zlib/Zlib.java",
        "+ (sourceLen >>> 25) + 13;", "+ (sourceLen >>> 25) + 12;",
        "compressBound by one byte -- still a valid upper bound for these "
        "inputs, so only a suite that compares the number detects it",
        families=("bound",),
    ),
    Mutant(
        "deflate-bound-fixedlen", "java/org/zlib/Deflater.java",
        "+ (sourceLen >>> 9) + 4;", "+ (sourceLen >>> 9) + 5;",
        "deflateBound's fixed-block estimate, again by one byte",
        families=("bound",),
    ),
    # --- the gzip wrapper ----------------------------------------------------
    #
    # The header is written in two arms, one per branch of the gzhead test, and
    # each arm has its own copy of the XFL expression.  A port that factored them
    # into one helper has to get both callers right, so there is a mutant per arm
    # and they are expected to be caught by different families.
    Mutant(
        "gzip-os-byte", "java/org/zlib/Deflater.java",
        "                putByte(Zutil.OS_CODE);",
        "                putByte(0x00);",
        "the OS byte in the default gzip header (the setHeader path writes "
        "gzhead.os and is unaffected, which separates the two)",
        families=("compress-window", "gzfile-roundtrip", "compose"),
    ),
    Mutant(
        "gzip-xfl-byte", "java/org/zlib/Deflater.java",
        "                putByte(level == 9 ? 2\n"
        "                        : (strategy >= Zlib.Z_HUFFMAN_ONLY || level < 2 ? 4 : 0));\n"
        "                putByte(Zutil.OS_CODE);",
        "                putByte(level == 9 ? 2\n"
        "                        : (strategy >= Zlib.Z_HUFFMAN_ONLY || level < 2 ? 4 : 2));\n"
        "                putByte(Zutil.OS_CODE);",
        "the XFL byte for mid-range levels, a header field no decompressor "
        "reads and every byte-comparison does",
        # Not gzheader: this is the gzhead == null arm, the default header deflate
        # writes when the caller never called setHeader.  The gzheader family goes
        # through the other arm, which has its own mutant below.
        families=("compress-window", "gzfile-roundtrip", "compose"),
    ),
    Mutant(
        "gzip-xfl-setheader", "java/org/zlib/Deflater.java",
        "                putByte(level == 9 ? 2\n"
        "                        : (strategy >= Zlib.Z_HUFFMAN_ONLY || level < 2 ? 4 : 0));\n"
        "                putByte(gzhead.os & 0xff);",
        "                putByte(level == 9 ? 2\n"
        "                        : (strategy >= Zlib.Z_HUFFMAN_ONLY || level < 2 ? 4 : 2));\n"
        "                putByte(gzhead.os & 0xff);",
        "the XFL byte on the setHeader path, which is a different arm of the "
        "same test and a separate copy of the expression",
        families=("gzheader",),
    ),
    # --- inflate -------------------------------------------------------------
    Mutant(
        "inflate-error-message", "java/org/zlib/Inflater.java",
        'msg = "incorrect header check";', 'msg = "invalid header check";',
        "the text of msg for a bad zlib header, which is part of the "
        "observable API and not an internal detail",
        families=("error-header",),
    ),
    Mutant(
        "inflate-window-half", "java/org/zlib/Inflater.java",
        "            wsize = 1 << wbits;", "            wsize = 1 << (wbits - 1);",
        "the inflate window size, so matches reaching further back than half "
        "the window resolve against the wrong history",
        # Not compose: the composed payloads are inflated by only a stride of the
        # family, and a match has to reach past 16 KB of history for a halved
        # window to change the output.  inflate-window is the family built for
        # exactly this, and dictionary reaches it too.
        families=("inflate-window", "inflate", "dictionary"),
    ),
    Mutant(
        "inflate-lbase-258", "java/org/zlib/internal/InfTrees.java",
        "35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258, 0, 0,",
        "35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 257, 0, 0,",
        "the base of length code 285, so maximum-length matches decode one "
        "byte short",
        families=("inflate", "compose", "oneshot"),
    ),
    Mutant(
        "sync-search-restart", "java/org/zlib/Inflater.java",
        "                got = 4 - got;", "                got = 0;",
        "syncsearch's partial-match backtrack, so sync() cannot find the "
        "five-byte full-flush marker at all: every real marker has a third zero "
        "in front of the 00 00 ff ff, and this is the branch that carries the "
        "match count across it",
        # sync-recover, not istate-sync.  The istate-sync cases call sync() on an
        # intact stream with no marker in it, so the search can only scan to the
        # end -- it never reaches this branch.
        families=("sync-recover",),
    ),
    Mutant(
        "inflate-mark-shift", "java/org/zlib/Inflater.java",
        "        return (((long) back) << 16)", "        return (((long) back) << 15)",
        "mark()'s packing of back-distance and length",
        families=("istate-mark",),
    ),
    # --- the dictionary, where the interesting case is an oversized one -------
    Mutant(
        "dict-head-not-tail", "java/org/zlib/Deflater.java",
        "dictOffset = dictLength - wSize;   /* use the tail */",
        "dictOffset = 0;                    /* mutant: keep the head */",
        "which end of an over-window dictionary setDictionary keeps -- "
        "invisible in the FDICT identifier, which is computed over the whole "
        "dictionary before truncation, and visible only in the body",
        families=("dictionary",),
    ),
    # --- the gz file layer ---------------------------------------------------
    Mutant(
        "gzgets-drop-newline", "java/org/zlib/GzFile.java",
        "                        n = i + 1;", "                        n = i;",
        "whether gets() keeps the newline it stopped at",
        families=("gzfile-gets",),
    ),
    # --- the compile-time flags word, part of the ABI ------------------------
    #
    # This one is graded on the audit phase as well as the behavioural one,
    # and it is in the table to prove the two do not overlap by accident: the
    # abi-surface family reads the word through the library, and the
    # compile-flags-unchanged gate reads it from the artifact.  A port that
    # answered a different word would be telling the truth about itself and a lie
    # about the thing it replaced.
    Mutant(
        "compile-flags-word", "java/org/zlib/Zlib.java",
        "        return 0xa9L;", "        return 0xaaL;",
        "the sizeof-uInt field of compileFlags, which a caller uses to decide "
        "whether its own build is compatible",
        families=("abi-surface",),
    ),
    # --- packaging: caught by the structural phase, not the behavioral one ----
    #
    # The C-to-C form of this file bumped the SONAME here.  A jar has no SONAME;
    # what it has is a versioned filename and an unversioned symlink beside it,
    # which is what a consumer's build script names.  Renaming the jar is the
    # same class of mistake -- everything still works, and nothing that already
    # referenced the artifact finds it.
    Mutant(
        "jar-name-unversioned", "CMakeLists.txt",
        "set(ZLIB_JAR_NAME zlib-${ZLIB_FULL_VERSION}.jar)",
        "set(ZLIB_JAR_NAME zlib.jar)",
        "the installed jar's name, so the versioned artifact a consumer's "
        "build script names is not there",
        families=("structure",), phase="structural",
    ),
    # --- controls: semantically identical, must NOT be detected --------------
    Mutant(
        "control-nmax", "java/org/zlib/internal/Checksums.java",
        "private static final int NMAX = 5552;",
        "private static final int NMAX = 5536;",
        "nothing: a smaller batch before the modulo, still a multiple of 16 so "
        "the unrolled loop is intact. The batch size is an implementation "
        "detail and a suite that detects it is grading the wrong thing",
        control=True,
    ),
    Mutant(
        "control-cast-parens", "java/org/zlib/internal/Checksums.java",
        "        int c = (int) ~crc;", "        int c = (int) (~crc);",
        "nothing: a syntactic no-op",
        control=True,
    ),
    # A control, and the reason it is one is worth recording.  `<= 0` versus
    # `< 0` here looks like it decides static-versus-dynamic on a tie, and it
    # does not: the line it guards is `optLenb = staticLenb`, which on a tie
    # assigns a value that is already there.  The encoding is chosen 20 lines
    # further down by `compareUnsigned(staticLenb, optLenb) == 0`, which the
    # mutation does not touch.
    #
    # This was measured rather than argued when the C-to-C form of the suite was
    # built: an instrumented trees.c counted 70 ties in 12,898 blocks over this
    # same corpus -- so the branch is reachable, at the default strategy, on
    # payloads the catalog grades -- and a 280,200-point sweep over (payload x
    # level x strategy x memLevel x windowBits) found zero differing outputs.
    # The mutation is unobservable through the library's interface, so a suite
    # that reported it would be grading the shape of the source rather than the
    # behavior of the product.
    Mutant(
        "control-static-tie", "java/org/zlib/Deflater.java",
        "if (Long.compareUnsigned(staticLenb, optLenb) <= 0",
        "if (Long.compareUnsigned(staticLenb, optLenb) < 0",
        "nothing: on a tie the guarded assignment is already a no-op, and the "
        "encoding is picked later by a comparison this does not change",
        control=True,
    ),
]

def apply_mutant(tree: Path, mutant: Mutant) -> None:
    target = tree / mutant.path
    text = target.read_text(encoding="utf-8")
    found = text.count(mutant.old)
    if found != mutant.count:
        raise SystemExit(
            f"mutant {mutant.name}: expected {mutant.count} occurrence(s) of "
            f"{mutant.old!r} in {mutant.path}, found {found}. The mutant is stale "
            f"and would have reported a false hole in the catalog."
        )
    target.write_text(text.replace(mutant.old, mutant.new), encoding="utf-8")


def grade(repo: Path, args: argparse.Namespace, tag: str) -> dict:
    out = args.out / tag
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    workspace = args.workspace / tag
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(Path(__file__).parent / "verify.py"),
        "--repo", str(repo),
        "--assets", str(args.assets),
        "--out", str(out),
        "--workspace", str(workspace),
        "--always-behavioural",
        "--shim", "off",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    report = out / "report.json"
    if not report.is_file():
        raise SystemExit(
            f"{tag}: no report produced (rc={proc.returncode})\n{proc.stderr[-3000:]}"
        )
    return json.loads(report.read_text(encoding="utf-8"))


def failing_families(report: dict) -> dict[str, int]:
    """Per-family failure counts, read from the full family table.

    report["failures"] is a truncated digest meant for a human reading
    summary.txt; report["families"] carries every family, which is what a
    coverage claim has to be built from.
    """
    out: dict[str, int] = {}
    for family, slot in report.get("families", {}).items():
        missed = slot["total"] - slot["passed"]
        if missed:
            out[family] = missed
    return out


def built_everywhere(report: dict) -> bool:
    """Did the mutated tree still install in both configurations?

    A mutant that does not build is not a test of the catalog: it would be
    "detected" by every case at once, and the detection would prove nothing about
    whether the suite can see the behavior the mutant was written to change.
    """
    builds = report.get("builds", {})
    return bool(builds) and all(b.get("installed") for b in builds.values())


def fractions_of(report: dict) -> dict[str, float]:
    """The three graded fractions, on one scale.

    Audit is reported as a gate count rather than a fraction, so it is
    converted here.  It is a phase a mutant can target because some porting
    mistakes are only visible in the artifact -- the class file, the module
    descriptor, the jar -- and never in a case's output.
    """
    scoring = report["scoring"]
    total = scoring.get("gates_total") or 1
    return {
        "behavioural": scoring["behavioural_compatibility"],
        "structural": scoring["structural_compatibility"],
        "audit": scoring.get("gates_passed", 0) / total,
    }


def judge(mutant: Mutant, report: dict) -> tuple[bool, str]:
    """Decide whether a mutant behaved as it was written to.

    For a real mutant, three things have to hold: the tree still built, the
    graded fraction it targets moved, and the families named with it are among the
    ones that failed.  The last is the part that makes this a coverage claim --
    without it, a mutant that broke something incidental would pass this check
    while the family it was aimed at still agreed with the reference.
    """
    fractions = fractions_of(report)
    families = failing_families(report)

    if mutant.control:
        stale = {k: v for k, v in fractions.items() if v < 1.0}
        if stale:
            detail = ", ".join(f"{k}={v:.6f}" for k, v in sorted(stale.items()))
            worst = sorted(families.items(), key=lambda kv: -kv[1])[:4]
            named = ", ".join(f"{f}:{n}" for f, n in worst)
            return False, f"control mutant was detected ({detail}; {named})"
        return True, "not detected, as intended"

    if not built_everywhere(report):
        return False, "the mutated tree did not build; the mutant is invalid"
    moved = fractions[mutant.phase]
    if moved >= 1.0:
        return False, (
            f"undetected: {mutant.phase} compatibility stayed at {moved:.6f}. "
            f"This is a hole in the catalog."
        )
    missing = [f for f in mutant.families if f not in families]
    if missing:
        return False, (
            f"detected ({mutant.phase}={moved:.6f}) but not by the families it "
            f"targets: {', '.join(missing)} all passed. Either the mutant does "
            f"not reach them or those families do not cover it."
        )
    return True, f"detected, {mutant.phase}={moved:.6f}"


def fresh_copy(source: Path, dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest, symlinks=True)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path,
                        help="a pristine reference State B tree to mutate")
    parser.add_argument("--assets", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--only", action="append", default=[],
                        help="run just these mutants (repeatable)")
    parser.add_argument("--skip-identity", action="store_true",
                        help="for re-runs of a single mutant, once identity has "
                             "already been established in this image")
    parser.add_argument("--keep-trees", action="store_true",
                        help="leave the mutated trees in place for inspection")
    args = parser.parse_args(argv)
    for name in ("source", "assets", "out", "workspace"):
        setattr(args, name, getattr(args, name).resolve())
    args.out.mkdir(parents=True, exist_ok=True)
    args.workspace.mkdir(parents=True, exist_ok=True)

    ok = True
    identity_summary: dict = {"skipped": True}
    if not args.skip_identity:
        print("=" * 74)
        print("identity: the unmutated reference must pass every case it is graded on")
        print("=" * 74)
        identity = grade(fresh_copy(args.source, args.workspace / "tree-identity"),
                         args, "identity")
        ident = fractions_of(identity)
        identity_summary = {
            "skipped": False,
            "behavioural": ident["behavioural"],
            "structural": ident["structural"],
            "audit": ident["audit"],
            "installed_both": built_everywhere(identity),
        }
        print(f"  behavioural={ident['behavioural']:.6f} "
              f"structural={ident['structural']:.6f} "
              f"audit={ident['audit']:.6f}")
        if any(v != 1.0 for v in ident.values()):
            # Nothing below can mean anything if this fails: a mutant would be
            # "detected" by cases that were already failing on the reference.
            print("  FAIL: the reference does not pass its own suite; the catalog "
                  "or the expectations are wrong")
            for family, count in sorted(failing_families(identity).items()):
                print(f"    {family:26s} {count} failed")
            ok = False
        else:
            print("  OK")

    results: list[dict] = []
    if args.only:
        unknown = set(args.only) - {m.name for m in MUTANTS}
        if unknown:
            raise SystemExit(f"unknown mutant(s): {', '.join(sorted(unknown))}")
    selected = [m for m in MUTANTS if not args.only or m.name in args.only]
    for mutant in selected:
        print()
        print("=" * 74)
        kind = "control" if mutant.control else "mutant "
        print(f"{kind} {mutant.name}")
        print(f"  breaks: {mutant.expect}")
        print("=" * 74)
        tree = fresh_copy(args.source, args.workspace / f"tree-{mutant.name}")
        apply_mutant(tree, mutant)
        report = grade(tree, args, mutant.name)
        families = failing_families(report)
        passed, verdict = judge(mutant, report)
        ok = ok and passed
        fractions = fractions_of(report)
        total_failed = sum(p["failed"] for p in report["phases"].values())
        print(f"  behavioural={fractions['behavioural']:.6f} "
              f"structural={fractions['structural']:.6f} "
              f"audit={fractions['audit']:.6f} "
              f"cases failed={total_failed}")
        for family, count in sorted(families.items(), key=lambda kv: -kv[1])[:8]:
            marker = " *" if family in mutant.families else "  "
            print(f"   {marker} {family:26s} {count} failed")
        print(f"  {'OK' if passed else 'FAIL'}: {verdict}")
        results.append({
            "mutant": mutant.name,
            "expect": mutant.expect,
            "control": mutant.control,
            "phase": mutant.phase,
            "targets": list(mutant.families),
            "behavioural": fractions["behavioural"],
            "structural": fractions["structural"],
            "audit": fractions["audit"],
            "installed_both": built_everywhere(report),
            "cases_failed": total_failed,
            "families": families,
            "ok": passed,
            "verdict": verdict,
        })
        if not args.keep_trees:
            shutil.rmtree(tree, ignore_errors=True)

    summary = args.out / "mutation-summary.json"
    summary.write_text(
        json.dumps(
            {"identity": identity_summary, "mutants": results, "all_ok": ok},
            indent=1, sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    print()
    print(f"summary written to {summary}")
    caught = sum(1 for r in results if r["ok"] and not r["control"])
    reals = sum(1 for r in results if not r["control"])
    print(f"real mutants caught: {caught}/{reals}")
    print("RESULT:", "all mutants behaved as expected" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())


