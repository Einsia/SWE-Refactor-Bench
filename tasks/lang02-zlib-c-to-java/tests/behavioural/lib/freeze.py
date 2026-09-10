#!/usr/bin/env python3
"""Freezes the grading inputs into the verifier image, at image build time.

Everything a submission is measured against is produced here, once, from the
pinned upstream tarball -- the same bytes the agent's workspace was unpacked
from.  By the time any submission exists, the corpus, the catalog and the
expected outputs are already files in a read-only layer.

That ordering is the whole design.  If the corpus were generated at grading time
from the submitted tree, a submission could shrink it by deleting the source
files it is built from; if the expectations were computed at grading time, the
submission would be in the container while the answers were being decided.
Neither is possible when both are frozen in the image.

What is specific to this task is what the reference is *for*.  In a C-to-C
migration the reference install is also the thing downstream code links against,
so it has to survive into the grading image and be built at the path it will be
read from.  Here nothing links against it: State B is a jar with no C ABI, and
the only programs ever built against the reference are the verifier's own
probe.c and consumer.c, both of which run here and never again.  So the
reference is a freeze-time instrument, and what survives into the image is the
expectation blob it produced.  That is why this script builds it in the
workspace rather than under the assets, and why verify.py opens no reference at
all.

Two ordering hazards are handled explicitly.

The first is that configuring zlib out-of-source *mutates its source tree*:
upstream's CMakeLists renames `zconf.h` to `zconf.h.included` so a stale
generated header cannot shadow the configured one.  The corpus reads `zconf.h`
as one of its payloads, and the audit gates compare the submission against a
pristine baseline -- so the reference is built from a private copy and the
baseline itself is never configured.  Building in place would have left a tree
that is neither State A nor a build directory, and the failure would have
surfaced as a missing corpus payload rather than as what it is.

The second is that the C probe and the C consumer must both answer *every*
behavioural case before the store is written.  A case the reference cannot run
has no correct answer, so oracle.freeze refuses to write a store containing one
and the image build fails here rather than every submission failing later.

The manifest this writes records the digest of each frozen input.  verify.py
checks those digests before grading anything, so a mismatch is reported as a
broken verifier rather than as a failing submission.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

# See the note in verify.py: isolated mode drops the script's directory, and both
# entry points are run that way.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build as buildmod  # noqa: E402
import catalog as catalogmod  # noqa: E402
import corpus as corpusmod  # noqa: E402
import oracle  # noqa: E402
import structure  # noqa: E402
import vlib  # noqa: E402
from vlib import Log  # noqa: E402

MANIFEST_SCHEMA = "swerefactor-verifier-manifest-v1"
TASK = "lang02-zlib-c-to-java"
UPSTREAM_VERSION = "1.3.1"

# The reference is built once.  There is no static/shared distinction to make:
# nothing is linked against it except two programs that run at freeze time, and
# a jar has no static-link mode for a second configuration to contrast with.
REFERENCE_CONFIG = "shared"

# Copied into the assets so grading has one directory to mount read-only.  The
# two C files are staged as well as the two Java ones: they are what produced the
# expectations, and an image that cannot show what its oracle was computed from
# is an image whose oracle cannot be audited.
STAGED_DIRS = ("probe", "surface", "shim")
STAGED_FILES = ("Consumer.java", "consumer.c")
# Read, never executed, and so kept out of the code tree: see the note in
# stage() about why `tests` and `data` are two arguments.
STAGED_DATA = ("source-contract.json",)


def stage(baseline: Path, tests: Path, data: Path, assets: Path, log: Log) -> None:
    """Copy the verifier's fixed inputs into the assets tree.

    The baseline copy is what tells a rewritten file from an untouched one, and
    it comes from the verifier's own authenticated tarball rather than from
    anything a submission could reach.  `probe/` carries both halves of the
    differential pair; only Probe.java is compiled at grading time, but probe.c
    is what the frozen answers came from and it is staged beside it so the two
    can be diffed without the image's build context.

    Two source directories rather than one: `tests` is code this suite executes
    (the probe pair, the surface enumerator, the compiler shim, the consumer),
    `data` is input it only reads (the source contract).  Every stage in the
    benchmark splits them the same way, so "is this file run or read?" is
    answerable from its path -- and `data/` is where the drift check looks for
    the copies that have to stay byte-identical to `environment/`.
    """
    log.write("staging fixed inputs")
    dest_baseline = assets / "baseline"
    if dest_baseline.exists():
        shutil.rmtree(dest_baseline)
    files = vlib.copy_tree(baseline, dest_baseline)
    for name in STAGED_DIRS:
        src = tests / name
        if not src.is_dir():
            raise SystemExit(f"missing verifier input directory: {src}")
        dest = assets / name
        if dest.exists():
            shutil.rmtree(dest)
        vlib.copy_tree(src, dest)
    for name in STAGED_FILES:
        src = tests / name
        if not src.is_file():
            raise SystemExit(f"missing verifier input: {src}")
        (assets / name).write_bytes(src.read_bytes())
    for name in STAGED_DATA:
        src = data / name
        if not src.is_file():
            raise SystemExit(f"missing verifier data file: {src}")
        (assets / name).write_bytes(src.read_bytes())
    # The two files grading actually compiles.  Named explicitly because a
    # staging bug here surfaces as every probe case failing to run, which reads
    # like a broken submission.
    for required in (assets / "probe" / "Probe.java",
                     assets / "surface" / "Surface.java",
                     assets / "Consumer.java",
                     assets / "shim" / "ccshim.py"):
        if not required.is_file():
            raise SystemExit(f"staging did not produce {required}")
    log.write(
        f"staged baseline ({files} files), {', '.join(STAGED_DIRS)}, "
        f"{', '.join(STAGED_FILES + STAGED_DATA)}"
    )


def reference_source(baseline: Path, workspace: Path, log: Log) -> Path:
    """A private copy of State A for the reference build to consume.

    Configuring zlib out-of-source renames `zconf.h` in the *source* tree.
    Handing the reference builder its own copy is what keeps `assets/baseline` a
    faithful State A: every "unchanged" comparison in the audit gates, and
    the corpus payload that reads `zconf.h`, both depend on that tree not having
    been configured.
    """
    dest = workspace / "reference-src"
    if dest.exists():
        shutil.rmtree(dest)
    files = vlib.copy_tree(baseline, dest)
    log.write(f"reference source copy: {files} files at {dest}")
    return dest


def check_reference(prefix: Path, work: Path, log: Log) -> str:
    """Confirm the reference install is the pinned version and is usable.

    zlib installs no executable, so there is nothing to ask `--version`.  The
    equivalent is to compile a three-line consumer against the installed header
    and library and have it print what the library reports at run time: that
    exercises the header, the library, the SONAME and the version in one step,
    and a wrong answer here means the whole oracle is wrong.

    The compile-flag word is returned as well as checked, because the contract
    pins it -- `version-unchanged` and `compile-flags-unchanged` grade the
    submission against a number that has to be the reference's own, and this is
    where that number is read from the reference rather than transcribed.
    """
    header = prefix / "include" / "zlib.h"
    if not header.is_file():
        raise SystemExit(f"reference installed no zlib.h at {header}")
    src = work / "refcheck.c"
    src.write_text(
        "#include <zlib.h>\n"
        "#include <stdio.h>\n"
        "int main(void){\n"
        "  printf(\"%s|%s|%lx|%d\\n\", ZLIB_VERSION, zlibVersion(),\n"
        "         (unsigned long)zlibCompileFlags(), ZLIB_VERNUM);\n"
        "  return 0;\n"
        "}\n"
    )
    binary = work / "refcheck"
    compiled = buildmod.compile_probe(
        src, prefix, binary, log, label="freeze-refcheck"
    )
    if not compiled.ok:
        raise SystemExit(
            f"a consumer could not be built against the reference install: "
            f"{compiled.tail()}"
        )
    result = vlib.run(
        [str(binary)], env=vlib.base_env(), timeout=60.0, log=log,
        label="freeze-refcheck-run",
    )
    if not result.ok:
        raise SystemExit(
            f"the reference check exited {result.returncode}: {result.tail()}"
        )
    reported = result.stdout.decode("utf-8", "replace").strip()
    fields = (reported.split("|") + ["", "", "", ""])[:4]
    header_version, runtime_version, flags, vernum = fields
    if header_version != UPSTREAM_VERSION or runtime_version != UPSTREAM_VERSION:
        raise SystemExit(
            f"reference reports version {header_version!r}/{runtime_version!r}, "
            f"expected {UPSTREAM_VERSION!r}"
        )
    log.write(
        f"reference: {prefix} (version {runtime_version}, "
        f"compile flags 0x{flags}, vernum {vernum})"
    )
    return flags


def check_pinned(contract: dict, flags: str, log: Log) -> None:
    """The contract's pinned numbers must be the reference's own.

    Two gates -- `version-unchanged` and `compile-flags-unchanged` -- grade a
    submission against numbers written in the contract file.  If those numbers
    ever drifted from what the reference actually reports, every correct
    submission would fail a mandatory gate and the report would blame the
    submission.  Asserting it here turns that into a failed image build.
    """
    pinned = contract["behavioral_contract"]["pinned_constants"]
    want_flags = pinned.get("ZLIB_COMPILE_FLAGS")
    got_flags = f"0x{flags}"
    if want_flags is not None and int(str(want_flags), 16) != int(got_flags, 16):
        raise SystemExit(
            f"contract pins ZLIB_COMPILE_FLAGS {want_flags} but the reference "
            f"reports {got_flags}; the contract and the oracle disagree"
        )
    if pinned.get("ZLIB_VERSION") not in (None, UPSTREAM_VERSION):
        raise SystemExit(
            f"contract pins ZLIB_VERSION {pinned['ZLIB_VERSION']!r} but this "
            f"image was built from {UPSTREAM_VERSION!r}"
        )
    log.write(f"pinned constants agree with the reference (flags {got_flags})")


def build_instruments(
    assets: Path, prefix: Path, work: Path, log: Log
) -> tuple[Path, Path]:
    """Compile the C halves of the two differential instruments.

    Both are the verifier's own C, compiled against the reference install.  Their
    Java counterparts -- Probe.java and Consumer.java -- are compiled at grading
    time against the submission's jar, and the two sides' per-case output is
    compared byte for byte.  Nothing is shared between the halves except the
    record format, which is what makes the comparison differential rather than a
    self-consistency check.
    """
    probe_src = assets / "probe" / "probe.c"
    if not probe_src.is_file():
        raise SystemExit(f"probe source missing at {probe_src}")
    probe_bin = work / "probe-reference"
    compiled = buildmod.compile_probe(
        probe_src, prefix, probe_bin, log, label="freeze-probe"
    )
    if not compiled.ok:
        raise SystemExit(
            f"the probe did not build against the reference: {compiled.tail()}"
        )
    # The self-description both halves answer.  Asked here because a probe that
    # cannot describe itself against the reference would make the two
    # probe-linkage cases unanswerable for every submission, and the failure
    # would read as a submission defect.
    keys = vlib.run(
        [str(probe_bin), "--list-keys"], env=vlib.base_env(), timeout=60.0,
        log=log, label="freeze-probe-keys",
    )
    if not keys.ok or not keys.stdout.strip():
        raise SystemExit(
            f"the reference probe would not list its keys: {keys.tail()}"
        )
    log.write(
        f"probe self-description: "
        f"{len(keys.stdout.decode('utf-8', 'replace').splitlines())} lines"
    )

    consumer_src = assets / "consumer.c"
    consumer_bin = work / "consumer-reference"
    compiled = buildmod.compile_consumer_c(
        consumer_src, prefix, consumer_bin, log, label="freeze-consumer"
    )
    if not compiled.ok:
        raise SystemExit(
            f"the consumer did not build against the reference: {compiled.tail()}"
        )
    return probe_bin, consumer_bin


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path,
                        help="the pristine State A tree, unpacked from the tarball")
    parser.add_argument("--assets", required=True, type=Path,
                        help="where the frozen inputs are written")
    parser.add_argument("--tests", type=Path, default=Path(__file__).resolve().parent,
                        help="the verifier source tree, staged into the assets")
    parser.add_argument("--data", type=Path,
                        default=Path(__file__).resolve().parent.parent / "data",
                        help="fixed inputs this suite reads but never executes")
    parser.add_argument("--workspace", type=Path, default=Path("/tmp/freeze"))
    parser.add_argument("--image", default="")
    args = parser.parse_args(argv)
    for name in ("baseline", "assets", "tests", "data", "workspace"):
        setattr(args, name, getattr(args, name).resolve())

    assets: Path = args.assets
    assets.mkdir(parents=True, exist_ok=True)
    args.workspace.mkdir(parents=True, exist_ok=True)
    log = Log(assets / "freeze.log")
    log.section("freeze verifier assets")

    stage(args.baseline, args.tests, args.data, assets, log)
    contract = vlib.read_json(assets / "source-contract.json")

    # 1. The corpus, built from the pristine baseline before anything configures
    #    it.  One of its payloads is `zconf.h`, which the reference configure
    #    would rename out from under it.
    corpus_dir = assets / "corpus"
    corpus_meta = corpusmod.build(args.baseline, corpus_dir)
    corpus_meta["reference_version"] = UPSTREAM_VERSION
    log.write(
        f"corpus: {corpus_meta['count']} payloads, "
        f"{corpus_meta['total_bytes']} bytes, {corpus_meta['digest']}"
    )

    # 2. The catalog, generated over that corpus.  The floors inside
    #    build_catalog abort the image build if coverage regressed.
    catalog = catalogmod.build_catalog(corpus_meta)
    vlib.write_json(assets / "catalog.json", catalog)
    counts = catalog["counts"]
    log.write(
        f"catalog: {counts['total']} cases ({counts['behavioural']} behavioural, "
        f"{counts['assertions']} assertions), {catalog['digest']}"
    )

    # 3. The reference, from its own copy of the baseline, with a real compiler.
    #    This is the one build in the whole system that is allowed to compile C.
    refsrc = reference_source(args.baseline, args.workspace, log)
    refbuilder = buildmod.ReferenceBuilder(
        refsrc, args.workspace / "reference", log
    )
    prefix = refbuilder.build(REFERENCE_CONFIG)
    flags = check_reference(prefix, args.workspace, log)
    check_pinned(contract, flags, log)
    # The baseline must have survived that: if it did not, every "unchanged"
    # comparison in the audit gates is being made against a mutated tree.
    if not (args.baseline / "zconf.h").is_file():
        raise SystemExit(
            "the baseline's zconf.h disappeared during the reference build; the "
            "reference was configured against the baseline instead of its copy"
        )

    # 4. The contract, asserted against itself.  Doing it here means a contract
    #    that contradicts itself fails the image build rather than every run.
    expectations = structure.Expectations.load(contract, assets / "baseline")
    log.write(
        f"contract self-consistent: {len(expectations.surface_classes)} types, "
        f"{expectations.member_count()} members, module "
        f"{expectations.module_name}"
    )

    # 5. The C halves of the probe and the consumer.
    probe_bin, consumer_bin = build_instruments(
        assets, prefix, args.workspace, log
    )

    # 6. The expectations.  Any crash, stall or unrunnable case here is a
    #    defective case rather than a defective submission, and oracle.freeze
    #    refuses to write a store that contains one.
    oracle_dir = assets / "oracle"
    oracle_dir.mkdir(parents=True, exist_ok=True)
    ctx = oracle.CollectContext(
        label="reference",
        prefix=prefix,
        build_dir=refbuilder.build_dir_for(REFERENCE_CONFIG),
        probe_command=[str(probe_bin)],
        consumer=[str(consumer_bin)],
        scratch=args.workspace / "reference-run",
        # The same binary answers both linkage sentinels.  A C library has one
        # linkage, so `@consumer` and `@consumer-cp` are the same program here --
        # which is exactly what lets the submission's two genuinely different
        # modes be graded against one frozen answer.  Passed explicitly rather
        # than defaulted, because None means "not run" on the submission side and
        # that must not be what the reference records.
        consumer_cp=[str(consumer_bin)],
    )
    # corpus_dir is the directory that *contains* blobs/, not blobs/ itself: the
    # probe and the driver runner each build their own <dir>/blobs/NNNNN.bin path.
    digest = oracle.freeze(catalog, corpus_meta, ctx, corpus_dir, oracle_dir, log)
    store = oracle.ExpectationStore(
        oracle_dir / "expectations.json", oracle_dir / "expectations.bin"
    )
    store.load()

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "task": TASK,
        "image": args.image,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "upstream_version": UPSTREAM_VERSION,
        "digests": {
            "catalog": catalog["digest"],
            "corpus": corpus_meta["digest"],
            "oracle": digest,
        },
        "counts": {
            "cases": counts["total"],
            "behavioural": counts["behavioural"],
            "assertions": counts["assertions"],
            "executions": counts["executions"],
            "by_kind": counts["by_kind"],
            "families": len(counts["by_family"]),
            "payloads": corpus_meta["count"],
            "payload_bytes": corpus_meta["total_bytes"],
            "expectations": len(store.entries),
            "expectations_by_namespace": store.meta.get("by_namespace", {}),
        },
        "contract": {
            "types": len(expectations.surface_classes),
            "members": expectations.member_count(),
            "module": expectations.module_name,
            "jar": expectations.jar_relpath,
            "class_major_version": expectations.major_version,
            "state_a_symbols": len(expectations.symbol_map),
        },
        # Recorded for auditability only.  Nothing under here survives into the
        # grading image: verify.py opens no reference, and these paths exist in
        # the build container alone.
        "reference": {
            "config": REFERENCE_CONFIG,
            "prefix": str(prefix),
            "build_dir": str(refbuilder.build_dir_for(REFERENCE_CONFIG)),
            "source": str(refsrc),
            "compile_flags": f"0x{flags}",
            "probe": str(probe_bin),
            "consumer": str(consumer_bin),
        },
    }
    vlib.write_json(assets / "verifier-manifest.json", manifest)
    log.write(
        f"frozen: {counts['total']} cases, {corpus_meta['count']} payloads, "
        f"{len(store.entries)} expectations, oracle {digest}"
    )
    print(json.dumps(manifest["counts"], indent=1, sort_keys=True))
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
